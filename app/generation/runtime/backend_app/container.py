"""Composition root: builds every long-lived collaborator once per application."""
from __future__ import annotations

from dataclasses import dataclass

from .application.services import Services, build_services
from .auth import Authenticator
from .auth_service import AuthService
from .config import Settings
from .db import Database
from .emailer import EmailService, OutboxEmailService
from .engine import Engine
from .infrastructure.repositories import REPOSITORIES
from .ratelimit import RateLimiter
from .repository import BaseRepository
from .security import RevocationList, TokenService
from .service_base import ServiceContext
from .spec import SpecView, load_spec


@dataclass
class Container:
    settings: Settings
    db: Database
    view: SpecView
    repos: dict[str, BaseRepository]
    engine: Engine
    services: Services
    authenticator: Authenticator | None
    email: EmailService
    limiter: RateLimiter


def build_container(settings: Settings, db: Database | None = None, email: EmailService | None = None) -> Container:
    spec = load_spec()
    view = SpecView(spec)
    db = db or Database(settings)
    email = email or OutboxEmailService()
    repos = {eid: REPOSITORIES[eid](ent) for eid, ent in view.entities.items()}
    engine = Engine(view, db, repos, settings)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    auth_service = authenticator = None
    if spec["auth"]:
        tokens = TokenService(settings, issuer=spec["project"]["key"])
        revocations = RevocationList()
        users = repos[spec["auth"]["entity"]]
        auth_service = AuthService(settings, view, db, users, tokens, revocations, email, limiter)
        authenticator = Authenticator(view, db, tokens, revocations, users)
    ctx = ServiceContext(settings, db, view, engine, repos, auth_service, email)
    return Container(settings, db, view, repos, engine, build_services(ctx), authenticator, email, limiter)
