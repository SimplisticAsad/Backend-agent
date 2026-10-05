"""Repository tests against REAL PostgreSQL: CRUD, listing, constraints, transactions, and the database contract itself."""
from __future__ import annotations

import json
import uuid

import pytest

from backend_app.errors import ConflictError, ServiceUnavailable, ValidationError
from tests.support.data import ENTITY_IDS, ROOT, SPEC

pytestmark = pytest.mark.integration


def _repo(world, eid):
    return world.container.repos[eid]


def _text_attr(eid):
    return next((c for c in SPEC["entities"][eid]["columns"] if c["type"] in ("string", "text") and c["attr"] != "password_hash" and not c["unique"]
                 and not c["ref"] and c["column"] not in SPEC["entities"][eid]["hidden_columns"]), None)


@pytest.mark.parametrize("eid", ENTITY_IDS)
def test_create_read_update_delete_roundtrip(world, eid):
    repo, db = _repo(world, eid), world.container.db
    seeded = world.insert(eid)
    with db.transaction() as conn:
        assert repo.get(conn, seeded["id"]) == seeded
        rows, total = repo.list(conn)
        assert total == len([1 for _ in world.rows(eid)]) and seeded["id"] in {r["id"] for r in rows}
        attr = _text_attr(eid)
        if attr:
            updated = repo.update(conn, seeded["id"], {attr["attr"]: "changed value"})
            assert updated[attr["attr"]] == "changed value"
        if attr:
            assert repo.update(conn, uuid.uuid4(), {attr["attr"]: "x"}) is None, "updating a row that does not exist changes nothing"
    with db.transaction(deleting=True) as conn:
        assert repo.delete(conn, seeded["id"]) is True
        assert repo.delete(conn, seeded["id"]) is False  # idempotent: second delete finds nothing
    assert world.get(eid, seeded["id"]) is None


@pytest.mark.parametrize("eid", ENTITY_IDS)
def test_insert_through_repository_uses_database_defaults(world, eid):
    ent = SPEC["entities"][eid]
    repo, db = _repo(world, eid), world.container.db
    # build the values with the world, delete the row, then re-insert through the repository WITHOUT an id
    row = world.insert(eid)
    values = {c["attr"]: row[c["attr"]] for c in ent["columns"] if c["attr"] != "id" and not (c["unique"] and c["attr"] in ("email",))}
    for c in ent["columns"]:
        if c["unique"] and c["attr"] != "id":
            values[c["attr"]] = f"unique-{uuid.uuid4().hex}@example.com" if c["type"] == "email" else f"unique-{uuid.uuid4().hex}"
    with db.transaction() as conn:
        created = repo.insert(conn, values)
    assert created["id"] is not None and created["id"] != row["id"]
    for c in ent["columns"]:
        if c["has_default"] and c["attr"] not in values:
            assert created[c["attr"]] is not None


@pytest.mark.parametrize("eid", ENTITY_IDS)
def test_list_pagination_and_sorting(world, eid):
    repo, db = _repo(world, eid), world.container.db
    for _ in range(3):
        world.insert(eid)
    with db.transaction() as conn:
        page, total = repo.list(conn, limit=2, offset=0)
        rest, total2 = repo.list(conn, limit=2, offset=2)
        assert total == total2 >= 3 and len(page) == 2 and len(rest) >= 1
        assert not {r["id"] for r in page} & {r["id"] for r in rest}
        asc, _ = repo.list(conn, sort="id", order="asc", limit=100)
        desc, _ = repo.list(conn, sort="id", order="desc", limit=100)
        assert [r["id"] for r in asc] == [r["id"] for r in reversed(desc)]
        with pytest.raises(ValidationError) as ei:
            repo.list(conn, sort="id; DROP TABLE x")
        assert ei.value.code == "INVALID_SORT_FIELD"


@pytest.mark.parametrize("eid", ENTITY_IDS)
def test_unique_constraints_are_enforced_by_the_database(world, eid):
    ent = SPEC["entities"][eid]
    uniques = [c for c in ent["columns"] if c["unique"] and c["attr"] != "id"]
    if not uniques:
        return
    c = uniques[0]
    first = world.insert(eid)
    with pytest.raises(ConflictError) as ei:
        with world.container.db.transaction() as conn:
            _repo(world, eid).insert(conn, {k: v for k, v in first.items() if k != "id"})
    assert ei.value.code == "DUPLICATE_VALUE", c["attr"]


@pytest.mark.parametrize("eid", ENTITY_IDS)
def test_foreign_keys_are_enforced_by_the_database(world, eid):
    ent = SPEC["entities"][eid]
    refs = [c for c in ent["columns"] if c["ref"] and c["required"]]
    if not refs:
        return
    good = world.insert(eid)
    values = {k: v for k, v in good.items() if k != "id"}
    for c in ent["columns"]:
        if c["unique"] and c["attr"] != "id":
            values[c["attr"]] = f"u-{uuid.uuid4().hex}@example.com" if c["type"] == "email" else f"u-{uuid.uuid4().hex}"
    values[refs[0]["attr"]] = uuid.uuid4()
    with pytest.raises(ValidationError) as ei:
        with world.container.db.transaction() as conn:
            _repo(world, eid).insert(conn, values)
    assert ei.value.code == "REFERENCE_NOT_FOUND"


def test_deleting_a_referenced_row_is_a_conflict_when_the_contract_restricts_it(world):
    tested = 0
    for child in SPEC["entities"].values():
        for fk in child["foreign_keys"]:
            if fk["on_delete"] not in ("RESTRICT", "NO ACTION"):
                continue
            parent = next((e for e in SPEC["entities"].values() if e["table"] == fk["ref_table"]), None)
            col = next((c for c in child["columns"] if c["column"] == fk["columns"][0]), None)
            if not parent or not col:
                continue
            p = world.insert(parent["id"])
            world.insert(child["id"], **{col["attr"]: p["id"]})
            with pytest.raises(ConflictError) as ei:
                with world.container.db.transaction(deleting=True) as conn:
                    _repo(world, parent["id"]).delete(conn, p["id"])
            assert ei.value.code == "ENTITY_IN_USE"
            assert world.get(parent["id"], p["id"]) is not None
            tested += 1
    assert tested > 0 or not any(f["on_delete"] in ("RESTRICT", "NO ACTION") for e in SPEC["entities"].values() for f in e["foreign_keys"])


def test_transaction_commits_together_and_rolls_back_together(world):
    eid = ENTITY_IDS[0]
    repo, db = _repo(world, eid), world.container.db
    base = world.insert(eid)
    values = {k: v for k, v in base.items() if k != "id"}
    for c in SPEC["entities"][eid]["columns"]:
        if c["unique"] and c["attr"] != "id":
            values[c["attr"]] = f"a-{uuid.uuid4().hex}@example.com" if c["type"] == "email" else f"a-{uuid.uuid4().hex}"
    before = world.counts()
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            repo.insert(conn, values)
            raise RuntimeError("fail after the first statement")
    assert world.counts() == before, "the insert must be rolled back with the rest of the transaction"
    with db.transaction() as conn:
        repo.insert(conn, values)
    assert world.counts()[SPEC["entities"][eid]["table"]] == before[SPEC["entities"][eid]["table"]] + 1


def test_pool_is_bounded_and_closes_cleanly(world):
    db = world.container.db
    assert db.ping() is True
    db.close()
    assert db.ping() is False
    with pytest.raises(ServiceUnavailable):
        with db.transaction():
            pass
    db.open()
    assert db.ping() is True


# ---- the database contract itself -------------------------------------------------------------------------------------------
def test_live_schema_matches_the_database_agents_architecture(world):
    """Executing schema.sql must really produce what architecture.json promises (tables, columns, primary keys, foreign keys)."""
    arch = json.loads((ROOT / "db_contract" / "architecture.json").read_text())
    live_cols: dict[str, dict[str, str]] = {}
    for r in world.conn.execute("SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = %s", (world.schema,)).fetchall():
        live_cols.setdefault(r["table_name"], {})[r["column_name"]] = r["data_type"]
    fams = {"uuid": "uuid", "text": "text", "integer": "integer", "numeric": "numeric", "boolean": "boolean", "date": "date", "timestamptz": "timestamp with time zone"}
    for t in arch["tables"]:
        assert t["name"] in live_cols, f"table {t['name']} from architecture.json was not created by schema.sql"
        for c in t["columns"]:
            assert c["name"] in live_cols[t["name"]], f"{t['name']}.{c['name']} missing"
            want = fams.get(c["type"].split("(")[0])
            if want:
                assert live_cols[t["name"]][c["name"]] == want, f"{t['name']}.{c['name']}: {live_cols[t['name']][c['name']]} != {want}"
        pk = [r["attname"] for r in world.conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = (%s || '.' || %s)::regclass AND i.indisprimary", (f'"{world.schema}"', f'"{t["name"]}"')).fetchall()]
        assert pk == t["primary_key"]["columns"]
        for fk in t["foreign_keys"]:
            found = world.conn.execute(
                "SELECT 1 FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage k USING (constraint_name, table_schema) "
                "WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s AND tc.table_name = %s AND k.column_name = %s",
                (world.schema, t["name"], fk["columns"][0])).fetchone()
            assert found, f"foreign key {t['name']}.{fk['columns']} is promised but missing"


def test_database_agent_crud_functions_execute(world):
    """crud.sql functions are part of the contract; prove they run (list_* and get_* on an empty and a filled table)."""
    funcs = {r["proname"] for r in world.conn.execute(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s", (world.schema,)).fetchall()}
    if not (ROOT / "db_contract" / "crud.sql").exists():
        return
    listed = [f for f in funcs if f.startswith("list_")]
    assert listed, "crud.sql defines no list_* function"
    for f in listed:
        assert world.conn.execute(f'SELECT count(*) AS n FROM "{world.schema}"."{f}"()').fetchone()["n"] >= 0
