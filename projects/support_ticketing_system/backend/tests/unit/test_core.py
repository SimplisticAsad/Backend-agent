"""Unit tests: state machines, rules, security primitives, error mapping, config, serialization. No database needed."""
from __future__ import annotations

import time
import uuid
from decimal import Decimal

import jwt
import psycopg
import psycopg_pool
import pytest

from backend_app.api_common import JsonDecimal
from backend_app.config import ConfigError, Settings
from backend_app.errors import (AppError, ConflictError, PermissionDenied, ServiceUnavailable, StateTransitionError, ValidationError, error_body,
                                translate_db_error)
from backend_app.rules import check_frozen, check_ownership, check_transition, check_transition_guards, scope_sql
from backend_app.security import RevocationList, TokenService, hash_password, password_fingerprint, verify_password
from backend_app.spec import SpecView
from pydantic import BaseModel
from tests.support.data import SPEC

VIEW = SpecView(SPEC)
SMS = list(SPEC["state_machines"].values()) or [None]
SETTINGS = Settings.from_env({"JWT_SECRET": "unit-test-secret-" + "x" * 32, "APP_ENV": "test"})


# ---- state machines --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sm", SMS, ids=lambda s: s["id"] if s else "none-defined")
def test_declared_transitions_work_for_their_roles(sm):
    if sm is None:
        return
    for t in sm["transitions"]:
        if t["roles"]:  # a transition with no roles can only be performed by the system, never through the API
            assert check_transition(VIEW, sm, t["from"], t["to"], frozenset(t["roles"]))["to"] == t["to"]


@pytest.mark.parametrize("sm", SMS, ids=lambda s: s["id"] if s else "none-defined")
def test_undeclared_transitions_are_rejected_with_allowed_list(sm):
    if sm is None:
        return
    declared = {(t["from"], t["to"]) for t in sm["transitions"]}
    all_roles = frozenset(VIEW.roles)
    checked = 0
    for a in sm["states"]:
        for b in sm["states"]:
            if (a, b) in declared:
                continue
            with pytest.raises(StateTransitionError) as ei:
                check_transition(VIEW, sm, a, b, all_roles)
            assert ei.value.status_code == 409 and ei.value.code == "INVALID_STATE_TRANSITION"
            assert set(ei.value.details["allowed"]) == {t["to"] for t in sm["transitions"] if t["from"] == a}
            checked += 1
    assert checked > 0


@pytest.mark.parametrize("sm", SMS, ids=lambda s: s["id"] if s else "none-defined")
def test_transition_requires_a_permitted_role(sm):
    if sm is None:
        return
    for t in sm["transitions"]:
        with pytest.raises(PermissionDenied) as ei:
            check_transition(VIEW, sm, t["from"], t["to"], frozenset({"role.nobody"}))
        assert ei.value.code == "TRANSITION_NOT_PERMITTED"


@pytest.mark.parametrize("sm", SMS, ids=lambda s: s["id"] if s else "none-defined")
def test_initial_state_and_states_match_the_entity_enum(sm):
    if sm is None:
        return
    col = next(c for c in SPEC["entities"][sm["entity"]]["columns"] if c["attr"] == sm["field"])
    assert set(sm["states"]) == set(col["enum"]) and sm["initial"] in sm["states"]


# ---- rules -----------------------------------------------------------------------------------------------------------
OWNERSHIP = [r for r in SPEC["rules"] if r["type"] == "ownership"]
FROZEN = [r for r in SPEC["rules"] if r["type"] == "frozen_state"]
GUARDS = [r for r in SPEC["rules"] if r["type"] == "transition_guard"]
SCOPES = [r for r in SPEC["rules"] if r["type"] == "row_scope"]


@pytest.mark.parametrize("rule", OWNERSHIP or [None], ids=lambda r: r["id"] if r else "none-defined")
def test_ownership_rule(rule):
    if rule is None:
        return
    me, other = uuid.uuid4(), uuid.uuid4()
    op = rule["operations"][0]
    non_exempt = frozenset(r for r in VIEW.roles if not (VIEW.role_ids_held(r) & set(rule["exempt_roles"])))
    exempt = frozenset(rule["exempt_roles"])
    check_ownership(VIEW, op, {rule["field"]: me}, me, non_exempt)  # the owner may act
    with pytest.raises(PermissionDenied) as ei:
        check_ownership(VIEW, op, {rule["field"]: other}, me, non_exempt)
    assert ei.value.code == rule["error_code"] and ei.value.status_code == 403
    if exempt:
        check_ownership(VIEW, op, {rule["field"]: other}, me, exempt)


@pytest.mark.parametrize("rule", FROZEN or [None], ids=lambda r: r["id"] if r else "none-defined")
def test_frozen_state_rule(rule):
    if rule is None:
        return
    op = rule["operations"][0]
    with pytest.raises(ConflictError) as ei:
        check_frozen(VIEW, op, {rule["field"]: rule["states"][0]})
    assert ei.value.code == rule["error_code"]
    check_frozen(VIEW, op, {rule["field"]: "some-other-state"})


@pytest.mark.parametrize("rule", GUARDS or [None], ids=lambda r: r["id"] if r else "none-defined")
def test_transition_guard_rule(rule):
    if rule is None:
        return
    op = rule["operations"][0]
    with pytest.raises(ConflictError) as ei:
        check_transition_guards(VIEW, op, {f: None for f in rule["require_fields_set"]}, rule["to"])
    assert ei.value.code == rule["error_code"]
    check_transition_guards(VIEW, op, {f: uuid.uuid4() for f in rule["require_fields_set"]}, rule["to"])
    check_transition_guards(VIEW, op, {f: None for f in rule["require_fields_set"]}, "__another_target__")


@pytest.mark.parametrize("rule", SCOPES or [None], ids=lambda r: r["id"] if r else "none-defined")
def test_scope_sql_binds_the_caller_as_a_parameter(rule):
    if rule is None:
        return
    uid = uuid.uuid4()
    frag, params = scope_sql(VIEW, rule["entity"], rule["scope"], uid)
    assert params == [uid]  # the caller id is a bound parameter, never interpolated
    assert "%s" in frag.as_string(None) if hasattr(frag, "as_string") else True


# ---- security primitives ------------------------------------------------------------------------------------------------
def test_password_hashing():
    h = hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert verify_password("correct horse", h) and not verify_password("wrong", h)
    assert hash_password("correct horse") != h  # salted
    assert not verify_password("x", "garbage") and not verify_password("x", "scrypt$1$2")


def test_access_token_roundtrip_and_rejections():
    ts = TokenService(SETTINGS, "proj")
    tok, ttl = ts.issue_access_token(str(uuid.uuid4()), "manager", "hash")
    claims = ts.decode_access_token(tok)
    assert claims.role == "manager" and claims.fingerprint == password_fingerprint("hash") and ttl == 3600
    with pytest.raises(jwt.PyJWTError):
        ts.decode_access_token(tok[:-3] + ("AAA" if tok[-3:] != "AAA" else "BBB"))  # tampered signature
    with pytest.raises(jwt.PyJWTError):
        ts.decode_access_token(jwt.encode({"sub": "x", "jti": "j", "iss": "proj", "aud": "access", "exp": int(time.time()) + 60}, None, algorithm="none"))  # alg=none
    with pytest.raises(jwt.PyJWTError):
        ts.decode_access_token(jwt.encode({"sub": "x", "jti": "j", "iss": "proj", "aud": "access", "exp": int(time.time()) - 5}, SETTINGS.jwt_secret, algorithm="HS256"))  # expired
    with pytest.raises(jwt.PyJWTError):
        ts.decode_access_token(jwt.encode({"sub": "x", "jti": "j", "iss": "proj", "aud": "access", "exp": int(time.time()) + 60}, "another-secret-" + "y" * 32, algorithm="HS256"))
    with pytest.raises(jwt.PyJWTError):
        TokenService(SETTINGS, "other-issuer").decode_access_token(tok)


def test_reset_and_access_tokens_are_not_interchangeable():
    ts = TokenService(SETTINGS, "proj")
    reset = ts.issue_reset_token(str(uuid.uuid4()), "hash")
    with pytest.raises(jwt.PyJWTError):
        ts.decode_access_token(reset)
    access, _ = ts.issue_access_token(str(uuid.uuid4()), "r", "hash")
    with pytest.raises(jwt.PyJWTError):
        ts.decode_reset_token(access)
    assert ts.decode_reset_token(reset).fingerprint == password_fingerprint("hash") != password_fingerprint("other")


def test_revocation_list():
    rl = RevocationList()
    assert not rl.is_revoked("a")
    rl.revoke("a", int(time.time()) + 60)
    assert rl.is_revoked("a")
    rl.revoke("b", int(time.time()) - 1)
    assert not rl.is_revoked("b")


# ---- error mapping ---------------------------------------------------------------------------------------------------------
CANARY = "LEAKCANARY secret-detail 10.0.0.5 SELECT * FROM users"


@pytest.mark.parametrize("exc,status,code", [
    (psycopg.errors.UniqueViolation(CANARY), 409, "DUPLICATE_VALUE"),
    (psycopg.errors.ForeignKeyViolation(CANARY), 422, "REFERENCE_NOT_FOUND"),
    (psycopg.errors.NotNullViolation(CANARY), 422, "REQUIRED_VALUE_MISSING"),
    (psycopg.errors.CheckViolation(CANARY), 422, "CONSTRAINT_VIOLATION"),
    (psycopg.errors.SerializationFailure(CANARY), 409, "CONCURRENT_UPDATE"),
    (psycopg.errors.DeadlockDetected(CANARY), 409, "CONCURRENT_UPDATE"),
    (psycopg.errors.QueryCanceled(CANARY), 503, "DATABASE_TIMEOUT"),
    (psycopg.OperationalError(CANARY), 503, "DATABASE_UNAVAILABLE"),
    (psycopg_pool.PoolTimeout(CANARY), 503, "DATABASE_BUSY"),
    (psycopg.errors.InvalidTextRepresentation(CANARY), 422, "INVALID_VALUE"),
    (psycopg.errors.SyntaxError(CANARY), 500, "DATABASE_ERROR"),
])
def test_database_errors_map_to_safe_application_errors(exc, status, code):
    e = translate_db_error(exc)
    assert isinstance(e, AppError) and e.status_code == status and e.code == code
    text = (e.message + str(e.details)).lower()
    for leak in ("leakcanary", "secret-detail", "10.0.0.5", "select", "psycopg", "postgres"):
        assert leak not in text, f"error message leaks driver text '{leak}': {e.message}"


def test_delete_of_referenced_row_is_a_conflict():
    e = translate_db_error(psycopg.errors.ForeignKeyViolation("x"), deleting=True)
    assert e.status_code == 409 and e.code == "ENTITY_IN_USE"


def test_error_body_has_both_contracts():
    b = error_body("X_CODE", "msg", {"a": 1}, "rid-1", {"f": "bad"})
    assert b["error"] == {"code": "X_CODE", "message": "msg", "details": {"a": 1}}
    assert b["message"] == "msg" and b["errors"] == {"f": "bad"} and b["request_id"] == "rid-1"


# ---- config / serialization ----------------------------------------------------------------------------------------------------
def test_production_requires_strong_secret_and_explicit_origins():
    with pytest.raises(ConfigError):
        Settings.from_env({"APP_ENV": "production"}).validate()
    with pytest.raises(ConfigError):
        Settings.from_env({"APP_ENV": "production", "JWT_SECRET": "short"}).validate()
    with pytest.raises(ConfigError):
        Settings.from_env({"APP_ENV": "production", "JWT_SECRET": "s" * 40, "CORS_ORIGINS": "*"}).validate()
    Settings.from_env({"APP_ENV": "production", "JWT_SECRET": "s" * 40, "CORS_ORIGINS": "https://app.example.com"}).validate()


def test_bad_schema_name_is_refused():
    with pytest.raises(ConfigError):
        Settings.from_env({"JWT_SECRET": "s" * 40, "DATABASE_SCHEMA": "x; DROP TABLE users"}).validate()


def test_decimals_serialise_as_json_numbers():
    class M(BaseModel):
        price: JsonDecimal

    assert M(price=Decimal("10.50")).model_dump_json() == '{"price":10.5}'
