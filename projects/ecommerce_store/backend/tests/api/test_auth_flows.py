"""Authentication flows driven by the spec's auth section: login, logout/revocation, password reset, brute-force limiting."""
from __future__ import annotations

import time

import jwt
import pytest

from tests.support.data import SPEC
from tests.support.world import PASSWORD

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not SPEC["auth"], reason="this project defines no authentication")]


def _path(kind: str) -> str:
    return SPEC["operations"][SPEC["auth"]["operations"][kind]]["endpoint"]["path"]


def _has(kind: str) -> bool:
    return kind in (SPEC["auth"] or {}).get("operations", {})


ROLE = sorted(SPEC["auth"]["role_map"])[0] if SPEC["auth"] else None


def test_login_returns_the_session_envelope_and_the_graph_user_fields(world):
    u = world.user(ROLE)
    r = world.client.post(_path("auth.login"), json={"email": u["email"], "password": PASSWORD})
    assert r.status_code == 200, r.text
    b = r.json()
    assert isinstance(b["token"], str) and b["token_type"] == "bearer" and b["expires_in"] > 0
    assert b["user"]["id"] == str(u["id"]) and b["user"]["email"] == u["email"]
    assert b["id"] == b["user"]["id"], "the graph's user schema must also be satisfiable at top level"
    assert "password_hash" not in r.text and "password_hash" not in b["user"]
    claims = jwt.decode(b["token"], options={"verify_signature": False})
    assert claims["sub"] == str(u["id"]) and claims["exp"] > time.time() and "password" not in claims and "email" not in claims


def test_wrong_password_and_unknown_email_look_identical(world):
    u = world.user(ROLE)
    a = world.client.post(_path("auth.login"), json={"email": u["email"], "password": "wrong-password"})
    b = world.client.post(_path("auth.login"), json={"email": "nobody@example.com", "password": "wrong-password"})
    assert a.status_code == b.status_code == 401
    assert a.json()["error"]["code"] == b.json()["error"]["code"] and a.json()["message"] == b.json()["message"]


def test_login_email_is_case_insensitive_and_validated(world):
    u = world.user(ROLE)
    assert world.client.post(_path("auth.login"), json={"email": u["email"].upper(), "password": PASSWORD}).status_code == 200
    assert world.client.post(_path("auth.login"), json={"email": "not-an-email", "password": PASSWORD}).status_code == 422
    assert world.client.post(_path("auth.login"), json={"email": u["email"]}).status_code == 422
    assert world.client.post(_path("auth.login"), json={"email": u["email"], "password": "x" * 5000}).status_code == 422


def test_token_is_rejected_when_the_account_disappears_or_the_password_changes(world):
    u = world.user(ROLE)
    tok = world.token(u)
    ent = SPEC["entities"][SPEC["auth"]["entity"]]
    h = {"Authorization": f"Bearer {tok}"}
    probe = next(o for o in SPEC["operations"].values() if o["access"] == "authenticated" or (o["access"] == "restricted" and o["kind"] == "list"))
    ok = world.client.request(probe["endpoint"]["method"], probe["endpoint"]["path"], headers=h)
    assert ok.status_code != 401
    world.conn.execute(f'UPDATE "{ent["table"]}" SET password_hash = %s WHERE id = %s', ("scrypt$x$y$z$a$b", u["id"]))
    assert world.client.request(probe["endpoint"]["method"], probe["endpoint"]["path"], headers=h).status_code == 401, "a password change must invalidate old tokens"
    world.conn.execute(f'DELETE FROM "{ent["table"]}" WHERE id = %s', (u["id"],))
    assert world.client.request(probe["endpoint"]["method"], probe["endpoint"]["path"], headers=h).status_code == 401


@pytest.mark.skipif(not _has("auth.logout"), reason="no logout operation in the graph")
def test_logout_revokes_the_token(world):
    u = world.user(ROLE)
    h = world.headers(u)
    assert world.client.post(_path("auth.logout"), headers=h).status_code == 204
    r = world.client.post(_path("auth.logout"), headers=h)
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_REVOKED"
    # other tokens of the same user are unaffected
    assert world.client.post(_path("auth.login"), json={"email": u["email"], "password": PASSWORD}).status_code == 200


@pytest.mark.skipif(not (_has("auth.request_password_reset") and _has("auth.reset_password")), reason="no password reset in the graph")
def test_password_reset_flow_is_single_use_and_does_not_enumerate_accounts(world):
    u = world.user(ROLE)
    old_token = world.token(u)
    known = world.client.post(_path("auth.request_password_reset"), json={"email": u["email"]})
    unknown = world.client.post(_path("auth.request_password_reset"), json={"email": "ghost@example.com"})
    assert known.status_code == unknown.status_code == 204 and known.content == unknown.content == b""
    assert [m.to_address for m in world.email.outbox] == [u["email"]], "only the existing account gets a message"
    token = world.email.outbox[-1].token
    new_pw = "BrandNewPassw0rd!"
    r = world.client.post(_path("auth.reset_password"), json={"reset_token": token, "new_password": new_pw})
    assert r.status_code == 204, r.text
    assert world.client.post(_path("auth.login"), json={"email": u["email"], "password": PASSWORD}).status_code == 401
    assert world.client.post(_path("auth.login"), json={"email": u["email"], "password": new_pw}).status_code == 200
    again = world.client.post(_path("auth.reset_password"), json={"reset_token": token, "new_password": "AnotherPassw0rd!"})
    assert again.status_code == 400, "a reset token must be single-use"
    assert world.client.post(_path("auth.login"), json={"email": u["email"], "password": new_pw}).status_code == 200
    probe = next(o for o in SPEC["operations"].values() if o["kind"] == "auth.logout")
    assert world.client.post(probe["endpoint"]["path"], headers={"Authorization": f"Bearer {old_token}"}).status_code == 401, "tokens issued before the reset are dead"


@pytest.mark.skipif(not _has("auth.reset_password"), reason="no password reset in the graph")
def test_reset_rejects_bad_tokens_and_weak_passwords(world):
    for bad in ("garbage", "a.b.c", ""):
        r = world.client.post(_path("auth.reset_password"), json={"reset_token": bad or "x", "new_password": "LongEnough1!"})
        assert r.status_code in (400, 422), r.text
    assert r.json().get("error")
    u = world.user(ROLE)
    world.client.post(_path("auth.request_password_reset"), json={"email": u["email"]})
    token = world.email.outbox[-1].token
    assert world.client.post(_path("auth.reset_password"), json={"reset_token": token, "new_password": "short"}).status_code == 422
    access = world.token(u)
    r = world.client.post(_path("auth.reset_password"), json={"reset_token": access, "new_password": "LongEnough1!"})
    assert r.status_code == 400, "an access token must not work as a reset token"


def test_login_is_rate_limited_against_brute_force(world):
    u = world.user(ROLE)
    world.container.limiter.limit = 5
    codes = [world.client.post(_path("auth.login"), json={"email": u["email"], "password": f"guess-{i}"}).status_code for i in range(8)]
    assert codes.count(401) == 5 and codes[5:] == [429, 429, 429]
    r = world.client.post(_path("auth.login"), json={"email": u["email"], "password": PASSWORD})
    assert r.status_code == 429 and r.headers.get("retry-after") and r.json()["error"]["code"] == "RATE_LIMITED"
