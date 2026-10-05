"""The Database Agent's SQL is EXECUTED on PostgreSQL and the live catalog is compared with its architecture.json (never assumed to work)."""
from __future__ import annotations

import json

import psycopg
import pytest

from app.contracts.database import introspect, load_database_contract, verify_live_schema
from app.errors import ErrorKind
from app.testing.pg_cluster import apply_sql, temp_schema
from tests.conftest import FIXTURES, PROJECTS

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("name", FIXTURES)
def test_schema_and_crud_sql_execute_and_match_the_architecture(pg_url, name):
    contract = load_database_contract(PROJECTS / name / "database")
    with temp_schema(pg_url, prefix="contract") as (base, schema):
        apply_sql(base, schema, contract.schema_sql)
        apply_sql(base, schema, contract.crud_sql)
        with psycopg.connect(base, autocommit=True) as conn:
            snap = introspect(conn, schema)
    assert verify_live_schema(snap, contract) == []
    committed = json.loads((PROJECTS / name / "database" / "database_state.json").read_text())
    assert {t: sorted(c["column_name"] for c in v["columns"]) for t, v in snap["tables"].items()} == \
           {t: sorted(c["column_name"] for c in v["columns"]) for t, v in committed["tables"].items()}, "the committed snapshot is stale"
    assert {f["name"] for f in snap["functions"]} == {f["name"] for f in committed["functions"]}


def test_a_contract_that_promises_more_than_the_sql_builds_is_reported(pg_url):
    contract = load_database_contract(PROJECTS / "task_manager" / "database")
    contract.tables["tasks"].columns["ghost"] = type(contract.tables["tasks"].columns["title"])("ghost", "text")
    contract.tables["tasks"].primary_key = ["id", "title"]
    with temp_schema(pg_url, prefix="contract") as (base, schema):
        apply_sql(base, schema, contract.schema_sql)
        with psycopg.connect(base, autocommit=True) as conn:
            snap = introspect(conn, schema)
    codes = {i.code for i in verify_live_schema(snap, contract)}
    assert {"column_not_created", "pk_differs"} <= codes
    assert all(i.kind == ErrorKind.DATABASE_CONTRACT_ERROR for i in verify_live_schema(snap, contract))
