"""backend_analysis.json and backend_architecture.json: deterministic, derived only from the graphs, contracts and rules."""
from __future__ import annotations

from typing import Any

from app.analysis.classifier import OpClass
from app.analysis.rules import RuleSet
from app.graph.model import GraphPackage, entity_key, find_auth_entity, snake_to_pascal
from app.integration.conflicts import IntegrationReport


def build_analysis(pkg: GraphPackage, classes: dict[str, OpClass], rules: RuleSet, report: IntegrationReport) -> dict[str, Any]:
    ops = pkg.operations
    ep = pkg.endpoint_by_operation
    return {
        "project": {"id": pkg.project_id, "name": pkg.project.get("name"), "graph_version": pkg.manifest.get("graph_version")},
        "entities": sorted(pkg.entities),
        "relationships": [{"id": r["id"], "source": r["source"], "target": r["target"], "type": r["type"], "cardinality": r["cardinality"]} for _, r in sorted(pkg.relationships.items())],
        "operations": sorted(ops),
        "commands": sorted(o for o, v in ops.items() if v["type"] == "command"),
        "queries": sorted(o for o, v in ops.items() if v["type"] == "query"),
        "operation_classification": {o: {"kind": c.kind, "handler": c.handler, "notes": c.notes} for o, c in sorted(classes.items())},
        "services": [{"id": s["id"], "entities": s.get("entity_refs", []), "operations": sorted(o for o, v in ops.items() if v["service_ref"] == s["id"])} for _, s in sorted(pkg.services.items())],
        "api_refs": sorted(pkg.endpoints),
        "api_endpoints": [{"id": e["id"], "method": e["method"], "path": e["path"], "operation": e["operation_ref"], "authenticated": e["authorization"]["authenticated"],
                           "roles": sorted(e["authorization"]["roles"])} for _, e in sorted(pkg.endpoints.items())],
        "request_schemas": sorted({e["request_schema_ref"] for e in pkg.endpoints.values() if e.get("request_schema_ref")}),
        "response_schemas": sorted({e["response_schema_ref"] for e in pkg.endpoints.values() if e.get("response_schema_ref")}),
        "permissions": [{"id": p["id"], "resource": p["resource"], "action": p["action"], "roles": sorted(p["roles"]), "operations": sorted(p["operation_refs"]),
                        "conditions": p.get("conditions", [])} for _, p in sorted(pkg.permissions.items())],
        "validation_rules": [{"id": v["id"], "kind": v["kind"], "target": v["target"], "condition": v["condition"], "operations": v.get("operation_refs", [])} for _, v in sorted(pkg.validations.items())],
        "derived_rules": rules.to_dict(),
        "workflows": [{"id": w["id"], "operations": sorted({s["operation_ref"] for s in w.get("steps", []) if s.get("operation_ref")})} for _, w in sorted(pkg.workflows.items())],
        "state_machines": [{"id": s["id"], "entity": s["entity_ref"], "field": s["field"], "states": s["states"], "initial": s["initial_state"],
                            "transitions": [[t["from"], t["to"]] for t in s["transitions"]]} for _, s in sorted(pkg.state_machines.items())],
        "authentication": {"entity": (find_auth_entity(pkg) or {}).get("id"), "login": next((o for o in ops if o.endswith(".login")), None)},
        "external_integrations": pkg.project.get("integration_requirements", []),
        "dependencies": {"implementation_order": pkg.docs["dependencies"].get("implementation_order", [])},
        "integration_conflicts": {"status": report.status, "count": len(report.conflicts)},
    }


def build_architecture(spec: dict[str, Any]) -> dict[str, Any]:
    ents, ops = spec["entities"], spec["operations"]
    services: dict[str, list[str]] = {}
    for oid, o in ops.items():
        services.setdefault(o["service"], []).append(oid)
    return {
        "architecture": {
            "style": "clean architecture (api -> application -> domain/infrastructure)", "api_framework": "fastapi", "validation": "pydantic v2",
            "database": "postgresql", "database_driver": "psycopg 3 + psycopg_pool", "repository_pattern": True, "service_layer": True,
            "authentication": bool(spec["auth"]), "authorization": {"role_checks": "dependency per operation", "object_level": "engine rules (ownership, row scope, guards)"},
            "api_versioning": spec["api"]["versioning"], "api_versioning_note": spec["api"]["note"],
            "transactions": "explicit psycopg_pool transaction per operation; row locks (FOR UPDATE) before read-then-write",
            "error_contract": "{error:{code,message,details}, message, errors, request_id}",
        },
        "modules": [
            {"name": "backend_app.api", "purpose": "HTTP routes, request/response schemas and dependencies; no business logic."},
            {"name": "backend_app.application", "purpose": "Application services: one method per graph operation."},
            {"name": "backend_app.domain", "purpose": "Domain entities mirroring graph entities, and domain exceptions."},
            {"name": "backend_app.infrastructure", "purpose": "Repositories over the database contract."},
            {"name": "backend_app.engine", "purpose": "Spec-driven implementation of CRUD, state transitions and object-level rules."},
            {"name": "backend_app.auth", "purpose": "Authentication and role authorization."},
        ],
        "services": [{"name": f"{snake_to_pascal(s.split('.', 1)[1])}Service", "id": s, "operations": sorted(o)} for s, o in sorted(services.items())],
        "repositories": [{"name": f"{e['class_name']}Repository", "entity": eid, "table": e["table"]} for eid, e in sorted(ents.items())],
        "routes": [{"endpoint": o["endpoint"]["id"], "method": o["endpoint"]["method"], "path": o["endpoint"]["path"], "operation": oid} for oid, o in sorted(ops.items())],
    }
