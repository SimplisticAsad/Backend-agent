"""Authentication (who is calling?) and role authorization (may this role call this operation?).

Object-level authorization (may this caller touch THIS row?) lives in rules.py / engine.py.
The caller's role is always read from the database on each request, never trusted from the token.
"""
from __future__ import annotations

from typing import Callable
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import Database
from .engine import Principal
from .errors import AuthenticationError, PermissionDenied
from .repository import BaseRepository
from .security import RevocationList, TokenService, password_fingerprint
from .spec import SpecView

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth", description="JWT obtained from the login operation.")


class Authenticator:
    def __init__(self, view: SpecView, db: Database, tokens: TokenService, revocations: RevocationList, user_repo: BaseRepository) -> None:
        self.view, self.db, self.tokens, self.revocations, self.users = view, db, tokens, revocations, user_repo

    def authenticate(self, credentials: HTTPAuthorizationCredentials | None) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
            raise AuthenticationError()
        try:
            claims = self.tokens.decode_access_token(credentials.credentials)
            user_id = UUID(claims.subject)
        except (jwt.PyJWTError, ValueError):
            raise AuthenticationError("The access token is invalid or has expired.", code="INVALID_TOKEN") from None
        if self.revocations.is_revoked(claims.token_id):
            raise AuthenticationError("The access token has been revoked.", code="TOKEN_REVOKED")
        with self.db.transaction() as conn:
            row = self.users.get(conn, user_id)
        if row is None or claims.fingerprint != password_fingerprint(row["password_hash"]):
            raise AuthenticationError("The access token is invalid or has expired.", code="INVALID_TOKEN")
        role_id = self.view.role_by_key.get(row["role"])
        if role_id is None:
            raise AuthenticationError("The account has no valid role.", code="INVALID_TOKEN")
        return Principal(user_id, row["email"], row["role"], role_id, self.view.role_ids_held(role_id), claims.token_id, claims.expires_at)


def authorize(op_id: str) -> Callable[..., Principal | None]:
    """FastAPI dependency factory: authentication + role check for one graph operation."""

    def _mark(request: Request) -> None:
        request.state.operation_id = op_id

    def _check(request: Request, principal: Principal) -> Principal:
        view: SpecView = request.app.state.container.view
        request.state.principal_id = str(principal.user_id)
        if view.operations[op_id]["access"] == "restricted" and principal.role_id not in view.allowed_role_ids(op_id):
            raise PermissionDenied()
        return principal

    def public(request: Request) -> None:
        _mark(request)
        return None

    def secured(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> Principal:
        _mark(request)
        return _check(request, request.app.state.container.authenticator.authenticate(credentials))

    # public operations must not advertise the bearer scheme in OpenAPI
    return public if _is_public(op_id) else secured


def _is_public(op_id: str) -> bool:
    from .spec import load_spec

    return load_spec()["operations"][op_id]["access"] == "public"
