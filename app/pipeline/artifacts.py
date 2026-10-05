"""Deterministic artifact writer: sorted keys, 2-space indent, trailing newline, no timestamps in content."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.observability import scrub_obj


class ArtifactStore:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.written: list[str] = []

    def save(self, name: str, data: Any) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(scrub_obj(data), indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        if name not in self.written:
            self.written.append(name)
        return path

    def load(self, name: str) -> Any:
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def exists(self, name: str) -> bool:
        return (self.dir / name).exists()
