"""Application exceptions and the single place where they become HTTP responses.

Error contract (every non-2xx body):
    {
      "error": {"code": "TASK_NOT_FOUND", "message": "...", "details": {...}},   # canonical
      "message": "...",                                                         # Frontend client reads this
      "errors": {"field": "first message", ...},                                # Frontend client reads this (422/400)
      "request_id": "..."
    }
Nothing from the database, the filesystem or the stack ever reaches a response body.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg
import psycopg_pool
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("app.errors")


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"
    default_message = "An unexpected error occurred."

    def __init__(self, message: str | None = None, *, code: str | None = None, details: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        self.message = message or self.default_message
        self.code = code or self.code
        self.details = details or {}
        self.headers = headers or {}
        super().__init__(self.message)


class ValidationError(AppError):
    status_code, code, default_message = 422, "VALIDATION_ERROR", "The request is not valid."


class MalformedRequest(AppError):
    status_code, code, default_message = 400, "MALFORMED_REQUEST", "The request could not be parsed."


class AuthenticationError(AppError):
    status_code, code, default_message = 401, "UNAUTHENTICATED", "Authentication is required."

    def __init__(self, message: str | None = None, **kw: Any):
        kw.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kw)


class PermissionDenied(AppError):
    status_code, code, default_message = 403, "FORBIDDEN", "You do not have permission to perform this action."


class EntityNotFound(AppError):
    status_code, code, default_message = 404, "NOT_FOUND", "The requested resource was not found."


class ConflictError(AppError):
    status_code, code, default_message = 409, "CONFLICT", "The request conflicts with the current state of the resource."


class StateTransitionError(ConflictError):
    code, default_message = "INVALID_STATE_TRANSITION", "This state change is not allowed."


class RateLimited(AppError):
    status_code, code, default_message = 429, "RATE_LIMITED", "Too many requests. Please wait and try again."


class PayloadTooLarge(AppError):
    status_code, code, default_message = 413, "PAYLOAD_TOO_LARGE", "The request body is too large."


class ExternalServiceError(AppError):
    status_code, code, default_message = 502, "EXTERNAL_SERVICE_ERROR", "An external service failed."


class ServiceUnavailable(AppError):
    status_code, code, default_message = 503, "SERVICE_UNAVAILABLE", "The service is temporarily unavailable."


class OperationNotImplemented(AppError):
    status_code, code, default_message = 501, "OPERATION_NOT_IMPLEMENTED", "This operation is specified but not implemented."


# ---------------------------------------------------------------------------------------------------
def error_body(code: str, message: str, details: dict[str, Any] | None = None, request_id: str | None = None,
               field_errors: dict[str, str] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message, "details": details or {}}, "message": message}
    if field_errors:
        body["errors"] = field_errors
    if request_id:
        body["request_id"] = request_id
    return body


def _rid(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _respond(request: Request, status: int, code: str, message: str, details: dict | None = None,
             field_errors: dict[str, str] | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    request.state.error_code = code
    return JSONResponse(error_body(code, message, details, _rid(request), field_errors), status_code=status, headers=headers)


def translate_db_error(exc: BaseException, *, deleting: bool = False) -> AppError:
    """Map driver errors to safe application errors. The original is logged server-side only."""
    log.error("database error: %s: %s", type(exc).__name__, str(exc).splitlines()[0] if str(exc) else "")
    if isinstance(exc, psycopg_pool.PoolTimeout):
        return ServiceUnavailable("The service is busy. Please retry shortly.", code="DATABASE_BUSY")
    if isinstance(exc, (psycopg_pool.PoolClosed, psycopg.OperationalError, psycopg.InterfaceError)) and not isinstance(exc, (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected, psycopg.errors.QueryCanceled)):
        return ServiceUnavailable("The database is currently unavailable.", code="DATABASE_UNAVAILABLE")
    if isinstance(exc, psycopg.errors.UniqueViolation):
        return ConflictError("A record with the same unique value already exists.", code="DUPLICATE_VALUE")
    if isinstance(exc, psycopg.errors.ForeignKeyViolation):
        if deleting:
            return ConflictError("The record is still referenced by other records.", code="ENTITY_IN_USE")
        return ValidationError("A referenced record does not exist.", code="REFERENCE_NOT_FOUND")
    if isinstance(exc, psycopg.errors.NotNullViolation):
        return ValidationError("A required value is missing.", code="REQUIRED_VALUE_MISSING")
    if isinstance(exc, psycopg.errors.CheckViolation):
        return ValidationError("The value violates a data constraint.", code="CONSTRAINT_VIOLATION")
    if isinstance(exc, (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected)):
        return ConflictError("The record was modified concurrently. Please retry.", code="CONCURRENT_UPDATE")
    if isinstance(exc, psycopg.errors.QueryCanceled):
        return ServiceUnavailable("The database took too long to respond.", code="DATABASE_TIMEOUT")
    if isinstance(exc, psycopg.DataError):
        return ValidationError("A value is out of the allowed range or malformed.", code="INVALID_VALUE")
    return AppError("A database error occurred.", code="DATABASE_ERROR")


def _field_errors(errors: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in errors:
        loc = [str(p) for p in e.get("loc", ()) if p not in ("body", "query", "path", "header")]
        key = ".".join(loc) or "body"
        out.setdefault(key, str(e.get("msg", "invalid value")))
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("application error %s: %s", exc.code, exc.message)
        return _respond(request, exc.status_code, exc.code, exc.message, exc.details, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = exc.errors()
        fields = _field_errors(errs)
        if errs and errs[0].get("type") in ("json_invalid", "model_attributes_type") and all(e.get("type") in ("json_invalid", "model_attributes_type") for e in errs):
            return _respond(request, 400, "MALFORMED_REQUEST", "The request body is not valid JSON.")
        details = {"fields": [{"field": k, "message": v} for k, v in fields.items()]}
        return _respond(request, 422, "VALIDATION_ERROR", "The request is not valid.", details, fields)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {404: ("NOT_FOUND", "The requested resource was not found."), 405: ("METHOD_NOT_ALLOWED", "This method is not allowed for the resource."),
                   401: ("UNAUTHENTICATED", "Authentication is required."), 413: ("PAYLOAD_TOO_LARGE", "The request body is too large."), 403: ("FORBIDDEN", "You do not have permission to perform this action.")}
        code, msg = mapping.get(exc.status_code, ("HTTP_ERROR", "The request could not be processed."))
        return _respond(request, exc.status_code, code, msg, headers=getattr(exc, "headers", None))

    @app.exception_handler(psycopg.Error)
    async def _db(request: Request, exc: psycopg.Error) -> JSONResponse:
        app_exc = translate_db_error(exc)
        return _respond(request, app_exc.status_code, app_exc.code, app_exc.message)

    @app.exception_handler(psycopg_pool.PoolTimeout)
    async def _pool(request: Request, exc: Exception) -> JSONResponse:
        app_exc = translate_db_error(exc)
        return _respond(request, app_exc.status_code, app_exc.code, app_exc.message)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled exception on %s %s", request.method, request.url.path)  # stack goes to the log only
        return _respond(request, 500, "INTERNAL_ERROR", "An unexpected error occurred.")
