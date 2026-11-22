"""FastAPI application factory and lifespan."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import AppState
from app.api.routes_chat import router as chat_router
from app.api.routes_health import router as health_router
from app.api.routes_sessions import router as sessions_router
from app.core.config import MIN_KEYS_FOR_READINESS, Settings, get_settings
from app.core.errors import CatchAllMiddleware, register_error_handlers
from app.core.logging import configure_logging
from app.db.database import Database
from app.services import cache_service
from app.services.chat_service import ChatService
from app.services.failover import FailoverService, KeyPool
from app.services.llm_service import LLMService
from app.services.session_service import SessionService

logger = logging.getLogger("app")

CORS_ALLOWED_HEADERS = ["Content-Type", "X-Simulate-Failure"]
CORS_ALLOWED_METHODS = ["GET", "POST", "OPTIONS"]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Order: key pool (fails fast on bad config) → DB schema → cache.
        key_pool = KeyPool.from_settings(settings)
        if len(key_pool) < MIN_KEYS_FOR_READINESS:
            logger.warning(
                "only %d API key(s) configured; /health will report 503 until >= %d",
                len(key_pool),
                MIN_KEYS_FOR_READINESS,
            )
        database = Database(settings.async_database_url, settings.database_path)
        await database.init_schema()
        cache = cache_service.init_cache(settings)
        sessions = SessionService(database, settings)
        llm = LLMService(settings, FailoverService(key_pool, settings))
        chat = ChatService(settings, database, sessions, llm)
        app.state.services = AppState(settings, database, key_pool, cache, sessions, chat)
        logger.info(
            "startup complete (env=%s, keys=%d, cache=%s, failure_simulation=%s)",
            settings.app_env,
            len(key_pool),
            cache.enabled,
            settings.enable_failure_simulation,
        )
        try:
            yield
        finally:
            await database.dispose()
            cache_service.disable_cache()
            logger.info("shutdown complete")

    app = FastAPI(
        title="Lyftr LLM Chatbot API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    register_error_handlers(app)
    # Added first → inner layer; CORS added last → outermost, so even 500s carry CORS headers.
    app.add_middleware(CatchAllMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=CORS_ALLOWED_METHODS,
        allow_headers=CORS_ALLOWED_HEADERS,
        allow_credentials=False,
        max_age=600,
    )
    app.include_router(health_router)
    app.include_router(chat_router)
    app.include_router(sessions_router)
    return app


def __getattr__(name: str):
    # `uvicorn app.main:app` — build lazily so importing this module (e.g. in tests)
    # does not read the environment or configure logging as a side effect.
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(name)
