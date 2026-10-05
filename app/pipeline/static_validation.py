"""Static validation of the generated backend: it compiles, imports, builds its OpenAPI document, and contains no unsafe patterns."""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.errors import ErrorKind, Issue

IMPORT_SCRIPT = """
import json, sys
from backend_app.app_factory import create_app
from backend_app.config import Settings
app = create_app(Settings(jwt_secret="static-validation-" + "x" * 40, app_env="test"))
json.dump(app.openapi(), open(sys.argv[1], "w"), indent=2, sort_keys=True)
"""
UNSAFE = [
    (re.compile(r"allow_origins\s*=\s*\[\s*[\"']\*"), "wildcard CORS origin"),
    (re.compile(r"^\s*except\s*:", re.M), "bare except"),
    (re.compile(r"verify_signature['\"]?\s*[:=]\s*False"), "token signature verification disabled"),
    (re.compile(r"""(?i)(?:password|secret|api_key)\s*=\s*["'][^"']{6,}["']"""), "hard-coded credential"),
    (re.compile(r"\b(?:eval|exec)\s*\("), "eval/exec"),
]


@dataclass
class StaticResult:
    ok: bool
    problems: list[Issue] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)
    openapi: dict[str, Any] | None = None


def scan_sql_safety(tree: ast.AST, rel: str) -> list[Issue]:
    out: list[Issue] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute" and node.args:
            a = node.args[0]
            def _is_str(n: ast.AST) -> bool:
                return isinstance(n, ast.JoinedStr) or (isinstance(n, ast.Constant) and isinstance(n.value, str))

            bad = (isinstance(a, ast.JoinedStr)
                   or (isinstance(a, ast.BinOp) and isinstance(a.op, (ast.Mod, ast.Add)) and (_is_str(a.left) or _is_str(a.right)))  # str concatenation / % formatting
                   or (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) and a.func.attr == "format" and _is_str(a.func.value)))  # "...".format(...)
            if bad:
                out.append(Issue(ErrorKind.SECURITY_FAILURE, "unsafe_sql", f"{rel}:{node.lineno}: SQL built with string interpolation", rel))
    return out


def static_validate(backend_dir: Path) -> StaticResult:
    res = StaticResult(True)
    pkg = backend_dir / "backend_app"
    bad_compile: list[Issue] = []
    sql_issues: list[Issue] = []
    unsafe: list[Issue] = []
    for f in sorted(pkg.rglob("*.py")):
        rel = f.relative_to(backend_dir).as_posix()
        text = f.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=rel)
        except SyntaxError as e:
            bad_compile.append(Issue(ErrorKind.IMPORT_ERROR, "syntax_error", f"{rel}:{e.lineno}: {e.msg}", rel))
            continue
        sql_issues += scan_sql_safety(tree, rel)
        for pat, label in UNSAFE:
            if pat.search(text):
                unsafe.append(Issue(ErrorKind.SECURITY_FAILURE, "unsafe_pattern", f"{rel}: {label}", rel))
    res.checks["compile"] = "passed" if not bad_compile else "failed"
    res.checks["sql_safety"] = "passed" if not sql_issues else "failed"
    res.checks["unsafe_patterns"] = "passed" if not unsafe else "failed"
    res.problems += bad_compile + sql_issues + unsafe
    if not bad_compile:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "openapi.json"
            env = {**os.environ, "PYTHONPATH": str(backend_dir), "PYTHONDONTWRITEBYTECODE": "1"}
            p = subprocess.run([sys.executable, "-c", IMPORT_SCRIPT, str(out)], cwd=backend_dir, env=env, capture_output=True, text=True, timeout=120)
            if p.returncode != 0 or not out.exists():
                tail = "\n".join(p.stderr.strip().splitlines()[-6:])
                res.problems.append(Issue(ErrorKind.IMPORT_ERROR, "import_failed", f"the generated application cannot be imported/built: {tail}"))
                res.checks["import"] = res.checks["openapi"] = "failed"
            else:
                res.checks["import"] = res.checks["openapi"] = "passed"
                res.openapi = json.loads(out.read_text())
                (backend_dir / "openapi.json").write_text(json.dumps(res.openapi, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        res.checks["import"] = res.checks["openapi"] = "not_run"
    res.ok = not res.problems
    return res


def build_api_contract(openapi: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """The IMPLEMENTED contract (what the running app documents), keyed by graph endpoint id."""

    def props(node: dict[str, Any]) -> dict[str, Any]:
        def deref(n: dict[str, Any]) -> dict[str, Any]:
            if "$ref" in n:
                return openapi["components"]["schemas"][n["$ref"].split("/")[-1]]
            if "anyOf" in n:
                return deref(next(x for x in n["anyOf"] if x.get("type") != "null"))
            return n

        n = deref(node)
        if n.get("type") == "array":
            n = deref(n["items"])
        return {"fields": sorted(n.get("properties", {})), "required": sorted(n.get("required", []))}

    eps = []
    for path, item in sorted(openapi["paths"].items()):
        for method, op in sorted(item.items()):
            oid = op.get("operationId", "")
            if oid.startswith("health."):
                continue
            ok = sorted(c for c in op["responses"] if c.isdigit() and c.startswith("2"))
            req = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            resp = op["responses"][ok[0]].get("content", {}).get("application/json", {}).get("schema") if ok else None
            eps.append({"endpoint": oid, "method": method.upper(), "path": path, "operation": next((o for o, v in spec["operations"].items() if v["endpoint"]["id"] == oid), None),
                        "request": props(req) if req else None, "response": props(resp) if resp else None, "success_status": int(ok[0]) if ok else None,
                        "security": op.get("security", []), "documented_errors": sorted(c for c in op["responses"] if c.isdigit() and int(c) >= 400),
                        "query_parameters": sorted(p["name"] for p in op.get("parameters", []) if p.get("in") == "query"),
                        "roles": spec["operations"][next(o for o, v in spec["operations"].items() if v["endpoint"]["id"] == oid)]["roles"] if oid in {v["endpoint"]["id"] for v in spec["operations"].values()} else []})
    return {
        "api_versioning": spec["api"], "endpoints": eps,
        "infrastructure_endpoints": [{"method": "GET", "path": "/health", "purpose": "liveness"}, {"method": "GET", "path": "/ready", "purpose": "readiness (database reachable)"}],
        "compatibility_shims": [
            {"id": "login_session_envelope", "description": "Login returns the Frontend Session {token, user} AND the graph user fields at top level."},
            {"id": "error_envelope", "description": "Errors carry {error:{code,message,details}} plus top-level message/errors for the Frontend client."},
            {"id": "list_headers", "description": "Lists are bare arrays (Frontend contract); pagination via limit/offset query parameters, total in X-Total-Count."},
        ],
        "additive_query_parameters": ["limit", "offset", "sort", "order"],
    }


def compare_openapi_to_graph(openapi: dict[str, Any], endpoints: list[dict[str, Any]]) -> list[Issue]:
    out: list[Issue] = []
    seen = set()
    for ep in endpoints:
        item = openapi["paths"].get(ep["path"], {})
        op = item.get(ep["method"].lower())
        if op is None:
            out.append(Issue(ErrorKind.API_CONTRACT_ERROR, "endpoint_missing", f"{ep['method']} {ep['path']} ({ep['id']}) is not exposed", ep["id"]))
            continue
        if op.get("operationId") != ep["id"]:
            out.append(Issue(ErrorKind.API_CONTRACT_ERROR, "operation_id_differs", f"{ep['path']}: operationId {op.get('operationId')} != {ep['id']}", ep["id"]))
        seen.add((ep["path"], ep["method"].lower()))
    for path, item in openapi["paths"].items():
        for m in item:
            if (path, m) not in seen and path not in ("/health", "/ready"):
                out.append(Issue(ErrorKind.API_CONTRACT_ERROR, "extra_endpoint", f"{m.upper()} {path} is not in the graph", path))
    return out
