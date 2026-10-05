"""Environment-based settings for the agent itself (not for the generated backend).

Secrets are never printed: `redacted()` is the only form that is safe to log.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

_SECRET_HINTS = ("KEY", "SECRET", "PASSWORD", "TOKEN", "URL")


def redact_url(url: str) -> str:
    """postgresql://user:pw@host/db -> postgresql://user:***@host/db"""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        creds = creds.split(":", 1)[0] + ":***"
    return f"{scheme}://{creds}@{host}"


@dataclass(frozen=True)
class Settings:
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    llm_base_url: str | None = None
    llm_api_key: str | None = field(default=None, repr=False)
    llm_timeout_seconds: float = 120.0
    max_corrections: int = 3
    test_database_url: str | None = field(default=None, repr=False)
    projects_dir: Path = Path("projects")
    frontend_agent_dir: Path | None = None
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env
        fad = e.get("FRONTEND_AGENT_DIR")
        return cls(
            llm_provider=e.get("LLM_PROVIDER", "anthropic"),
            llm_model=e.get("LLM_MODEL", "claude-sonnet-5-5"),
            llm_base_url=e.get("LLM_BASE_URL") or None,
            llm_api_key=e.get("LLM_API_KEY") or e.get("ANTHROPIC_API_KEY") or None,
            llm_timeout_seconds=float(e.get("LLM_TIMEOUT_SECONDS", "120")),
            max_corrections=max(0, int(e.get("MAX_CORRECTIONS", "3"))),
            test_database_url=e.get("TEST_DATABASE_URL") or None,
            projects_dir=Path(e.get("PROJECTS_DIR", "projects")),
            frontend_agent_dir=Path(fad) if fad else None,
            log_level=e.get("LOG_LEVEL", "INFO").upper(),
        )

    def with_changes(self, **kw) -> "Settings":
        return replace(self, **kw)

    def redacted(self) -> dict[str, object]:
        return {
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_base_url": self.llm_base_url,
            "llm_api_key": "***" if self.llm_api_key else None,
            "max_corrections": self.max_corrections,
            "test_database_url": redact_url(self.test_database_url) if self.test_database_url else None,
            "projects_dir": str(self.projects_dir),
            "frontend_agent_dir": str(self.frontend_agent_dir) if self.frontend_agent_dir else None,
        }
