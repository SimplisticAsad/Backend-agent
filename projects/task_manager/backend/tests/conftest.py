"""Shared fixtures. Tests run against a REAL PostgreSQL: TEST_DATABASE_URL (dedicated database) or a throw-away cluster.

If no database can be obtained the DB-dependent tests are SKIPPED with the reason, never silently mocked.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import pg  # noqa: E402
from tests.support.data import GRAPH, SPEC  # noqa: E402
from tests.support.world import World  # noqa: E402


@pytest.fixture(scope="session")
def spec():
    return SPEC


@pytest.fixture(scope="session")
def graph():
    return GRAPH


@pytest.fixture(scope="session")
def db_base_url():
    try:
        return pg.get_database_url()
    except (pg.DatabaseUnavailable, pg.UnsafeDatabase) as e:
        pytest.skip(f"no dedicated PostgreSQL available: {e}")


@pytest.fixture(scope="session")
def schema_name(db_base_url):
    """A private schema holding the Database Agent's schema.sql (+ crud.sql); dropped at the end of the session."""
    schema_sql = (ROOT / "db_contract" / "schema.sql").read_text()
    crud = ROOT / "db_contract" / "crud.sql"
    with pg.temp_schema(db_base_url) as (base, schema):
        pg.apply_sql(base, schema, schema_sql)
        if crud.exists():
            pg.apply_sql(base, schema, crud.read_text())
        yield schema


@pytest.fixture()
def world(db_base_url, schema_name, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_base_url)
    monkeypatch.setenv("DATABASE_SCHEMA", schema_name)
    monkeypatch.setenv("JWT_SECRET", "test-secret-" + "x" * 40)
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "100000")
    monkeypatch.setenv("APP_ENV", "test")
    w = World(SPEC, db_base_url, schema_name)
    try:
        yield w
    finally:
        w.close()


@pytest.fixture()
def client(world):
    return world.client
