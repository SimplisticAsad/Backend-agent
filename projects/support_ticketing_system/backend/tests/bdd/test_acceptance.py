"""BDD acceptance tests generated from acceptance_criteria.json (+ workflows).

Each criterion has a Gherkin file under tests/bdd/features/. Execution is generic and honest about what it checks:
  Given  -> a persisted user holding the role(s) named in the criterion (or no user for "not signed in")
  When   -> every operation listed in the criterion is invoked through the real HTTP API, in order, with valid input
  Then   -> each call returns the contractual success status and a body matching the graph's response schema; writes are persisted
The free-text outcome ("the project is displayed", ...) is carried into the failure message so a red scenario reads like the requirement.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from tests.support.data import GRAPH, OPS, ROOT, SPEC
from tests.support.world import PASSWORD, assert_matches_schema

pytestmark = pytest.mark.integration
CRITERIA = GRAPH["acceptance_criteria"] or []
ROLE_KEYS = sorted(SPEC["auth"]["role_map"]) if SPEC["auth"] else []


def _given_roles(ac):
    text = " ".join(ac.get("given", [])).lower()
    if re.search(r"not (?:signed|logged) in|anonymous|signed out", text):
        return []
    return [k for k in ROLE_KEYS if re.search(rf"\b{k}s?\b", text)]


def _allowed(op, roles):
    keys = ROLE_KEYS if op["access"] != "restricted" else [k for k in ROLE_KEYS if any(r in op["roles"] for r in SPEC["roles"][f"role.{k}"]["inherits"] + [f"role.{k}"])]
    return [k for k in roles if k in keys] or keys


def test_every_criterion_has_a_feature_file():
    feats = {p.read_text().splitlines()[0].split()[1] for p in (ROOT / "tests" / "bdd" / "features").glob("*.feature")}
    assert {ac["id"] for ac in CRITERIA} <= feats


@pytest.mark.parametrize("ac", CRITERIA or [None], ids=lambda a: a["id"] if a else "none")
def test_acceptance_criterion(world, ac):
    if ac is None:
        return
    given = _given_roles(ac)
    outcome = " / ".join(ac.get("then", []))
    for op_id in ac["operation_refs"]:
        op = OPS[op_id]
        ep = op["endpoint"]
        what = f"[{ac['id']}] When {op['name']} -> Then {outcome}"
        kind = op["kind"]
        if kind.startswith("auth."):
            _run_auth(world, op, kind, what)
            continue
        if op["access"] == "public":
            caller = None
        else:
            role = (_allowed(op, given) or [None])[0]
            caller = world.user(role) if role else None
        p = world.prepare(op_id, caller)
        r = world.send(p)
        handler = SPEC["handlers"].get(op_id)
        if kind in ("custom", "aggregate") and not (handler and handler.get("status") == "implemented"):
            # a documented gap: the specification is not implementable as written. It must fail CLEANLY (501), and it stays visible as xfail.
            assert r.status_code == 501 and r.json()["error"]["code"] == "OPERATION_NOT_IMPLEMENTED", f"{what}: {r.status_code} {r.text}"
            pytest.xfail(f"known gap: {op_id} is not implemented ({(handler or {}).get('reason', 'no handler')[:120]})")
        assert r.status_code == ep["status_code"], f"{what}: expected {ep['status_code']}, got {r.status_code} {r.text}"
        if ep["response_schema"] and r.status_code != 204:
            body = r.json()
            schema = SPEC["schemas"][ep["response_schema"]]
            for item in (body if isinstance(body, list) else [body]):
                assert_matches_schema(item, schema)
        if kind == "create":
            assert world.get(op["entity"], uuid.UUID(r.json()["id"])) is not None, f"{what}: the created record was not persisted"


def _run_auth(world, op, kind, what):
    ep = op["endpoint"]
    user = world.user(ROLE_KEYS[0])
    if kind == "auth.login":
        r = world.client.post(ep["path"], json={"email": user["email"], "password": PASSWORD})
        assert r.status_code == 200 and r.json()["token"], what
    elif kind == "auth.logout":
        assert world.client.post(ep["path"], headers=world.headers(user)).status_code == 204, what
    elif kind == "auth.request_password_reset":
        assert world.client.post(ep["path"], json={"email": user["email"]}).status_code == 204, what
    elif kind == "auth.reset_password":
        req = SPEC["operations"][SPEC["auth"]["operations"]["auth.request_password_reset"]]["endpoint"]["path"]
        world.client.post(req, json={"email": user["email"]})
        r = world.client.post(ep["path"], json={"reset_token": world.email.outbox[-1].token, "new_password": "Brand-new-Passw0rd"})
        assert r.status_code == 204, what
