"""Operation classification: which generic behaviour (or custom handler) implements each graph operation.

Kinds
  auth.login | auth.logout | auth.request_password_reset | auth.reset_password
  create | read | list | update | transition | delete      -> implemented by the generic, spec-driven engine
  aggregate | custom                                        -> need a handler (LLM `api_implementation` stage); otherwise 501

The classification is a pure function of the graph (+ optionally which columns have database defaults); the LLM never
decides it, so identical graphs always produce identical backends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.graph.model import GraphPackage, find_auth_entity

AUTH_SUFFIXES = {"login", "logout", "request_password_reset", "reset_password"}
AGGREGATE_SUFFIXES = {"progress", "stats", "summary", "report"}
OWNER_NAMES = ("owner_id", "user_id", "requester_id", "customer_id", "author_id", "created_by")


def user_ref_attrs(pkg: GraphPackage, entity_id: str) -> list[dict[str, Any]]:
    auth = find_auth_entity(pkg)
    if not auth:
        return []
    return [a for a in pkg.entities[entity_id]["attributes"] if a.get("reference_to") == auth["id"]]


def server_owner_fields(pkg: GraphPackage, entity_id: str) -> list[str]:
    """User-referencing owner-like attributes that no create operation of the entity accepts from the client."""
    creates = [o for o in pkg.operations.values() if o["entity_ref"] == entity_id and o["action"] == "create"]
    out = []
    for a in user_ref_attrs(pkg, entity_id):
        if a["name"] in OWNER_NAMES and creates and all(a["name"] not in (o["input"]["required_fields"] + o["input"]["optional_fields"]) for o in creates):
            out.append(a["name"])
    return out


@dataclass
class OpClass:
    op_id: str
    kind: str
    entity: str | None
    handler: str = "engine"  # engine | auth | custom
    params: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"op_id": self.op_id, "kind": self.kind, "entity": self.entity, "handler": self.handler, "params": self.params, "notes": self.notes}


def _singular(w: str) -> str:
    return w[:-3] + "y" if w.endswith("ies") else (w[:-1] if w.endswith("s") else w)


def classify_operation(pkg: GraphPackage, op: dict[str, Any], db_defaults: set[tuple[str, str]] | None = None) -> OpClass:
    db_defaults = db_defaults or set()
    suffix = op["id"].rsplit(".", 1)[1]
    auth = find_auth_entity(pkg)
    eid = op.get("entity_ref")
    ent = pkg.entities.get(eid or "")
    action = op["action"]
    inp = op["input"]
    provided = set(inp["required_fields"]) | set(inp["optional_fields"]) | set(inp["extra_fields"])

    if auth and eid == auth["id"] and suffix in AUTH_SUFFIXES:
        return OpClass(op["id"], f"auth.{suffix}", eid, "auth")
    if ent is None:
        return OpClass(op["id"], "custom", eid, "custom", notes=["operation has no entity"])

    sm = next((s for s in pkg.state_machines.values() if s["entity_ref"] == eid), None)
    if action == "create":
        server: dict[str, str] = {}
        unmet: list[str] = []
        owners = set(server_owner_fields(pkg, eid))
        for a in ent["attributes"]:
            n = a["name"]
            if n == "id" or n in inp["required_fields"] or n in inp["optional_fields"]:
                continue
            if a.get("reference_to") == (auth or {}).get("id") and n in OWNER_NAMES:
                server[n] = "principal.id"
            elif a["type"] == "enum":
                server[n] = "initial_state" if sm and sm["field"] == n else ("db_default" if (eid, n) in db_defaults else "first_enum")
            elif a["type"] == "datetime" and (n.endswith("_at") or (eid, n) in db_defaults):
                server[n] = "db_default_or_now"
            elif (eid, n) in db_defaults or not a["required"]:
                continue
            else:
                unmet.append(n)
        if unmet or inp["extra_fields"]:
            notes = []
            if unmet:
                notes.append(f"required attributes with no source: {unmet}")
            if inp["extra_fields"]:
                notes.append(f"takes non-attribute inputs {inp['extra_fields']}")
            return OpClass(op["id"], "custom", eid, "custom", {"unmet": unmet}, notes)
        return OpClass(op["id"], "create", eid, params={"server_fields": server, "owner_fields": sorted(owners)})
    if action == "read":
        return OpClass(op["id"], "read", eid)
    if action == "delete":
        return OpClass(op["id"], "delete", eid)
    if action == "list":
        if suffix in AGGREGATE_SUFFIXES:
            return OpClass(op["id"], "aggregate", eid, "custom", notes=["aggregate: the response schema is the plain entity list"])
        params: dict[str, Any] = {"filters": list(inp["optional_fields"]), "search": "query" in inp["extra_fields"]}
        if suffix.startswith("list_"):
            word = _singular(suffix[5:])
            for a in ent["attributes"]:
                if a["type"] == "enum" and word in a["enum_values"]:
                    params["implicit_filter"] = {"field": a["name"], "value": word}
        unknown_extra = [x for x in inp["extra_fields"] if x != "query"]
        if unknown_extra:
            return OpClass(op["id"], "custom", eid, "custom", notes=[f"list takes unrecognised inputs {unknown_extra}"])
        return OpClass(op["id"], "list", eid, params=params)
    if action == "update":
        if sm and (set(inp["required_fields"]) | set(inp["optional_fields"])) - {"id"} == {sm["field"]}:
            return OpClass(op["id"], "transition", eid, params={"state_machine": sm["id"], "field": sm["field"]})
        if inp["extra_fields"]:
            return OpClass(op["id"], "custom", eid, "custom", notes=[f"update takes unrecognised inputs {inp['extra_fields']}"])
        return OpClass(op["id"], "update", eid)
    return OpClass(op["id"], "custom", eid, "custom", notes=[f"action '{action}' has no generic implementation"])


def classify_all(pkg: GraphPackage, db_defaults: set[tuple[str, str]] | None = None) -> dict[str, OpClass]:
    return {oid: classify_operation(pkg, op, db_defaults) for oid, op in sorted(pkg.operations.items())}
