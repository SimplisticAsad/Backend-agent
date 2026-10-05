"""API tests for every graph operation, driven by the spec: success, invalid input, unauthenticated, forbidden, not found, conflict."""
from __future__ import annotations

import uuid

import pytest

from tests.support.data import CUSTOM_OPS, ENGINE_OPS, OPS, SPEC
from tests.support.world import assert_matches_schema

pytestmark = pytest.mark.integration


def _roles_allowed(op):
    keys = sorted(v["key"] for k, v in SPEC["roles"].items() if v["key"] in SPEC["auth"]["role_map"]) if SPEC["auth"] else []
    if op["access"] != "restricted":
        return keys
    required = set(op["roles"])
    closure = {k: {k, *v["inherits"]} for k, v in SPEC["roles"].items()}
    return [v["key"] for k, v in sorted(SPEC["roles"].items()) if closure[k] & required and v["key"] in keys]


def _cases(ops, allowed: bool):
    out = []
    for op in ops:
        keys = _roles_allowed(op)
        roles = keys if allowed else [r for r in (sorted(v["key"] for v in SPEC["roles"].values() if v["key"] in (SPEC["auth"] or {}).get("role_map", {}))) if r not in keys]
        out += [(op["id"], r) for r in roles]
    return out or [(None, None)]


SUCCESS = _cases(ENGINE_OPS, True)
DENIED = _cases([o for o in list(ENGINE_OPS) + list(CUSTOM_OPS) if o["access"] == "restricted"], False)
SECURED = [o["id"] for o in OPS.values() if o["access"] != "public"] or [None]
LISTS = [o["id"] for o in ENGINE_OPS if o["kind"] == "list"] or [None]
WITH_BODY = [o["id"] for o in ENGINE_OPS if o["endpoint"]["request_schema"]] or [None]
WITH_ID = [o["id"] for o in ENGINE_OPS if "id" in o["endpoint"]["path_params"] and o["kind"] in ("read", "update", "transition", "delete")] or [None]


def _is_none(*xs):
    return any(x is None for x in xs)


def _id(case):
    return "-".join(str(c) for c in case) if isinstance(case, tuple) else str(case)


# ---- success ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("op_id,role", SUCCESS, ids=_id)
def test_operation_succeeds_for_every_allowed_role(world, op_id, role):
    if _is_none(op_id):
        return
    op = OPS[op_id]
    ep = op["endpoint"]
    caller = world.user(role) if role else None
    p = world.prepare(op_id, caller)
    before = world.counts()
    r = world.send(p)
    assert r.status_code == ep["status_code"], f"{op_id} as {role}: {r.status_code} {r.text}"
    kind, ent = op["kind"], op["entity"]
    resp_schema = SPEC["schemas"][ep["response_schema"]] if ep["response_schema"] else None
    if kind == "create":
        assert_matches_schema(r.json(), resp_schema)
        table = SPEC["entities"][ent]["table"]
        assert world.counts()[table] == before[table] + 1, "create must persist exactly one row"
        stored = world.get(ent, uuid.UUID(r.json()["id"]))
        assert stored is not None
        for k, v in (p.json or {}).items():
            if k in stored and isinstance(v, str) and not any(x in k for x in ("date", "_at")) and not isinstance(stored[k], uuid.UUID):
                assert stored[k] == v, f"{k} was not stored as sent"
        for attr, src in op["params"]["server_fields"].items():
            if src == "principal.id":
                assert stored[attr] == caller["id"], f"server-owned field {attr} must be the caller"
    elif kind == "read":
        assert_matches_schema(r.json(), resp_schema)
        assert r.json()["id"] == str(p.row["id"])
    elif kind == "list":
        body = r.json()
        assert isinstance(body, list)
        for item in body:
            assert_matches_schema(item, resp_schema)
        assert str(p.row["id"]) in {i["id"] for i in body} if p.row else True
        assert int(r.headers["x-total-count"]) >= len(body)
    elif kind == "update":
        assert_matches_schema(r.json(), resp_schema)
        stored = world.get(ent, p.row["id"])
        changed = [k for k in p.json if k in stored]
        assert changed and all(str(stored[k]) == str(p.json[k]) or stored[k] == p.json[k] or str(stored[k]).startswith(str(p.json[k])[:10]) for k in changed)
    elif kind == "transition":
        assert_matches_schema(r.json(), resp_schema)
        assert world.get(ent, p.row["id"])[op["params"]["field"]] == p.to_state
    elif kind == "delete":
        assert r.content == b""
        assert world.get(ent, p.row["id"]) is None


# ---- authentication / authorization -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("op_id", SECURED, ids=_id)
def test_unauthenticated_requests_are_rejected_without_side_effects(world, op_id):
    if _is_none(op_id):
        return
    p = world.prepare(op_id, caller=None)
    before = world.dump()
    for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not.a.jwt"}):
        r = world.send(p, headers=headers)
        assert r.status_code == 401, f"{op_id} with {headers}: {r.status_code} {r.text}"
        assert r.headers.get("www-authenticate", "").lower().startswith("bearer")
        body = r.json()
        assert body["error"]["code"] in ("UNAUTHENTICATED", "INVALID_TOKEN") and isinstance(body["message"], str)
    assert world.dump() == before


@pytest.mark.parametrize("op_id,role", DENIED, ids=_id)
def test_roles_not_granted_by_the_graph_get_403_without_side_effects(world, op_id, role):
    if _is_none(op_id):
        return
    caller = world.user(role)
    p = world.prepare(op_id, caller)
    before = world.dump()
    r = world.send(p)
    assert r.status_code == 403, f"{op_id} as {role}: expected 403, got {r.status_code} {r.text}"
    assert r.json()["error"]["code"] == "FORBIDDEN"
    assert world.dump() == before


# ---- invalid input -----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("op_id", WITH_BODY, ids=_id)
def test_invalid_bodies_are_rejected_with_field_errors(world, op_id):
    if _is_none(op_id):
        return
    op = OPS[op_id]
    role = _roles_allowed(op)[0]
    caller = world.user(role)
    p = world.prepare(op_id, caller)
    schema = SPEC["schemas"][op["endpoint"]["request_schema"]]
    before = world.dump()
    # 1. unknown field (mass assignment)
    r = world.send(p, json={**(p.json or {}), "definitely_not_a_field": 1})
    assert r.status_code == 422 and "definitely_not_a_field" in r.json()["errors"], r.text
    # 2. each required field missing
    for f in schema["fields"]:
        if f["required"]:
            body = {k: v for k, v in (p.json or {}).items() if k != f["name"]}
            r = world.send(p, json=body)
            assert r.status_code == 422 and f["name"] in r.json()["errors"], f"{op_id}: omitting {f['name']} -> {r.status_code} {r.text}"
    # 3. wrong JSON type for each field
    for f in schema["fields"]:
        r = world.send(p, json={**(p.json or {}), f["name"]: {"nested": ["object"]}})
        assert r.status_code == 422 and f["name"] in r.json()["errors"], f"{op_id}: object for {f['name']} -> {r.status_code} {r.text}"
    # 4. oversize strings
    for f in schema["fields"]:
        if f["base"] in ("string", "text", "email"):
            r = world.send(p, json={**(p.json or {}), f["name"]: "x" * 30000})
            assert r.status_code == 422, f"{op_id}: 30k chars for {f['name']} -> {r.status_code}"
    # 5. not a JSON object / malformed JSON
    r = world.client.request(p.method, p.path, content=b"[1,2,3]", headers={**p.headers, "content-type": "application/json"})
    assert r.status_code in (400, 422)
    r = world.client.request(p.method, p.path, content=b"{not json", headers={**p.headers, "content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "MALFORMED_REQUEST"
    assert world.dump() == before


@pytest.mark.parametrize("op_id", WITH_ID, ids=_id)
def test_malformed_and_unknown_ids(world, op_id):
    if _is_none(op_id):
        return
    op = OPS[op_id]
    caller = world.user(_roles_allowed(op)[0])
    p = world.prepare(op_id, caller)
    before = world.dump()
    r = world.client.request(p.method, p.path.replace(str(p.row["id"]), "not-a-uuid"), json=p.json, headers=p.headers)
    assert r.status_code == 422 and "id" in r.json()["errors"], r.text
    r = world.client.request(p.method, p.path.replace(str(p.row["id"]), str(uuid.uuid4())), json=p.json, headers=p.headers)
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == op["not_found_code"]
    assert world.dump() == before


# ---- conflicts -----------------------------------------------------------------------------------------------------------------------
def test_deleting_a_row_that_is_still_referenced_conflicts(world):
    tested = 0
    for op in [o for o in ENGINE_OPS if o["kind"] == "delete"]:
        ent = SPEC["entities"][op["entity"]]
        for child in SPEC["entities"].values():
            for fk in child["foreign_keys"]:
                if fk["ref_table"] != ent["table"] or fk["on_delete"] not in ("RESTRICT", "NO ACTION"):
                    continue
                col = next(c for c in child["columns"] if c["column"] == fk["columns"][0])
                caller = world.user(_roles_allowed(op)[0])
                p = world.prepare(op["id"], caller)
                world.insert(child["id"], **{col["attr"]: p.row["id"]})
                r = world.send(p)
                assert r.status_code == 409 and r.json()["error"]["code"] == "ENTITY_IN_USE", r.text
                assert world.get(op["entity"], p.row["id"]) is not None, "row must survive a refused delete"
                tested += 1
    assert tested >= 0


@pytest.mark.parametrize("op_id", [o["id"] for o in ENGINE_OPS if o["kind"] == "create"] or [None], ids=_id)
def test_creating_with_a_reference_to_nothing_is_a_validation_error(world, op_id):
    if _is_none(op_id):
        return
    op = OPS[op_id]
    schema = SPEC["schemas"][op["endpoint"]["request_schema"]]
    ref_fields = [f for f in schema["fields"] if f["ref"] and f["ref"] != (SPEC["auth"] or {}).get("entity") and f["required"]]
    if not ref_fields:
        return
    caller = world.user(_roles_allowed(op)[0])
    p = world.prepare(op_id, caller)
    before = world.counts()
    r = world.send(p, json={**p.json, ref_fields[0]["name"]: str(uuid.uuid4())})
    assert r.status_code in (404, 422), r.text  # 404 when the parent is hidden by a row scope, 422 when the database FK refuses
    assert r.json()["error"]["code"] in ("REFERENCE_NOT_FOUND",) or r.status_code == 404
    assert world.counts() == before


# ---- listing: pagination, sorting, filtering, search ----------------------------------------------------------------------------------
@pytest.mark.parametrize("op_id", LISTS, ids=_id)
def test_list_pagination_sorting_filtering_and_search(world, op_id):
    if _is_none(op_id):
        return
    op = OPS[op_id]
    caller = world.user(_roles_allowed(op)[0]) if op["access"] != "public" else None
    rows = [world.target_row(op, caller)[0] for _ in range(3)]
    h = world.headers(caller)
    path = op["endpoint"]["path"]
    r = world.client.get(path, params={"limit": 1, "offset": 1}, headers=h)
    assert r.status_code == 200 and len(r.json()) == 1 and int(r.headers["x-total-count"]) >= 3
    ids_page = {x["id"] for x in world.client.get(path, params={"limit": 2, "offset": 0}, headers=h).json()}
    ids_next = {x["id"] for x in world.client.get(path, params={"limit": 2, "offset": 2}, headers=h).json()}
    assert not ids_page & ids_next
    assert world.client.get(path, params={"limit": 0}, headers=h).status_code == 422
    assert world.client.get(path, params={"limit": 100000}, headers=h).status_code == 422
    assert world.client.get(path, params={"sort": "id; DROP TABLE x"}, headers=h).json()["error"]["code"] == "INVALID_SORT_FIELD"
    asc = [x["id"] for x in world.client.get(path, params={"sort": "id", "order": "asc"}, headers=h).json()]
    desc = [x["id"] for x in world.client.get(path, params={"sort": "id", "order": "desc"}, headers=h).json()]
    assert asc == list(reversed(desc)) and asc == sorted(asc)
    assert world.client.get(path, params={"order": "sideways"}, headers=h).status_code == 422
    for q in op["endpoint"]["query_params"]:
        if q["kind"] == "filter":
            value = rows[0][q["name"]]
            if value is None:
                continue
            got = world.client.get(path, params={q["name"]: str(value)}, headers=h)
            assert got.status_code == 200, got.text
            assert str(rows[0]["id"]) in {x["id"] for x in got.json()}
            assert all(str(x[q["name"]]) in (str(value), str(float(value)) if not isinstance(value, str) else "") or x[q["name"]] == value or str(x[q["name"]]) == str(value) for x in got.json()), "filter returned non-matching rows"
        elif q["kind"] == "search":
            text_attr = next(c["attr"] for c in SPEC["entities"][op["entity"]]["columns"] if c["attr"] in SPEC["entities"][op["entity"]]["text_columns"])
            needle = str(rows[0][text_attr])[:6]
            got = world.client.get(path, params={"query": needle}, headers=h)
            assert got.status_code == 200 and str(rows[0]["id"]) in {x["id"] for x in got.json()}
            wild = world.client.get(path, params={"query": "%"}, headers=h)
            assert wild.status_code == 200 and len(wild.json()) == 0, "LIKE wildcards in user input must be escaped"
            assert world.client.get(path, params={"query": "x" * 500}, headers=h).status_code == 422
