"""Application-level exceptions (the domain vocabulary for failures).

They live in `backend_app.errors`, next to the single mapping that turns them into HTTP responses, and are re-exported here
so domain/application code depends on *what went wrong*, not on HTTP.
"""
from ..errors import (  # noqa: F401
    AppError,
    AuthenticationError,
    ConflictError,
    EntityNotFound,
    ExternalServiceError,
    PermissionDenied,
    StateTransitionError,
    ValidationError,
)
