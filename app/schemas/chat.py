"""POST /chat request/response models — field names are contractual (API_SPEC.md §2)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MESSAGE_MAX_CHARS = 4000
SESSION_ID_MAX_CHARS = 128
SYSTEM_PROMPT_MAX_CHARS = 10_000

ResolutionSource = Literal["LIVE_INFERENCE", "LLM_CACHE"]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=False)

    session_id: str = Field(min_length=1, max_length=SESSION_ID_MAX_CHARS)
    message: str
    system_prompt: str | None = Field(default=None, max_length=SYSTEM_PROMPT_MAX_CHARS)

    @field_validator("session_id")
    @classmethod
    def _session_id_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id must not be blank")
        return value

    @field_validator("message")
    @classmethod
    def _message_trimmed_length(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("message must not be empty")
        if len(trimmed) > MESSAGE_MAX_CHARS:
            raise ValueError(f"message must be at most {MESSAGE_MAX_CHARS} characters")
        return trimmed

    @field_validator("system_prompt")
    @classmethod
    def _blank_prompt_is_absent(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return value.strip()


class AttemptInfo(BaseModel):
    """Extra (bonus) per-attempt record; never contains a secret."""

    key_id: str
    status: int | None = None
    reason: str
    simulated: bool = False


class ChatMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    resolution_source: ResolutionSource
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int


class ChatResponse(BaseModel):
    session_id: str
    reply: str = Field(min_length=1)
    metadata: ChatMetadata
