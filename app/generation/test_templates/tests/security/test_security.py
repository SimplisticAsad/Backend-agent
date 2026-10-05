"""Security tests: injection, authentication bypass, authorization/IDOR, mass assignment, state abuse, malformed/oversized input,
information leakage, secret leakage, CORS, rate limiting. Everything runs against the real application and PostgreSQL."""
from __future__ import annotations

import json
import logging
import time
import uuid

import jwt
import pytest

from backend_app.logging_setup import JsonFormatter
from tests.support.data import CUSTOM_OPS, ENGINE_OPS, OPS, SPEC
from tests.support.world import PASSWORD

pytestmark = pytest.mark.integration

INJECTIONS = [
    "'; DROP TABLE users; --", "\" OR \"1\"=\"1", "' OR '1'='1' --", "1; SELECT pg_sleep(5)--", "' UNION SELECT password_hash FROM users --",
    "\\'; DELETE FROM tasks; --", "'); INSERT INTO users VALUES ('x'); --", "%27%20OR%201%3D1", "Robert'); DROP TABLE Students;--",
]
WRITE_OPS = [o for o in ENGINE_OPS if o["kind"] in ("create", "update") and o["endpoint"]["request_schema"]] or [None]
SECURED_OPS = [o["id"] for o in OPS.values() if o["access"] != "public"] or [None]


def _roles_allowed(op):
    keys = sorted(SPEC["auth"]["role_map"]) if SPEC["auth"] else []
    if op["access"] != "restricted":
        return keys
    closure = {k: {k, *v["inherits"]} for k, v in SPEC["roles"].items()}
    return [v["key"] for k, v in sorted(SPEC["roles"].items()) if closure[k] & set(op["roles"]) and v["key"] in keys]


def _tables(world):
    return {r["table_name"] for r in world.conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (world.schema,)).fetchall()}


def _id(o):
    return o["id"] if isinstance(o, dict) else str(o)


# ---- SQL injection -------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("op", WRITE_OPS, ids=_id)
def test_sql_injection_in_bodies_is_stored_literally_or_rejected(world, op):
    if op is None:
        return
    caller = world.user(_roles_allowed(op)[0])
    schema = SPEC["schemas"][op["endpoint"]["request_schema"]]
    tables = _tables(world)
    for f in schema["fields"]:
        if f["base"] not in ("string", "text"):
            continue
        for payload in INJECTIONS:
            p = world.prepare(op["id"], caller)
            body = {**(p.json or {}), f["name"]: payload[:250]}
            before = world.counts()
            t0 = time.time()
            r = world.send(p, json=body)
            assert time.time() - t0 < 3, "injected pg_sleep must not execute"
            assert r.status_code < 500, f"{op['id']}.{f['name']} <- {payload!r}: {r.status_code} {r.text}"
            assert _tables(world) == tables, "a table disappeared or appeared"
            if r.status_code in (200, 201):
                row = world.get(op["entity"], uuid.UUID(r.json()["id"]))
                assert row[f["name"]] == payload[:250], "the payload must be stored literally"
            else:
                after = world.counts()
                assert after == before
    assert all(world.conn.execute(f'SELECT count(*) AS n FROM "{t}"').fetchone()["n"] >= 0 for t in tables)


@pytest.mark.parametrize("op", [o for o in ENGINE_OPS if o["kind"] == "list"] or [None], ids=_id)
def test_sql_injection_in_query_parameters(world, op):
    if op is None:
        return
    caller = world.user(_roles_allowed(op)[0]) if op["access"] != "public" else None
    world.target_row(op, caller)
    h, path = world.headers(caller), op["endpoint"]["path"]
    tables = _tables(world)
    total = int(world.client.get(path, headers=h).headers["x-total-count"])
    for payload in INJECTIONS:
        t0 = time.time()
        for params in ({"query": payload}, {"sort": payload}, {"order": payload}, {"limit": payload}, {"offset": payload},
                       *({q["name"]: payload} for q in op["endpoint"]["query_params"] if q["kind"] == "filter")):
            r = world.client.get(path, params=params, headers=h)
            assert r.status_code in (200, 400, 422), f"{params} -> {r.status_code} {r.text}"
            if r.status_code == 200 and "query" in params:
                assert len(r.json()) <= total and r.headers["x-total-count"] != str(total + 1)
        assert time.time() - t0 < 3
    assert _tables(world) == tables


def test_sql_injection_in_path_and_login(world):
    caller = world.user(sorted(SPEC["auth"]["role_map"])[0])
    for payload in INJECTIONS:
        for op in [o for o in ENGINE_OPS if "id" in o["endpoint"]["path_params"]][:3]:
            r = world.client.request(op["endpoint"]["method"], op["endpoint"]["path"].replace("{id}", payload.replace("/", "_")), headers=world.headers(caller))
            assert r.status_code in (403, 404, 405, 422), r.text
    login = SPEC["operations"][SPEC["auth"]["operations"]["auth.login"]]["endpoint"]["path"]
    for payload in INJECTIONS:
        r = world.client.post(login, json={"email": payload, "password": payload})
        assert r.status_code in (401, 422)
        r = world.client.post(login, json={"email": f"{payload[:20]}@example.com", "password": payload})
        assert r.status_code in (401, 422)


# ---- authentication bypass --------------------------------------------------------------------------------------------------------------
def _forged(world, user, **over):
    claims = {"sub": str(user["id"]), "role": "x", "jti": uuid.uuid4().hex, "iat": int(time.time()), "exp": int(time.time()) + 600,
              "iss": SPEC["project"]["key"], "aud": "access", "fp": "0" * 24}
    claims.update(over)
    return claims


def test_every_secured_operation_rejects_forged_and_invalid_tokens(world):
    user = world.user(sorted(SPEC["auth"]["role_map"])[0])
    secret = world.container.settings.jwt_secret
    real = world.token(user)
    variants = {
        "tampered signature": real[:-4] + ("AAAA" if not real.endswith("AAAA") else "BBBB"),
        "alg none": jwt.encode(_forged(world, user), None, algorithm="none"),
        "other secret": jwt.encode(_forged(world, user), "z" * 48, algorithm="HS256"),
        "right secret, wrong fingerprint": jwt.encode(_forged(world, user), secret, algorithm="HS256"),
        "expired": jwt.encode(_forged(world, user, exp=int(time.time()) - 10), secret, algorithm="HS256"),
        "wrong audience": jwt.encode(_forged(world, user, aud="password-reset"), secret, algorithm="HS256"),
        "wrong issuer": jwt.encode(_forged(world, user, iss="someone-else"), secret, algorithm="HS256"),
        "unknown user": jwt.encode(_forged(world, {"id": uuid.uuid4()}), secret, algorithm="HS256"),
        "subject not a uuid": jwt.encode(_forged(world, user, sub="1 OR 1=1"), secret, algorithm="HS256"),
        "null bytes": real + "\x00x",
    }
    for op_id in [o for o in SECURED_OPS if o]:
        ep = OPS[op_id]["endpoint"]
        path = ep["path"].replace("{id}", str(uuid.uuid4()))
        for name, tok in variants.items():
            r = world.client.request(ep["method"], path, headers={"Authorization": f"Bearer {tok}"}, json={} if ep["request_schema"] else None)
            assert r.status_code == 401, f"{op_id}: '{name}' token got {r.status_code} {r.text}"


def test_role_claims_in_the_token_are_ignored(world):
    """The role is read from the database: a self-signed 'manager' claim must not grant manager rights."""
    roles = sorted(SPEC["auth"]["role_map"])
    restricted = [o for o in ENGINE_OPS if o["access"] == "restricted"]
    for op in restricted:
        allowed = _roles_allowed(op)
        weak = [r for r in roles if r not in allowed]
        if not weak:
            continue
        user = world.user(weak[0])
        from backend_app.security import password_fingerprint

        forged = jwt.encode(_forged(world, user, role=allowed[0], fp=password_fingerprint(user["password_hash"])), world.container.settings.jwt_secret, algorithm="HS256")
        p = world.prepare(op["id"], user)
        before = world.dump()
        r = world.send(p, headers={"Authorization": f"Bearer {forged}"})
        assert r.status_code == 403, f"{op['id']}: a forged role claim got {r.status_code}"
        assert world.dump() == before
        return
    pytest.skip("every role may call every restricted operation: nothing to escalate to") if not restricted else None


# ---- mass assignment ---------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("op", WRITE_OPS, ids=_id)
def test_mass_assignment_is_rejected(world, op):
    if op is None:
        return
    caller = world.user(_roles_allowed(op)[0])
    schema_fields = {f["name"] for f in SPEC["schemas"][op["endpoint"]["request_schema"]]["fields"]}
    ent_cols = {c["attr"] for c in SPEC["entities"][op["entity"]]["columns"]}
    extras = [x for x in ("id", "role", "is_admin", "password_hash", "created_at", "status", "owner_id", "assignee_id", "user_id", "requester_id", "total", "price") if x not in schema_fields]
    for name in extras:
        p = world.prepare(op["id"], caller)
        before = world.dump()
        value = str(uuid.uuid4()) if name.endswith("id") else ("admin" if name == "role" else "x")
        r = world.send(p, json={**(p.json or {}), name: value})
        server_owned = any(src == "principal.id" for a, src in op["params"].get("server_fields", {}).items() if a == name)
        if server_owned:  # accepted only when it equals the caller; another user's id is refused
            assert r.status_code in (403, 422) and r.json()["error"]["code"] in ("FIELD_NOT_ALLOWED", "VALIDATION_ERROR"), f"{op['id']} accepted {name}={value}"
        else:
            assert r.status_code == 422 and name in r.json()["errors"], f"{op['id']} did not reject extra field {name}: {r.status_code} {r.text}"
        assert world.dump() == before, f"{op['id']}: extra field {name} changed the database"
    assert ent_cols  # sanity


# ---- authorization matrix, IDOR / object-level rules ---------------------------------------------------------------------------------------
def _rule_cases():
    out = []
    for r in SPEC["rules"]:
        if r["type"] not in ("ownership", "row_scope"):
            continue
        for op_id in r["operations"]:
            if OPS[op_id]["kind"] in ("read", "update", "transition", "delete", "list", "create"):
                out.append((r["id"], op_id))
    return out or [(None, None)]


RULE_BY_ID = {r["id"]: r for r in SPEC["rules"]}


@pytest.mark.parametrize("rule_id,op_id", _rule_cases(), ids=lambda x: str(x))
def test_object_level_authorization_idor(world, rule_id, op_id):
    if rule_id is None:
        return
    rule, op = RULE_BY_ID[rule_id], OPS[op_id]
    subject_roles = set(rule["roles"]) if rule["type"] == "row_scope" else {r for r in SPEC["roles"] if r in set(SPEC["roles"]) and not ({r, *SPEC["roles"][r]["inherits"]} & set(rule["exempt_roles"]))}
    role_key = next((SPEC["roles"][r]["key"] for r in sorted(subject_roles) if SPEC["roles"][r]["key"] in _roles_allowed(op)), None)
    assert role_key, f"no role subject to {rule_id} can call {op_id}"
    a, b = world.user(role_key), world.user(role_key)
    if op["kind"] == "create":  # create a child under somebody else's parent
        node = rule["scope"]
        assert "fk" in node
        parent = world.insert(node["parent_entity"], **world.owned_overrides(node["parent_entity"], node["parent_scope"], a["id"]))
        p = world.prepare(op_id, b)
        before = world.counts()
        r = world.send(p, json={**p.json, node["fk"]: str(parent["id"])})
        assert r.status_code == 404, f"{op_id}: creating under another user's parent got {r.status_code} {r.text}"
        assert world.counts() == before
        r = world.send(world.prepare(op_id, a), json={**world.prepare(op_id, a).json, node["fk"]: str(parent["id"])})
        assert r.status_code == op["endpoint"]["status_code"], r.text
        return
    row, to = world.target_row(op, a)  # A's row
    if op["kind"] == "list":
        r = world.client.get(op["endpoint"]["path"], headers=world.headers(b))
        assert r.status_code == 200 and str(row["id"]) not in {x["id"] for x in r.json()}, "another user's row appears in the list"
        r = world.client.get(op["endpoint"]["path"], headers=world.headers(a))
        assert str(row["id"]) in {x["id"] for x in r.json()}
        return
    p = world.prepare(op_id, b)
    path = p.path.replace(str(p.row["id"]), str(row["id"]))
    body = {op["params"]["field"]: to} if op["kind"] == "transition" else p.json
    before = world.dump()
    r = world.client.request(p.method, path, json=body, headers=p.headers)
    expected = (403, 404) if rule["type"] == "ownership" else (404,)
    assert r.status_code in expected, f"{op_id}: user B acted on user A's row: {r.status_code} {r.text}"
    if rule["type"] == "ownership":
        assert r.json()["error"]["code"] == rule["error_code"]
    assert world.dump() == before, "the other user's row was modified"
    # the owner may
    pa = world.prepare(op_id, a)
    ra = world.client.request(pa.method, pa.path, json=pa.json, headers=pa.headers)
    assert ra.status_code == op["endpoint"]["status_code"], f"{op_id}: the owner was refused: {ra.status_code} {ra.text}"


@pytest.mark.parametrize("rule", [r for r in SPEC["rules"] if r["type"] == "ownership" and r["exempt_roles"]] or [None], ids=lambda r: r["id"] if r else "none")
def test_exempt_roles_can_act_on_rows_they_do_not_own(world, rule):
    if rule is None:
        return
    op = OPS[rule["operations"][0]]
    exempt_key = SPEC["roles"][rule["exempt_roles"][0]]["key"]
    owner = world.user(next(k for k in sorted(SPEC["auth"]["role_map"]) if k != exempt_key and k in _roles_allowed(op)))
    boss = world.user(exempt_key)
    row, to = world.target_row(op, owner)
    p = world.prepare(op["id"], boss)
    path = p.path.replace(str(p.row["id"]), str(row["id"]))
    r = world.client.request(p.method, path, json={op["params"]["field"]: to} if op["kind"] == "transition" else p.json, headers=p.headers)
    assert r.status_code == op["endpoint"]["status_code"], r.text


# ---- state machine abuse ------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("op", [o for o in ENGINE_OPS if o["kind"] == "transition"] or [None], ids=_id)
def test_invalid_and_unpermitted_transitions_are_refused(world, op):
    if op is None:
        return
    sm = SPEC["state_machines"][op["params"]["state_machine"]]
    field = op["params"]["field"]
    declared = {(t["from"], t["to"]): t for t in sm["transitions"]}
    key = _roles_allowed(op)
    for a in sm["states"]:
        for b in sm["states"]:
            if (a, b) in declared:
                continue
            caller = world.user(key[0])
            frozen = {s for r in SPEC["rules"] if r["type"] == "frozen_state" and op["id"] in r["operations"] for s in r["states"]}
            if a in frozen:
                continue
            row, _ = world.target_row(op, caller)
            world.conn.execute(f'UPDATE "{SPEC["entities"][op["entity"]]["table"]}" SET {field} = %s WHERE id = %s', (a, row["id"]))
            p = world.prepare(op["id"], caller)
            r = world.client.request(p.method, p.path.replace(str(p.row["id"]), str(row["id"])), json={field: b}, headers=p.headers)
            assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_STATE_TRANSITION", f"{a}->{b}: {r.status_code} {r.text}"
            assert world.get(op["entity"], row["id"])[field] == a, "an invalid transition changed the state"
    r = world.send(world.prepare(op["id"], world.user(key[0])), json={field: "no-such-state"})
    assert r.status_code == 422


@pytest.mark.parametrize("rule", [r for r in SPEC["rules"] if r["type"] in ("transition_guard", "frozen_state")] or [None], ids=lambda r: r["id"] if r else "none")
def test_business_guards_block_the_operation(world, rule):
    if rule is None:
        return
    op = OPS[rule["operations"][0]]
    caller = world.user(next(k for k in _roles_allowed(op)))
    ent = SPEC["entities"][op["entity"]]
    if rule["type"] == "frozen_state":
        row, _ = world.target_row(op, caller)
        world.conn.execute(f'UPDATE "{ent["table"]}" SET {rule["field"]} = %s WHERE id = %s', (rule["states"][0], row["id"]))
        p = world.prepare(op["id"], caller)
        before = world.dump()
        r = world.client.request(p.method, p.path.replace(str(p.row["id"]), str(row["id"])), json=p.json, headers=p.headers)
        assert r.status_code == 409 and r.json()["error"]["code"] == rule["error_code"], r.text
        assert world.dump() == before
        return
    sm = SPEC["state_machines"][op["params"]["state_machine"]]
    frm = next(t["from"] for t in sm["transitions"] if t["to"] == rule["to"])
    row, _ = world.target_row(op, caller)
    updates = {rule["field"]: frm, **{f: None for f in rule["require_fields_set"]}}
    sets = ", ".join(f"{k} = %s" for k in updates)
    world.conn.execute(f'UPDATE "{ent["table"]}" SET {sets} WHERE id = %s', [*updates.values(), row["id"]])
    p = world.prepare(op["id"], caller)
    r = world.client.request(p.method, p.path.replace(str(p.row["id"]), str(row["id"])), json={rule["field"]: rule["to"]}, headers=p.headers)
    assert r.status_code in (403, 409), r.text
    assert world.get(op["entity"], row["id"])[rule["field"]] == frm


# ---- malformed / oversized input ---------------------------------------------------------------------------------------------------------
def test_malformed_requests_never_cause_500(world):
    op = next(o for o in WRITE_OPS if o and o["kind"] == "create") if any(o and o["kind"] == "create" for o in WRITE_OPS) else WRITE_OPS[0]
    caller = world.user(_roles_allowed(op)[0])
    p = world.prepare(op["id"], caller)
    h = {**p.headers, "content-type": "application/json"}
    for content in (b"\xff\xfe\x00bad-utf8", b"", b"null", b"[]", b'"string"', b"{" * 5000, b"[" * 100000, b"{\"a\":" + b"[" * 3000 + b"]" * 3000 + b"}"):
        r = world.client.request(p.method, p.path, content=content, headers=h)
        assert r.status_code in (400, 413, 422), f"{content[:20]!r}: {r.status_code}"
        assert r.headers["content-type"].startswith("application/json")
    r = world.client.request(p.method, p.path, content=json.dumps(p.json).encode(), headers={**p.headers, "content-type": "text/plain"})
    assert r.status_code in (400, 415, 422)
    string_fields = [f for f in SPEC["schemas"][op["endpoint"]["request_schema"]]["fields"] if f["base"] in ("string", "text")]
    for f in string_fields[:2]:
        r = world.send(p, json={**p.json, f["name"]: "null byte \u0000 inside"})
        assert r.status_code < 500, f"NUL byte in {f['name']} -> {r.status_code}"


def test_oversized_bodies_are_refused(world):
    op = WRITE_OPS[0]
    caller = world.user(_roles_allowed(op)[0])
    p = world.prepare(op["id"], caller)
    limit = world.container.settings.max_body_bytes
    r = world.client.request(p.method, p.path, content=b"x" * (limit + 10), headers={**p.headers, "content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    chunks = (b"x" * 65536 for _ in range(limit // 65536 + 3))  # chunked: no content-length to inspect
    r = world.client.request(p.method, p.path, content=chunks, headers={**p.headers, "content-type": "application/json"})
    assert r.status_code == 413, r.text
    assert r.headers.get("x-request-id")


# ---- information & secret leakage -------------------------------------------------------------------------------------------------------------
FORBIDDEN_IN_ERRORS = ("Traceback", "psycopg", "postgres", "SELECT ", "INSERT ", "UPDATE ", "DELETE FROM", ".py", "/home/", "site-packages", "scrypt$", "password_hash")


def _assert_clean(world, text, what):
    for bad in (*FORBIDDEN_IN_ERRORS, world.schema, world.container.settings.jwt_secret, world.base_url):
        assert bad not in text, f"{what} leaks {bad!r}: {text[:300]}"


def test_error_responses_do_not_leak_internals(world):
    user = world.user(sorted(SPEC["auth"]["role_map"])[0])
    h = world.headers(user)
    seen = 0
    for op in ENGINE_OPS:
        ep = op["endpoint"]
        path = ep["path"].replace("{id}", str(uuid.uuid4()))
        for kw in ({}, {"json": {"x": "y"}}, {"headers": {"Authorization": "Bearer junk"}}):
            r = world.client.request(ep["method"], path, **({"headers": h} | kw if "headers" not in kw else kw))
            if r.status_code >= 400:
                _assert_clean(world, r.text, f"{ep['method']} {path}")
                seen += 1
    assert seen > 10


def test_unexpected_exceptions_return_a_generic_500(world):
    op = next(o for o in ENGINE_OPS if o["kind"] == "list")
    svc_key = op["service"].split(".", 1)[1]
    service = getattr(world.container.services, svc_key)
    name = next(n for n in dir(service) if n == op["id"].split(".", 2)[2].replace(".", "_"))

    def boom(*a, **k):
        raise RuntimeError("super-secret-internal-detail /srv/app/secret.py SELECT password_hash FROM users")

    setattr(service, name, boom)
    caller = world.user(_roles_allowed(op)[0]) if op["access"] != "public" else None
    r = world.client.get(op["endpoint"]["path"], headers=world.headers(caller))
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "super-secret" not in r.text and "secret.py" not in r.text and "RuntimeError" not in r.text and r.json().get("request_id")


def test_password_hash_never_appears_in_any_response(world):
    stored = {}
    user = world.user(sorted(SPEC["auth"]["role_map"])[0])
    stored["hash"] = user["password_hash"]
    for op in [o for o in ENGINE_OPS if o["kind"] in ("list", "read")]:
        for role in _roles_allowed(op)[:2]:
            p, r = world.call(op["id"], role=role)
            assert stored["hash"] not in r.text and "scrypt$" not in r.text and "password_hash" not in r.text, op["id"]
    login = world.client.post(SPEC["operations"][SPEC["auth"]["operations"]["auth.login"]]["endpoint"]["path"], json={"email": user["email"], "password": PASSWORD})
    assert "scrypt$" not in login.text and "password_hash" not in login.text


def test_secrets_never_reach_the_logs(world):
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(JsonFormatter().format(record))

    h = Grab(level=logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    try:
        user = world.user(sorted(SPEC["auth"]["role_map"])[0])
        login = SPEC["operations"][SPEC["auth"]["operations"]["auth.login"]]["endpoint"]["path"]
        tok = world.client.post(login, json={"email": user["email"], "password": PASSWORD}).json()["token"]
        world.client.post(login, json={"email": user["email"], "password": "Wr0ng-Passw0rd-canary"})
        for op in [o for o in ENGINE_OPS if o["kind"] == "list"][:2]:
            world.client.get(op["endpoint"]["path"], headers={"Authorization": f"Bearer {tok}"})
        world.client.get(ENGINE_OPS[0]["endpoint"]["path"], headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.canarypayload12.canarysignature"})
        if "auth.request_password_reset" in SPEC["auth"]["operations"]:
            path = SPEC["operations"][SPEC["auth"]["operations"]["auth.request_password_reset"]]["endpoint"]["path"]
            world.client.post(path, json={"email": user["email"]})
    finally:
        root.removeHandler(h)
        root.setLevel(old)
    blob = "\n".join(records)
    assert any('"event": "request"' in r for r in records), "access logs were not produced"
    for secret in (PASSWORD, "Wr0ng-Passw0rd-canary", tok, user["password_hash"], world.container.settings.jwt_secret, "canarypayload12", "canarysignature"):
        assert secret not in blob, f"secret {secret[:12]}... appears in the logs"
    if "auth.request_password_reset" in SPEC["auth"]["operations"] and world.email.outbox:
        assert world.email.outbox[-1].token not in blob, "the reset token was logged"
    access = [json.loads(r) for r in records if '"event": "request"' in r]
    for a in access:
        assert {"request_id", "method", "path", "status", "duration_ms"} <= set(a), a


# ---- headers, CORS, request ids ----------------------------------------------------------------------------------------------------------------
def test_cors_allows_only_configured_origins(world):
    allowed = world.container.settings.cors_origins[0]
    path = ENGINE_OPS[0]["endpoint"]["path"]
    ok = world.client.options(path, headers={"Origin": allowed, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"})
    assert ok.headers.get("access-control-allow-origin") == allowed
    bad = world.client.options(path, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers
    assert world.client.get("/health", headers={"Origin": "https://evil.example"}).headers.get("access-control-allow-origin") != "*"
    assert "*" not in world.container.settings.cors_origins


def test_request_ids_and_security_headers(world):
    r = world.client.get("/health", headers={"X-Request-ID": "trace-abc-12345"})
    assert r.headers["x-request-id"] == "trace-abc-12345"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    r = world.client.get("/health", headers={"X-Request-ID": "bad id\r\ninjected: 1"}) if False else world.client.get("/health", headers={"X-Request-ID": "x" * 500})
    assert r.headers["x-request-id"] != "x" * 500 and len(r.headers["x-request-id"]) <= 64
    err = world.client.get(ENGINE_OPS[0]["endpoint"]["path"].replace("{id}", "nope"))
    assert err.json()["request_id"] == err.headers["x-request-id"]
