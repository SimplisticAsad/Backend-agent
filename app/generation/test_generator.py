"""Generates the backend's own test suite into `<backend>/tests`.

Static, spec-driven test modules are copied from `test_templates/tests`; project-specific inputs are written as data:
  tests/data/graph_contract.json     RAW graph slices (api, backend, roles, permissions, validations, state machines, ...):
                                     the contract tests compare the running app with the graph itself, not with the derived spec
  tests/data/frontend_contract.json  the Frontend contract (when available)
  tests/data/known_conflicts.json    codes of documented integration conflicts (so compat tests assert the documented behaviour)
  tests/bdd/features/*.feature       one Gherkin feature per acceptance criterion
  tests/api/test_custom_*.py         tests for LLM-implemented operations (stage `testing`), AST-checked
"""
from __future__ import annotations

import ast
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.contracts.frontend import FrontendContract
from app.generation.generator import HEADER, Emitter, FileRecord
from app.graph.model import GraphPackage

TEMPLATES = Path(__file__).parent / "test_templates"
PG_CLUSTER = Path(__file__).resolve().parents[1] / "testing" / "pg_cluster.py"


def _feature_text(ac: dict[str, Any], pkg: GraphPackage) -> str:
    def block(kw: str, items: list[str]) -> list[str]:
        out = []
        for i, t in enumerate(items):
            out.append(f"    {kw if i == 0 else 'And'} {t}")
        return out

    lines = [f"# {ac['id']}  (requirements: {', '.join(ac.get('requirement_refs', []))}; operations: {', '.join(ac.get('operation_refs', []))})",
             f"Feature: {ac['name']}", "", f"  Scenario: {ac['name']}"]
    lines += block("Given", ac.get("given", [])) + block("When", ac.get("when", [])) + block("Then", ac.get("then", []))
    return "\n".join(lines) + "\n"


def graph_contract(pkg: GraphPackage) -> dict[str, Any]:
    return {
        "project_id": pkg.project_id,
        "api": pkg.docs["api"], "backend": pkg.docs["backend"],
        "roles": pkg.items("roles"), "permissions": pkg.items("permissions"), "entities": pkg.items("entities"),
        "state_machines": pkg.items("state_machines"), "validations": pkg.items("validations"),
        "acceptance_criteria": pkg.items("acceptance_criteria"), "workflows": pkg.items("workflows"),
    }


def frontend_contract_dict(fe: FrontendContract) -> dict[str, Any]:
    return {
        "endpoints": [asdict(e) for e in fe.endpoints], "entities": fe.entities, "roles": fe.roles, "permissions": fe.permissions,
        "state_machines": fe.state_machines, "expectations": fe.expectations,
    }


def generate_tests(spec: dict[str, Any], pkg: GraphPackage, out_dir: Path, *, frontend: FrontendContract | None = None,
                   conflicts: list[dict[str, Any]] | None = None, custom_tests: dict[str, str] | None = None) -> list[FileRecord]:
    out = Path(out_dir)
    import shutil

    # The agent owns exactly the directories it writes; anything else under tests/ (e.g. tests/custom/) is user code and is preserved.
    owned = {p.name for p in (TEMPLATES / "tests").iterdir() if p.is_dir() and p.name != "__pycache__"} | {"data"}
    for name in owned:
        shutil.rmtree(out / "tests" / name, ignore_errors=True)
    for f in ("conftest.py", "README.md", "__init__.py"):
        (out / "tests" / f).unlink(missing_ok=True)
    em = Emitter(out)
    for src in sorted((TEMPLATES / "tests").rglob("*")):
        if src.is_file() and "__pycache__" not in src.parts:
            rel = "tests/" + src.relative_to(TEMPLATES / "tests").as_posix()
            em.write(rel, src.read_text(encoding="utf-8"), "test", [], "spec-driven test module (agent template)")
    em.write("tests/support/pg.py", PG_CLUSTER.read_text(encoding="utf-8"), "test", [], "dedicated-PostgreSQL helper (copy of app/testing/pg_cluster.py)")
    em.write("tests/data/graph_contract.json", json.dumps(graph_contract(pkg), indent=1, sort_keys=True), "test", [pkg.project_id], "raw graph slices used by contract tests")
    if frontend is not None:
        em.write("tests/data/frontend_contract.json", json.dumps(frontend_contract_dict(frontend), indent=1, sort_keys=True), "test", ["frontend_contract"], "Frontend contract snapshot")
    em.write("tests/data/known_conflicts.json", json.dumps(sorted({c["code"] for c in (conflicts or [])}), indent=1), "test", [], "documented conflict codes")
    for ac_id, ac in sorted(pkg.acceptance_criteria.items()):
        em.write(f"tests/bdd/features/{ac_id.replace('.', '_')}.feature", _feature_text(ac, pkg), "test",
                 [ac_id] + ac.get("requirement_refs", []) + ac.get("workflow_refs", []) + ac.get("operation_refs", []), "BDD scenario from acceptance_criteria.json")
    covered: dict[str, str] = {}
    for name, code in sorted((custom_tests or {}).items()):
        em.write(f"tests/api/test_custom_{name}.py", HEADER + code, "test", [name], "tests for an LLM-implemented operation")
        for node in ast.parse(code).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_ac_"):
                doc = ast.get_docstring(node) or ""
                ac_id = doc.split(":", 1)[0].strip()
                if ac_id in pkg.acceptance_criteria:
                    covered[ac_id] = f"tests/api/test_custom_{name}.py::{node.name}"
    em.write("tests/data/custom_ac_coverage.json", json.dumps(covered, indent=1, sort_keys=True), "test", sorted(covered), "acceptance criteria covered by dedicated custom tests")
    em.write("tests/README.md", "Generated test suite. Run `pytest` (needs PostgreSQL: set TEST_DATABASE_URL to a dedicated database whose name contains 'test', "
             "or let the suite start a throw-away local cluster). Do not weaken or delete these tests: they encode the graph contract.\n", "test", [], "")
    return em.records
