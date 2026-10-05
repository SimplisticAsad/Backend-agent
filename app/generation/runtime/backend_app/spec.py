"""Loads the generated BackendSpec (generated/spec.json) and exposes read-only helpers used by the runtime."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SPEC_PATH = Path(__file__).with_name("generated") / "spec.json"


@lru_cache(maxsize=1)
def load_spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


class SpecView:
    """Derived lookups over the spec (role closure, rule index). Built once per application."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.spec = spec
        self.roles: dict[str, dict[str, Any]] = spec["roles"]
        self.entities: dict[str, dict[str, Any]] = spec["entities"]
        self.operations: dict[str, dict[str, Any]] = spec["operations"]
        self.rules: list[dict[str, Any]] = spec["rules"]
        self.state_machines: dict[str, dict[str, Any]] = spec["state_machines"]
        self._closure: dict[str, frozenset[str]] = {r: self._expand(r) for r in self.roles}
        self.role_by_key = {v["key"]: k for k, v in self.roles.items()}

    def _expand(self, role_id: str) -> frozenset[str]:
        out, stack = set(), [role_id]
        while stack:
            r = stack.pop()
            if r not in out:
                out.add(r)
                stack.extend(self.roles.get(r, {}).get("inherits", []))
        return frozenset(out)

    def role_ids_held(self, role_id: str) -> frozenset[str]:
        """The role itself plus all roles it inherits."""
        return self._closure.get(role_id, frozenset({role_id}))

    def allowed_role_ids(self, op_id: str) -> frozenset[str]:
        """Roles that may call the operation: any role that holds one of the required roles."""
        required = set(self.operations[op_id]["roles"])
        return frozenset(r for r in self.roles if self._closure[r] & required)

    def rules_for(self, op_id: str, rtype: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.rules if op_id in r["operations"] and (rtype is None or r["type"] == rtype)]
