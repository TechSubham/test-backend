"""Structured logging with secret redaction.

Redaction runs on every record handled by the root logger: configured API keys are
replaced verbatim, and common key/token shapes are masked even if they were never
configured (defence in depth for provider error messages that echo a key).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

REDACTED = "***REDACTED***"

_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API keys
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),  # OpenAI-style keys
    re.compile(r"\bgsk_[A-Za-z0-9]{16,}"),  # Groq keys
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)([?&]key=)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(x-goog-api-key['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9._\-]{8,}"),
]


class SecretRedactor:
    def __init__(self, secrets: list[str] | None = None) -> None:
        self._secrets = sorted({s for s in (secrets or []) if len(s) >= 4}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            if pattern.groups:
                text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
            else:
                text = pattern.sub(REDACTED, text)
        return text


class RedactionFilter(logging.Filter):
    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self.redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = self.redactor.redact(message)
        record.args = None
        if record.exc_info:
            # Render the traceback now, redacted, so formatters never see raw secrets.
            record.exc_text = self.redactor.redact(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "session_id", "key_id", "event"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False)


class StdoutHandler(logging.StreamHandler):
    """Resolves sys.stdout at emit time (works with test capture and stream swaps)."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stdout

    @stream.setter
    def stream(self, _value) -> None:
        pass


def configure_logging(level: str, secrets: list[str]) -> SecretRedactor:
    redactor = SecretRedactor(secrets)
    handler = StdoutHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter(redactor))

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_lyftr", False):
            root.removeHandler(existing)
    handler._lyftr = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)

    # Third-party loggers: keep them quiet and route through our redacting handler.
    for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "litellm", "httpx", "httpcore"):
        lib_logger = logging.getLogger(name)
        lib_logger.setLevel(logging.WARNING)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True
    return redactor
