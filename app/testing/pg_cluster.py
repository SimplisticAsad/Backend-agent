"""Dedicated PostgreSQL for tests (stdlib + psycopg only, so generated backends can reuse this file verbatim).

Resolution order:
  1. TEST_DATABASE_URL  - an existing *dedicated* database. Safety rules: the database name must contain "test" and the
                          host must be local, unless BACKEND_AGENT_TEST_DB_CONFIRM=<dbname> is exported.
  2. A throw-away local cluster started from the PostgreSQL binaries on this machine (initdb + pg_ctl, trust auth on
     127.0.0.1, fsync off, random port). Removed on exit.
  3. Otherwise `DatabaseUnavailable` - callers skip with that reason instead of silently mocking the database.

Tests never touch existing schemas: each run creates a random schema (`bt_<hex>`) and drops it afterwards.
"""
from __future__ import annotations

import atexit
import glob
import os
import pwd
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlparse

import psycopg

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


class DatabaseUnavailable(RuntimeError):
    """No dedicated PostgreSQL could be obtained."""


class UnsafeDatabase(RuntimeError):
    """TEST_DATABASE_URL points at something that does not look like a dedicated test database."""


def check_safe_test_url(url: str, env: dict[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    u = urlparse(url)
    name = (u.path or "/").lstrip("/")
    confirm = env.get("BACKEND_AGENT_TEST_DB_CONFIRM")
    if confirm and confirm == name:
        return
    if "test" not in name.lower():
        raise UnsafeDatabase(f"refusing to run destructive tests on database '{name}': its name must contain 'test' "
                             "(or export BACKEND_AGENT_TEST_DB_CONFIRM=<name> to confirm it is dedicated)")
    host = u.hostname or ""
    if host not in LOCAL_HOSTS and not host.startswith("/"):
        raise UnsafeDatabase(f"refusing to run destructive tests on non-local host '{host}' "
                             "(export BACKEND_AGENT_TEST_DB_CONFIRM=<dbname> to override)")


def _find_bin(name: str) -> str | None:
    for d in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if os.path.exists(os.path.join(d, name)):
            return os.path.join(d, name)
    return shutil.which(name)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Cluster:
    url: str  # admin URL of the dedicated test database
    _stop: object | None = None

    def stop(self) -> None:
        if self._stop:
            self._stop()
            self._stop = None


_CLUSTER: Cluster | None = None


def _run(cmd: list[str], as_postgres: bool) -> subprocess.CompletedProcess:
    if as_postgres:
        cmd = ["runuser", "-u", "postgres", "--", *cmd] if shutil.which("runuser") else ["su", "postgres", "-c", shlex.join(cmd)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _start_temp_cluster() -> Cluster:
    initdb, pg_ctl = _find_bin("initdb"), _find_bin("pg_ctl")
    if not initdb or not pg_ctl:
        raise DatabaseUnavailable("no PostgreSQL binaries (initdb/pg_ctl) found and TEST_DATABASE_URL is not set")
    as_pg = os.geteuid() == 0
    if as_pg:
        try:
            pwd.getpwnam("postgres")
        except KeyError:
            raise DatabaseUnavailable("running as root and there is no 'postgres' OS user to run the cluster")
    base = tempfile.mkdtemp(prefix="bagent-pg-")
    if as_pg:
        os.chmod(base, 0o755)
        shutil.chown(base, "postgres")
    data, port = os.path.join(base, "data"), _free_port()
    r = _run([initdb, "-D", data, "-A", "trust", "-U", "postgres", "--no-sync"], as_pg)
    if r.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise DatabaseUnavailable(f"initdb failed: {r.stderr.strip()[:300]}")
    opts = f"-p {port} -k {base} -c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=100"
    r = _run([pg_ctl, "-D", data, "-o", f'"{opts}"' if as_pg and not shutil.which("runuser") else opts, "-l", os.path.join(base, "log"), "-w", "start"], as_pg)
    if r.returncode != 0:
        shutil.rmtree(base, ignore_errors=True)
        raise DatabaseUnavailable(f"pg_ctl start failed: {r.stderr.strip()[:300]}")

    def stop() -> None:
        _run([pg_ctl, "-D", data, "-m", "immediate", "-w", "stop"], as_pg)
        shutil.rmtree(base, ignore_errors=True)

    admin = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("CREATE DATABASE backend_agent_test")
    return Cluster(f"postgresql://postgres@127.0.0.1:{port}/backend_agent_test", stop)


def get_database_url() -> str:
    """URL of a dedicated test database (starts a throw-away cluster on first use)."""
    global _CLUSTER
    env_url = os.environ.get("TEST_DATABASE_URL")
    if env_url:
        check_safe_test_url(env_url)
        return env_url
    if _CLUSTER is None:
        _CLUSTER = _start_temp_cluster()
        atexit.register(_CLUSTER.stop)
    return _CLUSTER.url


def with_search_path(url: str, schema: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}options=-c%20search_path%3D{schema}"


_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@contextmanager
def temp_schema(url: str | None = None, prefix: str = "bt") -> Iterator[tuple[str, str]]:
    """Create a random schema in the dedicated database; yield (base_url, schema); drop it on exit."""
    base = url or get_database_url()
    schema = f"{prefix}_{uuid.uuid4().hex[:10]}"
    assert _SCHEMA_RE.match(schema)
    with psycopg.connect(base, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield base, schema
    finally:
        try:
            with psycopg.connect(base, autocommit=True) as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        except psycopg.Error:
            pass


def apply_sql(base_url: str, schema: str, *scripts: str) -> None:
    """Execute SQL scripts (schema.sql, crud.sql) inside `schema`, each in one transaction."""
    with psycopg.connect(base_url, autocommit=False, options=f"-c search_path={schema}") as c:
        for s in scripts:
            if s:
                c.execute(s)
        c.commit()


def wait_ready(url: str, seconds: float = 5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with psycopg.connect(url, connect_timeout=1):
                return True
        except psycopg.Error:
            time.sleep(0.2)
    return False
