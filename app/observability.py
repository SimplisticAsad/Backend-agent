"""Stage-level observability: one record per major stage, persisted as JSON lines + a summary.

Records: stage, status, duration, errors, correction attempts, artifact paths. Secrets are scrubbed
before anything is written or printed.
"""
from __future__ import annotations

import json
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"), r"\1***"),
    (re.compile(r"(?i)((?:password|passwd|secret|api[_-]?key|token|authorization)\s*[=:]\s*)[^\s,;'\"]+"), r"\1***"),
    (re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "***jwt***"),
]


def scrub(text: str) -> str:
    for pat, rep in _SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


def scrub_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {k: ("***" if re.search(r"(?i)password|secret|token|api_key|authorization", str(k)) and isinstance(v, str) else scrub_obj(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    return obj


@dataclass
class StageRecord:
    stage: str
    status: str = "running"
    duration_ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    correction_attempts: int = 0
    artifacts: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return scrub_obj({
            "stage": self.stage, "status": self.status, "duration_ms": round(self.duration_ms, 1),
            "errors": self.errors, "warnings": self.warnings, "correction_attempts": self.correction_attempts,
            "artifacts": self.artifacts, "details": self.details,
        })


class StageLog:
    """Collects stage records; optionally appends them to a JSONL file and echoes to stderr."""

    def __init__(self, path: Path | None = None, echo: bool = False) -> None:
        self.records: list[StageRecord] = []
        self.path = path
        self.echo = echo
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def stage(self, name: str) -> Iterator[StageRecord]:
        rec = StageRecord(name)
        self.records.append(rec)
        t0 = time.perf_counter()
        try:
            yield rec
            if rec.status == "running":
                rec.status = "passed"
        except BaseException as e:  # recorded then re-raised; never swallowed
            rec.status = "failed"
            rec.errors.append(f"{type(e).__name__}: {e}")
            raise
        finally:
            rec.duration_ms = (time.perf_counter() - t0) * 1000
            self._emit(rec)

    def _emit(self, rec: StageRecord) -> None:
        line = json.dumps(rec.to_dict(), sort_keys=True)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.echo:
            print(f"[stage] {rec.stage:<28} {rec.status:<8} {rec.duration_ms:8.1f} ms", file=sys.stderr)

    def summary(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.records]
