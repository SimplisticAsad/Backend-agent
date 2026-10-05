"""backend_validation_report.json: computed by Python from EXECUTED results. No LLM text can change a status."""
from __future__ import annotations

from typing import Any

from app.integration.conflicts import IntegrationReport
from app.pipeline.testrun import SuiteResult

_RANK = {"failed": 5, "not_run": 3, "skipped": 3, "no_tests": 3, "passed_with_known_gaps": 1, "passed_with_conflicts": 1, "passed": 0, "not_available": 0}


def worst(statuses: list[str]) -> str:
    present = [s for s in statuses if s != "not_available"]
    if not present:
        return "not_available"
    return max(present, key=lambda s: _RANK.get(s, 2))


def _suite(results: dict[str, SuiteResult], *names: str) -> str:
    sel = [results[n].status if n in results else "not_run" for n in names]
    return worst(sel)


def contract_status(report: IntegrationReport | None, kinds: set[str], ran: bool) -> str:
    if report is None or not ran:
        return "not_available"
    rel = [c for c in report.conflicts if c.type.value in kinds]
    if any(c.severity == "critical" for c in rel):
        return "failed"
    return "passed_with_conflicts" if any(c.severity in ("major", "minor") for c in rel) else "passed"


def build_report(*, project: str, graph_ok: bool, graph_warnings: list[str], integration: IntegrationReport, db_available: bool, fe_available: bool,
                 static: dict[str, str], api_compare_ok: bool | None, results: dict[str, SuiteResult], tests_requested: bool, correction_attempts: int,
                 correction_stop: str, unimplemented: list[dict[str, str]], advisory: dict[str, Any], llm_provider: str, stage_log: list[dict[str, Any]],
                 db_skip_reason: str = "") -> dict[str, Any]:
    suites = {n: r.to_dict() for n, r in results.items()}
    failed = [n for n, r in results.items() if r.status == "failed"]
    skipped_db = [n for n, r in results.items() if r.status == "skipped" and "database" in r.skip_reason.lower()]
    warnings: list[str] = list(graph_warnings)
    warnings += [f"{c.type.value}/{c.code}: {c.description}" for c in integration.conflicts if c.severity in ("major", "critical")]
    warnings += [f"operation {u['operation']} is not implemented (501): {u['reason']}" for u in unimplemented]
    for n, r in results.items():
        warnings += [f"known gap in {n}: {g}" for g in r.known_gaps]
        if r.skipped and r.status != "skipped":
            warnings.append(f"{n}: {r.skipped} test(s) skipped: {r.skip_reason}")
    if db_skip_reason:
        warnings.append(db_skip_reason)

    report: dict[str, Any] = {
        "project": project,
        "graph_validation": "passed" if graph_ok else "failed",
        "database_contract": contract_status(integration, {"DATABASE_CONTRACT_CONFLICT"}, db_available),
        "frontend_contract": contract_status(integration, {"FRONTEND_API_CONFLICT"}, fe_available),
        "api_contract": "failed" if api_compare_ok is False or _suite(results, "contract") == "failed" else ("passed" if api_compare_ok and _suite(results, "contract") == "passed" else ("static_only" if api_compare_ok else "not_run")),
        "type_checks": "passed" if all(v == "passed" for v in static.values()) and static else ("failed" if static else "not_run"),
        "static_checks": static,
        "unit_tests": _suite(results, "unit"),
        "contract_tests": _suite(results, "contract"),
        "database_tests": _suite(results, "repository", "db_failure"),
        "api_tests": _suite(results, "api"),
        "security_tests": _suite(results, "security"),
        "integration_tests": _suite(results, "bdd", "frontend_compat") if fe_available else _suite(results, "bdd"),
        "bdd_tests": _suite(results, "bdd"),
        "frontend_compat_tests": _suite(results, "frontend_compat") if fe_available else "not_available",
        "correction_attempts": correction_attempts,
        "correction_stopped_because": correction_stop,
        "integration_conflicts": integration.to_dict()["summary"],
        "unimplemented_operations": unimplemented,
        "suites": suites,
        "llm_provider": llm_provider,
        "advisory": advisory,
        "warnings": warnings,
        "stages": stage_log,
    }
    if integration.status == "blocked":
        status = "blocked"
    elif not graph_ok or failed or "failed" in static.values() or api_compare_ok is False:
        status = "failed"
    elif not tests_requested or not results:
        status = "not_tested"
    elif skipped_db or any(r.status in ("skipped", "not_run") for n, r in results.items() if n in ("repository", "api", "security", "db_failure", "bdd")):
        status = "partial"
    elif integration.status == "conflicts_reported" or unimplemented or any(r.xfailed for r in results.values()):
        status = "passed_with_conflicts"
    else:
        status = "passed"
    report["status"] = status
    return report
