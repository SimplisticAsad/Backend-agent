"""In-memory view of a Graph-Agent package (the shape written by Graph-making-agent: `<graph>.json` files with an
envelope {schema_version, project_version, project_id, <list>} plus `graph_manifest.json`).

The backend never mutates graphs. `GraphPackage` only builds indices so every later stage can resolve IDs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

# graph name -> key that holds its list of objects
LIST_KEYS: dict[str, str] = {
    "actors": "actors", "roles": "roles", "entities": "entities", "relationships": "relationships",
    "capabilities": "capabilities", "requirements": "requirements", "screens": "screens",
    "components": "components", "workflows": "workflows", "api": "endpoints", "permissions": "permissions",
    "validations": "validations", "state_machines": "state_machines", "dependencies": "edges",
    "acceptance_criteria": "acceptance_criteria", "assumptions": "assumptions", "questions": "questions",
}

# Graphs the backend cannot work without (superset of the manifest's `downstream_contract.backend`).
BACKEND_REQUIRED = [
    "project", "actors", "roles", "entities", "relationships", "capabilities", "requirements", "workflows",
    "backend", "api", "permissions", "validations", "state_machines", "dependencies", "acceptance_criteria",
    "assumptions", "questions",
]


def snake_to_pascal(s: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[_\s]+", s) if p)


def entity_key(entity_id: str) -> str:
    """entity.project_member -> project_member"""
    return entity_id.split(".", 1)[1]


@dataclass
class GraphPackage:
    root: str
    manifest: dict[str, Any]
    docs: dict[str, dict[str, Any]]
    validation_report: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    # ---- generic access -------------------------------------------------------------------------
    def items(self, graph: str) -> list[dict[str, Any]]:
        doc = self.docs.get(graph, {})
        if graph == "backend":
            return []
        return list(doc.get(LIST_KEYS.get(graph, graph), []))

    @property
    def project_id(self) -> str:
        return self.manifest["project_id"]

    @property
    def project_key(self) -> str:
        return self.project_id.split(".", 1)[1]

    @property
    def project(self) -> dict[str, Any]:
        return self.docs["project"]

    # ---- indices --------------------------------------------------------------------------------
    @cached_property
    def entities(self) -> dict[str, dict[str, Any]]:
        return {e["id"]: e for e in self.items("entities")}

    @cached_property
    def roles(self) -> dict[str, dict[str, Any]]:
        return {r["id"]: r for r in self.items("roles")}

    @cached_property
    def actors(self) -> dict[str, dict[str, Any]]:
        return {a["id"]: a for a in self.items("actors")}

    @cached_property
    def services(self) -> dict[str, dict[str, Any]]:
        return {s["id"]: s for s in self.docs["backend"].get("services", [])}

    @cached_property
    def operations(self) -> dict[str, dict[str, Any]]:
        return {o["id"]: o for o in self.docs["backend"].get("operations", [])}

    @cached_property
    def schemas(self) -> dict[str, dict[str, Any]]:
        return {s["id"]: s for s in self.docs["api"].get("schemas", [])}

    @cached_property
    def endpoints(self) -> dict[str, dict[str, Any]]:
        return {e["id"]: e for e in self.items("api")}

    @cached_property
    def endpoint_by_operation(self) -> dict[str, dict[str, Any]]:
        return {e["operation_ref"]: e for e in self.items("api")}

    @cached_property
    def permissions(self) -> dict[str, dict[str, Any]]:
        return {p["id"]: p for p in self.items("permissions")}

    @cached_property
    def validations(self) -> dict[str, dict[str, Any]]:
        return {v["id"]: v for v in self.items("validations")}

    @cached_property
    def state_machines(self) -> dict[str, dict[str, Any]]:
        return {s["id"]: s for s in self.items("state_machines")}

    @cached_property
    def workflows(self) -> dict[str, dict[str, Any]]:
        return {w["id"]: w for w in self.items("workflows")}

    @cached_property
    def acceptance_criteria(self) -> dict[str, dict[str, Any]]:
        return {a["id"]: a for a in self.items("acceptance_criteria")}

    @cached_property
    def relationships(self) -> dict[str, dict[str, Any]]:
        return {r["id"]: r for r in self.items("relationships")}

    def attribute(self, entity_id: str, name: str) -> dict[str, Any] | None:
        e = self.entities.get(entity_id)
        return next((a for a in e["attributes"] if a["name"] == name), None) if e else None

    @cached_property
    def all_ids(self) -> set[str]:
        ids: set[str] = {self.project_id}
        for g in self.docs:
            if g in ("backend", "project"):
                continue
            ids.update(i["id"] for i in self.items(g) if isinstance(i, dict) and "id" in i)
        ids.update(self.services)
        ids.update(self.operations)
        ids.update(self.schemas)
        for e in self.entities.values():
            ids.update(a["id"] for a in e.get("attributes", []))
        return ids

    def role_key(self, role_id: str) -> str:
        """role.manager -> 'manager' (the value stored in the user's `role` column)."""
        return role_id.split(".", 1)[1]

    def role_id_for_key(self, key: str) -> str | None:
        rid = f"role.{key}"
        return rid if rid in self.roles else None

    def expand_role(self, role_id: str) -> set[str]:
        """The role itself plus every role it inherits (transitively)."""
        out, stack = set(), [role_id]
        while stack:
            r = stack.pop()
            if r in out:
                continue
            out.add(r)
            stack.extend(self.roles.get(r, {}).get("inherits", []))
        return out

    def roles_granting(self, allowed: list[str]) -> set[str]:
        """Role IDs that may act where `allowed` roles are required (a role inheriting an allowed role also qualifies)."""
        return {r for r in self.roles if self.expand_role(r) & set(allowed)}


def find_auth_entity(pkg: "GraphPackage") -> dict[str, Any] | None:
    """The entity that holds credentials: has email + password_hash + a role enum. None if the product has no login."""
    for e in pkg.entities.values():
        names = {a["name"]: a for a in e.get("attributes", [])}
        role = names.get("role")
        if {"email", "password_hash"} <= set(names) and role and role.get("type") == "enum":
            return e
    return None
