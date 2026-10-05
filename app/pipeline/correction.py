"""Bounded autonomous correction loop.

    run checks -> if red: diagnose (LLM, specialised prompt) -> apply guarded edits -> run checks again
At most `max_attempts` corrections ever happen, then the loop stops and reports; it cannot run forever, and it stops early when
a round produces no applicable edit (nothing can change, so another identical run would be pointless).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

R = TypeVar("R")


@dataclass
class Attempt:
    number: int
    prompt: str
    diagnosis: str
    edits_proposed: int
    edits_applied: int
    rejected: list[str] = field(default_factory=list)
    failures_before: int = 0
    failures_after: int | None = None

    def to_dict(self) -> dict:
        return {"attempt": self.number, "prompt": self.prompt, "diagnosis": self.diagnosis[:500], "edits_proposed": self.edits_proposed, "edits_applied": self.edits_applied,
                "rejected": self.rejected, "failures_before": self.failures_before, "failures_after": self.failures_after}


@dataclass
class LoopResult(Generic[R]):
    ok: bool
    attempts: list[Attempt]
    final: R
    stopped_because: str

    @property
    def correction_attempts(self) -> int:
        return len(self.attempts)


def correction_loop(*, max_attempts: int, run_checks: Callable[[], R], is_ok: Callable[[R], bool], count_failures: Callable[[R], int],
                    correct: Callable[[R, int], Attempt]) -> LoopResult[R]:
    result = run_checks()
    attempts: list[Attempt] = []
    while not is_ok(result):
        if len(attempts) >= max_attempts:
            return LoopResult(False, attempts, result, f"reached the maximum of {max_attempts} correction attempt(s)")
        attempt = correct(result, len(attempts) + 1)
        attempts.append(attempt)
        if attempt.edits_applied == 0:
            attempt.failures_after = count_failures(result)
            return LoopResult(False, attempts, result, "no applicable correction was proposed")
        result = run_checks()
        attempt.failures_after = count_failures(result)
    return LoopResult(True, attempts, result, "all checks passed")
