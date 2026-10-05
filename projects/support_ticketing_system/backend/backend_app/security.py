"""Password hashing (scrypt, stdlib), access tokens and password-reset tokens (JWT), in-process token revocation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
import uuid
from dataclasses import dataclass

import jwt

from .config import Settings

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1
ACCESS_AUDIENCE = "access"
RESET_AUDIENCE = "password-reset"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:  # malformed stored hash: never authenticates
        return False


DUMMY_HASH = hash_password("not-a-real-password")  # verified against when the account does not exist (timing equalisation)


def password_fingerprint(stored_hash: str) -> str:
    """Binds a reset token to the current password: once the password changes the token stops working."""
    return hashlib.sha256(stored_hash.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    token_id: str
    expires_at: int
    role: str | None = None
    fingerprint: str | None = None


class TokenService:
    def __init__(self, settings: Settings, issuer: str) -> None:
        self._s, self._iss = settings, issuer

    def issue_access_token(self, user_id: str, role: str, password_hash: str) -> tuple[str, int]:
        now = int(time.time())
        ttl = self._s.access_token_ttl_minutes * 60
        # `fp` binds the token to the current password: changing the password invalidates every existing token.
        claims = {"sub": user_id, "role": role, "fp": password_fingerprint(password_hash), "jti": uuid.uuid4().hex, "iat": now, "nbf": now, "exp": now + ttl, "iss": self._iss, "aud": ACCESS_AUDIENCE}
        return jwt.encode(claims, self._s.jwt_secret, algorithm=self._s.jwt_algorithm), ttl

    def decode_access_token(self, token: str) -> TokenClaims:
        c = self._decode(token, ACCESS_AUDIENCE)
        return TokenClaims(c["sub"], c["jti"], int(c["exp"]), c.get("role"), c.get("fp"))

    def issue_reset_token(self, user_id: str, password_hash: str) -> str:
        now = int(time.time())
        claims = {"sub": user_id, "fp": password_fingerprint(password_hash), "jti": uuid.uuid4().hex, "iat": now,
                  "exp": now + self._s.reset_token_ttl_minutes * 60, "iss": self._iss, "aud": RESET_AUDIENCE}
        return jwt.encode(claims, self._s.jwt_secret, algorithm=self._s.jwt_algorithm)

    def decode_reset_token(self, token: str) -> TokenClaims:
        c = self._decode(token, RESET_AUDIENCE)
        return TokenClaims(c["sub"], c["jti"], int(c["exp"]), fingerprint=c.get("fp"))

    def _decode(self, token: str, audience: str) -> dict:
        # The algorithm list is explicit: tokens claiming alg=none or another algorithm are rejected.
        return jwt.decode(token, self._s.jwt_secret, algorithms=[self._s.jwt_algorithm], audience=audience, issuer=self._iss,
                          options={"require": ["exp", "sub", "jti", "iss", "aud"]})


class RevocationList:
    """In-process denylist of token ids (logout). Not shared between processes: see README 'Known limitations'."""

    def __init__(self) -> None:
        self._items: dict[str, int] = {}
        self._lock = threading.Lock()

    def revoke(self, token_id: str, expires_at: int) -> None:
        with self._lock:
            now = int(time.time())
            self._items = {k: v for k, v in self._items.items() if v > now}
            self._items[token_id] = expires_at

    def is_revoked(self, token_id: str) -> bool:
        with self._lock:
            return token_id in self._items and self._items[token_id] > int(time.time())
