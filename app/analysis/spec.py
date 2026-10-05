"""BackendSpec: the resolved, deterministic description of the backend to generate.

Everything in it is derived from (graph, database contract, rules, handlers). It is what the generator renders and what
the generated runtime interprets, so there is exactly one place where "what the backend does" is decided.
Sorted keys and no randomness: identical inputs give a byte-identical spec.
"""
from __future__ import annotations

import re
from typing import Any

from app.analysis.classifier import OpClass
from app.analysis.typesys import parse_field_type
from app.contracts.database import DatabaseContract
from app.graph.model import GraphPackage, entity_key, find_auth_entity, snake_to_pascal

PACKAGE = "backend_app"
SPEC_VERSION = "1"


def _schema_class(schema_id: str, is_request: bool) -> str:
    parts = schema_id.split(".")[1:]
    return "".join(snake_to_pascal(p) for p in parts) + ("Request" if is_request else "Response")


def status_code_for(op: dict[str, Any], cls: OpClass) -> int:
    if op["output"]["cardinality"] == "none":
        return 204
    if cls.kind == "create" or (cls.kind == "custom" and op["action"] == "create"):
        return 201
    return 200


def build_entity_mapping(pkg: GraphPackage, db: DatabaseContract) -> dict[str, Any]:
    """Graph entity -> backend domain entity -> database table (the entity_mapping.json artifact)."""
    out = []
    for eid, e in sorted(pkg.entities.items()):
        key = entity_key(eid)
        t = db.table_for_entity(key)
        out.append({
            "graph_entity": eid, "domain_model": snake_to_pascal(key), "database_table": t.name if t else None,
            "primary_key": t.primary_key if t else None,
            "columns": {a["name"]: (a["name"] if t and a["name"] in t.columns else None) for a in e["attributes"]},
            "crud_functions": sorted(f.name for f in db.functions_for(t.name)) if t else [],
        })
    return {"entities": out}


def build_spec(pkg: GraphPackage, db: DatabaseContract, classes: dict[str, OpClass], rules: list[dict[str, Any]],
               handlers: dict[str, dict[str, Any]] | None = None, notes: list[str] | None = None,
               coverage: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    handlers = handlers or {}
    auth = find_auth_entity(pkg)
    spec: dict[str, Any] = {
        "spec_version": SPEC_VERSION,
        "project": {"id": pkg.project_id, "key": pkg.project_key, "name": pkg.project.get("name", pkg.project_key), "description": pkg.project.get("description", ""),
                    "package": PACKAGE},
        "api": {"versioning": "none", "base_path": "", "note": "paths are served exactly as in api.json; the Frontend dev proxy strips its /api prefix"},
        "roles": {rid: {"key": pkg.role_key(rid), "inherits": r.get("inherits", [])} for rid, r in sorted(pkg.roles.items())},
        "auth": None, "entities": {}, "state_machines": {}, "schemas": {}, "operations": {}, "rules": sorted(rules, key=lambda r: r["id"]),
        "handlers": handlers, "notes": notes or [], "coverage": sorted(coverage or [], key=lambda c: c["source"]),
    }

    # ---- entities --------------------------------------------------------------------------------------------------
    for eid, e in sorted(pkg.entities.items()):
        key = entity_key(eid)
        table = db.table_for_entity(key)
        assert table is not None, f"database contract has no table for {eid} (conflict detection should have blocked this)"
        cols = []
        for a in e["attributes"]:
            col = table.columns[a["name"]]
            cols.append({"attr": a["name"], "column": col.name, "type": a["type"], "pg_type": col.type, "required": bool(a["required"]),
                         "nullable": col.nullable, "has_default": col.has_default, "unique": bool(a.get("unique")),
                         "enum": list(a["enum_values"]) if a["type"] == "enum" else None, "ref": a.get("reference_to")})
        sm = next((s["id"] for s in pkg.state_machines.values() if s["entity_ref"] == eid), None)
        resp = pkg.schemas.get(f"schema.{key}")
        id_col = table.columns["id"]
        spec["entities"][eid] = {
            "id": eid, "key": key, "class_name": snake_to_pascal(key), "table": table.name, "id_has_default": id_col.has_default,
            "columns": cols, "state_machine": sm,
            "text_columns": [c["attr"] for c in cols if c["type"] in ("string", "text") and c["attr"] not in ("password_hash",)],
            "hidden_columns": ["password_hash"] if "password_hash" in table.columns else [],
            "response_fields": [f["name"] for f in resp["fields"]] if resp else [c["attr"] for c in cols if c["attr"] != "password_hash"],
            "unique_columns": [list(u) for u in table.unique_constraints],
            "foreign_keys": [{"columns": f.columns, "ref_table": f.ref_table, "on_delete": f.on_delete} for f in table.foreign_keys],
        }

    # ---- state machines --------------------------------------------------------------------------------------------
    for sid, sm in sorted(pkg.state_machines.items()):
        spec["state_machines"][sid] = {
            "id": sid, "entity": sm["entity_ref"], "field": sm["field"], "states": list(sm["states"]), "initial": sm["initial_state"],
            "transitions": [{"from": t["from"], "to": t["to"], "roles": sorted(t.get("role_refs", [])), "operation": t.get("operation_ref")} for t in sm["transitions"]],
        }

    # ---- schemas ---------------------------------------------------------------------------------------------------
    req_ids = {ep["request_schema_ref"] for ep in pkg.endpoints.values() if ep.get("request_schema_ref")}
    for sid, s in sorted(pkg.schemas.items()):
        is_req = sid in req_ids
        ent = pkg.entities.get(s.get("entity_ref") or "")
        fields = []
        for f in s["fields"]:
            ft = parse_field_type(f["type"])
            attr = next((a for a in ent["attributes"] if a["name"] == f["name"]), None) if ent else None
            fields.append({"name": f["name"], "type": str(ft), "base": ft.base, "required": bool(f["required"]), "non_empty": ft.non_empty,
                           "enum": list(attr["enum_values"]) if (ft.base == "enum" and attr) else None, "ref": (attr or {}).get("reference_to")})
        spec["schemas"][sid] = {"id": sid, "name": s["name"], "entity": s.get("entity_ref"), "kind": "request" if is_req else "response",
                                "class_name": _schema_class(sid, is_req), "fields": fields}

    # ---- auth ------------------------------------------------------------------------------------------------------
    if auth:
        ak = entity_key(auth["id"])
        ops = {c.kind: oid for oid, c in classes.items() if c.kind.startswith("auth.")}
        spec["auth"] = {
            "entity": auth["id"], "table": spec["entities"][auth["id"]]["table"], "identity_field": "email", "password_field": "password_hash",
            "role_field": "role", "role_map": {pkg.role_key(r): r for r in pkg.roles}, "operations": ops,
            "login_response_schema": pkg.endpoint_by_operation[ops["auth.login"]].get("response_schema_ref") if "auth.login" in ops else None,
        }

    # ---- operations ------------------------------------------------------------------------------------------------
    for oid, op in sorted(pkg.operations.items()):
        cls = classes[oid]
        ep = pkg.endpoint_by_operation[oid]
        path_params = re.findall(r"\{(\w+)\}", ep["path"])
        ent = spec["entities"].get(op["entity_ref"]) if op.get("entity_ref") else None
        qparams = []
        if ep["method"] == "GET" and ent:
            for name in op["input"]["optional_fields"]:
                col = next(c for c in ent["columns"] if c["attr"] == name)
                qparams.append({"name": name, "type": col["type"], "enum": col["enum"], "kind": "filter"})
            if "query" in op["input"]["extra_fields"]:
                qparams.append({"name": "query", "type": "string", "enum": None, "kind": "search"})
        nf = next((e["code"] for e in op["errors"] if e["code"].endswith("_NOT_FOUND")), f"{entity_key(op['entity_ref']).upper()}_NOT_FOUND" if op.get("entity_ref") else "NOT_FOUND")
        spec["operations"][oid] = {
            "id": oid, "name": op["name"], "description": op["description"], "kind": cls.kind, "handler": cls.handler, "entity": op.get("entity_ref"),
            "service": op["service_ref"], "action": op["action"], "type": op["type"], "access": op["access"], "roles": sorted(op["required_roles"]),
            "cardinality": op["output"]["cardinality"], "input": op["input"], "params": cls.params, "notes": cls.notes,
            "errors": op["errors"], "not_found_code": nf, "permissions": sorted(pid for pid, perm in pkg.permissions.items() if oid in perm.get("operation_refs", [])),
            "endpoint": {"id": ep["id"], "method": ep["method"], "path": ep["path"], "status_code": status_code_for(op, cls),
                         "request_schema": ep.get("request_schema_ref"), "response_schema": ep.get("response_schema_ref"),
                         "authenticated": bool(ep["authorization"]["authenticated"]), "roles": sorted(ep["authorization"]["roles"]),
                         "path_params": path_params, "query_params": qparams},
        }
    return spec
