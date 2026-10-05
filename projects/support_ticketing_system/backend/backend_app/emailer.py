"""Outbound e-mail abstraction. Business logic depends on the protocol, never on a vendor."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

log = logging.getLogger("app.email")


class EmailService(Protocol):
    def send_password_reset(self, to_address: str, reset_token: str) -> None: ...


@dataclass
class SentEmail:
    to_address: str
    kind: str
    token: str


class OutboxEmailService:
    """Keeps messages in memory (tests, local development). A real adapter implements `EmailService` instead."""

    def __init__(self) -> None:
        self.outbox: list[SentEmail] = []

    def send_password_reset(self, to_address: str, reset_token: str) -> None:
        self.outbox.append(SentEmail(to_address, "password_reset", reset_token))
        log.info("password reset e-mail queued", extra={"kind": "password_reset"})  # the token is never logged
