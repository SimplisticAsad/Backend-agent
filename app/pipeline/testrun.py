"""Running the generated backend's pytest suites as subprocesses and reading their JUnit XML."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from app.observability import scrub
from app.pipeline.failures import Failure, classify

# name, path, needs a database
SUITES = [
    ("unit", "tests/unit", False), ("contract", "tests/contract", False), ("repository", "tests/repository", True), ("api", "tests/api", True),
    ("security", "tests/security", True), ("db_failure", "tests/db_failure", True), ("bdd", "tests/bdd", True), ("frontend_compat", "tests/frontend_compat", True),
    ("e2e", "tests/e2e", True),
]


@dataclass
class SuiteResult:
    name: str
    ran: bool = False
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    duration_s: float = 0.0
    failures: list[Failure] = field(default_factory=list)
    skip_reason: str = ""
    known_gaps: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.ran:
            return "not_run" if not self.skip_reason else "skipped"
        if self.failed or self.errors:
            return "failed"
        if self.passed == 0 and not self.xfailed:
            return "no_tests"
        return "passed_with_known_gaps" if self.xfailed else "passed"

    def to_dict(self) -> dict:
        return {"status": self.status, "passed": self.passed, "failed": self.failed, "errors": self.errors, "skipped": self.skipped, "known_gaps": self.xfailed,
                "duration_s": round(self.duration_s, 2), "skip_reason": self.skip_reason, "known_gap_tests": self.known_gaps,
                "failures": [f.to_dict() for f in self.failures[:50]]}


def run_suite(backend_dir: Path, name: str, rel: str, env_extra: dict[str, str], timeout: int = 900) -> SuiteResult:
    res = SuiteResult(name)
    if not (backend_dir / rel).exists():
        res.skip_reason = "suite not generated for this project"
        return res
    with tempfile.TemporaryDirectory() as td:
        junit = Path(td) / "junit.xml"
        env = {**os.environ, "PYTHONPATH": str(backend_dir), "PYTHONDONTWRITEBYTECODE": "1", **env_extra}
        for k in ("DATABASE_URL", "DATABASE_SCHEMA"):
            env.pop(k, None)  # tests choose their own dedicated database
        t0 = time.time()
        try:
            proc = subprocess.run([sys.executable, "-m", "pytest", rel, "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", "-W", "ignore::DeprecationWarning", "--tb=short"],
                                  cwd=backend_dir, env=env, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            res.ran, res.errors = True, 1
            res.failures.append(Failure(name, rel, f"suite timed out after {timeout}s"))
            return res
        res.duration_s = time.time() - t0
        res.ran = True
        if not junit.exists():  # pytest could not even collect
            res.errors = 1
            msg = scrub((proc.stdout + proc.stderr)[-3000:])
            res.failures.append(Failure(name, rel, "pytest produced no report", msg, classify(name, rel, msg)))
            return res
        root = ET.parse(junit).getroot()
        for case in root.iter("testcase"):
            nodeid = f"{case.get('classname', '')}::{case.get('name', '')}"
            fail = case.find("failure")
            err = case.find("error")
            skip = case.find("skipped")
            if fail is not None or err is not None:
                node = fail if fail is not None else err
                text = scrub((node.get("message") or "") + "\n" + (node.text or ""))
                res.failures.append(Failure(name, nodeid, (node.get("message") or "")[:300], text, classify(name, nodeid, text)))
                if fail is not None:
                    res.failed += 1
                else:
                    res.errors += 1
            elif skip is not None:
                if "xfail" in (skip.get("type") or "") or "known gap" in (skip.get("message") or ""):
                    res.xfailed += 1
                    res.known_gaps.append(f"{case.get('name')}: {(skip.get('message') or '')[:160]}")
                else:
                    res.skipped += 1
                    res.skip_reason = res.skip_reason or (skip.get("message") or "")[:200]
            else:
                res.passed += 1
    return res
