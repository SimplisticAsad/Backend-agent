"""Do the generated test suites have teeth?  Break security-critical behaviour in a COPY of a generated backend and check the suites notice.

    python scripts/mutation_check.py projects/task_manager/backend
    python scripts/mutation_check.py projects/support_ticketing_system/backend

Each mutant removes or weakens one protection (ownership, role check, scope, state machine, token verification, CORS, error sanitising, ...).
A mutant is KILLED when the selected suites fail. A SURVIVOR is either a real gap in the tests or an *equivalent* mutant (a second line of defence still
holds, e.g. the response model strips password_hash even if the service leaks it): survivors are listed with a note so a human can judge.
Needs a dedicated PostgreSQL like the suites themselves (TEST_DATABASE_URL or a throw-away cluster).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# (name, file, original text, mutated text, suites to run, note shown if it survives)
MUTANTS = [
    ("skip ownership check", "backend_app/rules.py", '        if row.get(rule["field"]) != user_id:', "        if False:", "tests/security tests/api", ""),
    ("skip role check", "backend_app/auth.py", 'if view.operations[op_id]["access"] == "restricted" and principal.role_id not in view.allowed_role_ids(op_id):', "if False:", "tests/api tests/security", ""),
    ("allow undeclared transitions", "backend_app/rules.py", "    if match is None:", "    if False:", "tests/security tests/unit", ""),
    ("ignore transition guard", "backend_app/rules.py", "        if missing:", "        if False:", "tests/security tests/unit", ""),
    ("accept unknown request fields", "backend_app/api/schemas/{first_schema}.py", 'model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)',
     'model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)', "tests/security tests/contract tests/api", ""),
    ("login leaks password hash", "backend_app/auth_service.py", 'public = {k: v for k, v in row.items() if k != "password_hash"}', "public = dict(row)", "tests/api tests/security",
     "equivalent: the response model still strips password_hash (defence in depth)"),
    ("trust role claim in token", "backend_app/auth.py", 'role_id = self.view.role_by_key.get(row["role"])', 'role_id = self.view.role_by_key.get(claims.role or row["role"])', "tests/security", ""),
    ("no token revocation", "backend_app/security.py", "            return token_id in self._items and self._items[token_id] > int(time.time())", "            return False", "tests/api", ""),
    ("sort allow-list removed", "backend_app/repository.py", '        if sort_attr not in self.sortable and sort_attr != self.default_sort:', "        if False:", "tests/security tests/repository", ""),
    ("500 leaks exception text", "backend_app/errors.py", '        return _respond(request, 500, "INTERNAL_ERROR", "An unexpected error occurred.")', '        return _respond(request, 500, "INTERNAL_ERROR", repr(exc))', "tests/security", ""),
    ("wildcard CORS", "backend_app/app_factory.py", "allow_origins=list(settings.cors_origins)", 'allow_origins=["*"]', "tests/security", ""),
    ("no declared-size body limit", "backend_app/middleware.py", "if declared and declared.isdigit() and int(declared) > self.max_body:", "if False:", "tests/security",
     "equivalent: the streaming byte counter still enforces the limit (defence in depth)"),
    ("no scope on read", "backend_app/engine.py", "        if row is None or not self._in_scope(op, principal, conn, row):", "        if row is None:", "tests/security",
     "only observable in projects whose graph/heuristics define row scopes (carts, tickets)"),
    ("no list scope", "backend_app/engine.py", "                conds.append(R.scope_sql(self.view, op[\"entity\"], node, principal.user_id))", "                pass", "tests/security",
     "only observable in projects with row scopes"),
    ("no parent check on create", "backend_app/engine.py", 'if parent is None or not R.row_in_scope(self.view, self.repos, conn, node["parent_entity"], parent, node["parent_scope"], principal.user_id):', "if parent is None:", "tests/security",
     "only observable in projects with parent-chain scopes (ticket comments)"),
    ("frozen state ignored", "backend_app/rules.py", '        if row.get(rule["field"]) in rule["states"]:', "        if False:", "tests/security tests/unit", "only observable in projects with a frozen_state rule"),
]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    src = Path(argv[0]).resolve()
    schemas = sorted((src / "backend_app" / "api" / "schemas").glob("*.py"))
    first_schema = next((p.stem for p in schemas if p.stem != "__init__" and 'extra="forbid"' in p.read_text()), None)
    killed = survived = skipped = 0
    for name, rel, old, new, suites, note in MUTANTS:
        rel = rel.replace("{first_schema}", first_schema or "x")
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "b"
            shutil.copytree(src, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            f = work / rel
            if not f.exists() or old not in f.read_text():
                print(f"  n/a      {name}")
                skipped += 1
                continue
            f.write_text(f.read_text().replace(old, new, 1))
            r = subprocess.run([sys.executable, "-m", "pytest", *suites.split(), "-x", "-q", "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"], cwd=work,
                               capture_output=True, text=True, timeout=1200, env={**__import__("os").environ, "PYTHONPATH": str(work)})
        if r.returncode != 0:
            killed += 1
            print(f"  KILLED   {name}")
        else:
            survived += 1
            print(f"  SURVIVED {name}" + (f"   ({note})" if note else "   <-- investigate"))
    print(f"\n{killed} killed, {survived} survived, {skipped} not applicable to this project")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
