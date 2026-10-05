"""End-to-end pipeline against a REAL PostgreSQL (TEST_DATABASE_URL or an auto-provisioned throw-away cluster), with the mock LLM.

Covers, per fixture: Graph -> Backend -> Database -> API -> Frontend contract. And the correction loop with real injected defects:
generation -> test failure -> diagnosis -> correction -> tests pass, plus a defect that never converges (must stop at the maximum).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import app.pipeline.orchestrator as orch
from app.config.settings import Settings
from app.llm.mock import MockLLMProvider
from app.pipeline.orchestrator import BackendPipeline, Options
from tests.conftest import FIXTURES, PROJECTS, read_json

pytestmark = pytest.mark.integration


def _copy(name: str, dst: Path) -> Path:
    for sub in ("graphs", "validation", "database", "frontend"):
        if (PROJECTS / name / sub).exists():
            shutil.copytree(PROJECTS / name / sub, dst / sub)
    return dst


@pytest.mark.parametrize("name", FIXTURES)
def test_full_pipeline_per_fixture(pg_url, tmp_path, name):
    p = _copy(name, tmp_path / name)
    res = BackendPipeline(Options(project=p, mock=True, run_tests=True, require_database=True, max_corrections=3), Settings(), MockLLMProvider()).run()
    rep = res.report
    assert res.exit_code == 0 and res.status in ("passed", "passed_with_conflicts"), json.dumps({k: rep[k] for k in rep if k.endswith("_tests") or k in ("status", "warnings")}, indent=1)[:3000]
    # Graph -> Backend -> Database -> API -> Frontend contract
    assert rep["graph_validation"] == "passed" and rep["database_contract"] == "passed" and rep["api_contract"] == "passed" and rep["type_checks"] == "passed"
    assert rep["frontend_contract"] == ("passed_with_conflicts" if name == "task_manager" else "not_available")
    for k in ("unit_tests", "contract_tests", "database_tests", "api_tests", "security_tests", "bdd_tests"):
        assert rep[k] in ("passed", "passed_with_known_gaps"), (k, rep["suites"])
    assert rep["correction_attempts"] == 0
    if name == "task_manager":
        assert rep["frontend_compat_tests"] == "passed" and rep["integration_tests"] == "passed"
    assert all(s["failed"] == 0 and s["errors"] == 0 for s in rep["suites"].values())
    assert sum(s["passed"] for s in rep["suites"].values()) > 150, "the generated suites really ran"
    assert (p / "backend" / "openapi.json").exists() and (p / "artifacts" / "backend_validation_report.json").exists()
    # custom operations were implemented by validated handlers, with their own tests
    if name == "ecommerce_store":
        assert (p / "backend" / "tests" / "api" / "test_custom_place_order.py").exists() and rep["unimplemented_operations"] == []
    if name == "support_ticketing_system":
        assert [u["operation"] for u in rep["unimplemented_operations"]] == ["operation.ticket.stats"] and rep["suites"]["bdd"]["known_gaps"] == 1 or rep["suites"]["bdd"]["known_gap_tests"]


# ---- correction loop with injected defects -----------------------------------------------------------------------------------------------------
class Defect:
    def __init__(self, rel: str, good: str, bad: str):
        self.rel, self.good, self.bad = rel, good, bad

    def inject(self, backend: Path) -> None:
        f = backend / self.rel
        text = f.read_text(encoding="utf-8")
        assert text.count(self.good) == 1, f"cannot inject: {self.good!r} not found once in {self.rel}"
        f.write_text(text.replace(self.good, self.bad), encoding="utf-8")


def _run(monkeypatch, tmp_path, defect: Defect, scripted: dict, suites, max_corrections=3, name="task_manager"):
    p = _copy(name, tmp_path / name)
    original = orch.generate_backend

    def with_defect(spec, out, **kw):
        records = original(spec, out, **kw)
        defect.inject(Path(out))
        return records

    monkeypatch.setattr(orch, "generate_backend", with_defect)
    monkeypatch.setattr(orch, "SUITES", suites)
    provider = MockLLMProvider(scripted=scripted)
    res = BackendPipeline(Options(project=p, mock=True, run_tests=True, require_database=True, max_corrections=max_corrections), Settings(), provider).run()
    return res, provider, p


def _fix(path: str, search: str, replace: str, diagnosis="found the defect") -> str:
    return json.dumps({"diagnosis": diagnosis, "edits": [{"path": path, "search": search, "replace": replace}]})


def test_correction_api_authorization_failure_is_fixed(pg_url, monkeypatch, tmp_path):
    """generation -> API/security test failure -> diagnosis -> correction -> test passes"""
    d = Defect("backend_app/rules.py", '        if row.get(rule["field"]) != user_id:', "        if False:")
    res, provider, p = _run(monkeypatch, tmp_path, d, {"security_correction": _fix("backend_app/rules.py", "        if False:", '        if row.get(rule["field"]) != user_id:')},
                            [("security", "tests/security", True)])
    assert res.status in ("passed", "passed_with_conflicts"), res.report["suites"]["security"]["failures"][:2]
    assert res.report["correction_attempts"] == 1 and res.report["security_tests"] == "passed"
    corr = read_json(p / "artifacts" / "corrections.json")
    assert corr["attempts"][0]["prompt"] == "security_correction" and corr["attempts"][0]["edits_applied"] == 1 and corr["attempts"][0]["failures_before"] > 0 and corr["attempts"][0]["failures_after"] == 0
    assert 'if row.get(rule["field"]) != user_id' in (p / "backend" / "backend_app" / "rules.py").read_text()
    assert provider.prompt_ids().count("security_correction") == 1


def test_correction_database_error_is_fixed(pg_url, monkeypatch, tmp_path):
    """generation -> database error -> correction -> database tests pass"""
    d = Defect("backend_app/repository.py", 'sql.SQL("SELECT {cols} FROM {t} WHERE id = %s")', 'sql.SQL("SELECT {cols} FROM {t} WHERE idd = %s")')
    res, provider, p = _run(monkeypatch, tmp_path, d, {"database_error_correction": _fix("backend_app/repository.py", "WHERE idd = %s", "WHERE id = %s")},
                            [("repository", "tests/repository", True)])
    assert res.status in ("passed", "passed_with_conflicts") and res.report["correction_attempts"] == 1
    assert read_json(p / "artifacts" / "corrections.json")["attempts"][0]["prompt"] == "database_error_correction"
    assert "idd" not in (p / "backend" / "backend_app" / "repository.py").read_text()


def test_correction_frontend_contract_mismatch_is_fixed(pg_url, monkeypatch, tmp_path):
    """generation -> frontend contract mismatch -> correction -> contract test passes"""
    d = Defect("backend_app/errors.py", '"details": details or {}}, "message": message}', '"details": details or {}}}')
    res, provider, p = _run(monkeypatch, tmp_path, d, {"api_contract_correction": _fix("backend_app/errors.py", '"details": details or {}}}', '"details": details or {}}, "message": message}')},
                            [("unit", "tests/unit", False), ("frontend_compat", "tests/frontend_compat", True)])
    assert res.status in ("passed", "passed_with_conflicts") and res.report["correction_attempts"] == 1
    assert read_json(p / "artifacts" / "corrections.json")["attempts"][0]["prompt"] == "api_contract_correction"
    assert res.report["frontend_compat_tests"] == "passed"


def test_a_defect_that_never_converges_stops_at_the_maximum(pg_url, monkeypatch, tmp_path):
    """failure -> failure -> failure -> failure: terminates after the configured maximum (3 corrections), never loops."""
    d = Defect("backend_app/rules.py", '        if row.get(rule["field"]) != user_id:', "        if False:")
    useless = [_fix("backend_app/rules.py", "        if False:" + (f"  # try {i}" if i else ""), f"        if False:  # try {i + 1}") for i in range(5)]
    res, provider, p = _run(monkeypatch, tmp_path, d, {"security_correction": useless}, [("security", "tests/security", True)], max_corrections=3)
    assert res.status == "failed" and res.exit_code == 1
    assert res.report["correction_attempts"] == 3 and provider.prompt_ids().count("security_correction") == 3
    assert "maximum of 3" in res.report["correction_stopped_because"]
    assert res.report["security_tests"] == "failed" and res.report["suites"]["security"]["failures"]


def test_unsafe_corrections_are_refused_and_the_loop_stops(pg_url, monkeypatch, tmp_path):
    """The LLM tries to 'fix' the failure by deleting the ownership check: refused. It must not cheat the tests."""
    d = Defect("backend_app/rules.py", '        if row.get(rule["field"]) != user_id:\n            raise PermissionDenied(rule["message"], code=rule["error_code"])',
               '        if row.get(rule["field"]) != user_id and False:\n            raise PermissionDenied(rule["message"], code=rule["error_code"])')
    cheat = _fix("backend_app/rules.py", 'if row.get(rule["field"]) != user_id and False:\n            raise PermissionDenied(rule["message"], code=rule["error_code"])', "pass")
    res, provider, p = _run(monkeypatch, tmp_path, d, {"security_correction": cheat}, [("security", "tests/security", True)])
    assert res.status == "failed"
    att = read_json(p / "artifacts" / "corrections.json")["attempts"][0]
    assert att["edits_applied"] == 0 and any("weaken" in r for r in att["rejected"])
    assert "and False" in (p / "backend" / "backend_app" / "rules.py").read_text(), "the refused edit must not have touched the file"


def test_tests_are_never_edited_by_corrections(pg_url, monkeypatch, tmp_path):
    d = Defect("backend_app/rules.py", '        if row.get(rule["field"]) != user_id:', "        if False:")
    weaken = _fix("tests/security/test_security.py", "assert r.status_code in expected", "assert True")
    res, provider, p = _run(monkeypatch, tmp_path, d, {"security_correction": weaken}, [("security", "tests/security", True)])
    att = read_json(p / "artifacts" / "corrections.json")["attempts"][0]
    assert att["edits_applied"] == 0 and any("inside backend_app/" in r for r in att["rejected"])
    assert "assert True" not in (p / "backend" / "tests" / "security" / "test_security.py").read_text()
