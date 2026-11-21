from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Header

from app.api.deps import AppState, get_state
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.simulation import SimulationPlan, parse_simulation_header

router = APIRouter()

ERROR_RESPONSES = {
    422: {"description": "Validation error (FastAPI default shape)"},
    503: {
        "description": "Every key failed",
        "content": {"application/json": {"example": {"detail": "all providers unavailable"}}},
    },
}


@router.post("/chat", response_model=ChatResponse, responses=ERROR_RESPONSES)
async def chat(
    body: ChatRequest,
    state: AppState = Depends(get_state),  # noqa: B008
    x_simulate_failure: str | None = Header(default=None, alias="X-Simulate-Failure"),  # noqa: B008
) -> ChatResponse:
    started = time.perf_counter()
    plan = SimulationPlan.none()
    if state.settings.enable_failure_simulation:
        plan = parse_simulation_header(x_simulate_failure, state.key_pool.ids)
    return await state.chat.handle(body, plan, started)
