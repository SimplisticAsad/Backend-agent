"""Runtime configuration, read from environment variables (never hard-coded; see .env.example)."""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field

_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PRODUCTION_ENVS = {"production", "prod", "staging"}


class ConfigError(RuntimeError):
    """Invalid or unsafe configuration; the application refuses to start."""


def _int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except ValueError:
        raise ConfigError(f"{key} must be an integer")


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    database_url: str | None = field(default=None, repr=False)
    database_schema: str | None = None
    log_level: str = "INFO"
    jwt_secret: str = field(default="", repr=False)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60
    reset_token_ttl_minutes: int = 30
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_pool_timeout_seconds: float = 5.0
    db_statement_timeout_ms: int = 15_000
    max_body_bytes: int = 1_048_576
    rate_limit_per_minute: int = 20
    default_page_limit: int = 100
    max_page_limit: int = 1000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = dict(os.environ if env is None else env)
        app_env = e.get("APP_ENV", "development").lower()
        secret = e.get("JWT_SECRET", "")
        if not secret and app_env not in PRODUCTION_ENVS:
            secret = secrets.token_urlsafe(48)  # per-process dev secret: tokens do not survive a restart
        origins = tuple(o.strip() for o in e.get("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip())
        return cls(
            app_env=app_env, database_url=e.get("DATABASE_URL") or None, database_schema=e.get("DATABASE_SCHEMA") or None,
            log_level=e.get("LOG_LEVEL", "INFO").upper(), jwt_secret=secret, jwt_algorithm=e.get("JWT_ALGORITHM", "HS256"),
            access_token_ttl_minutes=_int(e, "ACCESS_TOKEN_TTL_MINUTES", 60), reset_token_ttl_minutes=_int(e, "RESET_TOKEN_TTL_MINUTES", 30),
            cors_origins=origins, db_pool_min=_int(e, "DB_POOL_MIN", 1), db_pool_max=_int(e, "DB_POOL_MAX", 10),
            db_pool_timeout_seconds=float(e.get("DB_POOL_TIMEOUT_SECONDS", "5")), db_statement_timeout_ms=_int(e, "DB_STATEMENT_TIMEOUT_MS", 15_000),
            max_body_bytes=_int(e, "MAX_BODY_BYTES", 1_048_576), rate_limit_per_minute=_int(e, "RATE_LIMIT_PER_MINUTE", 20),
            default_page_limit=_int(e, "DEFAULT_PAGE_LIMIT", 100), max_page_limit=_int(e, "MAX_PAGE_LIMIT", 1000),
        )

    @property
    def is_production(self) -> bool:
        return self.app_env in PRODUCTION_ENVS

    def validate(self) -> "Settings":
        if self.database_schema and not _SCHEMA_RE.match(self.database_schema):
            raise ConfigError("DATABASE_SCHEMA must match ^[a-z][a-z0-9_]{0,62}$")
        if self.jwt_algorithm not in ("HS256", "HS384", "HS512"):
            raise ConfigError("JWT_ALGORITHM must be one of HS256/HS384/HS512")
        if self.is_production:
            if len(self.jwt_secret) < 32:
                raise ConfigError("JWT_SECRET (>= 32 characters) is required when APP_ENV is production/staging")
            if "*" in self.cors_origins:
                raise ConfigError("CORS_ORIGINS must list explicit origins in production; '*' is refused")
        if not self.jwt_secret:
            raise ConfigError("JWT_SECRET is not set")
        if self.db_pool_min < 0 or self.db_pool_max < max(1, self.db_pool_min):
            raise ConfigError("invalid DB pool size")
        return self
