"""Graph field-type grammar and its mapping to Python/Pydantic types.

Entity attributes use plain types (`uuid`, `string`, `enum`, ...). API schema fields use the same names plus optional
suffixes: `!` = must be present/non-empty, `*` = unique, `>entity` = reference to an entity (`uuid!>user`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TYPE_RE = re.compile(r"^(?P<base>[a-z]+)(?P<flags>[!*]*)(?:>(?P<ref>[a-z0-9_]+))?$")

BASE_TYPES = {"uuid", "string", "text", "integer", "decimal", "boolean", "date", "datetime", "enum", "json", "email", "url"}

# generated-code type expressions
PY_TYPES = {
    "uuid": "UUID", "string": "str", "text": "str", "email": "str", "url": "str", "integer": "int", "decimal": "Decimal",
    "boolean": "bool", "date": "date", "datetime": "datetime", "json": "Any",
}

MAX_STRING = 255
MAX_TEXT = 20_000
MAX_URL = 2048
MAX_EMAIL = 254
MAX_PASSWORD = 128
MIN_NEW_PASSWORD = 8


@dataclass(frozen=True)
class FieldType:
    base: str
    non_empty: bool = False
    unique: bool = False
    ref: str | None = None

    def __str__(self) -> str:
        return f"{self.base}{'!' if self.non_empty else ''}{'*' if self.unique else ''}{('>' + self.ref) if self.ref else ''}"


def parse_field_type(raw: str) -> FieldType:
    m = _TYPE_RE.match(raw.strip())
    if not m or m.group("base") not in BASE_TYPES:
        raise ValueError(f"unsupported graph field type {raw!r}")
    flags = m.group("flags")
    return FieldType(m.group("base"), "!" in flags, "*" in flags, m.group("ref"))


def pydantic_annotation(ft: FieldType, enum_values: list[str] | None = None, *, request: bool, name: str = "") -> tuple[str, list[str]]:
    """Return (type expression, Field(...) keyword args) for generated Pydantic models."""
    kw: list[str] = []
    base = ft.base
    if base == "enum":
        vals = enum_values or []
        return "Literal[" + ", ".join(repr(v) for v in vals) + "]", kw
    t = PY_TYPES[base]
    if request:
        if base in ("string", "text"):
            kw.append(f"max_length={MAX_STRING if base == 'string' else MAX_TEXT}")
            if ft.non_empty or base == "string":
                kw.append("min_length=1")
        elif base == "email":
            t = "EmailStr"
            kw.append(f"max_length={MAX_EMAIL}")
        elif base == "url":
            kw += [f"max_length={MAX_URL}", "pattern=r'^https?://'"]
        elif base == "decimal":
            kw += ["max_digits=12", "decimal_places=2"]
        elif base == "integer":
            kw += ["ge=-2147483648", "le=2147483647"]
    return t, kw
