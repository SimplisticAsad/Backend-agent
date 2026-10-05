"""MockLLMProvider: deterministic, offline, free. The whole agent test-suite runs on it (no API key, no network).

Optional `scripted` answers override the built-in handlers per prompt id (a list is consumed in order; the last item repeats).
That is how tests simulate a model that proposes a bad answer first, or a specific code correction.
"""
from __future__ import annotations

from typing import Any

from app.llm.base import LLMProvider, LLMRequest, LLMResponse
from app.llm.mock_handlers import respond


class MockLLMProvider(LLMProvider):
    name = "mock"

    def __init__(self, scripted: dict[str, list[str] | str] | None = None) -> None:
        self.scripted = {k: ([v] if isinstance(v, str) else list(v)) for k, v in (scripted or {}).items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._used: dict[str, int] = {}

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append((request.prompt_id, request.context))
        pid = request.prompt_id
        if pid in self.scripted and self.scripted[pid]:
            i = min(self._used.get(pid, 0), len(self.scripted[pid]) - 1)
            self._used[pid] = self._used.get(pid, 0) + 1
            return LLMResponse(self.scripted[pid][i], self.name, "mock")
        return LLMResponse(respond(pid, request.context), self.name, "mock")

    def prompt_ids(self) -> list[str]:
        return [p for p, _ in self.calls]
