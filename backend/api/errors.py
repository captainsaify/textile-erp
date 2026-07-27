"""The uniform error envelope -- docs/10_API.md §5.

`code` is the stable identifier the frontend switches on; `message` is
copy that may be reworded freely. Keeping clients off the message text
is the whole point of having both.

Domain exceptions already carry a `code` and user-facing copy (see
backend/core/exceptions.py), so this maps them rather than restating
them, and picks the HTTP status from what the exception *means* --
never 500, which is reserved for genuinely unexpected failures.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.core.exceptions import (
    DomainError,
    DuplicateInvoiceError,
    NotFoundError,
    UnauthorizedRoleError,
    ValidationError,
)
from backend.core.logging import get_logger
from backend.services.auth_service import AuthError

logger = get_logger(__name__)


def envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return body


def _status_for(exc: DomainError) -> int:
    if isinstance(exc, NotFoundError):
        return status.HTTP_404_NOT_FOUND
    if isinstance(exc, UnauthorizedRoleError):
        return status.HTTP_403_FORBIDDEN
    if isinstance(exc, DuplicateInvoiceError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, ValidationError):
        return status.HTTP_400_BAD_REQUEST
    # a warning the caller must acknowledge is a semantically invalid
    # state, not a malformed request -- §5 maps that to 422
    return status.HTTP_422_UNPROCESSABLE_ENTITY


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        # expected business outcomes are logged at INFO, never ERROR
        # (docs/01_Architecture.md §10)
        logger.info("domain_error", code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=_status_for(exc),
            content=envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(AuthError)
    async def _auth(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=envelope("invalid_credentials", str(exc)),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException) -> JSONResponse:
        codes = {
            400: "bad_request",
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            409: "conflict",
            429: "rate_limited",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(codes.get(exc.status_code, "error"), str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=envelope(
                "validation_error",
                "The request body or query parameters aren't valid.",
                {"fields": exc.errors()},
            ),
        )
