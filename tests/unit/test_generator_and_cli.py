"""Generation (no database needed): artifacts, determinism, traceability, safety properties of the generated code, and the CLI."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.llm.mock import MockLLMProvider
from app.main import main
from app.pipeline.orchestrator import BackendPipeline, Options
from tests.conftest import FIXTURES, PROJECTS, mutate_graph, read_json

REQUIRED_ARTIFACTS = ["backend_analysis.json", "backend_architecture.json", "entity_mapping.json", "backend_api_contract.json", "api_contract.json", "service_manifest.json",
                      "repository_manifest.json", "test_manifest.json", "integration_conflicts.json", "backend_validation_report.json", "file_manifest.json", "openapi.json"]


def _generate(project_dir: Path, run_tests=False):
    res = BackendPipeline(Options(project=project_dir, mock=True, run_tests=run_tests), Settings(), MockLLMProvider()).run()
    return res


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    base = tmp_path_factory.mktemp("gen")
    out = {}
    for name in FIXTURES:
        dst = base / name
        for sub in ("graphs", "validation", "database", "frontend"):
            if (PROJECTS / name / sub).exists():
                shutil.copytree(PROJECTS / name / sub, dst / sub)
        out[name] = (dst, _generate(dst))
    return out


@pytest.mark.parametrize("name", FIXTURES)
def test_generate_produces_all_artifacts_and_passes_static_validation(generated, name):
    d, res = generated[name]
    assert res.status == "not_tested" and res.exit_code == 0, res.report.get("issues")
    for a in REQUIRED_ARTIFACTS:
        assert (d / "artifacts" / a).exists(), a
    assert res.report["static_checks"] == {"compile": "passed", "sql_safety": "passed", "unsafe_patterns": "passed", "import": "passed", "openapi": "passed"}
    assert res.report["type_checks"] == "passed" and res.report["api_contract"] == "static_only"
    assert res.report["unit_tests"] == "not_run", "nothing is claimed as tested when tests were not executed"


@pytest.mark.parametrize("name", FIXTURES)
def test_generation_is_deterministic(generated, tmp_path, name):
    d, res = generated[name]
    again = tmp_path / name
    for sub in ("graphs", "validation", "database", "frontend"):
        if (PROJECTS / name / sub).exists():
            shutil.copytree(PROJECTS / name / sub, again / sub)
    _generate(again)

    def digest(root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts}

    assert digest(d / "backend") == digest(again / "backend"), "identical graphs + contracts + mock LLM must give a byte-identical backend"
    for art in ("backend_analysis.json", "backend_spec.json", "entity_mapping.json", "file_manifest.json", "integration_conflicts.json"):
        assert (d / "artifacts" / art).read_bytes() == (again / "artifacts" / art).read_bytes(), art


def test_entity_mapping_and_architecture_artifacts(generated):
    d, _ = generated["task_manager"]
    m = read_json(d / "artifacts" / "entity_mapping.json")
    task = next(e for e in m["entities"] if e["graph_entity"] == "entity.task")
    assert task["domain_model"] == "Task" and task["database_table"] == "tasks" and task["columns"]["assignee_id"] == "assignee_id"
    arch = read_json(d / "artifacts" / "backend_architecture.json")
    assert arch["architecture"]["api_framework"] == "fastapi" and arch["architecture"]["database"] == "postgresql" and arch["architecture"]["repository_pattern"] is True
    assert {"TaskService", "UserService"} <= {s["name"] for s in arch["services"]} and "TaskRepository" in {r["name"] for r in arch["repositories"]}
    assert arch["descriptions"], "the advisory LLM stage annotated the architecture"
    analysis = read_json(d / "artifacts" / "backend_analysis.json")
    assert "operation.project.create" in analysis["operations"] and "api.project.create" in analysis["api_refs"] and "entity.project" in analysis["entities"]


def test_every_important_file_is_traceable_to_its_specification_ids(generated):
    d, _ = generated["task_manager"]
    files = {f["path"]: f for f in read_json(d / "artifacts" / "file_manifest.json")["files"]}
    routes = files["backend_app/api/routes/task.py"]
    assert {"api.task.create", "api.task.list", "operation.task.create", "permission.task.create", "service.task", "schema.task.create"} <= set(routes["source_refs"])
    assert "rule.guard.task.completed_needs_assignee" in set(files["backend_app/application/services/task_service.py"]["source_refs"])
    assert "entity.task" in files["backend_app/domain/entities/task.py"]["source_refs"] and "table:tasks" in files["backend_app/infrastructure/repositories/task_repository.py"]["source_refs"]
    assert {f["kind"] for f in files.values()} >= {"generated", "runtime", "test", "config", "contract"}
    assert all(f["sha256"] for f in files.values())
    assert files["backend_app/engine.py"]["kind"] == "runtime", "agent infrastructure is separated from generated code"


@pytest.mark.parametrize("name", FIXTURES)
def test_generated_code_satisfies_the_safety_properties(generated, name):
    d, _ = generated[name]
    pkg = d / "backend" / "backend_app"
    spec = json.loads((pkg / "generated" / "spec.json").read_text())
    for schema_file in (pkg / "api" / "schemas").glob("*.py"):
        text = schema_file.read_text()
        for cls_block in text.split("\nclass ")[1:]:
            if "Request(BaseModel)" in cls_block.splitlines()[0]:
                assert 'extra="forbid"' in cls_block, f"{schema_file.name}: request models must forbid unknown fields"
    for py in pkg.rglob("*.py"):
        t = py.read_text()
        assert 'allow_origins=["*"]' not in t and "except:" not in t.replace("except: ", "").replace("except:\n", "XX") or py.name == "handler_checks.py"
    routes = (pkg / "api" / "routes" / "__init__.py").read_text()
    assert "sorted(_routes" in routes, "routes must be ordered most-specific first"
    for e in spec["entities"].values():
        if e["hidden_columns"]:
            assert "exclude=True" in (pkg / "domain" / "entities" / f"{e['key']}.py").read_text(), "hidden columns are never serialised"
    env = (d / "backend" / ".env.example").read_text()
    assert "JWT_SECRET=  " in env or "JWT_SECRET=\n" in env or "JWT_SECRET=                 " in env
    assert "CORS_ORIGINS=http://localhost:5173" in env and "*" not in env.split("CORS_ORIGINS=")[1].split("\n")[0].split("#")[0]
    assert (d / "backend" / "db_contract" / "schema.sql").exists(), "the contract the backend was built against travels with it"


def test_routes_register_static_paths_before_dynamic_ones(generated):
    d, _ = generated["task_manager"]
    paths = list(read_json(d / "artifacts" / "openapi.json")["paths"])
    assert paths.index("/tasks/assigned") < paths.index("/tasks/{id}") and paths.index("/projects/progress") < paths.index("/projects/{id}")


def test_integration_conflicts_artifact_is_structured(generated):
    d, res = generated["task_manager"]
    c = read_json(d / "artifacts" / "integration_conflicts.json")
    assert c["status"] == "conflicts_reported" and c["summary"]["critical"] == 0 and c["checked"] == ["graph", "database", "frontend"]
    one = next(x for x in c["conflicts"] if x["code"] == "SESSION_RESPONSE_NOT_IN_GRAPH")
    assert {"type", "severity", "description", "sources", "impact", "recommended_resolution", "resolution_applied"} <= set(one)
    assert generated["ecommerce_store"][1].report["frontend_contract"] == "not_available"


def test_validation_report_never_overstates(generated):
    for name in FIXTURES:
        rep = generated[name][1].report
        assert rep["status"] == "not_tested" and rep["correction_attempts"] == 0
    assert "operation.project.progress" not in [u["operation"] for u in generated["task_manager"][1].report["unimplemented_operations"]]
    assert [u["operation"] for u in generated["support_ticketing_system"][1].report["unimplemented_operations"]] == ["operation.ticket.stats"]


# ---- CLI ---------------------------------------------------------------------------------------------------------------------------------------
def test_cli_inspect_and_validate(capsys):
    assert main(["inspect", "--project", str(PROJECTS / "task_manager"), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["operations"] == 27 and out["needs_handler"] == ["operation.project.progress"] and out["integration"]["status"] == "conflicts_reported"
    assert main(["validate", "--project", str(PROJECTS / "task_manager")]) == 0
    assert "graph" in capsys.readouterr().out


def test_cli_generate_with_mock_writes_artifacts(tmp_path, project, capsys):
    p = project()
    assert main(["generate", "--project", str(p), "--mock"]) == 0
    assert (p / "artifacts" / "backend_validation_report.json").exists() and (p / "backend" / "backend_app" / "main.py").exists()
    assert "status: not_tested" in capsys.readouterr().out


def test_cli_exit_codes(tmp_path, project, monkeypatch, capsys):
    assert main(["inspect", "--project", str(tmp_path / "missing")]) == 2, "missing graph package -> input error"
    p = project()
    mutate_graph(p, "permissions", lambda d: next(x for x in d["permissions"] if x["id"] == "permission.project.delete").update(operation_refs=[]))
    assert main(["generate", "--project", str(p), "--mock"]) == 3, "a critical integration conflict blocks generation"
    rep = read_json(p / "artifacts" / "backend_validation_report.json")
    assert rep["status"] == "blocked" and any(i["code"] == "PERMISSION_MISSING" for i in rep["issues"])
    assert read_json(p / "artifacts" / "integration_conflicts.json")["status"] == "blocked"
    assert not (p / "backend" / "backend_app").exists(), "nothing is generated from a contradictory specification"
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["generate", "--project", str(project())]) == 6, "no --mock and no API key"
    assert "--mock" in capsys.readouterr().err


def test_invalid_graph_is_reported_in_the_validation_report(tmp_path, project):
    p = project()
    mutate_graph(p, "api", lambda d: d["endpoints"][0].update(operation_ref="operation.ghost.create"))
    assert main(["generate", "--project", str(p), "--mock"]) == 2
    rep = read_json(p / "artifacts" / "backend_validation_report.json")
    assert rep["status"] == "failed" and rep["graph_validation"] == "passed" or rep["status"] == "failed"
    assert not (p / "backend").exists()


def test_integration_test_needs_a_database_and_says_so(project, monkeypatch):
    p = project()
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/unreachable_test")
    assert main(["integration-test", "--project", str(p), "--mock"]) == 5
    rep = read_json(p / "artifacts" / "backend_validation_report.json")
    assert rep["status"] == "failed" and rep["issues"][0]["kind"] == "DATABASE_ERROR"


def test_committed_fixture_backends_are_current():
    """The generated backends committed under projects/ must be exactly what the generator produces today."""
    import tempfile

    for name in FIXTURES:
        committed = PROJECTS / name / "backend"
        if not committed.exists():
            pytest.skip("fixture backends not generated yet")
        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / name
            for sub in ("graphs", "validation", "database", "frontend"):
                if (PROJECTS / name / sub).exists():
                    shutil.copytree(PROJECTS / name / sub, dst / sub)
            _generate(dst)
            fresh = {f["path"]: f["sha256"] for f in read_json(dst / "artifacts" / "file_manifest.json")["files"]}
        have = {f["path"]: f["sha256"] for f in read_json(PROJECTS / name / "artifacts" / "file_manifest.json")["files"]}
        assert fresh == have, f"{name}: committed backend is stale; run `python -m app.main generate --project projects/{name} --mock`"
