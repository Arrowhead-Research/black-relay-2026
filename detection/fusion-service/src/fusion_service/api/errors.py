"""The error envelope from API-CONTRACT.md and the handlers that produce it."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fusion_service.domain.errors import CursorExpired, InvalidCursor, StorageUnavailable


class ErrorBody(BaseModel):
    code: str
    message: str | None = None
    resume_after: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# error.code for HTTPExceptions raised inside routes, by status. Anything else is generic.
HTTP_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "invalid_request",
    503: "storage_unavailable",
}

# OpenAPI documentation for responses every authenticated route can return.
COMMON_ERROR_RESPONSES = {
    401: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def error_response(
    status_code: int, code: str, *, message: str | None = None, headers=None, **extra
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, **extra))
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content=body.model_dump(exclude_none=True),
    )


async def _validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    return error_response(422, "invalid_request")


async def _http_error(request: Request, error: HTTPException) -> JSONResponse:
    code = HTTP_ERROR_CODES.get(error.status_code, "error")
    return error_response(error.status_code, code, message=error.detail, headers=error.headers)


async def _storage_unavailable(request: Request, error: StorageUnavailable) -> JSONResponse:
    return error_response(503, "storage_unavailable")


async def _cursor_expired(request: Request, error: CursorExpired) -> JSONResponse:
    return error_response(
        410,
        "cursor_expired",
        resume_after=error.floor,
        message="history was deleted; resuming accepts a gap",
    )


async def _invalid_cursor(request: Request, error: InvalidCursor) -> JSONResponse:
    return error_response(400, "cursor_ahead")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(StorageUnavailable, _storage_unavailable)
    app.add_exception_handler(CursorExpired, _cursor_expired)
    app.add_exception_handler(InvalidCursor, _invalid_cursor)
