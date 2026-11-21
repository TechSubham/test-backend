"""Domain exceptions and JSON error handling.

Contract: every error is JSON with a `detail` field; never a stack trace or HTML page.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("app.errors")

ALL_PROVIDERS_UNAVAILABLE = "all providers unavailable"
SESSION_NOT_FOUND = "session not found"
INTERNAL_ERROR = "internal server error"


class AppError(Exception):
    status_code = 500
    detail = INTERNAL_ERROR

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.detail)
        if detail is not None:
            self.detail = detail


class AllProvidersUnavailable(AppError):
    status_code = 503
    detail = ALL_PROVIDERS_UNAVAILABLE

    def __init__(self, attempts: list | None = None) -> None:
        super().__init__()
        self.attempts = attempts or []


class SessionNotFound(AppError):
    status_code = 404
    detail = SESSION_NOT_FOUND


async def _app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    # FastAPI default shape: {"detail": [...]} with status 422.
    assert isinstance(exc, RequestValidationError)
    return await request_validation_exception_handler(request, exc)


async def _unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled exception: %s", type(exc).__name__, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": INTERNAL_ERROR})


class CatchAllMiddleware:
    """Innermost safety net: converts any escaped exception into JSON 500.

    Registered *before* CORSMiddleware so CORS stays the outer layer and even 500
    responses carry CORS headers (otherwise browsers report an opaque network error).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:  # noqa: BLE001 — last line of defence
            logger.error("unhandled exception: %s", type(exc).__name__, exc_info=exc)
            if response_started:
                raise
            response = JSONResponse(status_code=500, content={"detail": INTERNAL_ERROR})
            await response(scope, receive, send)


def register_error_handlers(app: FastAPI) -> None:
    handlers: dict[type[Exception], Callable[[Request, Exception], Awaitable[JSONResponse]]] = {
        AppError: _app_error_handler,
        StarletteHTTPException: _http_exception_handler,
        RequestValidationError: _validation_handler,
        Exception: _unhandled_handler,
    }
    for exc_type, handler in handlers.items():
        app.add_exception_handler(exc_type, handler)
