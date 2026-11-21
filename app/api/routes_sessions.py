from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import AppState, get_state
from app.schemas.sessions import HistoryMessage, SessionHistoryResponse

router = APIRouter()


@router.get(
    "/sessions/{session_id}/messages",
    response_model=SessionHistoryResponse,
    responses={
        404: {
            "description": "Unknown session",
            "content": {"application/json": {"example": {"detail": "session not found"}}},
        }
    },
)
async def session_messages(
    session_id: str,
    state: AppState = Depends(get_state),  # noqa: B008
) -> SessionHistoryResponse:
    stored = await state.sessions.full_history(session_id)  # raises SessionNotFound → 404
    return SessionHistoryResponse(
        session_id=session_id,
        messages=[
            HistoryMessage(
                role=m.role,
                content=m.content,
                created_at=m.created_at,
                metadata=m.metadata if m.role == "assistant" else None,
            )
            for m in stored
        ],
    )
