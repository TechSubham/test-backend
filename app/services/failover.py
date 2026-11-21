"""Key pool and deterministic failover — ARCHITECTURE.md §4.

* Every provider call is `litellm.acompletion` (looked up at call time).
* Fixed order key_1 → key_2 → …, each key at most once per request, no hidden retries
  (`num_retries=0`, `max_retries=0`).
* Simulated failures (X-Simulate-Failure) are per request: no network call, no change to
  key health, no change to order.
* Secrets never leave this module except as the `api_key` argument to LiteLLM.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

import litellm
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import AllProvidersUnavailable
from app.services import cache_service
from app.services.simulation import SimulationPlan

logger = logging.getLogger("app.failover")

LIVE_INFERENCE = "LIVE_INFERENCE"
LLM_CACHE = "LLM_CACHE"

QUOTA_MARKERS = (
    "quota",
    "resource_exhausted",
    "resource exhausted",
    "insufficient_quota",
    "billing",
    "exceeded your current",
)


# ---------------------------------------------------------------------------------------
# Key pool
# ---------------------------------------------------------------------------------------
class KeyPoolConfigError(ValueError):
    pass


def _fingerprint(secret: SecretStr) -> str:
    """In-memory identity of a credential, used to group duplicates. Never exposed."""
    return hashlib.sha256(secret.get_secret_value().encode()).hexdigest()


@dataclass(frozen=True)
class ProviderKey:
    id: str  # key_1, key_2, ... — position in LLM_API_KEYS, never derived from the secret
    model: str
    secret: SecretStr = field(repr=False)
    credential: str = field(repr=False, default="")  # fingerprint shared by duplicates


@dataclass
class KeyHealth:
    """Informational only — never changes attempt order (determinism, §4.7)."""

    last_outcome: str = "unused"
    consecutive_failures: int = 0


class KeyPool:
    def __init__(self, keys: list[ProviderKey]) -> None:
        self.keys: tuple[ProviderKey, ...] = tuple(keys)
        # Health is tracked per *credential*: duplicate entries of one key share it.
        self._health: dict[str, KeyHealth] = {k.credential: KeyHealth() for k in self.keys}

    @classmethod
    def from_settings(cls, settings: Settings) -> KeyPool:
        secrets = settings.api_keys
        models = settings.key_models
        if models and len(models) != len(secrets):
            raise KeyPoolConfigError(
                f"LLM_KEY_MODELS has {len(models)} entries but LLM_API_KEYS has {len(secrets)}"
            )
        keys = [
            ProviderKey(
                id=f"key_{i}",
                model=models[i - 1] if models else settings.llm_model,
                secret=secret,
                credential=_fingerprint(secret),
            )
            for i, secret in enumerate(secrets, start=1)
        ]
        pool = cls(keys)
        if pool.unique_secrets != len(keys):
            logger.warning(
                "LLM_API_KEYS contains duplicate keys (%s); duplicates are not independent",
                ", ".join(pool.duplicate_ids()),
            )
        logger.info("key pool loaded: %s", ", ".join(f"{k.id}->{k.model}" for k in keys) or "empty")
        return pool

    def __len__(self) -> int:
        return len(self.keys)

    @property
    def ids(self) -> list[str]:
        return [k.id for k in self.keys]

    @property
    def unique_secrets(self) -> int:
        return len({k.credential for k in self.keys})

    def duplicate_ids(self) -> list[str]:
        seen: set[str] = set()
        dupes = []
        for k in self.keys:
            if k.credential in seen:
                dupes.append(k.id)
            seen.add(k.credential)
        return dupes

    def health_of(self, key_id: str) -> KeyHealth:
        key = next(k for k in self.keys if k.id == key_id)
        return self._health[key.credential]

    @property
    def healthy_count(self) -> int:
        """Distinct credentials whose last real call did not fail."""
        return sum(1 for h in self._health.values() if h.consecutive_failures == 0)

    def record_success(self, key: ProviderKey) -> None:
        h = self._health[key.credential]
        h.last_outcome, h.consecutive_failures = "success", 0

    def record_failure(self, key: ProviderKey, reason: str) -> None:
        h = self._health[key.credential]
        h.last_outcome = reason
        h.consecutive_failures += 1


# ---------------------------------------------------------------------------------------
# Attempts and classification
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Attempt:
    key_id: str
    reason: str  # rate_limit | quota | server_error | timeout | connection | auth |
    #              request_error | empty_reply | unexpected
    status: int | None = None
    simulated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "key_id": self.key_id,
            "status": self.status,
            "reason": self.reason,
            "simulated": self.simulated,
        }


def classify(exc: BaseException) -> tuple[str, int | None]:
    """Map a provider exception to (reason, status). Every reason triggers fallback."""
    status = getattr(exc, "status_code", None)
    status = status if isinstance(status, int) else None
    message = str(exc).lower()

    if any(marker in message for marker in QUOTA_MARKERS):
        return "quota", status
    if isinstance(exc, litellm.RateLimitError) or status == 429:
        return "rate_limit", 429
    # Timeout subclasses APIConnectionError → must be checked first.
    if isinstance(exc, litellm.Timeout | asyncio.TimeoutError | TimeoutError):
        return "timeout", status
    # LiteLLM wraps transport failures as InternalServerError("... Connection error.").
    if isinstance(exc, litellm.APIConnectionError) or "connection error" in message:
        return "connection", status
    if isinstance(exc, litellm.AuthenticationError | litellm.PermissionDeniedError) or status in (
        401,
        403,
    ):
        return "auth", status
    if isinstance(
        exc,
        litellm.InternalServerError | litellm.BadGatewayError | litellm.ServiceUnavailableError,
    ) or (status is not None and status >= 500):
        return "server_error", status
    if isinstance(exc, litellm.APIError | litellm.BadRequestError):
        return "request_error", status
    return "unexpected", status


def _simulated_reason(status: int) -> str:
    return "rate_limit" if status == 429 else "server_error"


# ---------------------------------------------------------------------------------------
# Result and loop
# ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LLMResult:
    reply: str
    model: str
    resolution_source: str
    key_id: str | None
    fallback_attempts: int
    input_tokens: int
    output_tokens: int
    attempts: tuple[Attempt, ...] = ()


def build_call_kwargs(
    settings: Settings, key: ProviderKey, messages: list[dict[str, str]], *, read_cache: bool
) -> dict[str, Any]:
    """Arguments for one LiteLLM call. All non-message params are constants (stable cache key)."""
    return {
        "model": key.model,
        "messages": messages,
        "api_key": key.secret.get_secret_value(),
        "api_base": settings.llm_api_base,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_output_tokens,
        "timeout": settings.llm_timeout_seconds,
        "num_retries": 0,
        "max_retries": 0,
        "caching": True,
        "cache": {"no-cache": not read_cache},
    }


def _reply_text(response: Any) -> str:
    try:
        return (response.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, TypeError):
        return ""


def _count_tokens(model: str, *, messages=None, text: str | None = None) -> int:
    try:
        if messages is not None:
            return int(litellm.token_counter(model=model, messages=messages))
        return int(litellm.token_counter(model=model, text=text or ""))
    except Exception:  # noqa: BLE001 — tokenizer may be unavailable offline
        chars = sum(len(m.get("content", "")) for m in messages) if messages else len(text or "")
        return max(1, chars // 4)


def _usage(response: Any, model: str, messages, reply: str) -> tuple[int, int]:
    usage = getattr(response, "usage", None)
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    # LiteLLM reports 0/0 when the provider omitted usage; a real prompt is never 0 tokens
    # and a non-empty reply never has 0 completion tokens → estimate per request instead.
    if not isinstance(prompt, int) or prompt <= 0:
        prompt = _count_tokens(model, messages=messages)
    if not isinstance(completion, int) or (completion <= 0 and reply):
        completion = _count_tokens(model, text=reply)
    return prompt, completion


class FailoverService:
    def __init__(self, pool: KeyPool, settings: Settings) -> None:
        self.pool = pool
        self.settings = settings

    async def complete(self, messages: list[dict[str, str]], plan: SimulationPlan) -> LLMResult:
        failures: list[Attempt] = []
        # Simulation active → never read the cache, so the simulated keys are really
        # exercised (Phase 0 finding: a hit would short-circuit the failure). Writes stay on.
        read_cache = not plan.active

        for key in self.pool.keys:
            simulated_status = plan.status_for(key.id)
            if simulated_status is not None:
                attempt = Attempt(
                    key.id, _simulated_reason(simulated_status), simulated_status, True
                )
                failures.append(attempt)
                self._log_attempt(attempt)
                continue  # no network call, no health change

            outcome = await self._try_key(key, messages, read_cache)
            if isinstance(outcome, Attempt):
                failures.append(outcome)
                self._log_attempt(outcome)
                continue

            response, hit, kwargs = outcome
            reply = _reply_text(response)
            input_tokens, output_tokens = _usage(response, key.model, messages, reply)
            if not hit:
                self.pool.record_success(key)
                await cache_service.write_through(response, kwargs)
            result = LLMResult(
                reply=reply,
                model=key.model,
                resolution_source=LLM_CACHE if hit else LIVE_INFERENCE,
                key_id=None if hit else key.id,
                fallback_attempts=0 if hit else len(failures),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                attempts=() if hit else tuple(failures),
            )
            logger.info(
                "llm success source=%s key=%s fallback_attempts=%d",
                result.resolution_source,
                result.key_id,
                result.fallback_attempts,
            )
            return result

        logger.warning(
            "all providers unavailable: %s",
            ", ".join(f"{a.key_id}:{a.reason}{'(sim)' if a.simulated else ''}" for a in failures),
        )
        raise AllProvidersUnavailable([a.as_dict() for a in failures])

    async def _try_key(self, key: ProviderKey, messages, read_cache: bool):
        """One key: returns (response, cache_hit, kwargs) on success or a failed Attempt.

        If a cache hit has an empty reply (a poisoned entry), the same key is retried once
        with cache reads off — this is not a fallback and is not counted as one.
        """
        for allow_cache in (read_cache, False) if read_cache else (False,):
            kwargs = build_call_kwargs(self.settings, key, messages, read_cache=allow_cache)
            try:
                response = await litellm.acompletion(**kwargs)
            except Exception as exc:  # noqa: BLE001 — never escapes the loop
                reason, status = classify(exc)
                self.pool.record_failure(key, reason)
                logger.debug("provider error on %s: %s", key.id, exc)  # redacted by filter
                return Attempt(key.id, reason, status)
            hit = (
                response._hidden_params.get("cache_hit") is True
                if hasattr(response, "_hidden_params")
                else False
            )
            if _reply_text(response):
                return response, hit, kwargs
            if not hit:
                self.pool.record_failure(key, "empty_reply")
                return Attempt(key.id, "empty_reply")
            logger.warning("empty cached reply for %s; retrying without cache read", key.id)
        self.pool.record_failure(key, "empty_reply")
        return Attempt(key.id, "empty_reply")

    @staticmethod
    def _log_attempt(attempt: Attempt) -> None:
        logger.warning(
            "attempt failed key=%s reason=%s status=%s simulated=%s",
            attempt.key_id,
            attempt.reason,
            attempt.status,
            attempt.simulated,
        )
