"""Shared API pieces: error response models for OpenAPI, documented error statuses, pagination parameters."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code, e.g. TASK_NOT_FOUND")
    message: str = Field(description="Safe, human-readable message")
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail
    message: str = Field(description="Same as error.message (read by the Frontend client)")
    errors: dict[str, str] | None = Field(default=None, description="Field-level messages for 400/422")
    request_id: str | None = None


def error_responses(*, authenticated: bool, restricted: bool, has_path_id: bool, mutating: bool, rate_limited: bool = False) -> dict[int | str, dict[str, Any]]:
    out: dict[int | str, dict[str, Any]] = {
        400: {"model": ErrorResponse, "description": "Malformed request"},
        422: {"model": ErrorResponse, "description": "Validation error"},
        500: {"model": ErrorResponse, "description": "Unexpected server error"},
        503: {"model": ErrorResponse, "description": "Database or dependency unavailable"},
    }
    if authenticated:
        out[401] = {"model": ErrorResponse, "description": "Missing, invalid or expired credentials"}
    if restricted:
        out[403] = {"model": ErrorResponse, "description": "The caller's role or ownership does not permit this"}
    if has_path_id:
        out[404] = {"model": ErrorResponse, "description": "Resource not found"}
    if mutating:
        out[409] = {"model": ErrorResponse, "description": "Conflict with the current state"}
    if rate_limited:
        out[429] = {"model": ErrorResponse, "description": "Too many requests"}
    return out


from decimal import Decimal  # noqa: E402
from typing import Annotated  # noqa: E402

from pydantic import PlainSerializer  # noqa: E402

JsonDecimal = Annotated[Decimal, PlainSerializer(lambda v: float(v), return_type=float, when_used="json")]
"""Decimals are exposed as JSON numbers (the Frontend types them as `number`)."""
