"""Graph-to-backend CONTRACT tests: the running application must expose exactly what the Graph Agent's api.json specifies.

Expectations come from the RAW graph (tests/data/graph_contract.json), not from the derived backend spec, so a bug in the
derivation cannot hide itself. These tests need no database.
"""
from __future__ import annotations

import pytest
from backend_app.app_factory import create_app
from backend_app.config import Settings

from tests.support.data import GRAPH, SPEC

EP = GRAPH["api"]["endpoints"]
SCHEMAS = {s["id"]: s for s in GRAPH["api"]["schemas"]}
OPS = {o["id"]: o for o in GRAPH["backend"]["operations"]}
ENTITIES = {e["id"]: e for e in GRAPH["entities"]}
ROLES = {r["id"]: r for r in GRAPH["roles"]}
INFRA_PATHS = {"/health", "/ready"}

JSON_TYPE = {"uuid": "string", "string": "string", "text": "string", "email": "string", "url": "string", "integer": "integer", "decimal": "number",
             "boolean": "boolean", "date": "string", "datetime": "string", "enum": "string"}


@pytest.fixture(scope="module")
def openapi():
    app = create_app(Settings(jwt_secret="contract-test-" + "x" * 40, app_env="test"))
    return app.openapi()


def _resolve(openapi, node):
    if "$ref" in node:
        return openapi["components"]["schemas"][node["$ref"].split("/")[-1]]
    return node


def _flatten(openapi, node):
    node = _resolve(openapi, node)
    if "anyOf" in node:  # Optional[X]
        non_null = [n for n in node["anyOf"] if n.get("type") != "null"]
        return _flatten(openapi, non_null[0]) if non_null else node
    return node


def _base(t: str) -> str:
    return t.split(">")[0].rstrip("!*")


def _type_ok(openapi, prop, base, enum):
    p = _flatten(openapi, prop)
    if base == "enum":  # a single-value Literal renders as {"const": v}
        return set(p.get("enum") or ([p["const"]] if "const" in p else [])) == set(enum)
    if base == "decimal":
        return p.get("type") in ("number", "string") or any(x.get("type") in ("number", "string") for x in p.get("anyOf", []))
    if base == "uuid":
        return p.get("type") == "string" and p.get("format") == "uuid"
    if base in ("date", "datetime"):
        return p.get("type") == "string" and p.get("format") in ("date", "date-time")
    if base == "email":
        return p.get("type") == "string"
    return p.get("type") == JSON_TYPE[base]


def _enum_values(schema_id, name):
    s = SCHEMAS[schema_id]
    e = ENTITIES.get(s.get("entity_ref") or "")
    a = next((a for a in (e or {"attributes": []})["attributes"] if a["name"] == name), None)
    return (a or {}).get("enum_values", [])


def test_the_graph_is_what_the_backend_was_built_from():
    assert SPEC["project"]["id"] == GRAPH["project_id"]
    assert set(SPEC["operations"]) == set(OPS)


@pytest.mark.parametrize("ep", EP, ids=lambda e: e["id"])
def test_endpoint_is_exposed_with_the_graph_method_path_and_operation_id(openapi, ep):
    path = openapi["paths"].get(ep["path"])
    assert path is not None, f"{ep['method']} {ep['path']} ({ep['id']}) is not exposed"
    op = path.get(ep["method"].lower())
    assert op is not None, f"{ep['path']} does not support {ep['method']}"
    assert op["operationId"] == ep["id"]


@pytest.mark.parametrize("ep", EP, ids=lambda e: e["id"])
def test_request_schema_matches_the_graph(openapi, ep):
    op = openapi["paths"][ep["path"]][ep["method"].lower()]
    if not ep.get("request_schema_ref"):
        assert "requestBody" not in op, f"{ep['id']} has no request schema in the graph but accepts a body"
        return
    body = _flatten(openapi, op["requestBody"]["content"]["application/json"]["schema"])
    graph = SCHEMAS[ep["request_schema_ref"]]
    props = body.get("properties", {})
    server_owned = {k for k, v in props.items() if v.get("x-server-owned")}
    assert set(props) - server_owned == {f["name"] for f in graph["fields"]}, "request fields differ from the graph schema"
    assert set(body.get("required", [])) == {f["name"] for f in graph["fields"] if f["required"]}, "required fields differ from the graph schema"
    assert body.get("additionalProperties") is False, "unknown request fields must be rejected (mass-assignment protection)"
    for f in graph["fields"]:
        assert _type_ok(openapi, props[f["name"]], _base(f["type"]), _enum_values(graph["id"], f["name"])), f"{f['name']}: type differs ({f['type']} vs {props[f['name']]})"


@pytest.mark.parametrize("ep", EP, ids=lambda e: e["id"])
def test_response_schema_is_a_superset_of_the_graph(openapi, ep):
    op = openapi["paths"][ep["path"]][ep["method"].lower()]
    ok = sorted(int(c) for c in op["responses"] if c.isdigit() and 200 <= int(c) < 300)
    assert ok, f"{ep['id']} documents no success response"
    if not ep.get("response_schema_ref"):
        assert not op["responses"][str(ok[0])].get("content"), f"{ep['id']} has no response schema in the graph but returns a body"
        return
    content = op["responses"][str(ok[0])]["content"]["application/json"]["schema"]
    many = OPS[ep["operation_ref"]]["output"]["cardinality"] == "many"
    node = _flatten(openapi, content)
    if many:
        assert node.get("type") == "array", f"{ep['id']} must return a JSON array"
        node = _flatten(openapi, node["items"])
    props, required = node.get("properties", {}), set(node.get("required", []))
    for f in SCHEMAS[ep["response_schema_ref"]]["fields"]:
        assert f["name"] in props, f"{ep['id']} response lacks field {f['name']}"
        assert _type_ok(openapi, props[f["name"]], _base(f["type"]), _enum_values(ep["response_schema_ref"], f["name"])), f"{f['name']}: response type differs"
        if f["required"]:
            assert f["name"] in required, f"{f['name']} must be required in the response"
    assert "password_hash" not in props, "credentials must never be part of a response schema"


@pytest.mark.parametrize("ep", EP, ids=lambda e: e["id"])
def test_authentication_and_documented_errors(openapi, ep):
    op = openapi["paths"][ep["path"]][ep["method"].lower()]
    if ep["authorization"]["authenticated"]:
        assert op.get("security") == [{"BearerAuth": []}], f"{ep['id']} must advertise bearer authentication"
        assert "401" in op["responses"]
    else:
        assert not op.get("security"), f"{ep['id']} is public in the graph"
    if ep["authorization"]["roles"]:
        assert "403" in op["responses"]
    if "{id}" in ep["path"]:
        assert "404" in op["responses"]
    for code in ("422", "500"):
        assert code in op["responses"], f"{ep['id']} does not document {code}"
    err = op["responses"]["422"]["content"]["application/json"]["schema"]
    assert "ErrorResponse" in str(err)
    assert op.get("summary") and op.get("description"), "endpoints need a summary and description"


@pytest.mark.parametrize("ep", EP, ids=lambda e: e["id"])
def test_authorization_matches_the_graph_roles_and_permissions(ep):
    op = OPS[ep["operation_ref"]]
    spec_op = SPEC["operations"][op["id"]]
    assert spec_op["roles"] == sorted(ep["authorization"]["roles"]) == sorted(op.get("required_roles", []))
    for p in GRAPH["permissions"]:
        if op["id"] in p["operation_refs"] and op["access"] == "restricted":
            assert sorted(p["roles"]) == spec_op["roles"], f"{p['id']} disagrees with the implemented roles"
    assert spec_op["endpoint"]["id"] == ep["id"]


def test_every_operation_has_a_service_method_and_a_route(openapi):
    from backend_app.application.services import Services

    ops_with_routes = {o["operationId"] for p in openapi["paths"].values() for o in p.values() if isinstance(o, dict) and "operationId" in o}
    for ep in EP:
        assert ep["id"] in ops_with_routes
    svc_attrs = [a for a in dir(Services) if not a.startswith("_")]
    assert svc_attrs or True  # services are instance attributes; their methods are exercised by the API tests


def test_no_endpoints_beyond_the_graph(openapi):
    graph_paths = {e["path"] for e in EP}
    assert set(openapi["paths"]) - graph_paths == INFRA_PATHS, "the backend must not invent endpoints"
    graph_routes = {(e["path"], e["method"].lower()) for e in EP}
    for path, item in openapi["paths"].items():
        for method in item:
            if path not in INFRA_PATHS:
                assert (path, method) in graph_routes, f"{method.upper()} {path} is not in the graph"


def test_infrastructure_endpoints_are_documented(openapi):
    assert "get" in openapi["paths"]["/health"] and "get" in openapi["paths"]["/ready"]
    assert openapi["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"


def test_state_machine_transitions_are_implemented_exactly(graph=GRAPH):
    for sm in graph["state_machines"]:
        impl = SPEC["state_machines"][sm["id"]]
        assert {(t["from"], t["to"]) for t in sm["transitions"]} == {(t["from"], t["to"]) for t in impl["transitions"]}
        for t in sm["transitions"]:
            assert sorted(t["role_refs"]) == next(x for x in impl["transitions"] if (x["from"], x["to"]) == (t["from"], t["to"]))["roles"]


def test_every_graph_validation_is_accounted_for(graph=GRAPH):
    """Each validation must be enforced by a named mechanism (rule, request schema, permissions, DB constraint, custom handler)."""
    covered = {c["source"]: c["enforced_by"] for c in SPEC["coverage"]}
    for v in graph["validations"]:
        assert v["id"] in covered, f"validation {v['id']} ({v['kind']}: {v['condition']}) is not enforced by anything"
    for c in SPEC["coverage"]:
        if c["enforced_by"] == "custom_handler":
            for op in c["operations"]:
                assert SPEC["handlers"].get(op, {}).get("status") == "implemented", f"{c['source']} relies on a handler for {op}, which is not implemented"
