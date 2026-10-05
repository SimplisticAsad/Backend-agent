"""LLM provider contract. Stages never import a vendor SDK; they talk to `LLMProvider.generate`."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    """Transport/protocol failure talking to a provider (never contains API keys)."""


@dataclass(frozen=True)
class LLMRequest:
    prompt_id: str
    system: str
    user: str
    # Structured copy of the inputs embedded in `user`. Real providers ignore it; the deterministic mock reads it.
    context: dict[str, Any] = field(default_factory=dict)
    json_mode: bool = True


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str = ""


class LLMProvider(ABC):
    name = "abstract"

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        """Return the model's raw text answer (expected to be a JSON document)."""
