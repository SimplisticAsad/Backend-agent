"""Frontend contract: what the Frontend Agent's generated client will actually send and expect.

Two sources are combined:
  1. The Frontend agent's project graphs (its own `nodes` format: api / entities / roles / permissions / state_machines),
     snapshotted under `<project>/frontend/graphs/` or read from a Frontend-Agent checkout.
  2. The expectations baked into the Frontend agent's generated API client (`src/lib/api/client.ts`):
     error body keys, session shape, bare-array lists, Bearer auth, 204 handling, `/api` prefix stripped by the dev proxy.
     `CLIENT_EXPECTATIONS` mirrors that file; `verify_client_expectations` re-checks it against a checkout when available.

The backend never modifies the Frontend repository; this module is read-only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.errors import ContractError, ErrorKind, Issue

CLIENT_EXPECTATIONS: dict[str, Any] = {
    "base_path": "/api",  # stripped by the Vite dev proxy: the backend serves paths WITHOUT this prefix
    "dev_origin": "http://localhost:5173",
    "auth_header": "Authorization: Bearer <token>",
    "list_shape": "bare JSON array (no pagination envelope)",
    "no_content_status": 204,
    "session_shape": {"token": "string", "user": "AuthUser"},
    "error_body_keys": {"message": "string (<=200 chars, single line)", "errors": "{field: string | string[]}"},
    "validation_statuses": [400, 422],
    "handled_statuses": [400, 401, 403, 404, 409, 422, 429, 500],
}


@dataclass
class FField:
    name: str
    type: str
    required: bool
    source: str = "form"  # form | path | query | input | session.user_id | session.role


@dataclass
class FEndpoint:
    id: str
    method: str
    path: str
    auth: bool
    permission: str | None
    fields: list[FField]
    response_shape: str  # list | single | none | session
    response_entity: str | None
    errors: list[int]
    roles: list[str] = field(default_factory=list)  # role keys granted by `permission`

    @property
    def norm_path(self) -> str:
        return re.sub(r"\{\w+\}", "{}", self.path)


@dataclass
class FrontendContract:
    endpoints: list[FEndpoint]
    entities: dict[str, dict[str, Any]]
    roles: dict[str, dict[str, Any]]
    state_machines: dict[str, dict[str, Any]]
    permissions: dict[str, dict[str, Any]]
    expectations: dict[str, Any]
    origin: str = ""

    def role_keys_for_permission(self, perm: str | None) -> list[str]:
        if not perm or perm not in self.permissions:
            return []
        return sorted(self.roles[r]["key"] for r in self.permissions[perm].get("roles", []) if r in self.roles)

    def state_machine_for(self, entity_id: str) -> dict[str, Any] | None:
        return next((s for s in self.state_machines.values() if s.get("entity") == entity_id), None)


def _nodes(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContractError(Issue(ErrorKind.FRONTEND_CONTRACT_ERROR, "artifact_missing", f"frontend graph {path.name} not found", str(path)))
    except json.JSONDecodeError as e:
        raise ContractError(Issue(ErrorKind.FRONTEND_CONTRACT_ERROR, "artifact_invalid", f"{path.name}: {e}", str(path)))
    nodes = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(nodes, list):
        raise ContractError(Issue(ErrorKind.FRONTEND_CONTRACT_ERROR, "unexpected_format",
                                  f"{path.name}: expected the Frontend agent's {{'nodes': [...]}} format", str(path)))
    return nodes


def load_frontend_contract(directory: str | Path) -> FrontendContract:
    """`directory` holds the Frontend agent's graph files (api.json, entities.json, ...), optionally under graphs/."""
    d = Path(directory)
    if (d / "graphs").is_dir():
        d = d / "graphs"
    manifest_files: dict[str, str] = {}
    if (d / "graph_manifest.json").exists():
        manifest_files = json.loads((d / "graph_manifest.json").read_text(encoding="utf-8")).get("files", {})

    def f(name: str) -> Path:
        return d / manifest_files.get(name, f"{name}.json")

    roles = {n["id"]: n for n in _nodes(f("roles"))}
    perms = {n["id"]: n for n in _nodes(f("permissions"))}
    entities = {n["id"]: n for n in _nodes(f("entities"))}
    sms = {n["id"]: n for n in _nodes(f("state_machines"))} if f("state_machines").exists() else {}
    endpoints: list[FEndpoint] = []
    role_key = {rid: r["key"] for rid, r in roles.items()}
    for n in _nodes(f("api")):
        req = n.get("request") or {}
        resp = n.get("response") or {}
        perm = n.get("permission")
        granted = sorted(role_key[r] for r in (perms.get(perm, {}).get("roles", []) if perm else []) if r in role_key)
        endpoints.append(FEndpoint(
            n["id"], n["method"].upper(), n["path"], bool(n.get("auth", True)), perm,
            [FField(x["name"], x.get("type", "string"), bool(x.get("required")), x.get("source", "form")) for x in req.get("fields", [])],
            resp.get("shape", "none"), resp.get("entity"), list(n.get("errors", [])), granted,
        ))
    return FrontendContract(endpoints, entities, roles, sms, perms, dict(CLIENT_EXPECTATIONS), origin=str(d))


def verify_client_expectations(frontend_repo: str | Path) -> list[Issue]:
    """Re-check CLIENT_EXPECTATIONS against `<repo>/app/generation/templates/frontend/src/lib/api/client.ts` (read-only)."""
    p = Path(frontend_repo) / "app/generation/templates/frontend/src/lib/api/client.ts"
    if not p.exists():
        return [Issue(ErrorKind.FRONTEND_CONTRACT_ERROR, "client_not_found", f"cannot find the Frontend client at {p}", str(p))]
    src = p.read_text(encoding="utf-8")
    checks = [
        (r"body\.message", "reads error.message from the top level of the error body"),
        (r"body\.errors", "reads field errors from the top level 'errors' key"),
        (r"Bearer \$\{token\}", "sends 'Authorization: Bearer <token>'"),
        (r"status === 204", "treats 204 as an empty success"),
        (r"\?\? '/api'", "uses /api as the default base path"),
    ]
    return [Issue(ErrorKind.FRONTEND_CONTRACT_ERROR, "client_expectation_changed", f"Frontend client no longer {what}", str(p))
            for pat, what in checks if not re.search(pat, src)]
