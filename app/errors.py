"""Error taxonomy shared by every pipeline stage.

`ErrorKind` is the classification the correction loop routes on (one specialised prompt per kind).
`Issue` is a structured, serialisable problem report. `BackendAgentError` carries a list of issues.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ErrorKind(str, Enum):
    GRAPH_ERROR = "GRAPH_ERROR"
    GRAPH_REFERENCE_ERROR = "GRAPH_REFERENCE_ERROR"
    DATABASE_CONTRACT_ERROR = "DATABASE_CONTRACT_ERROR"
    FRONTEND_CONTRACT_ERROR = "FRONTEND_CONTRACT_ERROR"
    TYPE_ERROR = "TYPE_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    AUTHORIZATION_ERROR = "AUTHORIZATION_ERROR"
    STATE_TRANSITION_ERROR = "STATE_TRANSITION_ERROR"
    API_CONTRACT_ERROR = "API_CONTRACT_ERROR"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    TEST_FAILURE = "TEST_FAILURE"
    INTEGRATION_FAILURE = "INTEGRATION_FAILURE"
    SECURITY_FAILURE = "SECURITY_FAILURE"
    CONFIG_ERROR = "CONFIG_ERROR"


class ConflictType(str, Enum):
    GRAPH_API_CONFLICT = "GRAPH_API_CONFLICT"
    GRAPH_FORMAT_CONFLICT = "GRAPH_FORMAT_CONFLICT"
    GRAPH_GAP = "GRAPH_GAP"
    DATABASE_CONTRACT_CONFLICT = "DATABASE_CONTRACT_CONFLICT"
    FRONTEND_API_CONFLICT = "FRONTEND_API_CONFLICT"


@dataclass(frozen=True)
class Issue:
    kind: ErrorKind
    code: str
    message: str
    location: str | None = None
    refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["refs"] = list(self.refs)
        return d

    def __str__(self) -> str:
        loc = f" [{self.location}]" if self.location else ""
        return f"{self.kind.value}/{self.code}: {self.message}{loc}"


class BackendAgentError(Exception):
    """A stage cannot continue. Carries structured issues; the CLI maps it to an exit code."""

    exit_code = 1

    def __init__(self, issues: list[Issue] | Issue | str, kind: ErrorKind = ErrorKind.INTEGRATION_FAILURE):
        if isinstance(issues, str):
            issues = [Issue(kind, "error", issues)]
        elif isinstance(issues, Issue):
            issues = [issues]
        self.issues: list[Issue] = list(issues)
        super().__init__("; ".join(str(i) for i in self.issues) or "backend agent error")

    @property
    def kinds(self) -> set[ErrorKind]:
        return {i.kind for i in self.issues}


class GraphError(BackendAgentError):
    exit_code = 2


class ContractError(BackendAgentError):
    exit_code = 2


class IntegrationBlocked(BackendAgentError):
    """Critical integration conflicts: generation must not proceed."""

    exit_code = 3


class GenerationFailed(BackendAgentError):
    exit_code = 4
