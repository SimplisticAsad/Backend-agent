"""Classification of test failures into the error taxonomy that selects the correction prompt."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.errors import ErrorKind


@dataclass
class Failure:
    suite: str
    nodeid: str
    message: str
    output: str = ""
    kind: ErrorKind = ErrorKind.TEST_FAILURE

    def to_dict(self) -> dict:
        return {"suite": self.suite, "test": self.nodeid, "kind": self.kind.value, "message": self.message[:600], "output": self.output[:3000]}


def classify(suite: str, nodeid: str, text: str) -> ErrorKind:
    t = text
    if re.search(r"ModuleNotFoundError|ImportError|cannot import name", t):
        return ErrorKind.IMPORT_ERROR
    if suite == "security":
        return ErrorKind.AUTHORIZATION_ERROR if re.search(r"403|FORBIDDEN|role|owner|IDOR|escalat", t, re.I) and not re.search(r"inject|leak", t, re.I) else ErrorKind.SECURITY_FAILURE
    if suite == "contract":
        return ErrorKind.API_CONTRACT_ERROR
    if suite == "frontend_compat":
        return ErrorKind.FRONTEND_CONTRACT_ERROR
    if re.search(r"psycopg\.errors|UndefinedTable|UndefinedColumn|sqlstate|relation \".*\" does not exist|column .* does not exist", t, re.I):
        return ErrorKind.DATABASE_ERROR
    if suite in ("repository", "db_failure") and re.search(r"DATABASE_|rollback|transaction", t, re.I):
        return ErrorKind.DATABASE_ERROR
    if re.search(r"INVALID_STATE_TRANSITION|state_machine|transition", t, re.I):
        return ErrorKind.STATE_TRANSITION_ERROR
    if re.search(r"TypeError|AttributeError|NameError", t):
        return ErrorKind.TYPE_ERROR
    if re.search(r"pydantic|ValidationError|VALIDATION_ERROR| 422", t):
        return ErrorKind.VALIDATION_ERROR
    if re.search(r"\b(RuntimeError|KeyError|ValueError|IndexError|ZeroDivisionError)\b", t):
        return ErrorKind.RUNTIME_ERROR
    if suite in ("bdd", "api"):
        return ErrorKind.INTEGRATION_FAILURE if re.search(r"\b5\d\d\b", t) else ErrorKind.TEST_FAILURE
    return ErrorKind.TEST_FAILURE


PROMPT_FOR_KIND = {
    ErrorKind.IMPORT_ERROR: "runtime_correction", ErrorKind.TYPE_ERROR: "runtime_correction", ErrorKind.RUNTIME_ERROR: "runtime_correction",
    ErrorKind.TEST_FAILURE: "runtime_correction", ErrorKind.VALIDATION_ERROR: "runtime_correction", ErrorKind.STATE_TRANSITION_ERROR: "runtime_correction",
    ErrorKind.INTEGRATION_FAILURE: "runtime_correction", ErrorKind.DATABASE_ERROR: "database_error_correction",
    ErrorKind.API_CONTRACT_ERROR: "api_contract_correction", ErrorKind.FRONTEND_CONTRACT_ERROR: "api_contract_correction",
    ErrorKind.SECURITY_FAILURE: "security_correction", ErrorKind.AUTHORIZATION_ERROR: "security_correction",
}
PRIORITY = ["security_correction", "database_error_correction", "api_contract_correction", "runtime_correction"]


def choose_prompt(failures: list[Failure]) -> str:
    wanted = {PROMPT_FOR_KIND.get(f.kind, "runtime_correction") for f in failures}
    return next((p for p in PRIORITY if p in wanted), "runtime_correction")
