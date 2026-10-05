"""Backend Agent CLI.

    python -m app.main inspect          --project projects/task_manager
    python -m app.main generate         --project projects/task_manager [--mock]
    python -m app.main validate         --project projects/task_manager
    python -m app.main test             --project projects/task_manager [--mock]
    python -m app.main integration-test --project projects/task_manager [--mock]

Exit codes: 0 ok · 1 tests/validation failed · 2 invalid input (graph/contract) · 3 blocked by critical integration conflicts ·
4 generation failure · 5 no dedicated PostgreSQL for integration-test · 6 usage / configuration.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.analysis.classifier import classify_all
from app.analysis.rules import derive_rules
from app.config.settings import Settings
from app.contracts.database import load_database_contract
from app.contracts.frontend import load_frontend_contract
from app.errors import BackendAgentError, ErrorKind
from app.graph.loader import load_graph_package, resolve_project
from app.graph.validator import validate_graph
from app.integration.conflicts import detect_all
from app.llm.base import LLMError
from app.llm.factory import create_provider
from app.pipeline.orchestrator import EXIT_FAILED, EXIT_INPUT, Options, BackendPipeline

EXIT_USAGE = 6


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.main", description="Backend Agent: graphs + database contract -> tested FastAPI/PostgreSQL backend")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser, llm: bool = True) -> None:
        sp.add_argument("--project", "-p", type=Path, required=True, help="project directory (contains graphs/, database/, optionally frontend/)")
        sp.add_argument("--database-dir", type=Path, help="Database Agent artifacts (default <project>/database)")
        sp.add_argument("--frontend-dir", type=Path, help="Frontend Agent graphs (default <project>/frontend)")
        sp.add_argument("--json", action="store_true", help="print machine-readable output")
        if llm:
            sp.add_argument("--mock", action="store_true", help="deterministic offline LLM (no API key, no network)")
            sp.add_argument("--provider", choices=["mock", "anthropic", "openai_compatible", "ollama"], help="LLM provider (env LLM_PROVIDER)")
            sp.add_argument("--model", help="model id (env LLM_MODEL)")
            sp.add_argument("--output", "-o", type=Path, help="where to write the generated backend (default <project>/backend)")
            sp.add_argument("--strict-frontend", action="store_true", help="treat major Frontend conflicts as blocking")
            sp.add_argument("--verbose", "-v", action="store_true", help="echo stage progress to stderr")

    common(sub.add_parser("inspect", help="load and analyse a project; read-only"), llm=False)
    common(sub.add_parser("validate", help="validate graphs, contracts and conflicts (and the generated backend if present)"), llm=False)
    common(sub.add_parser("generate", help="generate the backend, tests and artifacts, then validate statically"))
    t = sub.add_parser("test", help="generate and run the generated test suites (database suites are skipped, with a reason, if no PostgreSQL is available)")
    common(t)
    it = sub.add_parser("integration-test", help="full pipeline against a real PostgreSQL with bounded autonomous correction")
    common(it)
    it.add_argument("--max-corrections", type=int, help="correction attempts (default 3)")
    return p


def _settings(args: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    ch = {}
    if getattr(args, "provider", None):
        ch["llm_provider"] = args.provider
    if getattr(args, "model", None):
        ch["llm_model"] = args.model
    return s.with_changes(**ch) if ch else s


def _load(args: argparse.Namespace):
    pkg = load_graph_package(args.project)
    v = validate_graph(pkg)
    v.raise_if_invalid()
    project_dir, _ = resolve_project(args.project)
    db = load_database_contract(args.database_dir or project_dir / "database")
    fdir = args.frontend_dir or (project_dir / "frontend" if (project_dir / "frontend").exists() else None)
    fe = load_frontend_contract(fdir) if fdir else None
    return pkg, v, db, fe, project_dir


def cmd_inspect(args: argparse.Namespace) -> int:
    pkg, v, db, fe, project_dir = _load(args)
    report = detect_all(pkg, db, fe)
    defaults = {(e["id"], a["name"]) for e in pkg.entities.values() for a in e["attributes"] if (t := db.table_for_entity(e["id"].split(".", 1)[1])) and t.columns[a["name"]].has_default}
    classes = classify_all(pkg, defaults)
    rules = derive_rules(pkg, classes)
    out = {
        "project": pkg.project_id, "graphs": sorted(pkg.docs), "entities": sorted(pkg.entities), "operations": len(pkg.operations), "endpoints": len(pkg.endpoints),
        "operation_kinds": {k: sum(1 for c in classes.values() if c.kind == k) for k in sorted({c.kind for c in classes.values()})},
        "needs_handler": sorted(o for o, c in classes.items() if c.handler == "custom"),
        "database": {"tables": sorted(db.tables), "crud_functions": len(db.functions)}, "frontend_contract": bool(fe),
        "rules": [r["id"] for r in rules.rules], "unmapped_conditions": [u["source"] for u in rules.unmapped], "assumptions": rules.assumptions,
        "integration": report.to_dict()["summary"] | {"status": report.status}, "warnings": [str(w) for w in v.warnings] + pkg.warnings,
    }
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"{out['project']}: {len(out['entities'])} entities, {out['operations']} operations, {out['endpoints']} endpoints")
        print(f"  operation kinds : {out['operation_kinds']}")
        print(f"  needs a handler : {out['needs_handler'] or 'none'}")
        print(f"  database        : {len(db.tables)} tables, {len(db.functions)} CRUD functions")
        print(f"  frontend        : {'contract loaded (' + str(len(fe.endpoints)) + ' endpoints)' if fe else 'no contract'}")
        print(f"  derived rules   : {len(out['rules'])} (unmapped conditions: {len(out['unmapped_conditions'])})")
        print(f"  conflicts       : {report.status} {out['integration']}")
        for c in report.conflicts:
            if c.severity in ("critical", "major"):
                print(f"    [{c.severity}] {c.type.value}/{c.code}: {c.description[:140]}")
    return 3 if report.blocking else 0


def cmd_validate(args: argparse.Namespace) -> int:
    pkg, v, db, fe, project_dir = _load(args)
    report = detect_all(pkg, db, fe)
    problems = [f"{c.type.value}/{c.code}: {c.description}" for c in report.blocking]
    backend = project_dir / "backend"
    static = None
    if (backend / "backend_app").exists():
        from app.pipeline.static_validation import compare_openapi_to_graph, static_validate

        sv = static_validate(backend)
        static = {"checks": sv.checks, "problems": [str(p) for p in sv.problems]}
        if sv.openapi:
            extra = compare_openapi_to_graph(sv.openapi, [{"id": e["id"], "method": e["method"], "path": e["path"]} for e in pkg.endpoints.values()])
            static["problems"] += [str(i) for i in extra]
        problems += static["problems"]
    out = {"graph": "valid", "database_contract": "loaded", "frontend_contract": "loaded" if fe else "not_available", "conflicts": report.to_dict()["summary"] | {"status": report.status},
           "generated_backend": static or "not generated", "problems": problems}
    print(json.dumps(out, indent=2) if args.json else "\n".join([f"graph             : valid ({len(pkg.operations)} operations)", f"conflicts         : {report.status} {out['conflicts']}",
                                                                  f"generated backend : {static['checks'] if static else 'not generated'}"] + [f"  PROBLEM {p}" for p in problems]))
    return 3 if report.blocking else (1 if problems else 0)


def _pipeline(args: argparse.Namespace, *, run_tests: bool, require_db: bool, corrections: int | None) -> int:
    settings = _settings(args)
    try:
        provider = create_provider(settings, mock=args.mock)
    except (LLMError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    opts = Options(project=args.project, mock=args.mock, database_dir=args.database_dir, frontend_dir=args.frontend_dir, output_dir=args.output, max_corrections=corrections,
                   run_tests=run_tests, require_database=require_db, strict_frontend=args.strict_frontend, echo=args.verbose)
    res = BackendPipeline(opts, settings, provider).run()
    rep = res.report
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(f"status: {res.status}")
        for k in ("graph_validation", "database_contract", "frontend_contract", "api_contract", "type_checks", "unit_tests", "contract_tests", "database_tests", "api_tests",
                  "security_tests", "integration_tests", "correction_attempts"):
            if k in rep:
                print(f"  {k:<20}: {rep[k]}")
        for i in rep.get("issues", []):
            print(f"  ISSUE {i['kind']}/{i['code']}: {i['message']}")
        for w in rep.get("warnings", [])[:12]:
            print(f"  warning: {w[:160]}")
        print(f"artifacts: {Path(args.project) / 'artifacts'}")
        if res.backend_dir:
            print(f"backend  : {res.backend_dir}")
    return res.exit_code


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "inspect":
            return cmd_inspect(args)
        if args.command == "validate":
            return cmd_validate(args)
        if args.command == "generate":
            return _pipeline(args, run_tests=False, require_db=False, corrections=0)
        if args.command == "test":
            return _pipeline(args, run_tests=True, require_db=False, corrections=0)
        if args.command == "integration-test":
            return _pipeline(args, run_tests=True, require_db=True, corrections=args.max_corrections)
    except BackendAgentError as e:
        for i in e.issues:
            print(f"error: {i}", file=sys.stderr)
        return EXIT_INPUT if ErrorKind.GRAPH_ERROR in e.kinds or ErrorKind.GRAPH_REFERENCE_ERROR in e.kinds or ErrorKind.DATABASE_CONTRACT_ERROR in e.kinds else EXIT_FAILED
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INPUT
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
