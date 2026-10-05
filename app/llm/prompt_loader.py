"""Prompt files: every LLM call renders an explicit file from app/prompts. Python contains no prompt text.

Format: first line `<!-- prompt-id: name -->`; `{{VARIABLE}}` placeholders (strict: missing or unused variables are errors);
`{{include:shared/file.md}}` fragments. Required sections are enforced by tests (see REQUIRED_SECTIONS).
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
REQUIRED_SECTIONS = ["ROLE", "OBJECTIVE", "INPUTS", "CONTEXT", "CONSTRAINTS", "SOURCE OF TRUTH", "OUTPUT FORMAT", "VALIDATION RULES", "FAILURE CONDITIONS"]
_VAR = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
_INC = re.compile(r"\{\{include:([a-zA-Z0-9_./-]+)\}\}")
_ID = re.compile(r"^<!--\s*prompt-id:\s*([a-z0-9_]+)\s*-->")


class PromptError(ValueError):
    pass


class PromptLoader:
    def __init__(self, directory: Path = PROMPTS_DIR) -> None:
        self.dir = directory

    def available(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.md") if not p.name.startswith("_"))

    def raw(self, prompt_id: str) -> str:
        path = self.dir / f"{prompt_id}.md"
        if not path.exists():
            raise PromptError(f"no prompt file for '{prompt_id}'")
        return self._expand(path.read_text(encoding="utf-8"))

    def _expand(self, text: str, depth: int = 0) -> str:
        if depth > 3:
            raise PromptError("include nesting too deep")

        def inc(m: re.Match) -> str:
            p = self.dir / m.group(1)
            if not p.exists():
                raise PromptError(f"missing include {m.group(1)}")
            return self._expand(p.read_text(encoding="utf-8"), depth + 1).strip()

        return _INC.sub(inc, text)

    def variables(self, prompt_id: str) -> set[str]:
        return set(_VAR.findall(self.raw(prompt_id)))

    def render(self, prompt_id: str, variables: dict[str, str]) -> str:
        text = self.raw(prompt_id)
        m = _ID.match(text)
        if not m or m.group(1) != prompt_id:
            raise PromptError(f"prompt file {prompt_id}.md must start with <!-- prompt-id: {prompt_id} -->")
        needed = set(_VAR.findall(text))
        missing, extra = needed - set(variables), set(variables) - needed
        if missing:
            raise PromptError(f"{prompt_id}: missing variables {sorted(missing)}")
        if extra:
            raise PromptError(f"{prompt_id}: unused variables {sorted(extra)}")
        return _VAR.sub(lambda mm: variables[mm.group(1)], text)

    def sections(self, prompt_id: str) -> list[str]:
        return re.findall(r"^#\s+(.+?)\s*$", self.raw(prompt_id), re.M)
