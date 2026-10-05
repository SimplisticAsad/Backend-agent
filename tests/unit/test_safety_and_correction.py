"""Handler/test AST checks, guarded patch application, failure classification, the bounded correction loop, observability, DB safety."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.errors import ErrorKind
from app.generation.handler_checks import check_handler_body, check_test_module
from app.observability import StageLog, scrub, scrub_obj
from app.config.settings import redact_url
from app.pipeline.correction import Attempt, correction_loop
from app.pipeline.failures import Failure, choose_prompt, classify
from app.pipeline.patch import Edit, apply_edits
from app.testing.pg_cluster import UnsafeDatabase, check_safe_test_url

GOOD = textwrap.dedent('''
    with self.db.transaction() as conn:
        row = conn.execute('SELECT id FROM "orders" WHERE id = %s FOR UPDATE', (row_id,)).fetchone()
        if row is None:
            raise EntityNotFound("x", code="X")
        conn.execute('UPDATE "orders" SET total = total + %s WHERE id = %s', (1, row_id))
    return row
''')


# ---- handler checks ------------------------------------------------------------------------------------------------------------------
def test_a_well_formed_handler_is_accepted():
    assert check_handler_body(GOOD, allowed_tables={"orders"}) == []


@pytest.mark.parametrize("code,fragment", [
    ('with self.db.transaction() as conn:\n    conn.execute(f"SELECT * FROM t WHERE id = {row_id}")\nreturn 1', "string LITERAL"),
    ('with self.db.transaction() as conn:\n    conn.execute("SELECT * FROM t WHERE id = " + str(row_id))\nreturn 1', "string LITERAL"),
    ('with self.db.transaction() as conn:\n    conn.execute("SELECT %s".format(1))\nreturn 1', "string LITERAL"),
    ('with self.db.transaction() as conn:\n    conn.execute("DROP TABLE users")\nreturn 1', "forbidden SQL keyword"),
    ('with self.db.transaction() as conn:\n    conn.execute("COMMIT")\nreturn 1', "forbidden SQL keyword"),
    ('import os\nreturn os.listdir(".")', "imports are not allowed"),
    ('return eval("1+1")', "eval"),
    ('x = open("/etc/passwd").read()\nreturn x', "open"),
    ('try:\n    return 1\nexcept:\n    pass', "bare 'except:'"),
    ('try:\n    return 1\nexcept Exception:\n    pass', "Exception"),
    ('conn = self.db.pool\nconn.execute("SELECT 1")\nreturn 1', "inside `with self.db.transaction"),
    ('with self.db.transaction() as conn:\n    conn.execute("SELECT * FROM secrets")\nreturn 1', "unknown table"),
    ('x = principal.__class__\nreturn x', "dunder"),
    ('def broken(:\n    pass', "does not parse"),
])
def test_unsafe_handlers_are_rejected(code, fragment):
    problems = check_handler_body(code, allowed_tables={"orders"})
    assert problems and any(fragment in p for p in problems), problems


@pytest.mark.parametrize("src,fragment", [
    ("import os\ndef test_x(world):\n    assert 1", "import of 'os'"),
    ("import pytest\n@pytest.mark.skip\ndef test_x(world):\n    assert 1", "skip"),
    ("def test_x(world):\n    assert True", "assert True"),
    ("def test_x(world):\n    x = 1", "no assertion"),
    ("def test_x(world):\n    try:\n        assert 0\n    except Exception:\n        pass", "swallow"),
    ("def helper(world):\n    assert 1", "no test_ function"),
])
def test_unsafe_custom_tests_are_rejected(src, fragment):
    assert any(fragment in p for p in check_test_module(src))


def test_a_test_may_delegate_its_assertions_to_a_helper():
    assert check_test_module("def _h(world):\n    assert world\n\ndef test_a(world):\n    _h(world)\n") == []


# ---- patch application ----------------------------------------------------------------------------------------------------------------------
@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "backend_app").mkdir()
    (tmp_path / "backend_app" / "generated").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "backend_app" / "rules.py").write_text("def check(x):\n    if x != 1:\n        raise PermissionDenied('no')\n    return x\n", encoding="utf-8")
    (tmp_path / "backend_app" / "generated" / "spec.json").write_text("{}", encoding="utf-8")
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    return tmp_path


def _apply(tree, path, search, replace):
    return apply_edits(tree, [Edit(path, search, replace)])[0]


def test_a_valid_edit_is_applied(tree):
    r = _apply(tree, "backend_app/rules.py", "return x", "return x + 0")
    assert r.applied and "x + 0" in (tree / "backend_app/rules.py").read_text()


@pytest.mark.parametrize("path,search,replace,fragment", [
    ("tests/test_a.py", "assert 1", "assert 2", "inside backend_app/"),
    ("backend_app/generated/spec.json", "{}", '{"a": 1}', "protected"),
    ("../outside.py", "x", "y", "inside backend_app/"),
    ("backend_app/rules.py", "nothing like this", "y", "occurs 0 times"),
    ("backend_app/rules.py", "x", "y", "occurs 3 times"),
    ("backend_app/rules.py", "        raise PermissionDenied('no')", "        pass", "weaken"),
    ("backend_app/rules.py", "return x", "try:\n        return x\n    except:\n        return None", "bare 'except:'"),
    ("backend_app/rules.py", "return x", "return eval('x')", "eval/exec"),
    ("backend_app/rules.py", "return x", "return x +", "does not compile"),
    ("backend_app/missing.py", "a", "b", "does not exist"),
])
def test_dangerous_or_invalid_edits_are_refused(tree, path, search, replace, fragment):
    before = (tree / "backend_app/rules.py").read_text()
    r = _apply(tree, path, search, replace)
    assert not r.applied and fragment in r.reason, r
    assert (tree / "backend_app/rules.py").read_text() == before, "a refused edit must leave the file untouched"


def test_edits_cannot_introduce_string_built_sql_or_wildcard_cors(tree):
    for bad in ('conn.execute(f"SELECT {x}")', 'conn.execute("SELECT %s" % x)', 'allow_origins=["*"]', "subprocess.run(x)"):
        r = _apply(tree, "backend_app/rules.py", "return x", f"{bad}\n    return x")
        assert not r.applied, bad


# ---- classification -> correction prompt ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("suite,text,kind", [
    ("unit", "ModuleNotFoundError: No module named 'x'", ErrorKind.IMPORT_ERROR),
    ("api", "AttributeError: 'NoneType' object has no attribute 'id'", ErrorKind.TYPE_ERROR),
    ("repository", 'psycopg.errors.UndefinedTable: relation "tasks" does not exist', ErrorKind.DATABASE_ERROR),
    ("api", "assert 409 == 200 INVALID_STATE_TRANSITION", ErrorKind.STATE_TRANSITION_ERROR),
    ("contract", "AssertionError: POST /projects is not exposed", ErrorKind.API_CONTRACT_ERROR),
    ("frontend_compat", "AssertionError: client cannot read message", ErrorKind.FRONTEND_CONTRACT_ERROR),
    ("security", "assert 200 == 403 user B acted on user A's row (IDOR)", ErrorKind.AUTHORIZATION_ERROR),
    ("security", "payload was not stored literally: injection", ErrorKind.SECURITY_FAILURE),
    ("api", "AssertionError: expected 201, got 500", ErrorKind.INTEGRATION_FAILURE),
    ("unit", "RuntimeError: boom", ErrorKind.RUNTIME_ERROR),
    ("api", "pydantic_core ValidationError: 1 validation error", ErrorKind.VALIDATION_ERROR),
])
def test_failure_classification(suite, text, kind):
    assert classify(suite, "tests/x.py::t", text) == kind


def test_each_error_kind_selects_its_own_correction_prompt():
    f = lambda k: Failure("s", "n", "m", kind=k)  # noqa: E731
    assert choose_prompt([f(ErrorKind.TYPE_ERROR)]) == "runtime_correction"
    assert choose_prompt([f(ErrorKind.DATABASE_ERROR)]) == "database_error_correction"
    assert choose_prompt([f(ErrorKind.API_CONTRACT_ERROR)]) == "api_contract_correction"
    assert choose_prompt([f(ErrorKind.FRONTEND_CONTRACT_ERROR)]) == "api_contract_correction"
    assert choose_prompt([f(ErrorKind.SECURITY_FAILURE)]) == "security_correction"
    assert choose_prompt([f(ErrorKind.TYPE_ERROR), f(ErrorKind.DATABASE_ERROR), f(ErrorKind.AUTHORIZATION_ERROR)]) == "security_correction", "security wins"


# ---- the correction loop ---------------------------------------------------------------------------------------------------------------------------
def _loop(results, applied, max_attempts=3):
    state = {"runs": 0, "corrections": 0}

    def run():
        r = results[min(state["runs"], len(results) - 1)]
        state["runs"] += 1
        return r

    def correct(r, n):
        state["corrections"] += 1
        return Attempt(n, "runtime_correction", "d", 1, applied, [], r)

    out = correction_loop(max_attempts=max_attempts, run_checks=run, is_ok=lambda r: r == 0, count_failures=lambda r: r, correct=correct)
    return out, state


def test_loop_stops_immediately_when_everything_passes():
    out, st = _loop([0], 1)
    assert out.ok and st == {"runs": 1, "corrections": 0}


def test_failure_then_correction_then_pass():
    out, st = _loop([3, 0], 1)
    assert out.ok and out.correction_attempts == 1 and st["runs"] == 2 and out.attempts[0].failures_after == 0


def test_repeated_failures_terminate_after_the_configured_maximum():
    """failure -> failure -> failure -> failure must stop: initial run + 3 corrections, never more."""
    out, st = _loop([3, 3, 3, 3, 3, 3], 1, max_attempts=3)
    assert not out.ok and st == {"runs": 4, "corrections": 3} and "maximum of 3" in out.stopped_because
    out0, st0 = _loop([3, 3], 1, max_attempts=0)
    assert not out0.ok and st0 == {"runs": 1, "corrections": 0}


def test_loop_stops_early_when_no_correction_can_be_applied():
    out, st = _loop([3, 3, 3], 0, max_attempts=3)
    assert not out.ok and st["corrections"] == 1 and "no applicable correction" in out.stopped_because


# ---- observability & safety ------------------------------------------------------------------------------------------------------------------------
def test_secrets_are_scrubbed_everywhere():
    assert "hunter2" not in scrub("postgresql://u:hunter2@h/db") and "***" in scrub("Authorization: Bearer abc.def.ghi")
    assert "tok123" not in scrub("password=tok123 token: tok123")
    d = scrub_obj({"password": "x", "nested": {"api_key": "k", "msg": "postgres://u:pw@h/d"}, "ok": "fine"})
    assert d["password"] == "***" and d["nested"]["api_key"] == "***" and "pw@" not in d["nested"]["msg"] and d["ok"] == "fine"
    assert redact_url("postgresql://agent:s3cret@localhost:5432/x") == "postgresql://agent:***@localhost:5432/x"


def test_stage_log_records_status_duration_errors_and_artifacts(tmp_path):
    log = StageLog(tmp_path / "stages.jsonl")
    with log.stage("good") as st:
        st.artifacts.append("a.json")
    with pytest.raises(RuntimeError):
        with log.stage("bad"):
            raise RuntimeError("boom password=hunter2")
    recs = log.summary()
    assert [r["status"] for r in recs] == ["passed", "failed"] and recs[0]["artifacts"] == ["a.json"] and recs[0]["duration_ms"] >= 0
    assert "hunter2" not in (tmp_path / "stages.jsonl").read_text()


@pytest.mark.parametrize("url,ok", [
    ("postgresql://u@localhost:5432/backend_agent_test", True), ("postgresql://u@127.0.0.1/app_test_db", True),
    ("postgresql://u@localhost/production", False), ("postgresql://u@db.prod.example.com/app_test", False), ("postgresql://u@localhost/database_agent", False),
])
def test_destructive_tests_refuse_non_dedicated_databases(url, ok):
    if ok:
        check_safe_test_url(url, {})
    else:
        with pytest.raises(UnsafeDatabase):
            check_safe_test_url(url, {})


def test_explicit_confirmation_allows_a_named_database():
    check_safe_test_url("postgresql://u@localhost/database_agent", {"BACKEND_AGENT_TEST_DB_CONFIRM": "database_agent"})
