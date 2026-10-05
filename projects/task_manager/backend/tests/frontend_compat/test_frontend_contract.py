"""Frontend compatibility tests: the backend is exercised the way the Frontend Agent's GENERATED client calls it.

Source: the Frontend Agent's graphs (tests/data/frontend_contract.json) and the behaviour of its generated `client.ts`
(Bearer header, bare-array lists, 204 handling, error bodies read as {message, errors}, session = {token, user}).

Known, documented integration conflicts (tests/data/known_conflicts.json) are NOT hidden: where the Frontend sends something the
Graph does not define, these tests assert the *documented* behaviour (the backend refuses it with a clear 422/404) so that the
conflict stays visible and cannot silently change. Anything not documented must simply work.
"""
from __future__ import annotations

import re
import uuid

import pytest

from tests.support.data import CONFLICTS, FRONTEND, GRAPH, OPS, SPEC
from tests.support.world import assert_matches_schema

pytestmark = pytest.mark.integration
FE = FRONTEND
GRAPH_EP = {(e["method"], re.sub(r"\{\w+\}", "{}", e["path"])): e for e in GRAPH["api"]["endpoints"]}
ENDPOINTS = FE["endpoints"]
DOCUMENTED = set(CONFLICTS)


def _gep(fe):
    return GRAPH_EP.get((fe["method"], re.sub(r"\{\w+\}", "{}", fe["path"])))


def _id(fe):
    return fe["id"]


def _callers(world, fe):
    """Users of every role the Frontend says may call this endpoint (and that exist in the graph)."""
    keys = [k for k in fe["roles"] if k in SPEC["auth"]["role_map"]] if fe["auth"] else [None]
    return [world.user(k) if k else None for k in (keys or [None])]


def _frontend_body(world, fe, op, caller, *, include_unknown: bool):
    """Mimic the generated TS client: `compact({...})` of the form fields (+ session-derived fields), omitting empty values."""
    gschema = SPEC["schemas"][op["endpoint"]["request_schema"]] if op["endpoint"]["request_schema"] else {"fields": []}
    known = {f["name"] for f in gschema["fields"]}
    body = {}
    sent_unknown = []
    for f in fe["fields"]:
        if f["source"] not in ("form", "input", "session.user_id", "session.role"):
            continue
        if f["source"] == "session.user_id":
            body[f["name"]] = str(caller["id"])
            continue
        if f["source"] == "session.role":
            body[f["name"]] = caller["role"]
            continue
        if f["name"] in known:
            gf = next(x for x in gschema["fields"] if x["name"] == f["name"])
            body[f["name"]] = world.payload_value(op["entity"], gf, caller, op) if op["entity"] else "x"
            if op["kind"] == "transition":
                body[f["name"]] = world.pick_transition(op, caller)[1]
        elif include_unknown:
            body[f["name"]] = str(world.anon_user["id"]) if f["type"] in ("ref", "uuid") else ("2030-01-01" if f["type"] == "date" else "text")
            sent_unknown.append(f["name"])
    return body, sent_unknown


def _build(world, fe, caller, *, include_unknown=True):
    gep = _gep(fe)
    op = OPS[gep["operation_ref"]]
    p = world.prepare(op["id"], caller)
    body, unknown = _frontend_body(world, fe, op, caller, include_unknown=include_unknown)
    if fe["response_shape"] == "session":
        body = {"email": caller["email"], "password": caller["password"]}
    return op, p, (body if (fe["fields"] and any(f["source"] in ("form", "input", "session.user_id") for f in fe["fields"])) else None), unknown


def test_the_client_expectations_are_known():
    ex = FE["expectations"]
    assert ex["list_shape"].startswith("bare JSON array") and ex["no_content_status"] == 204 and ex["session_shape"].keys() == {"token", "user"}


@pytest.mark.parametrize("fe", ENDPOINTS, ids=_id)
def test_every_frontend_endpoint_is_exposed_or_documented_as_a_conflict(world, fe):
    gep = _gep(fe)
    if gep is not None:
        assert fe["method"] == gep["method"]
        return
    assert "ENDPOINT_NOT_IN_GRAPH" in DOCUMENTED, f"{fe['method']} {fe['path']} is expected by the frontend, absent from the graph, and NOT documented as a conflict"
    user = world.user(sorted(SPEC["auth"]["role_map"])[0])
    r = world.client.request(fe["method"], fe["path"].replace("{id}", str(uuid.uuid4())), headers=world.headers(user))
    assert r.status_code in (404, 405) and r.json()["error"]["code"] in ("NOT_FOUND", "METHOD_NOT_ALLOWED"), "a missing endpoint must fail cleanly, never with a 500"
    assert r.json()["message"]


@pytest.mark.parametrize("fe", [e for e in ENDPOINTS if _gep(e)], ids=_id)
def test_frontend_style_requests_succeed_for_the_roles_the_frontend_allows(world, fe):
    for caller in _callers(world, fe):
        if caller is None and fe["response_shape"] != "session":
            op = OPS[_gep(fe)["operation_ref"]]
        if fe["response_shape"] == "session":
            caller = world.user(sorted(SPEC["auth"]["role_map"])[0])
        gep = _gep(fe)
        op = OPS[gep["operation_ref"]]
        if op["access"] == "restricted" and caller and caller["role"] not in [SPEC["roles"][r]["key"] for r in op["roles"]]:
            continue  # the frontend/graph role mismatch is a documented conflict (ROLES_DIFFER); the role-matrix tests cover 403
        op, p, body, unknown = _build(world, fe, caller, include_unknown=False)
        headers = {"Authorization": f"Bearer {world.token(caller)}"} if fe["auth"] and caller else {}
        r = world.client.request(fe["method"], p.path, json=body, headers=headers)
        assert r.status_code == op["endpoint"]["status_code"], f"{fe['id']} as {caller and caller['role']}: {r.status_code} {r.text}"
        _check_shape(world, fe, op, r)


def _check_shape(world, fe, op, r):
    shape = fe["response_shape"]
    if shape == "none":
        assert r.status_code == 204 and r.content == b""
    elif shape == "list":
        assert isinstance(r.json(), list), "the frontend client expects a bare JSON array"
    elif shape == "single":
        assert isinstance(r.json(), dict)
        assert_matches_schema(r.json(), SPEC["schemas"][op["endpoint"]["response_schema"]])
    elif shape == "session":
        body = r.json()
        assert isinstance(body["token"], str) and body["token"]
        assert isinstance(body["user"], dict) and body["user"]["id"] and body["user"]["role"] in SPEC["auth"]["role_map"]


@pytest.mark.parametrize("fe", [e for e in ENDPOINTS if _gep(e) and e["method"] in ("POST", "PATCH", "PUT")], ids=_id)
def test_fields_the_graph_does_not_define_are_refused_as_documented(world, fe):
    gep = _gep(fe)
    op = OPS[gep["operation_ref"]]
    callers = [c for c in _callers(world, fe) if c and (op["access"] != "restricted" or c["role"] in [SPEC["roles"][r]["key"] for r in op["roles"]])]
    if not callers or fe["response_shape"] == "session":
        return
    caller = callers[0]
    op, p, body, unknown = _build(world, fe, caller, include_unknown=True)
    if not unknown:
        return
    assert "FRONTEND_FIELD_NOT_IN_GRAPH" in DOCUMENTED, f"{fe['id']} sends {unknown} which the graph does not define, and the conflict is not documented"
    before = world.dump()
    r = world.client.request(fe["method"], p.path, json=body, headers={"Authorization": f"Bearer {world.token(caller)}"})
    assert r.status_code == 422, f"{fe['id']}: documented behaviour is 422, got {r.status_code} {r.text}"
    assert set(unknown) <= set(r.json()["errors"]), "the field errors must name the unknown fields so the frontend can show them"
    assert world.dump() == before
    r = world.client.request(fe["method"], p.path, json=_build(world, fe, caller, include_unknown=False)[2], headers={"Authorization": f"Bearer {world.token(caller)}"})
    assert r.status_code == op["endpoint"]["status_code"], "without the undefined fields the same request must work"


@pytest.mark.parametrize("fe", [e for e in ENDPOINTS if _gep(e) and e["auth"]], ids=_id)
def test_error_bodies_are_readable_by_the_frontend_client(world, fe):
    """client.ts: reads body.message (<=200 chars, single line, no markup) and body.errors ({field: string|string[]}); maps statuses."""
    ok_statuses = set(FE["expectations"]["handled_statuses"])
    gep = _gep(fe)
    op = OPS[gep["operation_ref"]]
    path = fe["path"].replace("{id}", str(uuid.uuid4()))
    responses = [world.client.request(fe["method"], path)]  # 401
    wrong_role = [k for k in SPEC["auth"]["role_map"] if op["access"] == "restricted" and f"role.{k}" not in op["roles"]]
    if wrong_role:
        responses.append(world.client.request(fe["method"], path, headers=world.headers(world.user(wrong_role[0]))))  # 403
    allowed = [k for k in SPEC["auth"]["role_map"] if op["access"] != "restricted" or f"role.{k}" in op["roles"]]
    if allowed and "{id}" in fe["path"]:
        responses.append(world.client.request(fe["method"], path, json={} if fe["method"] != "GET" else None, headers=world.headers(world.user(allowed[0]))))  # 404/422
    if allowed and op["endpoint"]["request_schema"]:
        responses.append(world.client.request(fe["method"], fe["path"].replace("{id}", str(uuid.uuid4())), json={"unexpected": 1}, headers=world.headers(world.user(allowed[0]))))  # 422
    for r in responses:
        assert r.status_code in ok_statuses, f"status {r.status_code} is not handled by the frontend client"
        body = r.json()
        msg = body["message"]
        assert isinstance(msg, str) and 0 < len(msg) <= 200 and "\n" not in msg and "<" not in msg and not re.search(r"\bat\s.+\(", msg)
        if "errors" in body:
            assert isinstance(body["errors"], dict) and all(isinstance(k, str) and isinstance(v, (str, list)) for k, v in body["errors"].items())
        if r.status_code in (400, 422) and "errors" in body:
            assert body["errors"]


def test_enum_values_match_or_the_conflict_is_documented(world):
    for fid, ent in FE["entities"].items():
        for f in ent.get("fields", []):
            if f.get("type") != "enum" or fid not in {e["id"] for e in GRAPH["entities"]}:
                continue
            g = next((a for e in GRAPH["entities"] if e["id"] == fid for a in e["attributes"] if a["name"] == f["name"]), None)
            if not g:
                continue
            if set(f["values"]) == set(g["enum_values"]):
                continue
            assert {"ENUM_CASE_DIFFERS", "ENUM_VALUES_DIFFER"} & DOCUMENTED, f"{fid}.{f['name']}: frontend {f['values']} vs graph {g['enum_values']} is not documented"
            # documented behaviour: the backend refuses values it does not define, it never guesses
            for op in [o for o in OPS.values() if o["kind"] == "transition" and o["entity"] == fid]:
                caller = world.user(next(k for k in SPEC["auth"]["role_map"] if f"role.{k}" in op["roles"]))
                p = world.prepare(op["id"], caller)
                r = world.send(p, json={f["name"]: f["values"][0]})
                assert r.status_code == 422, "an undefined enum value must be refused with 422"


def test_the_frontend_dev_origin_is_allowed_by_cors(world):
    origin = FE["expectations"]["dev_origin"]
    r = world.client.options("/health", headers={"Origin": origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization,content-type"})
    assert r.headers.get("access-control-allow-origin") == origin


def test_the_dev_proxy_prefix_is_not_part_of_backend_paths(world):
    """The Frontend dev proxy strips /api before forwarding: the backend serves graph paths as-is."""
    assert FE["expectations"]["base_path"] == "/api"
    assert world.client.get("/api/health").status_code == 404
    assert world.client.get("/health").status_code == 200


@pytest.mark.parametrize("fe", [e for e in ENDPOINTS if _gep(e) and any(f["source"] == "query" for f in e["fields"])], ids=_id)
def test_query_parameters_the_graph_does_not_define_are_ignored_as_documented(world, fe):
    """The client sends e.g. ?project_id=...; the graph defines no such filter, so the backend ignores it (documented conflict) and never fails."""
    gep = _gep(fe)
    op = OPS[gep["operation_ref"]]
    undefined = [f["name"] for f in fe["fields"] if f["source"] == "query" and f["name"] not in {q["name"] for q in op["endpoint"]["query_params"]}]
    if not undefined:
        return
    assert "FRONTEND_QUERY_NOT_IN_GRAPH" in DOCUMENTED, f"{fe['id']} sends {undefined}, which the graph does not define, and the conflict is not documented"
    caller = next(c for c in _callers(world, fe) if c)
    p = world.prepare(op["id"], caller)
    plain = world.client.get(p.path, headers=world.headers(caller))
    filtered = world.client.get(p.path, params={undefined[0]: str(uuid.uuid4())}, headers=world.headers(caller))
    assert plain.status_code == filtered.status_code == 200
    assert len(filtered.json()) == len(plain.json()), "documented behaviour: the undefined filter is ignored, not applied and not an error"
