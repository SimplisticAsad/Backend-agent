"""Business-rule derivation: free-text graph rules -> a small, closed rule DSL the generated backend can enforce.

The baseline is DETERMINISTIC (patterns below). The LLM `business_rules` stage may *add* rules for conditions the
patterns could not map, but it can never remove or weaken a baseline rule (see `merge_rules`).

Rule types (all carry `id`, `source`, `origin`, `operations`):
  ownership         caller must be the row's `field` unless their role is in `exempt_roles`  (IDOR protection)
  row_scope         roles in `roles` only see/act on rows whose `scope` resolves to the caller. A scope node is either
                    {"field": "<user-ref column>"} or {"fk": col, "parent_entity": id, "parent_scope": <node>} (parent chain)
  transition_guard  moving `field` to `to` requires `require_fields_set` to be non-null
  frozen_state      rows whose `field` is in `states` cannot be modified by `operations`
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.analysis.classifier import OWNER_NAMES, OpClass, server_owner_fields, user_ref_attrs
from app.graph.model import GraphPackage, entity_key, find_auth_entity

SCOPE_OWNER_NAMES = ("user_id", "requester_id", "customer_id")
OWNER_NAMES = ("owner_id", "user_id", "requester_id", "customer_id", "author_id", "created_by")
MUTATING = {"update", "transition", "delete"}
ROW_KINDS = {"read", "list", "update", "transition", "delete", "aggregate"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z_]+", text.lower()))


def _stem_match(word: str, stem: str) -> bool:
    return word == stem or word.rstrip("s") == stem or word == stem + "s"


def _owner_field_from_text(pkg: GraphPackage, entity_id: str, cond: str) -> str | None:
    """'caller is the assignee or has role manager' -> 'assignee_id'."""
    words = _words(cond)
    for a in user_ref_attrs(pkg, entity_id):
        stem = a["name"][:-3] if a["name"].endswith("_id") else a["name"]
        if any(_stem_match(w, stem) for w in words):
            return a["name"]
    if re.search(r"assigned to", cond, re.I):
        for a in user_ref_attrs(pkg, entity_id):
            if "assign" in a["name"]:
                return a["name"]
    return None


def _roles_named(pkg: GraphPackage, text: str) -> set[str]:
    words = _words(text)
    return {rid for rid in pkg.roles if any(_stem_match(w, pkg.role_key(rid)) for w in words)}


class RuleSet:
    def __init__(self) -> None:
        self.rules: list[dict[str, Any]] = []
        self.coverage: list[dict[str, Any]] = []  # per validation/permission-condition: how it is enforced
        self.unmapped: list[dict[str, Any]] = []
        self.assumptions: list[str] = []

    def add(self, rule: dict[str, Any]) -> None:
        for r in self.rules:
            same_id = r["id"] == rule["id"]
            same_ownership = (rule["type"] == "ownership" and r["type"] == "ownership" and r["entity"] == rule["entity"]
                              and r["field"] == rule["field"] and set(r["operations"]) == set(rule["operations"]))
            if same_id or same_ownership:
                return
        self.rules.append(rule)

    def to_dict(self) -> dict[str, Any]:
        return {"rules": sorted(self.rules, key=lambda r: r["id"]), "coverage": sorted(self.coverage, key=lambda c: c["source"]),
                "unmapped": sorted(self.unmapped, key=lambda u: u["source"]), "assumptions": self.assumptions}


def derive_rules(pkg: GraphPackage, classes: dict[str, OpClass]) -> RuleSet:
    rs = RuleSet()
    auth = find_auth_entity(pkg)

    def op_roles(op_id: str) -> set[str]:
        return set(pkg.operations[op_id].get("required_roles", []))

    # ---- 1. validations ----------------------------------------------------------------------------------------
    for val in pkg.validations.values():
        ent, field, cond, kind = val["target"]["entity_ref"], val["target"].get("field"), val["condition"], val["kind"]
        ops = list(val.get("operation_refs", []))
        fail = val.get("failure", {})
        code, msg = fail.get("error_code") or val["id"].split(".", 1)[1].upper().replace(".", "_"), fail.get("message", val["name"])
        src = val["id"]
        handled = False
        if kind == "ownership":
            f = _owner_field_from_text(pkg, ent, cond)
            if f:
                exempt = sorted(_roles_named(pkg, re.sub(r"caller is the \w+", "", cond)) & {r for o in ops for r in op_roles(o)})
                rs.add({"id": f"rule.ownership.{src.split('.', 1)[1]}", "type": "ownership", "entity": ent, "operations": ops, "field": f,
                        "exempt_roles": exempt, "error_code": code, "message": msg, "status": 403, "source": src, "origin": "validation"})
                rs.coverage.append({"source": src, "enforced_by": "rule:ownership", "operations": ops})
                handled = True
        elif kind == "state":
            m = re.search(r"move to (?P<to>\w+) when (?P<f>\w+) is set", cond, re.I)
            if m:
                rs.add({"id": f"rule.guard.{src.split('.', 1)[1]}", "type": "transition_guard", "entity": ent, "operations": ops, "field": _state_field(pkg, ent) or field,
                        "to": m["to"].lower(), "require_fields_set": [m["f"]], "error_code": code, "message": msg, "status": 409, "source": src, "origin": "validation"})
                rs.coverage.append({"source": src, "enforced_by": "rule:transition_guard", "operations": ops})
                handled = True
            else:
                m = re.search(r"status (?P<s>\w+) cannot be (?:edited|modified|changed|updated)", cond, re.I)
                sf = _state_field(pkg, ent)
                if m and sf:
                    rs.add({"id": f"rule.frozen.{src.split('.', 1)[1]}", "type": "frozen_state", "entity": ent, "operations": ops, "field": sf, "states": [m["s"].lower()],
                            "error_code": code, "message": msg, "status": 409, "source": src, "origin": "validation"})
                    rs.coverage.append({"source": src, "enforced_by": "rule:frozen_state", "operations": ops})
                    handled = True
        elif kind in ("required", "format"):
            rs.coverage.append({"source": src, "enforced_by": "request_schema", "operations": ops})
            handled = True
        elif kind == "authorization":
            if re.search(r"has role|credentials|match an existing account", cond, re.I) or all(classes[o].kind.startswith("auth.") or pkg.operations[o]["access"] == "restricted" for o in ops):
                rs.coverage.append({"source": src, "enforced_by": "permissions" if ops and pkg.operations[ops[0]]["access"] == "restricted" else "authentication", "operations": ops})
                handled = True
        elif kind == "uniqueness":
            rs.coverage.append({"source": src, "enforced_by": "database_constraint", "operations": ops})
            handled = True
        if not handled and ops and all(classes[o].kind in ("custom", "aggregate") for o in ops):
            rs.coverage.append({"source": src, "enforced_by": "custom_handler", "operations": ops})
            handled = True
        if not handled:
            rs.unmapped.append({"source": src, "kind": kind, "condition": cond, "entity": ent, "operations": ops,
                                "reason": "no deterministic pattern; the LLM business_rules stage may map it, otherwise it is NOT enforced beyond schema/permissions"})

    # ---- 2. permission conditions ------------------------------------------------------------------------------
    for perm in pkg.permissions.values():
        for i, cond in enumerate(perm.get("conditions", [])):
            ent, ops = perm["resource"], list(perm.get("operation_refs", []))
            f = _owner_field_from_text(pkg, ent, cond)
            src = f"{perm['id']}#{i}"
            if f and re.search(r"\bassigned to\b", cond, re.I):
                if re.search(r"\bmay only\b|\bcan only\b", cond, re.I):
                    named = _roles_named(pkg, cond.split("may only")[0].split("can only")[0])
                    exempt = sorted({r for o in ops for r in op_roles(o)} - named) if named else []
                    rs.add({"id": f"rule.ownership.{perm['id'].split('.', 1)[1]}", "type": "ownership", "entity": ent, "operations": ops, "field": f, "exempt_roles": exempt,
                            "error_code": _owner_error_code(pkg, ops, ent), "message": "You can only act on rows assigned to you.", "status": 403, "source": src, "origin": "permission_condition"})
                    rs.coverage.append({"source": src, "enforced_by": "rule:ownership", "operations": ops})
                else:
                    rs.add({"id": f"rule.scope.{perm['id'].split('.', 1)[1]}", "type": "row_scope", "entity": ent, "operations": ops, "roles": sorted(perm["roles"]),
                            "scope": {"field": f}, "origin": "permission_condition", "source": src})
                    rs.coverage.append({"source": src, "enforced_by": "rule:row_scope", "operations": ops})
            else:
                rs.unmapped.append({"source": src, "kind": "permission_condition", "condition": cond, "entity": ent, "operations": ops,
                                    "reason": "permission condition text not understood by the deterministic patterns"})

    # ---- 3. implicit data-subject scoping (secure default) -----------------------------------------------------------
    if auth:
        _implicit_scopes(pkg, classes, rs, auth)
    return rs


def _owner_error_code(pkg: GraphPackage, ops: list[str], ent: str) -> str:
    for v in pkg.validations.values():
        if v["kind"] == "ownership" and v["target"]["entity_ref"] == ent and set(ops) & set(v.get("operation_refs", [])):
            return v["failure"].get("error_code") or "NOT_OWNER"
    return f"{entity_key(ent).upper()}_NOT_OWNED_BY_CALLER"


def _state_field(pkg: GraphPackage, entity_id: str) -> str | None:
    sm = next((s for s in pkg.state_machines.values() if s["entity_ref"] == entity_id), None)
    if sm:
        return sm["field"]
    enums = [a for a in pkg.entities[entity_id]["attributes"] if a["type"] == "enum" and a["name"] in ("status", "state")]
    return enums[0]["name"] if enums else None


def _implicit_scopes(pkg: GraphPackage, classes: dict[str, OpClass], rs: RuleSet, auth: dict[str, Any]) -> None:
    explicit = {(r["entity"], o) for r in rs.rules if r["type"] in ("row_scope", "ownership") for o in r["operations"]}
    scopes: dict[tuple[str, str], dict[str, Any]] = {}  # (entity, role) -> {"field":..} | {"via":..}
    for r in rs.rules:
        if r["type"] == "row_scope":
            for role in r["roles"]:
                scopes[(r["entity"], role)] = r["scope"]

    def ops_of(entity_id: str, kinds: set[str]) -> list[dict[str, Any]]:
        return [o for o in pkg.operations.values() if o["entity_ref"] == entity_id and classes[o["id"]].kind.split(".")[0] in kinds]

    # pass 1: direct data-subject scope on entities whose owner field is server-assigned on create
    for ent_id in sorted(pkg.entities):
        if ent_id == auth["id"]:
            continue
        owner = next((f for f in server_owner_fields(pkg, ent_id) if f in SCOPE_OWNER_NAMES), None)
        if not owner:
            continue
        creators = set()
        for o in ops_of(ent_id, {"create", "custom"}):
            if o["action"] == "create":
                creators |= pkg.roles_granting(o["required_roles"]) if o["access"] == "restricted" else set()
        roles = sorted(creators)
        ops = [o["id"] for o in ops_of(ent_id, ROW_KINDS) if (ent_id, o["id"]) not in explicit and set(roles) & pkg.roles_granting(o["required_roles"])]
        if roles and ops:
            rs.add({"id": f"rule.scope.implicit.{entity_key(ent_id)}.{owner}", "type": "row_scope", "entity": ent_id, "operations": ops, "roles": roles,
                    "scope": {"field": owner}, "origin": "implicit_data_subject", "source": f"heuristic:{ent_id}.{owner} is assigned from the caller on create"})
            for r in roles:
                scopes[(ent_id, r)] = {"field": owner}
            rs.assumptions.append(f"{ent_id}: rows are private to the user in '{owner}' for roles {sorted(r.split('.', 1)[1] for r in roles)} "
                                  "(the graph does not state this; inferred because the field is server-assigned on create)")
    # pass 2: children of a scoped parent inherit the parent's scope for the same roles
    for ent_id in sorted(pkg.entities):
        if ent_id == auth["id"]:
            continue
        for a in pkg.entities[ent_id]["attributes"]:
            parent = a.get("reference_to")
            if not parent or parent == auth["id"] or parent == ent_id:
                continue
            roles = sorted(r for (pe, r) in scopes if pe == parent and (ent_id, r) not in scopes)
            if not roles:
                continue
            ops = [o["id"] for o in ops_of(ent_id, ROW_KINDS | {"create"}) if (ent_id, o["id"]) not in explicit and set(roles) & pkg.roles_granting(o["required_roles"])]
            if not ops:
                continue
            groups: dict[str, list[str]] = {}
            for r in roles:
                node = {"fk": a["name"], "parent_entity": parent, "parent_scope": scopes[(parent, r)]}
                groups.setdefault(json.dumps(node, sort_keys=True), []).append(r)
            for n, (raw, grp) in enumerate(sorted(groups.items())):
                node = json.loads(raw)
                gops = [o for o in ops if set(grp) & pkg.roles_granting(pkg.operations[o]["required_roles"])]
                rs.add({"id": f"rule.scope.implicit.{entity_key(ent_id)}.via_{a['name']}" + (f".{n}" if len(groups) > 1 else ""), "type": "row_scope", "entity": ent_id,
                        "operations": gops, "roles": sorted(grp), "scope": node, "origin": "implicit_parent_scope",
                        "source": f"heuristic:{ent_id} belongs to {parent} which is private to its owner"})
                for r in grp:
                    scopes[(ent_id, r)] = node
                rs.assumptions.append(f"{ent_id}: rows inherit the privacy of their parent {parent} (via {a['name']}) for roles {sorted(x.split('.', 1)[1] for x in grp)}")


def merge_rules(baseline: RuleSet, llm_rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Merge LLM-proposed rules: additive only. Returns (accepted, rejection reasons).

    Rejected: id collides with a baseline rule (the LLM may not redefine it), unknown rule type, or malformed shape.
    Baseline rules are never removed or altered, so authorization/validation can only get stricter.
    """
    allowed = {"ownership": {"entity", "operations", "field", "exempt_roles", "error_code", "message"},
               "row_scope": {"entity", "operations", "roles", "scope"},
               "transition_guard": {"entity", "operations", "field", "to", "require_fields_set", "error_code", "message"},
               "frozen_state": {"entity", "operations", "field", "states", "error_code", "message"}}
    base_ids = {r["id"] for r in baseline.rules}
    accepted, rejected = [], []
    for r in llm_rules:
        rid, t = r.get("id"), r.get("type")
        if not rid or t not in allowed:
            rejected.append(f"{rid!r}: unknown or missing rule type {t!r}")
        elif rid in base_ids:
            rejected.append(f"{rid}: collides with a baseline rule (LLM may not redefine rules)")
        elif not allowed[t] <= set(r):
            rejected.append(f"{rid}: missing keys {sorted(allowed[t] - set(r))}")
        else:
            accepted.append({**r, "origin": "llm", "status": r.get("status", 409 if t != "ownership" else 403), "source": r.get("source", "llm:business_rules")})
    return accepted, rejected
