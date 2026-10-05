"""Deterministic graph loading: manifest first, then the graphs the backend consumes.

Checks done here (structural, before semantic validation):
  * graph_manifest.json exists, parses, names the project
  * every required graph is listed and present, parses, has the expected list key
  * manifest SHA-256 checksums match file contents (a tampered/stale package is rejected)
  * every file's project_id matches the manifest
  * if the Graph agent's own validation_report.json exists it must say status=valid & safe_for_downstream
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.errors import ErrorKind, GraphError, Issue
from app.graph.model import BACKEND_REQUIRED, LIST_KEYS, GraphPackage

MANIFEST = "graph_manifest.json"


def resolve_project(path: str | Path) -> tuple[Path, Path]:
    """Return (project_dir, graphs_dir) for a project directory or a graphs directory."""
    p = Path(path)
    if (p / MANIFEST).exists():
        return (p.parent if p.name == "graphs" else p), p
    if (p / "graphs" / MANIFEST).exists():
        return p, p / "graphs"
    raise GraphError(Issue(ErrorKind.GRAPH_ERROR, "missing_manifest", f"no {MANIFEST} in {p} or {p / 'graphs'}", str(p)))


def _read(path: Path, issues: list[Issue]) -> tuple[Any, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        issues.append(Issue(ErrorKind.GRAPH_ERROR, "file_missing", f"missing graph file {path.name}", str(path)))
        return None, None
    try:
        return json.loads(text), text
    except json.JSONDecodeError as e:
        issues.append(Issue(ErrorKind.GRAPH_ERROR, "invalid_json", f"{path.name} is not valid JSON: {e}", str(path)))
        return None, None


def load_graph_package(path: str | Path, *, verify_checksums: bool = True) -> GraphPackage:
    project_dir, gdir = resolve_project(path)
    issues: list[Issue] = []
    manifest, _ = _read(gdir / MANIFEST, issues)
    if not isinstance(manifest, dict):
        raise GraphError(issues or [Issue(ErrorKind.GRAPH_ERROR, "manifest_shape", "manifest must be a JSON object")])
    graphs: dict[str, str] = manifest.get("graphs") or {}
    if not manifest.get("project_id"):
        issues.append(Issue(ErrorKind.GRAPH_ERROR, "manifest_no_project", "manifest has no project_id", MANIFEST))
    if not isinstance(graphs, dict) or not graphs:
        issues.append(Issue(ErrorKind.GRAPH_ERROR, "manifest_no_graphs", "manifest lists no graphs", MANIFEST))
        raise GraphError(issues)

    docs: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for name in BACKEND_REQUIRED:
        rel = graphs.get(name)
        if not rel:
            issues.append(Issue(ErrorKind.GRAPH_ERROR, "manifest_entry_missing", f"manifest has no entry for graph '{name}'", MANIFEST))
            continue
        doc, text = _read(gdir / rel, issues)
        if doc is None:
            continue
        if not isinstance(doc, dict):
            issues.append(Issue(ErrorKind.GRAPH_ERROR, "graph_shape", f"{rel} must be a JSON object", rel))
            continue
        if verify_checksums:
            expected = (manifest.get("checksums") or {}).get(name)
            if expected and text is not None and hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
                issues.append(Issue(ErrorKind.GRAPH_ERROR, "checksum_mismatch", f"{rel} does not match the manifest checksum (stale or edited)", rel))
        if doc.get("project_id") != manifest.get("project_id"):
            issues.append(Issue(ErrorKind.GRAPH_ERROR, "project_id_mismatch", f"{rel}: project_id {doc.get('project_id')!r} != manifest {manifest.get('project_id')!r}", rel))
        key = LIST_KEYS.get(name)
        if key and not isinstance(doc.get(key), list):
            issues.append(Issue(ErrorKind.GRAPH_ERROR, "missing_list", f"{rel}: expected a list under '{key}'", rel))
        docs[name] = doc
    if "backend" in docs:
        for k in ("services", "operations"):
            if not isinstance(docs["backend"].get(k), list):
                issues.append(Issue(ErrorKind.GRAPH_ERROR, "missing_list", f"backend.json: expected a list under '{k}'", "backend.json"))
    if "api" in docs:
        if not isinstance(docs["api"].get("schemas"), list):
            issues.append(Issue(ErrorKind.GRAPH_ERROR, "missing_list", "api.json: expected a list under 'schemas'", "api.json"))
    if issues:
        raise GraphError(issues)

    report = None
    rpath = project_dir / "validation" / "validation_report.json"
    if rpath.exists():
        report, _ = _read(rpath, issues)
        if isinstance(report, dict):
            if report.get("status") != "valid" or not report.get("safe_for_downstream", False):
                raise GraphError(Issue(ErrorKind.GRAPH_ERROR, "graph_agent_report_invalid",
                                       "the Graph agent's validation report says this package is not safe for downstream agents",
                                       str(rpath)))
        if issues:
            raise GraphError(issues)
    else:
        warnings.append("no validation/validation_report.json from the Graph agent; relying on the backend's own validation only")
    return GraphPackage(root=str(gdir), manifest=manifest, docs=docs, validation_report=report, warnings=warnings)
