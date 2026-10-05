"""Applying LLM-proposed code corrections safely.

An edit is an exact search/replace on ONE file inside the generated package. It is rejected unless it passes every guard; an
accepted edit that breaks compilation is rolled back. Corrections can therefore fix implementations but cannot weaken the
backend: removing authorization/validation tokens, adding blanket exception handling, string-built SQL or CORS wildcards is refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

EDITABLE_ROOT = "backend_app"
PROTECTED = ("backend_app/generated/spec.json",)
# tokens that carry security/validation meaning: an edit may not reduce their count in a file
GUARD_TOKENS = ["authorize(", "check_ownership", "check_transition", "check_frozen", "check_transition_guards", "scope_sql", "_in_scope", "_check_parent_access",
                "PermissionDenied", "AuthenticationError", 'extra="forbid"', "max_length", "min_length", "FIELD_NOT_ALLOWED", "decode_access_token", "verify_password",
                "is_revoked", "fingerprint", "allowed_role_ids", "escape_like", "sortable", "algorithms=", "audience=", "max_body"]
FORBIDDEN_NEW = [
    (re.compile(r"except\s*:"), "bare 'except:'"),
    (re.compile(r"except\s+(?:Base)?Exception\s*(?:as\s+\w+)?\s*:\s*(?:pass|\.\.\.|return None)\b"), "swallowing exceptions"),
    (re.compile(r"allow_origins\s*=\s*\[\s*[\"']\*"), "wildcard CORS"),
    (re.compile(r"verify_signature['\"]?\s*[:=]\s*False"), "disabled token verification"),
    (re.compile(r"\.execute\(\s*f[\"']"), "f-string SQL"),
    (re.compile(r"\.execute\(\s*[\"'][^\"']*[\"']\s*%\s*[\(\w]"), "%-formatted SQL"),
    (re.compile(r"\.execute\(\s*[\"'][^\"']*[\"']\s*\.format\("), ".format() SQL"),
    (re.compile(r"\b(?:eval|exec)\s*\("), "eval/exec"),
    (re.compile(r"subprocess|os\.system"), "process execution"),
    (re.compile(r"pytest\.(?:skip|xfail)|@pytest\.mark\.(?:skip|xfail)"), "skipping tests"),
]


@dataclass
class Edit:
    path: str
    search: str
    replace: str

    @classmethod
    def from_dict(cls, d: dict) -> "Edit":
        return cls(str(d.get("path", "")), str(d.get("search", "")), str(d.get("replace", "")))


@dataclass
class EditResult:
    edit: Edit
    applied: bool
    reason: str = ""


def _resolve(root: Path, rel: str) -> tuple[Path | None, str]:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts or p.parts[0] != EDITABLE_ROOT:
        return None, f"path must be inside {EDITABLE_ROOT}/"
    if rel in PROTECTED or p.suffix != ".py":
        return None, "only generated Python modules may be edited (spec.json, tests and the database contract are protected)"
    target = (root / p).resolve()
    if root.resolve() not in target.parents:
        return None, "path escapes the project"
    if not target.is_file():
        return None, "file does not exist"
    return target, ""


def check_edit(root: Path, edit: Edit) -> tuple[Path | None, str]:
    target, why = _resolve(root, edit.path)
    if target is None:
        return None, why
    text = target.read_text(encoding="utf-8")
    if not edit.search:
        return None, "empty search string"
    if text.count(edit.search) != 1:
        return None, f"search string occurs {text.count(edit.search)} times (must be exactly once)"
    for tok in GUARD_TOKENS:
        if edit.replace.count(tok) < edit.search.count(tok):
            return None, f"edit would remove or weaken a protection ('{tok}')"
    for pat, label in FORBIDDEN_NEW:
        if pat.search(edit.replace) and not pat.search(edit.search):
            return None, f"edit introduces {label}"
    return target, ""


def apply_edits(root: Path, edits: list[Edit]) -> list[EditResult]:
    results: list[EditResult] = []
    for e in edits:
        target, why = check_edit(root, e)
        if target is None:
            results.append(EditResult(e, False, why))
            continue
        original = target.read_text(encoding="utf-8")
        updated = original.replace(e.search, e.replace, 1)
        try:
            compile(updated, str(target), "exec")
        except SyntaxError as err:
            results.append(EditResult(e, False, f"result does not compile: {err.msg} (line {err.lineno})"))
            continue
        target.write_text(updated, encoding="utf-8")
        results.append(EditResult(e, True))
    return results
