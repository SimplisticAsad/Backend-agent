"""End-to-end over the REAL network stack: the production entry point (`uvicorn backend_app.main:app`, APP_ENV=production) is started as a
separate process against the dedicated test database and driven with plain HTTP requests, like the Frontend would drive it.

(This is not a browser test: the Frontend application itself is not launched. See the Backend Agent README, "Known limitations".)
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from tests.support.data import ENGINE_OPS, OPS, ROOT, SPEC
from tests.support.world import PASSWORD

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live(world):
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT), "APP_ENV": "production", "CORS_ORIGINS": "http://localhost:5173", "RATE_LIMIT_PER_MINUTE": "1000",
           "JWT_SECRET": "live-server-secret-" + "s" * 40}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend_app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(base + "/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            proc.kill()
            pytest.fail("the server did not start: " + (proc.communicate()[0] or "")[-800:])
        yield base, proc
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()


def test_the_production_server_serves_the_graph_over_real_http(world, live):
    base, proc = live
    assert httpx.get(base + "/health").json() == {"status": "ok"}
    assert httpx.get(base + "/ready").json() == {"status": "ready", "database": "ok"}
    assert httpx.get(base + "/docs").status_code == 404, "the interactive docs are disabled in production"
    assert httpx.get(base + "/openapi.json").status_code == 200
    login = SPEC["operations"][SPEC["auth"]["operations"]["auth.login"]]["endpoint"]["path"]
    served = 0
    for role in sorted(SPEC["auth"]["role_map"]):
        user = world.user(role)  # created directly in the database the server uses
        r = httpx.post(base + login, json={"email": user["email"], "password": PASSWORD})
        assert r.status_code == 200 and r.headers["x-request-id"], r.text
        headers = {"Authorization": f"Bearer {r.json()['token']}"}
        for op in ENGINE_OPS:
            ep = op["endpoint"]
            if op["kind"] != "list" or ep["path_params"] or role not in [SPEC["roles"][x]["key"] for x in (op["roles"] or [f"role.{role}"])]:
                continue
            got = httpx.get(base + ep["path"], headers=headers)
            assert got.status_code == 200 and isinstance(got.json(), list), f"{op['id']} as {role}: {got.status_code} {got.text}"
            served += 1
    assert served > 0
    secured = next(o for o in OPS.values() if o["access"] != "public" and o["endpoint"]["method"] == "GET" and not o["endpoint"]["path_params"])
    assert httpx.get(base + secured["endpoint"]["path"]).status_code == 401
    pre = httpx.options(base + secured["endpoint"]["path"], headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert pre.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in httpx.options(base + secured["endpoint"]["path"], headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}).headers


def test_the_server_shuts_down_gracefully(world, live):
    base, proc = live
    assert httpx.get(base + "/ready").status_code == 200
    proc.send_signal(signal.SIGTERM)
    code = proc.wait(15)  # uvicorn finishes its shutdown, then re-raises the signal: 0 or -SIGTERM are both a clean stop
    assert code in (0, -signal.SIGTERM), code
    out = proc.stdout.read()
    assert "application stopped" in out, "the lifespan shutdown hook (which closes the connection pool) must have run"
    assert "Traceback" not in out
