"""Credential flows: login, logout, password reset. Implemented once, driven by the spec's auth section."""
from __future__ import annotations

from typing import Any

import jwt

from .config import Settings
from .db import Database
from .emailer import EmailService
from .engine import Principal
from .errors import AuthenticationError, MalformedRequest
from .ratelimit import RateLimiter
from .repository import BaseRepository
from .security import DUMMY_HASH, RevocationList, TokenService, hash_password, password_fingerprint, verify_password
from .spec import SpecView


class AuthService:
    def __init__(self, settings: Settings, view: SpecView, db: Database, users: BaseRepository, tokens: TokenService,
                 revocations: RevocationList, email: EmailService, limiter: RateLimiter) -> None:
        self.s, self.view, self.db, self.users, self.tokens = settings, view, db, users, tokens
        self.revocations, self.email, self.limiter = revocations, email, limiter
        self.auth_spec = view.spec["auth"]

    def _error_code(self, kind: str, default: str) -> str:
        op = self.view.operations.get(self.auth_spec["operations"].get(kind, ""), {})
        return (op.get("errors") or [{"code": default}])[0]["code"]

    def login(self, email: str, password: str, client: str) -> dict[str, Any]:
        self.limiter.check(f"login:{client}")
        self.limiter.check(f"login-id:{email.lower()}")
        with self.db.transaction() as conn:
            row = self.users.get_by(conn, self.auth_spec["identity_field"], email, case_insensitive=True)
        ok = verify_password(password, row["password_hash"] if row else DUMMY_HASH)  # always one hash verification: no timing oracle
        if not row or not ok or row["role"] not in self.view.role_by_key:
            raise AuthenticationError("Email or password is wrong.", code=self._error_code("auth.login", "INVALID_CREDENTIALS"))
        token, ttl = self.tokens.issue_access_token(str(row["id"]), row["role"], row["password_hash"])
        public = {k: v for k, v in row.items() if k != "password_hash"}
        return {**public, "token": token, "token_type": "bearer", "expires_in": ttl, "user": public}

    def logout(self, principal: Principal) -> None:
        self.revocations.revoke(principal.token_id, principal.token_exp)

    def request_password_reset(self, email: str, client: str) -> None:
        self.limiter.check(f"reset-request:{client}")
        with self.db.transaction() as conn:
            row = self.users.get_by(conn, self.auth_spec["identity_field"], email, case_insensitive=True)
        if row:  # the response is identical whether or not the account exists
            self.email.send_password_reset(row["email"], self.tokens.issue_reset_token(str(row["id"]), row["password_hash"]))

    def reset_password(self, reset_token: str, new_password: str, client: str) -> None:
        self.limiter.check(f"reset:{client}")
        bad = MalformedRequest("The reset token is invalid or expired.", code=self._error_code("auth.reset_password", "INVALID_RESET_TOKEN"))
        try:
            claims = self.tokens.decode_reset_token(reset_token)
        except jwt.PyJWTError:
            raise bad from None
        from uuid import UUID

        try:
            uid = UUID(claims.subject)
        except ValueError:
            raise bad from None
        with self.db.transaction() as conn:
            row = self.users.get(conn, uid, for_update=True)
            if row is None or claims.fingerprint != password_fingerprint(row["password_hash"]):
                raise bad
            self.users.update(conn, uid, {"password_hash": hash_password(new_password)})
