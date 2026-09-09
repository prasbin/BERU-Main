"""Application error types and FastAPI exception handlers.

All BERU-specific failures derive from :class:`BeruError`, which carries an HTTP
status code, a stable machine-readable ``error_type``, and an optional detail
payload. Handlers translate exceptions into a consistent JSON envelope:

    {"error": {"type": "not_found", "message": "...", "detail": {...}}}

This keeps API error responses predictable for clients and future UIs.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.core.logging import get_logger

logger = get_logger(__name__)


class BeruError(Exception):
    """Base class for all BERU application errors."""

    status_code: int = 500
    error_type: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if error_type is not None:
            self.error_type = error_type
        self.detail = detail


class NotFoundError(BeruError):
    status_code = 404
    error_type = "not_found"


class AuthenticationError(BeruError):
    """Raised when a request is missing or presents an invalid API key."""

    status_code = 401
    error_type = "authentication_error"


class RateLimitError(BeruError):
    """Raised when a client exceeds the per-IP rate limit."""

    status_code = 429
    error_type = "rate_limit_exceeded"


class BadRequestError(BeruError):
    status_code = 400
    error_type = "bad_request"


class ConfigurationError(BeruError):
    status_code = 500
    error_type = "configuration_error"


class LLMProviderError(BeruError):
    """Raised when an upstream LLM provider fails or returns invalid data."""

    status_code = 502
    error_type = "llm_provider_error"


def _envelope(error_type: str, message: str, detail: Any | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"type": error_type, "message": message}}
    if detail is not None:
        body["error"]["detail"] = detail
    return body


# Stamped on every error response so observability middleware can count error
# envelopes by type without parsing bodies. Documented in the metrics reference.
ERROR_TYPE_HEADER = "X-Beru-Error-Type"


def _error_response(
    error_type: str,
    message: str,
    *,
    status_code: int,
    detail: Any | None = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content=_envelope(error_type, message, detail),
    )
    response.headers[ERROR_TYPE_HEADER] = error_type
    return response


async def _handle_beru_error(request: Request, exc: BeruError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.error("BeruError on %s %s: %s", request.method, request.url.path, exc.message)
    # Provider errors may embed network/response details useful for local
    # debugging; log them fully but never return them to the client.
    if isinstance(exc, LLMProviderError):
        logger.warning(
            "LLM provider failure on %s %s: %s (detail=%s)",
            request.method,
            request.url.path,
            exc.message,
            exc.detail,
        )
        return _error_response(
            exc.error_type,
            "The language model provider could not complete the request.",
            status_code=exc.status_code,
        )
    return _error_response(
        exc.error_type,
        exc.message,
        status_code=exc.status_code,
        detail=exc.detail,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register consistent JSON error handlers on the FastAPI app."""

    app.exception_handler(BeruError)(_handle_beru_error)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_response(
            "validation_error",
            "Request validation failed",
            status_code=422,
            detail=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error_response(
            "http_error",
            str(exc.detail),
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return _error_response(
            "internal_error",
            "An unexpected error occurred.",
            status_code=500,
        )
