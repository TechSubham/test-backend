from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import AppState, get_state
from app.core.config import MIN_KEYS_FOR_READINESS
from app.schemas.health import HealthResponse
from app.services import cache_service

router = APIRouter()

NOT_READY = "service not ready"


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"description": "Not ready", "content": {"application/json": {}}}},
)
async def health(state: AppState = Depends(get_state)):  # noqa: B008
    pool = state.key_pool
    db_ok = await state.database.ping()
    cache_ok = state.cache.enabled and cache_service.is_enabled()

    problems: list[str] = []
    if not db_ok:
        problems.append("database unreachable")
    if len(pool) < MIN_KEYS_FOR_READINESS:
        problems.append(
            f"{len(pool)} API key(s) configured; at least {MIN_KEYS_FOR_READINESS} required"
        )
    elif pool.unique_secrets < MIN_KEYS_FOR_READINESS:
        problems.append(
            f"only {pool.unique_secrets} distinct API key(s); duplicates "
            f"({', '.join(pool.duplicate_ids())}) are not independent — "
            f"at least {MIN_KEYS_FOR_READINESS} distinct keys required"
        )
    if not cache_ok:
        problems.append("LLM cache not enabled")

    if problems:
        return JSONResponse(
            status_code=503,
            content={
                "detail": NOT_READY,
                "status": "unavailable",
                "problems": problems,
                "keys_configured": len(pool),
                "keys_unique": pool.unique_secrets,
                "cache_enabled": cache_ok,
            },
        )

    return HealthResponse(
        status="ok",
        keys_configured=len(pool),
        cache_enabled=True,
        cache_type=state.cache.cache_type,
        keys_unique=pool.unique_secrets,
        keys_healthy=pool.healthy_count,
        min_keys_required=MIN_KEYS_FOR_READINESS,
        failure_simulation_enabled=state.settings.enable_failure_simulation,
        database="ok",
        model=state.settings.llm_model,
    )
