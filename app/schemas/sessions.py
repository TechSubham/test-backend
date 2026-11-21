"""GET /sessions/{session_id}/messages models (API_SPEC.md §3)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, SerializerFunctionWrapHandler, model_serializer


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    created_at: str  # extra field
    metadata: dict[str, Any] | None = None  # assistant messages only; omitted for user

    @model_serializer(mode="wrap")
    def _omit_absent_metadata(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        if data.get("metadata") is None:
            data.pop("metadata", None)  # user messages carry no metadata key (API_SPEC §3)
        return data


class SessionHistoryResponse(BaseModel):
    session_id: str
    messages: list[HistoryMessage]
