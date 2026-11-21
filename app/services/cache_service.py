"""LiteLLM response cache — ARCHITECTURE.md §5.

Initialisation and dependency self-check live here. The write-through helper
(`write_through`) is used by the failover loop after every live success (§5.4).
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

import litellm

from app.core.config import Settings

logger = logging.getLogger("app.cache")

# LiteLLM's cache path imports these. Phase 0: without fastapi/orjson writes failed silently.
# Phase 1: without soundfile, acompletion(caching=True) raised ModuleNotFoundError on the
# cache *read*. The last entry is the exact module LiteLLM imports on that read path, so a
# missing transitive dependency is caught at startup (→ /health 503) instead of on a chat.
REQUIRED_MODULES = (
    "fastapi",
    "orjson",
    "soundfile",
    "litellm.llms.anthropic.messages.native.response_cache",
)


@dataclass(frozen=True)
class CacheStatus:
    enabled: bool
    cache_type: str
    error: str | None = None


def missing_cache_dependencies() -> list[str]:
    missing = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 — any import failure disables the cache
            missing.append(name)
    return missing


def init_cache(settings: Settings) -> CacheStatus:
    litellm.telemetry = False
    missing = missing_cache_dependencies()
    if missing:
        logger.error("LiteLLM cache disabled: missing modules %s", missing)
        litellm.cache = None
        return CacheStatus(False, settings.cache_type, f"missing modules: {missing}")
    litellm.enable_cache(
        type="local",
        supported_call_types=["acompletion", "completion"],
        ttl=settings.cache_ttl_seconds,
    )
    logger.info("LiteLLM cache enabled (type=local, ttl=%ss)", settings.cache_ttl_seconds)
    return CacheStatus(True, settings.cache_type)


def is_enabled() -> bool:
    return litellm.cache is not None


def disable_cache() -> None:
    litellm.cache = None


async def write_through(response: Any, call_kwargs: dict[str, Any]) -> None:
    """Persist a live response synchronously so an immediate repeat is a hit (§5.4)."""
    if litellm.cache is None:
        return
    try:
        await litellm.cache.async_add_cache(response, **call_kwargs)
    except Exception as exc:  # noqa: BLE001 — cache must never break a successful reply
        logger.warning("cache write-through failed: %s", type(exc).__name__)
