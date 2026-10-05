"""The controlled backend pipeline. Python owns the lifecycle; the LLM works inside bounded, validated stages.

LOAD -> INSPECT/VALIDATE GRAPH -> DATABASE CONTRACT -> FRONTEND CONTRACT -> CONFLICT DETECTION (stop on critical)
     -> ANALYSIS -> RULES -> HANDLERS -> ARCHITECTURE/SPEC -> CODE -> TESTS -> STATIC VALIDATION
     -> DATABASE -> SUITES -> CORRECTION LOOP -> FINAL VALIDATION
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.analysis.analysis import build_analysis, build_architecture
from app.analysis.classifier import classify_all
from app.analysis.rules import RuleSet, derive_rules
from app.analysis.spec import build_entity_mapping, build_spec
from app.config.settings import Settings
from app.contracts.database import DatabaseContract, load_database_contract
from app.contracts.frontend import FrontendContract, load_frontend_contract
from app.errors import BackendAgentError, ContractError, ErrorKind, GraphError, IntegrationBlocked, Issue
from app.generation.generator import FileRecord, compute_method_names, generate_backend
from app.generation.test_generator import generate_tests
from app.graph.loader import load_graph_package, resolve_project
from app.graph.model import GraphPackage
from app.graph.validator import validate_graph
from app.integration.conflicts import IntegrationReport, detect_all
from app.llm.base import LLMProvider
from app.llm.client import LLMClient, LLMOutputError
from app.observability import StageLog
from app.pipeline.artifacts import ArtifactStore
from app.pipeline.correction import Attempt, LoopResult, correction_loop
from app.pipeline.failures import Failure, choose_prompt
from app.pipeline.llm_stages import SCHEMAS, LLMStages, _dump
from app.pipeline.patch import Edit, apply_edits
from app.pipeline.static_validation import build_api_contract, compare_openapi_to_graph, static_validate
from app.pipeline.testrun import SUITES, SuiteResult, run_suite
from app.testing import pg_cluster
from app.validation.report import build_report

EXIT_OK, EXIT_FAILED, EXIT_INPUT, EXIT_BLOCKED, EXIT_GENERATION, EXIT_NO_DB = 0, 1, 2, 3, 4, 5


@dataclass
class Options:
    project: Path
    mock: bool = False
    database_dir: Path | None = None
    frontend_dir: Path | None = None
    output_dir: Path | None = None  # generated backend; default <project>/backend
    max_corrections: int | None = None
    run_tests: bool = False
    require_database: bool = False
    strict_frontend: bool = False
    echo: bool = False


@dataclass
class PipelineResult:
    status: str
    exit_code: int
    report: dict[str, Any]
    artifacts: list[str] = field(default_factory=list)
    backend_dir: Path | None = None


class BackendPipeline:
    def __init__(self, opts: Options, settings: Settings, provider: LLMProvider) -> None:
        self.o, self.settings = opts, settings
        self.project_dir, self.graphs_dir = resolve_project(opts.project)
        self.backend_dir = opts.output_dir or self.project_dir / "backend"
        self.art = ArtifactStore(self.project_dir / "artifacts")
        self.log = StageLog(self.art.dir / "run_logs" / "stages.jsonl", echo=opts.echo)
        (self.art.dir / "run_logs").mkdir(parents=True, exist_ok=True)
        (self.art.dir / "run_logs" / "stages.jsonl").write_text("", encoding="utf-8")
        self.client = LLMClient(provider)
        self.llm = LLMStages(self.client)
        self.provider_name = provider.name
        self.pkg: GraphPackage | None = None
        self.db: DatabaseContract | None = None
        self.fe: FrontendContract | None = None
        self.integration = IntegrationReport()
        self.advisory: dict[str, Any] = {}
        self.records: list[FileRecord] = []
        self.corrections: list[Attempt] = []

    # ================================================================================================================
    def run(self) -> PipelineResult:
        try:
            return self._run()
        except GraphError as e:
            return self._fail("failed", EXIT_INPUT, e, graph_ok=False)
        except ContractError as e:
            return self._fail("failed", EXIT_INPUT, e)
        except IntegrationBlocked as e:
            return self._fail("blocked", EXIT_BLOCKED, e)

    def _fail(self, status: str, code: int, err: BackendAgentError, graph_ok: bool = True) -> PipelineResult:
        issues = [i.to_dict() for i in err.issues]
        report = {"status": status, "graph_validation": "passed" if graph_ok else "failed", "issues": issues, "stages": self.log.summary(),
                  "warnings": [], "correction_attempts": 0}
        self.art.save("backend_validation_report.json", report)
        return PipelineResult(status, code, report, self.art.written)

    # ================================================================================================================
    def _run(self) -> PipelineResult:
        o = self.o
        with self.log.stage("repository_inspection") as st:
            st.details = {"project": str(self.project_dir), "graphs": str(self.graphs_dir)}
        with self.log.stage("graph_loading") as st:
            self.pkg = load_graph_package(self.project_dir)
            st.details = {"graphs": sorted(self.pkg.docs), "project_id": self.pkg.project_id}
        pkg = self.pkg
        with self.log.stage("graph_validation") as st:
            v = validate_graph(pkg)
            st.warnings = [str(w) for w in v.warnings] + pkg.warnings
            v.raise_if_invalid()
        with self.log.stage("database_contract_loading") as st:
            ddir = o.database_dir or self.project_dir / "database"
            self.db = load_database_contract(ddir)
            st.details = {"tables": sorted(self.db.tables), "functions": len(self.db.functions)}
        with self.log.stage("frontend_contract_loading") as st:
            fdir = o.frontend_dir or (self.project_dir / "frontend" if (self.project_dir / "frontend").exists() else (self.settings.frontend_agent_dir if self.settings.frontend_agent_dir else None))
            self.fe = load_frontend_contract(fdir) if fdir and Path(fdir).exists() else None
            st.details = {"available": self.fe is not None, "endpoints": len(self.fe.endpoints) if self.fe else 0}
        with self.log.stage("integration_analysis") as st:
            self.integration = detect_all(pkg, self.db, self.fe)
            if o.strict_frontend:
                for c in self.integration.conflicts:
                    if c.type.value == "FRONTEND_API_CONFLICT" and c.severity == "major":
                        c.severity = "critical"
            self.art.save("integration_conflicts.json", self.integration.to_dict())
            st.artifacts.append("integration_conflicts.json")
            st.details = self.integration.to_dict()["summary"]
            if self.integration.blocking:
                raise IntegrationBlocked([Issue(c.kind, c.code, c.description, refs=tuple(c.sources)) for c in self.integration.blocking])

        defaults = {(e["id"], a["name"]) for e in pkg.entities.values() for a in e["attributes"]
                    if (t := self.db.table_for_entity(e["id"].split(".", 1)[1])) and t.columns[a["name"]].has_default}
        with self.log.stage("backend_analysis") as st:
            classes = classify_all(pkg, defaults)
            baseline = derive_rules(pkg, classes)
            analysis = build_analysis(pkg, classes, baseline, self.integration)
            mapping = build_entity_mapping(pkg, self.db)
            self.art.save("backend_analysis.json", analysis)
            self.art.save("entity_mapping.json", mapping)
            st.artifacts += ["backend_analysis.json", "entity_mapping.json"]
        spec0 = build_spec(pkg, self.db, classes, baseline.rules, coverage=baseline.coverage)

        # ---- LLM: advisory analysis + functional rules -------------------------------------------------------------------
        known_ids = set(pkg.all_ids) | {r["id"] for r in baseline.rules} | {s for c in self.integration.conflicts for s in c.sources} | {u["source"] for u in baseline.unmapped}
        with self.log.stage("llm_graph_analysis") as st:
            ctx = {"summary": {"operations": len(pkg.operations), "entities": len(pkg.entities), "engine_operations": sum(1 for c in classes.values() if c.handler != "custom"),
                               "custom_operations": sum(1 for c in classes.values() if c.handler == "custom")},
                   "conflicts": [c.to_dict() for c in self.integration.conflicts if c.severity in ("major", "critical")], "unmapped": baseline.unmapped}
            self.advisory["graph_analysis"] = self._advisory(st, lambda: self.llm.graph_analysis(ctx, known_ids))
        rules_all = list(baseline.rules)
        with self.log.stage("llm_business_rules") as st:
            if baseline.unmapped:
                ctx = {"unmapped": baseline.unmapped, "baseline": baseline.rules, "roles": sorted(pkg.roles), "operations": {o_: v.get("entity_ref") for o_, v in pkg.operations.items()},
                       "entities": {e: [c["attr"] for c in v["columns"]] for e, v in spec0["entities"].items()}}
                try:
                    out = self.llm.business_rules(ctx, pkg, baseline, spec0["entities"])
                    rules_all += out["rules"]
                    resolved = {r["source"] for r in out["rules"] if r.get("source", "").startswith("validation.")}
                    baseline.unmapped = [u for u in baseline.unmapped if u["source"] not in resolved]
                    for r in out["rules"]:
                        baseline.coverage.append({"source": r["source"], "enforced_by": f"rule:{r['type']}", "operations": r["operations"]})
                    self.advisory["business_rules"] = {"accepted": [r["id"] for r in out["rules"]], "unresolved": out["unresolved"]}
                except LLMOutputError as e:
                    st.warnings.append(f"business_rules rejected: {e}")
                    self.advisory["business_rules"] = {"accepted": [], "unresolved": [{"source": u["source"], "reason": "LLM output rejected"} for u in baseline.unmapped]}
            else:
                st.details = {"skipped": "every graph condition was mapped by the deterministic baseline"}

        # ---- handlers for operations the engine cannot implement -----------------------------------------------------------------
        handlers: dict[str, dict[str, Any]] = {}
        with self.log.stage("llm_api_implementation") as st:
            names = compute_method_names(spec0)
            custom = [oid for oid, c in classes.items() if c.handler == "custom"]
            if custom:
                ctx = self._handler_context(pkg, spec0, classes, custom, names)
                handlers = self.llm.api_implementation(ctx)
                st.details = {"operations": custom, "implemented": [k for k, v in handlers.items() if v["status"] == "implemented"]}
            self.art.save("handlers.json", handlers)
            st.artifacts.append("handlers.json")

        with self.log.stage("architecture_generation") as st:
            spec = build_spec(pkg, self.db, classes, rules_all, handlers, notes=[a for a in baseline.assumptions], coverage=baseline.coverage)
            arch = build_architecture(spec)
            arch_ctx = {k: arch[k] for k in ("modules", "services", "repositories")}
            self.advisory["architecture"] = self._advisory(st, lambda: self.llm.architecture(arch_ctx))
            arch["descriptions"] = (self.advisory["architecture"] or {}).get("descriptions", {})
            self.art.save("backend_architecture.json", arch)
            self.art.save("backend_spec.json", spec)
            st.artifacts += ["backend_architecture.json", "backend_spec.json"]
        with self.log.stage("llm_domain_authorization_database_review") as st:
            dctx = {"entities": [{"id": e, "rules": [r["id"] for r in rules_all if r["entity"] == e], "state_machine": ent["state_machine"], "unique": [c["attr"] for c in ent["columns"] if c["unique"]]}
                                 for e, ent in sorted(spec["entities"].items())]}
            self.advisory["domain_model"] = self._advisory(st, lambda: self.llm.domain_model(dctx))
            actx = {"matrix": {oid: o_["roles"] or ["(any authenticated)" if o_["access"] == "authenticated" else "(public)"] for oid, o_ in sorted(spec["operations"].items())},
                    "rules": [{"id": r["id"], "type": r["type"], "origin": r.get("origin", ""), "source": r.get("source", "")} for r in rules_all], "unmapped": baseline.unmapped}
            self.advisory["authorization"] = self._advisory(st, lambda: self.llm.authorization(actx, known_ids | {r["id"] for r in rules_all} | set(spec["operations"])))
            bctx = {"entity_mapping": mapping, "conflicts": [c.to_dict() for c in self.integration.conflicts if c.type.value == "DATABASE_CONTRACT_CONFLICT"]}
            self.advisory["database_integration"] = self._advisory(st, lambda: self.llm.database_integration(bctx))
            self.art.save("llm_notes.json", self.advisory)

        # ---- code ---------------------------------------------------------------------------------------------------------------------
        with self.log.stage("code_generation") as st:
            self.backend_dir.mkdir(parents=True, exist_ok=True)
            self.records = generate_backend(spec, self.backend_dir, db_contract_dir=self.db_dir())
            st.details = {"files": len(self.records)}
        with self.log.stage("test_generation") as st:
            custom_tests: dict[str, str] = {}
            impl = {k: v for k, v in handlers.items() if v["status"] == "implemented"}
            if impl:
                tctx = {"handlers": impl, "operations": self._op_contexts(pkg, spec, classes, list(impl), names), "entities": self._entity_context(spec), "auth_entity": (spec["auth"] or {}).get("entity"),
                        "state_initial": {sm["entity"]: sm["initial"] for sm in spec["state_machines"].values()}}
                custom_tests = self.llm.testing(tctx)
            self.records += generate_tests(spec, pkg, self.backend_dir, frontend=self.fe, conflicts=[c.to_dict() for c in self.integration.conflicts], custom_tests=custom_tests)
            if self.fe is None:
                for sub in ("tests/frontend_compat",):
                    shutil.rmtree(self.backend_dir / sub, ignore_errors=True)
                    self.records = [r for r in self.records if not r.path.startswith(sub)]
            st.details = {"files": sum(1 for r in self.records if r.kind == "test"), "custom_test_modules": sorted(custom_tests)}
        self._write_manifests(spec, classes)

        # ---- static validation ------------------------------------------------------------------------------------------------------
        with self.log.stage("static_validation") as st:
            sv = static_validate(self.backend_dir)
            static = sv.checks
            api_ok: bool | None = None
            if sv.openapi:
                issues = compare_openapi_to_graph(sv.openapi, [{"id": e["id"], "method": e["method"], "path": e["path"]} for e in pkg.endpoints.values()])
                api_ok = not issues
                sv.problems += issues
                self.art.save("backend_api_contract.json", build_api_contract(sv.openapi, spec))
                self.art.save("openapi.json", sv.openapi)
                st.artifacts += ["backend_api_contract.json", "openapi.json"]
            st.errors = [str(p) for p in sv.problems]
            if sv.problems:
                st.status = "failed"
            static_problems = sv.problems

        results: dict[str, SuiteResult] = {}
        loop: LoopResult | None = None
        db_skip = ""
        if o.run_tests and not static_problems:
            try:
                url = pg_cluster.get_database_url()
                env = {"TEST_DATABASE_URL": url}
            except (pg_cluster.DatabaseUnavailable, pg_cluster.UnsafeDatabase) as e:
                env, db_skip = {}, f"database suites skipped: {e}"
                if o.require_database:
                    self._finish(spec, classes, static, api_ok, {}, db_skip, 0, "database unavailable", handlers)
                    return PipelineResult("failed", EXIT_NO_DB, json.loads((self.art.dir / "backend_validation_report.json").read_text()), self.art.written, self.backend_dir)
            max_c = self.o.max_corrections if self.o.max_corrections is not None else self.settings.max_corrections
            with self.log.stage("test_execution") as st:
                if env:
                    loop = correction_loop(
                        max_attempts=max_c, run_checks=lambda: self._run_suites(env), is_ok=lambda r: all(s.status not in ("failed",) for s in r.values()),
                        count_failures=lambda r: sum(len(s.failures) for s in r.values()), correct=lambda r, n: self._correct(r, n, spec, pkg))
                    results = loop.final
                    st.correction_attempts = loop.correction_attempts
                    st.details = {"stopped_because": loop.stopped_because}
                else:
                    results = self._run_suites({})
                st.status = "passed" if all(s.status != "failed" for s in results.values()) else "failed"
            self.art.save("corrections.json", {"attempts": [a.to_dict() for a in self.corrections], "stopped_because": loop.stopped_because if loop else "not run"})
        unimpl = [{"operation": oid, "reason": h.get("reason", "")} for oid, h in sorted(spec["handlers"].items()) if h["status"] != "implemented"]
        unimpl += [{"operation": oid, "reason": "no handler decision"} for oid, o_ in sorted(spec["operations"].items()) if o_["handler"] == "custom" and oid not in spec["handlers"]]
        return self._finish(spec, classes, static, api_ok, results, db_skip, loop.correction_attempts if loop else 0, loop.stopped_because if loop else "tests not run", handlers,
                            static_problems=static_problems)

    # ================================================================================================================
    def db_dir(self) -> Path:
        return self.o.database_dir or self.project_dir / "database"

    def _advisory(self, st, fn):
        try:
            return fn()
        except LLMOutputError as e:
            st.warnings.append(f"advisory stage rejected: {e}")
            return None

    def _entity_context(self, spec: dict[str, Any]) -> dict[str, Any]:
        return {eid: {"id": eid, "key": e["key"], "class_name": e["class_name"], "table": e["table"], "state_machine": e["state_machine"],
                      "columns": [{k: c[k] for k in ("attr", "column", "type", "required", "has_default", "enum", "ref")} for c in e["columns"]]} for eid, e in sorted(spec["entities"].items())}

    def _op_contexts(self, pkg: GraphPackage, spec: dict[str, Any], classes: dict, ops: list[str], names: dict[str, str]) -> dict[str, Any]:
        out = {}
        for oid in ops:
            so, g = spec["operations"][oid], pkg.operations[oid]
            roles = [r.split(".", 1)[1] for r in (so["roles"] or [])]
            if not roles and spec["auth"]:
                roles = sorted(spec["auth"]["role_map"])
            vals = []
            for v in pkg.validations.values():
                if oid in v.get("operation_refs", []):
                    hint = next((h for h in ("cart", "stock") if h in v["id"]), "")
                    vals.append({"id": v["id"], "kind": v["kind"], "condition": v["condition"], "error_code": v["failure"].get("error_code"), "message": v["failure"].get("message"), "kind_hint": hint})
            sch = spec["schemas"].get(so["endpoint"]["request_schema"] or "")
            out[oid] = {"id": oid, "name": so["name"], "description": so["description"], "kind": so["kind"], "entity": so["entity"], "action": so["action"],
                        "endpoint": {k: so["endpoint"][k] for k in ("id", "method", "path", "status_code", "path_params")}, "role_key": roles[0] if roles else "", "roles": roles,
                        "request_fields": [{"name": f["name"], "base": f["base"], "required": f["required"]} for f in (sch["fields"] if sch else [])],
                        "validations": vals, "errors": g["errors"], "acceptance": [{"id": a["id"], "name": a["name"], "given": a.get("given", []), "when": a.get("when", []), "then": a.get("then", [])}
                                                                                       for a in pkg.acceptance_criteria.values() if oid in a.get("operation_refs", [])], "service_key": so["service"].split(".", 1)[1], "method_name": names[oid], "note": "; ".join(so["notes"])}
        return out

    def _handler_context(self, pkg: GraphPackage, spec: dict[str, Any], classes: dict, custom: list[str], names: dict[str, str]) -> dict[str, Any]:
        return {"operations": list(self._op_contexts(pkg, spec, classes, custom, names).values()), "entities": self._entity_context(spec),
                "auth_entity": (spec["auth"] or {}).get("entity"), "state_initial": {sm["entity"]: sm["initial"] for sm in spec["state_machines"].values()},
                "rules": [{"id": r["id"], "type": r["type"], "operations": r["operations"]} for r in spec["rules"]]}

    def _write_manifests(self, spec: dict[str, Any], classes: dict) -> None:
        names = compute_method_names(spec)
        svc: dict[str, list] = {}
        for oid, o_ in sorted(spec["operations"].items()):
            svc.setdefault(o_["service"], []).append({"operation": oid, "method": names[oid], "kind": o_["kind"], "handler": o_["handler"],
                                                      "implemented": o_["handler"] != "custom" or spec["handlers"].get(oid, {}).get("status") == "implemented"})
        self.art.save("service_manifest.json", {"services": [{"id": s, "class": f"{s.split('.', 1)[1].title().replace('_', '')}Service", "operations": ops} for s, ops in sorted(svc.items())]})
        self.art.save("repository_manifest.json", {"repositories": [{"entity": e, "class": f"{ent['class_name']}Repository", "table": ent["table"], "unique_columns": ent["unique_columns"],
                                                                    "foreign_keys": ent["foreign_keys"]} for e, ent in sorted(spec["entities"].items())]})
        self.art.save("file_manifest.json", {"files": sorted((r.to_dict() for r in self.records), key=lambda r: r["path"]),
                                             "ownership": {"generated": "regenerated from graphs on every run; do not edit", "runtime": "agent infrastructure copied verbatim",
                                                           "contract": "copy of the Database Agent artifacts", "test": "generated, spec-driven; must not be weakened", "config": "project metadata"}})
        tests = [r.to_dict() for r in self.records if r.kind == "test" and r.path.endswith(".py") and "/support/" not in r.path and not r.path.endswith("__init__.py")]
        self.art.save("test_manifest.json", {"suites": [{"name": n, "path": p, "needs_database": d} for n, p, d in SUITES], "modules": tests,
                                             "features": sorted(r.path for r in self.records if r.path.endswith(".feature"))})

    # ---- tests & correction ----------------------------------------------------------------------------------------------------------
    def _run_suites(self, env: dict[str, str]) -> dict[str, SuiteResult]:
        results: dict[str, SuiteResult] = {}
        for name, rel, needs_db in SUITES:
            if needs_db and not env:
                r = SuiteResult(name)
                r.skip_reason = "database required: no dedicated PostgreSQL available"
                results[name] = r
                continue
            results[name] = run_suite(self.backend_dir, name, rel, env)
        return results

    def _source_files_for(self, failures: list[Failure]) -> dict[str, str]:
        wanted: list[str] = []
        for f in failures:
            for m in re.findall(r"(backend_app/[\w/]+\.py)", f.output + " " + f.message):
                if m not in wanted:
                    wanted.append(m)
        for default in ("backend_app/engine.py", "backend_app/rules.py"):
            if default not in wanted:
                wanted.append(default)
        files, budget = {}, 40_000
        for rel in wanted[:8]:
            p = self.backend_dir / rel
            if p.is_file() and "/generated/spec.json" not in rel:
                t = p.read_text(encoding="utf-8")
                if budget - len(t) < 0:
                    continue
                files[rel] = t
                budget -= len(t)
        return files

    def _correct(self, results: dict[str, SuiteResult], n: int, spec: dict[str, Any], pkg: GraphPackage) -> Attempt:
        failures = [f for r in results.values() for f in r.failures][:12]
        prompt_id = choose_prompt(failures)
        files = self._source_files_for(failures)
        ctx_spec = {"operations": {oid: {k: o_[k] for k in ("kind", "roles", "access")} for oid, o_ in spec["operations"].items()},
                    "rules": spec["rules"], "roles": spec["roles"], "state_machines": spec["state_machines"],
                    "conflicts": [{"code": c.code, "type": c.type.value, "severity": c.severity} for c in self.integration.conflicts]}
        failure_ctx = {"attempt": n, "kinds": sorted({f.kind.value for f in failures}), "failures": [f.to_dict() for f in failures]}
        schema = '{"diagnosis": "string", "edits": [{"path": "backend_app/...", "search": "exact text occurring once", "replace": "new text"}]}'

        def validate(d: Any) -> list[str]:
            if not isinstance(d, dict) or set(d) != {"diagnosis", "edits"}:
                return ["answer must be an object with exactly 'diagnosis' and 'edits'"]
            if not isinstance(d["edits"], list):
                return ["edits must be a list"]
            return [f"edit {i}: needs exactly path, search, replace (strings)" for i, e in enumerate(d["edits"])
                    if not isinstance(e, dict) or set(e) != {"path", "search", "replace"} or not all(isinstance(v, str) for v in e.values())]

        before = sum(len(r.failures) for r in results.values())
        try:
            data = self.client.call(prompt_id, {"FAILURE_JSON": _dump(failure_ctx), "SOURCE_FILES": _dump(files), "INPUT_JSON": _dump(ctx_spec), "OUTPUT_SCHEMA": schema},
                                    {"failure": failure_ctx, "files": files, "spec": ctx_spec}, validate)
        except LLMOutputError as e:
            att = Attempt(n, prompt_id, f"correction answer rejected: {e}", 0, 0, [str(e)], before)
            self.corrections.append(att)
            return att
        edits = [Edit.from_dict(e) for e in data["edits"]]
        applied = apply_edits(self.backend_dir, edits)
        att = Attempt(n, prompt_id, data["diagnosis"], len(edits), sum(1 for r in applied if r.applied), [f"{r.edit.path}: {r.reason}" for r in applied if not r.applied], before)
        self.corrections.append(att)
        return att

    # ---- final ---------------------------------------------------------------------------------------------------------------------------
    def _finish(self, spec, classes, static, api_ok, results, db_skip, attempts, stop, handlers, static_problems=None) -> PipelineResult:
        unimpl = [{"operation": oid, "reason": h.get("reason", "")} for oid, h in sorted(spec["handlers"].items()) if h["status"] != "implemented"]
        unimpl += [{"operation": oid, "reason": "no handler decision"} for oid, o_ in sorted(spec["operations"].items()) if o_["handler"] == "custom" and oid not in spec["handlers"]]
        report = build_report(project=self.pkg.project_id, graph_ok=True, graph_warnings=self.pkg.warnings, integration=self.integration, db_available=self.db is not None,
                              fe_available=self.fe is not None, static=static, api_compare_ok=api_ok, results=results, tests_requested=self.o.run_tests, correction_attempts=attempts,
                              correction_stop=stop, unimplemented=unimpl, advisory=self.advisory, llm_provider=self.provider_name, stage_log=self.log.summary(), db_skip_reason=db_skip)
        if static_problems:
            report["static_problems"] = [i.to_dict() for i in static_problems]
        fr_ctx = {"validation_report": {"status": report["status"], "warnings": report["warnings"][:30], "correction_attempts": attempts},
                  "conflict_codes": sorted({c.code for c in self.integration.conflicts})}
        try:
            report["advisory"]["final_review"] = self.llm.final_review(fr_ctx)
        except LLMOutputError:
            report["advisory"]["final_review"] = None
        self.art.save("backend_validation_report.json", report)
        code = EXIT_OK if report["status"] in ("passed", "passed_with_conflicts", "not_tested", "partial") else (EXIT_BLOCKED if report["status"] == "blocked" else EXIT_FAILED)
        return PipelineResult(report["status"], code, report, self.art.written, self.backend_dir)
