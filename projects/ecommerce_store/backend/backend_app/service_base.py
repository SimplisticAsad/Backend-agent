"""Base classes for generated application services."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .auth_service import AuthService
from .config import Settings
from .db import Database
from .emailer import EmailService
from .engine import Engine
from .errors import OperationNotImplemented
from .repository import BaseRepository
from .spec import SpecView


@dataclass
class ServiceContext:
    settings: Settings
    db: Database
    view: SpecView
    engine: Engine
    repos: dict[str, BaseRepository]
    auth: AuthService | None
    email: EmailService


class BaseService:
    """Services hold no state: every public method is one graph operation and delegates to the engine or a handler."""

    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self.engine = ctx.engine
        self.db = ctx.db
        self.repos = ctx.repos
        self.view = ctx.view

    def not_implemented(self, op_id: str) -> Any:
        raise OperationNotImplemented(
            f"{op_id} is specified in the graph but has no implementation yet.", details={"operation": op_id})
