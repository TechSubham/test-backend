"""LLM service — builds the exact message list and maps failover results to metadata.

The message list sent to LiteLLM (and therefore the cache key) is:
    [{"role": "system", ...}, *history (oldest first), {"role": "user", <new message>}]
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.config import Settings
from app.db.repository import StoredMessage
from app.services.failover import FailoverService, LLMResult
from app.services.simulation import SimulationPlan


def resolve_system_prompt(requested: str | None, stored: str | None, default: str) -> str:
    """request (non-blank) → session's stored prompt → DEFAULT_SYSTEM_PROMPT."""
    for candidate in (requested, stored):
        if candidate is not None and candidate.strip():
            return candidate
    return default


def build_messages(
    system_prompt: str, history: Sequence[StoredMessage], user_message: str
) -> list[dict[str, str]]:
    turns = [{"role": m.role, "content": m.content} for m in history]
    # A truncated window must not start with an assistant turn.
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return [
        {"role": "system", "content": system_prompt},
        *turns,
        {"role": "user", "content": user_message},
    ]


def to_metadata(result: LLMResult, latency_ms: int) -> dict[str, Any]:
    """Contract metadata (API_SPEC §2) plus the extra `attempts` list. No secrets."""
    return {
        "resolution_source": result.resolution_source,
        "model": result.model,
        "latency_ms": int(latency_ms),
        "input_tokens": int(result.input_tokens),
        "output_tokens": int(result.output_tokens),
        "key_id": result.key_id,
        "fallback_attempts": int(result.fallback_attempts),
        "attempts": [a.as_dict() for a in result.attempts],
    }


class LLMService:
    def __init__(self, settings: Settings, failover: FailoverService) -> None:
        self.settings = settings
        self.failover = failover

    async def generate(
        self,
        *,
        system_prompt: str,
        history: Sequence[StoredMessage],
        user_message: str,
        plan: SimulationPlan,
    ) -> tuple[LLMResult, list[dict[str, str]]]:
        messages = build_messages(system_prompt, history, user_message)
        return await self.failover.complete(messages, plan), messages
