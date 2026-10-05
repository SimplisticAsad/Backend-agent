"""LLM client: prompt file + provider + JSON parsing + semantic validation, with a bounded repair loop.

`call()` never loops forever: at most `max_repairs` correction rounds, then `LLMOutputError` with every problem found.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from app.llm.base import LLMError, LLMProvider, LLMRequest
from app.llm.prompt_loader import PromptLoader
from app.observability import scrub

Validator = Callable[[Any], list[str]]


class LLMOutputError(RuntimeError):
    def __init__(self, prompt_id: str, problems: list[str], attempts: int) -> None:
        self.prompt_id, self.problems, self.attempts = prompt_id, problems, attempts
        super().__init__(f"{prompt_id}: invalid output after {attempts} attempt(s): " + "; ".join(problems[:5]))


@dataclass
class CallRecord:
    prompt_id: str
    attempts: int = 0
    problems: list[str] = field(default_factory=list)
    provider: str = ""


def extract_json(text: str) -> Any:
    """Parse a JSON document from model text (tolerates a ```json fence or leading/trailing prose)."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start = min((i for i in (t.find("{"), t.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise
        return json.JSONDecoder().raw_decode(t[start:])[0]


class LLMClient:
    def __init__(self, provider: LLMProvider, loader: PromptLoader | None = None, max_repairs: int = 2) -> None:
        self.provider, self.loader, self.max_repairs = provider, loader or PromptLoader(), max_repairs
        self.records: list[CallRecord] = []

    def call(self, prompt_id: str, variables: dict[str, str], context: dict[str, Any] | None = None, validate: Validator | None = None) -> Any:
        system = self.loader.raw("shared/system") if (self.loader.dir / "shared" / "system.md").exists() else "Return only the requested JSON."
        prompt = self.loader.render(prompt_id, variables)
        rec = CallRecord(prompt_id, provider=self.provider.name)
        self.records.append(rec)
        problems: list[str] = []
        for attempt in range(self.max_repairs + 1):
            rec.attempts = attempt + 1
            user = prompt if not problems else self.loader.render("structured_output_correction", {"PROMPT_ID": prompt_id, "PROBLEMS": "\n".join(f"- {p}" for p in problems), "ORIGINAL_PROMPT": prompt})
            try:
                resp = self.provider.generate(LLMRequest(prompt_id, system, user, context or {}))
            except LLMError:
                raise
            try:
                data = extract_json(resp.text)
            except (json.JSONDecodeError, ValueError) as e:
                problems = [f"response is not valid JSON ({e})"]
                continue
            problems = validate(data) if validate else []
            if not problems:
                rec.problems = []
                return data
        rec.problems = [scrub(p) for p in problems]
        raise LLMOutputError(prompt_id, problems, rec.attempts)
