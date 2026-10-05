"""Database failure tests: outages, pool exhaustion, closed pools, constraint violations, rollbacks. Responses must be safe and logs useful."""
from __future__ import annotations

import logging
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from backend_app.app_factory import create_app
from backend_app.config import Settings
from tests.support.data import ENGINE_OPS, OPS, SPEC

pytestmark = pytest.mark.integration
SAFE = ("psycopg", "postgres", "Traceback", "SELECT ", "127.0.0.1", "connection to server", "refused")
PUBLIC = [o for o in OPS.values() if o["access"] == "public"]


def _no_leak(text: str):
    for bad in SAFE:
        assert bad not in text, f"error response leaks {bad!r}: {text[:300]}"


@pytest.fixture()
def dead_app(monkeypatch):
    """An application whose database is unreachable (nothing listens on port 1)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:hunter2@127.0.0.1:1/missing")
    monkeypatch.setenv("DATABASE_SCHEMA", "none")
    monkeypatch.setenv("JWT_SECRET", "outage-" + "x" * 40)
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("APP_ENV", "test")
    app = create_app(Settings.from_env())
    with TestClient(app, raise_server_exceptions=False) as c:
        yield app, c


def test_outage_liveness_stays_up_readiness_goes_down(dead_app, caplog):
    app, c = dead_app
    assert c.get("/health").status_code == 200
    r = c.get("/ready")
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    _no_leak(r.text)
    assert "hunter2" not in r.text


def test_outage_returns_safe_503_for_every_operation(dead_app, caplog):
    app, c = dead_app
    from backend_app.security import TokenService, password_fingerprint

    caplog.set_level(logging.INFO)
    tok, _ = TokenService(app.state.container.settings, SPEC["project"]["key"]).issue_access_token(str(uuid.uuid4()), "x", "h")
    seen = 0
    for op in OPS.values():
        ep = op["endpoint"]
        path = ep["path"].replace("{id}", str(uuid.uuid4()))
        body = None
        if ep["request_schema"]:
            body = {f["name"]: ("a@example.com" if f["base"] == "email" else str(uuid.uuid4()) if f["base"] == "uuid" else (op["roles"] and "x") or "x") for f in SPEC["schemas"][ep["request_schema"]]["fields"] if f["required"]}
            for f in SPEC["schemas"][ep["request_schema"]]["fields"]:
                if f["enum"] and f["name"] in body:
                    body[f["name"]] = f["enum"][0]
                if f["base"] in ("integer", "decimal") and f["name"] in body:
                    body[f["name"]] = 1
                if "password" in f["name"] or "token" in f["name"]:
                    body[f["name"]] = "LongEnough-pw-1!"
        r = c.request(ep["method"], path, json=body, headers={"Authorization": f"Bearer {tok}"} if op["access"] != "public" else {})
        if r.status_code in (400, 422, 429):  # request rejected before touching the database
            continue
        assert r.status_code == 503, f"{op['id']}: expected 503 during an outage, got {r.status_code} {r.text}"
        assert r.json()["error"]["code"] in ("DATABASE_UNAVAILABLE", "DATABASE_BUSY", "DATABASE_TIMEOUT")
        _no_leak(r.text)
        seen += 1
    assert seen > 0
    logs = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "hunter2" not in logs, "the database password must never be logged"
    assert any("database" in rec.getMessage().lower() for rec in caplog.records), "useful diagnostics must be logged server-side"


def test_closed_pool_is_a_503_not_a_500(world):
    user = world.user(sorted(SPEC["auth"]["role_map"])[0])
    h = world.headers(user)
    op = next(o for o in ENGINE_OPS if o["kind"] == "list" and o["access"] != "public")
    world.container.db.close()
    r = world.client.get(op["endpoint"]["path"], headers=h)
    assert r.status_code == 503 and r.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    _no_leak(r.text)
    assert world.client.get("/ready").status_code == 503
    world.container.db.open()
    assert world.client.get(op["endpoint"]["path"], headers=h).status_code in (200, 403)


def test_pool_exhaustion_returns_503_busy(monkeypatch, world):
    monkeypatch.setenv("DB_POOL_MAX", "1")
    monkeypatch.setenv("DB_POOL_MIN", "1")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "0.3")
    app = create_app(Settings.from_env())
    with TestClient(app, raise_server_exceptions=False) as c:
        db = app.state.container.db
        assert db.ping()
        result = {}
        with db._pool.connection():  # hold the only connection
            def call():
                result["r"] = c.get("/ready")

            t = threading.Thread(target=call)
            t.start()
            t.join(5)
        assert result["r"].status_code == 503
        _no_leak(result["r"].text)
        assert db.ping(), "the pool must recover once the connection is returned"


def test_transaction_is_rolled_back_when_a_later_step_fails(world):
    """Two inserts in one transaction where the second violates a constraint: neither may persist."""
    child = next((e for e in SPEC["entities"].values() if any(c["ref"] and c["required"] and c["ref"] != (SPEC["auth"] or {}).get("entity") for c in e["columns"])), None)
    if child is None:
        return
    ref_col = next(c for c in child["columns"] if c["ref"] and c["required"] and c["ref"] != (SPEC["auth"] or {}).get("entity"))
    parent_eid = ref_col["ref"]
    good = world.insert(child["id"])
    parent_repo, child_repo = world.container.repos[parent_eid], world.container.repos[child["id"]]
    parent_vals = {k: v for k, v in world.insert(parent_eid).items() if k != "id"}
    for c in SPEC["entities"][parent_eid]["columns"]:
        if c["unique"] and c["attr"] != "id":
            parent_vals[c["attr"]] = f"u-{uuid.uuid4().hex}@example.com" if c["type"] == "email" else f"u-{uuid.uuid4().hex}"
    child_vals = {k: v for k, v in good.items() if k != "id"}
    for c in child["columns"]:
        if c["unique"] and c["attr"] != "id":
            child_vals[c["attr"]] = f"u-{uuid.uuid4().hex}@example.com" if c["type"] == "email" else f"u-{uuid.uuid4().hex}"
    child_vals[ref_col["attr"]] = uuid.uuid4()  # dangling reference: fails at the second statement
    before = world.counts()
    with pytest.raises(Exception):
        with world.container.db.transaction() as conn:
            parent_repo.insert(conn, parent_vals)
            child_repo.insert(conn, child_vals)
    assert world.counts() == before, "the first insert must be rolled back together with the failed second one"


@pytest.mark.parametrize("op", [o for o in ENGINE_OPS if o["kind"] == "create"] or [None], ids=lambda o: o["id"] if o else "none")
def test_constraint_violations_surface_as_client_errors(world, op):
    if op is None:
        return
    schema = SPEC["schemas"][op["endpoint"]["request_schema"]]
    refs = [f for f in schema["fields"] if f["ref"] and f["required"] and f["ref"] != (SPEC["auth"] or {}).get("entity")]
    keys = sorted(SPEC["auth"]["role_map"])
    allowed = [k for k in keys if k in [SPEC["roles"][r]["key"] for r in SPEC["roles"]] and (op["access"] != "restricted" or f"role.{k}" in op["roles"])]
    caller = world.user(allowed[0]) if allowed else None
    if caller is None or not refs:
        return
    p = world.prepare(op["id"], caller)
    r = world.send(p, json={**p.json, refs[0]["name"]: str(uuid.uuid4())})
    assert r.status_code in (404, 422), r.text
    _no_leak(r.text)
