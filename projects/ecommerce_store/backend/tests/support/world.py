"""Spec-driven test world: builds valid rows, callers and requests for ANY generated backend.

Nothing here knows about tasks, orders or tickets: it reads backend_app/generated/spec.json (the resolved graph) and the
live database, so the same tests exercise every project.
"""
from __future__ import annotations

import itertools
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from backend_app.security import hash_password
from backend_app.spec import SpecView

PASSWORD = "Passw0rd!test"
_PW_HASH: str | None = None


def pw_hash() -> str:
    global _PW_HASH
    if _PW_HASH is None:
        _PW_HASH = hash_password(PASSWORD)
    return _PW_HASH


@dataclass
class Prepared:
    op_id: str
    method: str
    path: str
    json: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    caller: dict[str, Any] | None = None
    row: dict[str, Any] | None = None
    to_state: str | None = None


class World:
    def __init__(self, spec: dict[str, Any], base_url: str, schema: str) -> None:
        from backend_app.app_factory import create_app
        from backend_app.config import Settings

        self.spec, self.view = spec, SpecView(spec)
        self.base_url, self.schema = base_url, schema
        self.conn = psycopg.connect(base_url, autocommit=True, row_factory=dict_row, options=f"-c search_path={schema}")
        self.truncate()
        self.app = create_app(Settings.from_env())
        self.container = self.app.state.container
        self.email = self.container.email
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.client.__enter__()  # runs lifespan: opens the pool
        self._n = itertools.count(1)
        self._tokens: dict[Any, str] = {}
        self._anon: dict[str, Any] | None = None
        self.auth = spec["auth"]
        self.role_keys = sorted(spec["auth"]["role_map"]) if self.auth else []

    # ---- lifecycle ---------------------------------------------------------------------------------------------------
    def close(self) -> None:
        try:
            self.client.__exit__(None, None, None)
        finally:
            self.conn.close()

    def truncate(self) -> None:
        tables = ", ".join(f'"{e["table"]}"' for e in self.spec["entities"].values())
        self.conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")

    # ---- values --------------------------------------------------------------------------------------------------------
    def n(self) -> int:
        return next(self._n)

    def value_for(self, entity_id: str, col: dict[str, Any], overrides: dict[str, Any] | None = None) -> Any:
        t, name = col["type"], col["attr"]
        n = self.n()
        if col["enum"]:
            ent = self.spec["entities"][entity_id]
            sm = self.spec["state_machines"].get(ent["state_machine"] or "")
            return sm["initial"] if sm and sm["field"] == name else col["enum"][0]
        if t == "uuid":
            return uuid.uuid4()
        if t == "email":
            return f"user{n}@example.com"
        if t == "url":
            return f"https://example.com/{n}"
        if t in ("string", "text"):
            return f"{name} {n}"
        if t == "integer":
            return 5
        if t == "decimal":
            return Decimal("10.00")
        if t == "boolean":
            return True
        if t == "date":
            return date.today() + timedelta(days=7)
        if t == "datetime":
            return datetime.now(timezone.utc)
        if t == "json":
            return "{}"
        raise AssertionError(f"no sample for type {t}")

    @property
    def anon_user(self) -> dict[str, Any]:
        if self._anon is None:
            self._anon = self.user(self.role_keys[0])
        return self._anon

    # ---- rows -------------------------------------------------------------------------------------------------------------
    def insert(self, entity_id: str, **overrides: Any) -> dict[str, Any]:
        ent = self.spec["entities"][entity_id]
        values: dict[str, Any] = {}
        for c in ent["columns"]:
            a = c["attr"]
            if a in overrides:
                values[a] = overrides[a]
            elif a == "id":
                continue
            elif c["column"] in ent["hidden_columns"]:
                values[a] = pw_hash()
            elif c["ref"]:
                if c["required"]:
                    ref = c["ref"]
                    values[a] = (self.anon_user["id"] if self.auth and ref == self.auth["entity"] else self.insert(ref)["id"])
            elif c["required"] or c["unique"]:
                values[a] = self.value_for(entity_id, c)
        if "id" in overrides:
            values["id"] = overrides["id"]
        cols = list(values)
        q = sql.SQL("INSERT INTO {t} ({c}) VALUES ({p}) RETURNING *").format(
            t=sql.Identifier(ent["table"]), c=sql.SQL(", ").join(map(sql.Identifier, cols)), p=sql.SQL(", ").join(sql.Placeholder() * len(cols)))
        return self.conn.execute(q, list(values.values())).fetchone()

    def user(self, role_key: str, **overrides: Any) -> dict[str, Any]:
        assert self.auth, "this project has no authentication"
        row = self.insert(self.auth["entity"], role=role_key, **overrides)
        row["password"] = PASSWORD
        return row

    def get(self, entity_id: str, row_id: Any) -> dict[str, Any] | None:
        t = self.spec["entities"][entity_id]["table"]
        return self.conn.execute(sql.SQL("SELECT * FROM {t} WHERE id = %s").format(t=sql.Identifier(t)), (row_id,)).fetchone()

    def rows(self, entity_id: str) -> list[dict[str, Any]]:
        t = self.spec["entities"][entity_id]["table"]
        return self.conn.execute(sql.SQL("SELECT * FROM {t} ORDER BY id").format(t=sql.Identifier(t))).fetchall()

    def counts(self) -> dict[str, int]:
        return {e["table"]: self.conn.execute(sql.SQL("SELECT count(*) AS n FROM {t}").format(t=sql.Identifier(e["table"]))).fetchone()["n"]
                for e in self.spec["entities"].values()}

    def dump(self) -> dict[str, list[dict[str, Any]]]:
        return {eid: self.rows(eid) for eid in self.spec["entities"]}

    # ---- auth ---------------------------------------------------------------------------------------------------------------
    def token(self, user: dict[str, Any]) -> str:
        if user["id"] not in self._tokens:
            r = self.client.post(self._login_path(), json={"email": user["email"], "password": user["password"]})
            assert r.status_code == 200, f"login failed for {user['email']}: {r.status_code} {r.text}"
            self._tokens[user["id"]] = r.json()["token"]
        return self._tokens[user["id"]]

    def headers(self, user: dict[str, Any] | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(user)}"} if user else {}

    def _login_path(self) -> str:
        return self.spec["operations"][self.auth["operations"]["auth.login"]]["endpoint"]["path"]

    def allowed_roles(self, op_id: str) -> list[str]:
        ids = self.view.allowed_role_ids(op_id) if self.spec["operations"][op_id]["access"] == "restricted" else set(self.view.roles)
        return sorted(self.view.roles[r]["key"] for r in ids if self.view.roles[r]["key"] in self.role_keys)

    def denied_roles(self, op_id: str) -> list[str]:
        return [r for r in self.role_keys if r not in self.allowed_roles(op_id)]

    # ---- scope-aware row construction ---------------------------------------------------------------------------------------
    def owned_overrides(self, entity_id: str, node: dict[str, Any], user_id: Any) -> dict[str, Any]:
        if "field" in node:
            return {node["field"]: user_id}
        parent = self.insert(node["parent_entity"], **self.owned_overrides(node["parent_entity"], node["parent_scope"], user_id))
        return {node["fk"]: parent["id"]}

    def _rules(self, op_id: str, rtype: str) -> list[dict[str, Any]]:
        return self.view.rules_for(op_id, rtype)

    def _held(self, caller: dict[str, Any] | None) -> frozenset[str]:
        return self.view.role_ids_held(self.view.role_by_key[caller["role"]]) if caller and self.auth else frozenset()

    def pick_transition(self, op: dict[str, Any], caller: dict[str, Any] | None, strict: bool = False) -> tuple[str, str]:
        sm = self.spec["state_machines"][op["params"]["state_machine"]]
        held = self._held(caller)
        frozen = {s for r in self._rules(op["id"], "frozen_state") for s in r["states"]}
        for t in sm["transitions"]:
            if (caller is None or set(t["roles"]) & held) and t["from"] not in frozen:
                return t["from"], t["to"]
        if strict:
            raise AssertionError(f"no transition permitted for {caller and caller['role']} in {sm['id']}")
        t = next(t for t in sm["transitions"] if t["from"] not in frozen)  # the role is expected to be refused: any transition will do
        return t["from"], t["to"]

    def target_row(self, op: dict[str, Any], caller: dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
        overrides: dict[str, Any] = {}
        held = self._held(caller)
        uid = caller["id"] if caller else None
        for r in self._rules(op["id"], "row_scope"):
            if set(r["roles"]) & held:
                overrides.update(self.owned_overrides(op["entity"], r["scope"], uid))
        for r in self._rules(op["id"], "ownership"):
            if not set(r["exempt_roles"]) & held:
                overrides[r["field"]] = uid
        to = None
        imp = op["params"].get("implicit_filter")
        if imp:  # e.g. list_agents only returns users whose role is 'agent'
            overrides[imp["field"]] = imp["value"]
        if op["kind"] == "transition":
            frm, to = self.pick_transition(op, caller)
            overrides[op["params"]["field"]] = frm
            for g in self._rules(op["id"], "transition_guard"):
                if g["to"] == to:
                    for f in g["require_fields_set"]:
                        overrides.setdefault(f, uid if uid else self.anon_user["id"])
        return self.insert(op["entity"], **overrides), to

    def payload_value(self, entity_id: str, f: dict[str, Any], caller: dict[str, Any] | None, op: dict[str, Any]) -> Any:
        ent = self.spec["entities"][entity_id]
        col = next((c for c in ent["columns"] if c["attr"] == f["name"]), None)
        if col is None:  # non-attribute input such as `password`
            return PASSWORD if "password" in f["name"] else f"{f['name']}-{self.n()}"
        if col["ref"]:
            if self.auth and col["ref"] == self.auth["entity"]:
                return str(self.anon_user["id"])
            overrides: dict[str, Any] = {}
            held = self._held(caller)
            for r in self._rules(op["id"], "row_scope"):
                node = r["scope"]
                if "fk" in node and node["fk"] == f["name"] and set(r["roles"]) & held:
                    overrides = self.owned_overrides(col["ref"], node["parent_scope"], caller["id"])
            return str(self.insert(col["ref"], **overrides)["id"])
        v = self.value_for(entity_id, col)
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        if isinstance(v, Decimal):
            return float(v)
        return v

    def payload(self, op: dict[str, Any], caller: dict[str, Any] | None, to_state: str | None = None) -> dict[str, Any]:
        ep = op["endpoint"]
        schema = self.spec["schemas"][ep["request_schema"]]
        body: dict[str, Any] = {}
        if op["kind"] == "transition":
            return {op["params"]["field"]: to_state}
        fields = schema["fields"]
        chosen = [f for f in fields if f["required"]]
        if op["kind"] == "update" and not chosen and fields:
            chosen = [next((f for f in fields if f["base"] in ("string", "text")), fields[0])]
        for f in chosen:
            body[f["name"]] = self.payload_value(op["entity"], f, caller, op)
        return body

    def prepare(self, op_id: str, caller: dict[str, Any] | None = None, *, role: str | None = None) -> Prepared:
        op = self.spec["operations"][op_id]
        ep = op["endpoint"]
        if caller is None and role is not None:
            caller = self.user(role)
        row, to = None, None
        path = ep["path"]
        if "id" in ep["path_params"] and op["entity"]:
            if op["kind"] in ("update", "transition", "read", "delete"):
                row, to = self.target_row(op, caller)
            else:  # custom handlers: any existing row
                row = self.insert(op["entity"])
            path = path.replace("{id}", str(row["id"]))
        body = self.payload(op, caller, to) if ep["request_schema"] else None
        return Prepared(op_id, ep["method"], path, body, {}, self.headers(caller) if op["access"] != "public" or caller else {}, caller, row, to)

    def send(self, p: Prepared, **kw: Any):
        """Send a prepared request. Keyword overrides: json=, params=, headers=, content=."""
        body = kw.pop("json", p.json)
        params = kw.pop("params", p.params) or None
        headers = kw.pop("headers", p.headers)
        return self.client.request(p.method, p.path, json=body, params=params, headers=headers, **kw)

    def call(self, op_id: str, role: str | None = None, caller: dict[str, Any] | None = None):
        p = self.prepare(op_id, caller, role=role)
        return p, self.send(p)


# ---- response/contract helpers ----------------------------------------------------------------------------------------------
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def check_type(base: str, value: Any, enum: list[str] | None) -> bool:
    if value is None:
        return True
    if base == "uuid":
        return isinstance(value, str) and bool(_UUID.match(value))
    if base in ("string", "text", "email", "url"):
        return isinstance(value, str)
    if base == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if base == "decimal":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if base == "boolean":
        return isinstance(value, bool)
    if base in ("date", "datetime"):
        return isinstance(value, str) and bool(re.match(r"^\d{4}-\d{2}-\d{2}", value))
    if base == "enum":
        return value in (enum or [])
    return True


def assert_matches_schema(body: dict[str, Any], schema: dict[str, Any]) -> None:
    assert isinstance(body, dict), f"expected an object, got {type(body).__name__}"
    for f in schema["fields"]:
        if f["required"]:
            assert f["name"] in body, f"response is missing required field '{f['name']}' of {schema['id']}: {sorted(body)}"
        if f["name"] in body:
            assert check_type(f["base"], body[f["name"]], f.get("enum")), f"field '{f['name']}' = {body[f['name']]!r} is not a valid {f['type']}"
    assert "password_hash" not in body, "password hash leaked in a response"
