"""Static safety checks for code and tests written by an LLM. Anything that fails is REJECTED, never silently accepted.

Handler bodies (service-method bodies) and generated custom tests are untrusted input: they are parsed (never executed) and must
satisfy a small, strict policy before they are written into the generated backend.
"""
from __future__ import annotations

import ast
import re
import textwrap

FORBIDDEN_CALLS = {"eval", "exec", "compile", "open", "__import__", "input", "globals", "locals", "vars", "setattr", "delattr", "breakpoint", "getattr"}
FORBIDDEN_NAMES = {"os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib", "ctypes", "pickle", "marshal", "requests", "httpx", "urllib"}
DDL = re.compile(r"\b(DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|COPY|COMMIT|ROLLBACK|BEGIN|SAVEPOINT|SET\s+ROLE|SET\s+SEARCH_PATH)\b", re.I)
SQL_WORD = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.I)


def check_handler_body(body: str, *, allowed_tables: set[str] | None = None) -> list[str]:
    """Problems with a service-method body (empty list = acceptable)."""
    problems: list[str] = []
    src = "def _handler(self, principal, *, row_id=None, body=None, query=None):\n" + textwrap.indent(body.rstrip() + "\n", "    ")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"handler does not parse: {e.msg} (line {e.lineno})"]
    uses_execute = False
    uses_tx = "self.db.transaction" in body
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            problems.append("imports are not allowed in a handler body")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            problems.append("global/nonlocal are not allowed")
        elif isinstance(node, ast.ExceptHandler):
            if node.type is None:
                problems.append("bare 'except:' is not allowed")
            elif isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
                problems.append("catching Exception/BaseException is not allowed")
            if all(isinstance(b, ast.Pass) for b in node.body):
                problems.append("an exception handler may not simply pass (errors must not be swallowed)")
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            if isinstance(f, ast.Name) and f.id in FORBIDDEN_CALLS:
                problems.append(f"call to {f.id}() is not allowed")
            if isinstance(f, ast.Attribute) and f.attr == "execute":
                uses_execute = True
                arg = node.args[0] if node.args else None
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    problems.append("conn.execute() must receive a string LITERAL (bound parameters only; no f-strings, concatenation, % or .format)")
                else:
                    if DDL.search(arg.value):
                        problems.append(f"forbidden SQL keyword in statement: {arg.value[:60]!r}")
                    if allowed_tables is not None:
                        for t in re.findall(r'(?:FROM|JOIN|INTO|UPDATE)\s+"?([a-z_][a-z0-9_]*)"?', arg.value, re.I):
                            if t.lower() not in allowed_tables and t.lower() not in ("select", "lateral", "only"):
                                problems.append(f"statement references unknown table '{t}'")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"dunder attribute access '{node.attr}' is not allowed")
        elif isinstance(node, (ast.Await, ast.AsyncFunctionDef, ast.Yield, ast.YieldFrom)):
            problems.append("handlers are plain synchronous code")
    if uses_execute and not uses_tx:
        problems.append("every conn.execute() must run inside `with self.db.transaction() as conn:`")
    if not any(isinstance(n, ast.Return) for n in ast.walk(tree)) and "raise " not in body:
        problems.append("a handler must return a result (or raise)")
    return sorted(set(problems))


ALLOWED_TEST_IMPORTS = {"pytest", "uuid", "decimal", "datetime", "json", "tests", "re", "time", "threading", "backend_app"}


def check_test_module(source: str) -> list[str]:
    """Problems with an LLM-written custom test module."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"test module does not parse: {e.msg} (line {e.lineno})"]
    problems: list[str] = []
    tests = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    if not tests:
        problems.append("test module defines no test_ function")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_TEST_IMPORTS:
                    problems.append(f"import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_TEST_IMPORTS:
                problems.append(f"import from '{node.module}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr in ("skip", "xfail", "skipif") and isinstance(node.value, (ast.Name, ast.Attribute)):
            problems.append("tests must not skip or xfail")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            problems.append(f"call to {node.func.id}() is not allowed in tests")
        elif isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and node.test.value is True:
            problems.append("'assert True' proves nothing")
        elif isinstance(node, ast.ExceptHandler) and (node.type is None or all(isinstance(b, ast.Pass) for b in node.body)):
            problems.append("tests must not swallow exceptions")
    asserting_helpers = {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("test_") and any(isinstance(x, ast.Assert) for x in ast.walk(n))}
    for fn in tests:
        direct = any(isinstance(n, ast.Assert) for n in ast.walk(fn))
        delegated = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in asserting_helpers for n in ast.walk(fn))
        raises = any(isinstance(n, ast.Attribute) and n.attr == "raises" for n in ast.walk(fn))
        if not (direct or delegated or raises):
            problems.append(f"{fn.name} contains no assertion (directly or through a helper)")
    return sorted(set(problems))
