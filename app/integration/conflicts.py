"""Integration conflict detection across the Graph, Database and Frontend sources.

Source-of-truth hierarchy (never silently inverted):
    product + domain -> Graph Agent | persistence -> Database Agent | client expectations -> Frontend Agent (+ graph API)

Severity semantics
    critical  the backend cannot be correct against these sources (inconsistent graph, database cannot hold the
              data, ...). Generation is BLOCKED; nothing is generated from a contradictory specification.
    major     sources disagree but the backend can follow the hierarchy (graph wins) and stay internally correct.
              Reported with the impact on the other layer and a recommended resolution. Generation continues.
    minor     cosmetic/additive differences, or a conflict the backend resolved with an explicit, documented shim.
    info      noted for traceability.
Every conflict has: type, severity, sources, impact, recommended resolution (+ resolution actually applied, if any).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.contracts.database import GRAPH_TO_PG, DatabaseContract, pg_family
from app.contracts.frontend import FrontendContract
from app.errors import ConflictType, ErrorKind
from app.graph.model import GraphPackage, entity_key, find_auth_entity

_PATH_PARAM = re.compile(r"\{\w+\}")
SEVERITIES = ("critical", "major", "minor", "info")


@dataclass
class Conflict:
    type: ConflictType
    severity: str
    code: str
    description: str
    sources: list[str]
    impact: str
    recommended_resolution: str
    kind: ErrorKind = ErrorKind.INTEGRATION_FAILURE
    resolution_applied: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"], d["kind"] = self.type.value, self.kind.value
        return d


@dataclass
class IntegrationReport:
    conflicts: list[Conflict] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.severity == "critical"]

    @property
    def status(self) -> str:
        if self.blocking:
            return "blocked"
        if any(c.severity in ("major", "minor") for c in self.conflicts):
            return "conflicts_reported"
        return "clean"

    def by_type(self, t: ConflictType) -> list[Conflict]:
        return [c for c in self.conflicts if c.type == t]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "summary": {s: sum(1 for c in self.conflicts if c.severity == s) for s in SEVERITIES},
            "checked": self.checked,
            "skipped": self.skipped,
            "conflicts": [c.to_dict() for c in sorted(self.conflicts, key=lambda c: (SEVERITIES.index(c.severity), c.type.value, c.code, c.description))],
        }


# ---------------------------------------------------------------------------------------------------
def detect_graph_conflicts(pkg: GraphPackage) -> list[Conflict]:
    out: list[Conflict] = []
    add = lambda *a, **k: out.append(Conflict(*a, **k))  # noqa: E731
    A, G = ConflictType.GRAPH_API_CONFLICT, ConflictType.GRAPH_GAP

    for op in pkg.operations.values():
        ep = pkg.endpoint_by_operation.get(op["id"])
        op_roles = set(op.get("required_roles", []))
        if ep:
            ep_roles, ep_auth = set(ep["authorization"].get("roles", [])), ep["authorization"].get("authenticated", True)
            if op["access"] == "restricted" and ep_roles != op_roles:
                add(A, "critical", "ENDPOINT_ROLES_DIFFER_FROM_OPERATION",
                    f"{ep['id']} authorizes {sorted(ep_roles)} but {op['id']} requires {sorted(op_roles)}",
                    [ep["id"], op["id"]], "authorization cannot be implemented unambiguously", "make api.json and backend.json agree on the roles",
                    ErrorKind.AUTHORIZATION_ERROR)
            if (op["access"] == "public") == bool(ep_auth):
                add(A, "critical", "ENDPOINT_AUTH_DIFFERS_FROM_OPERATION",
                    f"{ep['id']} authenticated={ep_auth} but {op['id']} access={op['access']}", [ep["id"], op["id"]],
                    "unclear whether the endpoint is public", "align endpoint.authorization.authenticated with operation.access", ErrorKind.AUTHORIZATION_ERROR)
            if op["type"] == "command" and ep["method"] == "GET":
                add(A, "critical", "COMMAND_ON_GET", f"{ep['id']} maps command {op['id']} to GET", [ep["id"], op["id"]],
                    "state-changing operation reachable by a safe/cacheable verb", "use POST/PATCH/PUT/DELETE for commands", ErrorKind.API_CONTRACT_ERROR)
            if op["type"] == "query" and ep["method"] != "GET":
                add(A, "critical", "QUERY_ON_NON_GET", f"{ep['id']} maps query {op['id']} to {ep['method']}", [ep["id"], op["id"]],
                    "read operation using a mutating verb", "use GET for queries", ErrorKind.API_CONTRACT_ERROR)
            sch = pkg.schemas.get(ep.get("request_schema_ref") or "")
            if sch:
                sf = {f["name"] for f in sch["fields"]}
                allowed = set(op["input"]["required_fields"]) | set(op["input"]["optional_fields"]) | set(op["input"]["extra_fields"])
                missing_required = (set(op["input"]["required_fields"]) | set(op["input"]["extra_fields"])) - {"id"} - sf
                extra = sf - allowed
                if missing_required or extra:
                    add(A, "critical", "REQUEST_SCHEMA_DIFFERS_FROM_OPERATION",
                        f"{sch['id']} fields {sorted(sf)} do not match {op['id']} input (required/extra not in schema: {sorted(missing_required)}, schema fields not in operation: {sorted(extra)})",
                        [sch["id"], op["id"]], "the request contract is ambiguous", "regenerate api.json/backend.json so both describe the same input", ErrorKind.API_CONTRACT_ERROR)
        # permission roles vs operation roles
        for p in pkg.permissions.values():
            if op["id"] in p.get("operation_refs", []) and op["access"] == "restricted" and set(p["roles"]) != op_roles:
                add(A, "critical", "PERMISSION_ROLES_DIFFER_FROM_OPERATION",
                    f"{p['id']} grants {sorted(p['roles'])} but {op['id']} requires {sorted(op_roles)}", [p["id"], op["id"]],
                    "roles that may run the operation are contradictory", "make permissions.json and backend.json agree", ErrorKind.AUTHORIZATION_ERROR)
    # every protected operation must be backed by a permission (or an explicit authorization assumption)
    covered = {o for p in pkg.permissions.values() for o in p.get("operation_refs", [])}
    for op in pkg.operations.values():
        if op["access"] == "restricted" and op["id"] not in covered and not op.get("assumption_refs"):
            add(A, "critical", "PERMISSION_MISSING", f"{op['id']} is restricted to {sorted(op['required_roles'])} but no permission grants it and no authorization assumption explains it",
                [op["id"]], "authorization cannot be traced to the specification", "add a permission for the operation in permissions.json", ErrorKind.AUTHORIZATION_ERROR)
    # workflow actors must be able to run the operations their steps call
    for w in pkg.workflows.values():
        actor_roles = {r for a in w.get("actor_refs", []) for r in (pkg.actors[a].get("role_refs") or [pkg.actors[a].get("role_ref")]) if r}
        for step in w.get("steps", []):
            op = pkg.operations.get(step.get("operation_ref") or "")
            if op and op["access"] == "restricted" and actor_roles and not (pkg.roles_granting(op["required_roles"]) & actor_roles):
                add(A, "critical", "WORKFLOW_ACTOR_CANNOT_RUN_OPERATION",
                    f"{w['id']} step {step['id']} calls {op['id']} but its actors hold {sorted(actor_roles)} (needs {op['required_roles']})",
                    [w["id"], op["id"]], "a specified workflow can never succeed", "fix the workflow's actors or the operation's roles", ErrorKind.AUTHORIZATION_ERROR)
    # state machine transition roles vs operation roles
    for sm in pkg.state_machines.values():
        for t in sm["transitions"]:
            op = pkg.operations.get(t.get("operation_ref") or "")
            if op and op["access"] == "restricted":
                unreachable = set(t.get("role_refs", [])) - pkg.roles_granting(op["required_roles"])
                if unreachable:
                    add(A, "critical", "TRANSITION_ROLE_CANNOT_CALL_OPERATION",
                        f"{sm['id']} {t['from']}->{t['to']} lets {sorted(unreachable)} transition, but {op['id']} is restricted to {op['required_roles']}",
                        [sm["id"], op["id"]], "transition permission is contradictory", "align the transition roles with the operation", ErrorKind.STATE_TRANSITION_ERROR)
    # auth: role enum values vs roles
    auth = find_auth_entity(pkg)
    login = next((o for o in pkg.operations.values() if o["id"].endswith(".login")), None)
    if login and not auth:
        add(G, "critical", "LOGIN_WITHOUT_CREDENTIAL_ENTITY",
            "the graph defines a login operation but no entity has email + password_hash + role", [login["id"]],
            "authentication cannot be implemented", "add a credential-bearing user entity to the graph", ErrorKind.GRAPH_ERROR)
    if auth:
        role_attr = next(a for a in auth["attributes"] if a["name"] == "role")
        for v in role_attr["enum_values"]:
            if pkg.role_id_for_key(v) is None:
                add(G, "critical", "ROLE_VALUE_WITHOUT_ROLE", f"{role_attr['id']} allows '{v}' but there is no role.{v}", [role_attr["id"]],
                    "users with that role could not be authorized", f"add role.{v} to roles.json or remove the enum value", ErrorKind.AUTHORIZATION_ERROR)
        if not any(o["entity_ref"] == auth["id"] and o["action"] == "create" for o in pkg.operations.values()):
            add(G, "major", "NO_USER_PROVISIONING", f"no operation creates {auth['id']}: users can log in but cannot be created through the API",
                [auth["id"]], "the product cannot onboard its first user via the API",
                "add a create-user (or self-registration) operation to the graph",
                resolution_applied="generated backend ships `python -m backend_app.manage create-user` for provisioning", kind=ErrorKind.INTEGRATION_FAILURE)
    # fields that rules depend on but no operation can ever set
    settable: dict[str, set[str]] = {}
    for op in pkg.operations.values():
        if op["entity_ref"]:
            settable.setdefault(op["entity_ref"], set()).update(op["input"]["required_fields"] + op["input"]["optional_fields"])
    for val in pkg.validations.values():
        t = val["target"]
        if val["kind"] in ("state", "ownership") and t.get("field") and t["field"] not in settable.get(t["entity_ref"], set()):
            if t["field"] in ("assignee_id", "owner_id", "requester_id", "user_id"):
                add(G, "major", "RULE_FIELD_NEVER_SETTABLE",
                    f"{val['id']} depends on {t['entity_ref']}.{t['field']} but no operation in the graph can set it",
                    [val["id"], t["entity_ref"]], "the rule can never be satisfied/exercised through the API (e.g. tasks can never get an assignee)",
                    f"add '{t['field']}' to an operation's input (e.g. on create/update or a dedicated assign operation)", kind=ErrorKind.INTEGRATION_FAILURE)
    # aggregates whose response schema cannot carry the aggregate
    for op in pkg.operations.values():
        suffix = op["id"].rsplit(".", 1)[1]
        if suffix in ("progress", "stats", "summary") and op["output"]["cardinality"] == "many":
            add(G, "minor", "AGGREGATE_WITHOUT_RESULT_SCHEMA",
                f"{op['id']} is an aggregate but its response schema is the plain entity list ({pkg.endpoint_by_operation[op['id']].get('response_schema_ref')})",
                [op["id"]], "aggregate numbers have no contractual place in the response",
                "add a dedicated aggregate response schema in api.json")
    # password reset needs a token store; no table in the graph
    if any(o["id"].endswith(".reset_password") for o in pkg.operations.values()):
        add(G, "minor", "NO_RESET_TOKEN_STORE", "password reset operations exist but the graph defines no entity for reset tokens or sessions",
            ["operation.user.reset_password"], "reset tokens/logout cannot be persisted",
            "model reset tokens / sessions as an entity if persistence or revocation across instances is required",
            resolution_applied="stateless signed reset tokens bound to the current password hash; in-process token revocation list")
    return out


# ---------------------------------------------------------------------------------------------------
def _graph_family_ok(graph_type: str, pg_fam: str) -> bool:
    return pg_fam in GRAPH_TO_PG.get(graph_type, ())


def detect_database_conflicts(pkg: GraphPackage, db: DatabaseContract) -> list[Conflict]:
    out: list[Conflict] = []
    D = ConflictType.DATABASE_CONTRACT_CONFLICT
    add = lambda *a, **k: out.append(Conflict(D, *a, kind=ErrorKind.DATABASE_CONTRACT_ERROR, **k))  # noqa: E731
    for e in pkg.entities.values():
        key = entity_key(e["id"])
        table = db.table_for_entity(key)
        if table is None:
            add("critical", "TABLE_MISSING", f"no table for {e['id']} in the database contract", [e["id"], "database_contract"],
                "the backend cannot persist this entity", f"the Database Agent must create a table for {e['id']}")
            continue
        for a in e["attributes"]:
            col = table.columns.get(a["name"])
            if col is None:
                add("critical", "COLUMN_MISSING", f"{a['id']} has no column {table.name}.{a['name']}", [a["id"], f"{table.name}"],
                    "attribute cannot be stored or returned", f"add column {a['name']} to {table.name}")
                continue
            if not _graph_family_ok(a["type"], col.family):
                add("critical", "TYPE_MISMATCH", f"{a['id']} is '{a['type']}' but {table.name}.{col.name} is {col.type}", [a["id"], f"{table.name}.{col.name}"],
                    "values cannot round-trip between API and database", f"make {table.name}.{col.name} a {'/'.join(GRAPH_TO_PG.get(a['type'], ('?',)))} column")
            if a["required"] and col.nullable and a["name"] != "id":
                add("minor", "NULLABLE_BUT_REQUIRED", f"{a['id']} is required but {table.name}.{col.name} is nullable", [a["id"], f"{table.name}.{col.name}"],
                    "database will not stop a NULL here; the backend validates it", f"declare {table.name}.{col.name} NOT NULL")
            if not a["required"] and not col.nullable and not col.has_default:
                add("critical", "NOT_NULL_BUT_OPTIONAL", f"{a['id']} is optional but {table.name}.{col.name} is NOT NULL without default", [a["id"], f"{table.name}.{col.name}"],
                    "inserts that omit the optional attribute fail", f"make {table.name}.{col.name} nullable or give it a default")
            if a.get("unique") and a["name"] != "id" and [a["name"]] not in table.unique_constraints:
                add("major", "UNIQUE_NOT_ENFORCED", f"{a['id']} is unique but {table.name} has no UNIQUE constraint on it", [a["id"], table.name],
                    "duplicates are possible under concurrency", f"add UNIQUE ({a['name']}) to {table.name}")
            if a.get("reference_to"):
                ref_table = db.table_for_entity(entity_key(a["reference_to"]))
                if ref_table and not any(f.columns == [a["name"]] and f.ref_table == ref_table.name for f in table.foreign_keys):
                    add("major", "FOREIGN_KEY_MISSING", f"{a['id']} references {a['reference_to']} but {table.name}.{a['name']} has no foreign key", [a["id"], table.name],
                        "dangling references are possible", f"add FOREIGN KEY ({a['name']}) REFERENCES {ref_table.name}(id)")
            if a["type"] == "enum" and col.family == "text" and not any(a["name"] in c for c in table.check_constraints):
                add("info", "ENUM_NOT_CHECKED", f"{a['id']} is an enum stored as text without a visible CHECK constraint", [a["id"], table.name],
                    "the backend is the only guard on enum values", "optionally add a CHECK constraint")
        graph_cols = {a["name"] for a in e["attributes"]}
        for c in table.columns.values():
            if c.name not in graph_cols and not c.nullable and not c.has_default:
                add("critical", "UNKNOWN_REQUIRED_COLUMN", f"{table.name}.{c.name} is NOT NULL without default but is not an attribute of {e['id']}",
                    [table.name, e["id"]], "inserts through the graph contract always fail", f"remove the column, give it a default or add the attribute to the graph")
        if len(table.primary_key) != 1 or table.primary_key != ["id"]:
            add("critical", "PRIMARY_KEY_UNEXPECTED", f"{table.name} primary key is {table.primary_key}; the graph's API addresses rows by 'id'", [table.name],
                "GET/PATCH/DELETE /{id} cannot address rows", "use a single 'id' primary key")
    return out


# ---------------------------------------------------------------------------------------------------
def _response_schema_fields(pkg: GraphPackage, entity_id: str) -> dict[str, dict[str, Any]]:
    sch = pkg.schemas.get(f"schema.{entity_key(entity_id)}")
    if sch:
        return {f["name"]: f for f in sch["fields"]}
    return {a["name"]: a for a in pkg.entities[entity_id]["attributes"] if a["name"] != "password_hash"}


def detect_frontend_conflicts(pkg: GraphPackage, fe: FrontendContract) -> list[Conflict]:
    out: list[Conflict] = []
    F = ConflictType.FRONTEND_API_CONFLICT
    add = lambda *a, **k: out.append(Conflict(F, *a, kind=ErrorKind.FRONTEND_CONTRACT_ERROR, **k))  # noqa: E731
    graph_routes = {(ep["method"], _PATH_PARAM.sub("{}", ep["path"])): ep for ep in pkg.endpoints.values()}

    for fep in fe.endpoints:
        gep = graph_routes.get((fep.method, fep.norm_path))
        if gep is None:
            similar = [ep for (m, p), ep in graph_routes.items() if p.rstrip("s") == fep.norm_path.rstrip("s") or p.startswith(fep.norm_path.split("/{}")[0])]
            hint = f" (graph has {similar[0]['method']} {similar[0]['path']})" if similar else ""
            add("major", "ENDPOINT_NOT_IN_GRAPH", f"frontend {fep.id} calls {fep.method} {fep.path} but the graph defines no such endpoint{hint}",
                [fep.id, "graph:api.json"], "the frontend feature using this call will receive 404/405 from the backend",
                "add the operation + endpoint to the Graph (preferred) or change the frontend call; the backend does not invent endpoints")
            continue
        op = pkg.operations[gep["operation_ref"]]
        graph_roles = sorted(pkg.role_key(r) for r in gep["authorization"]["roles"])
        if fep.auth and graph_roles and fep.roles and sorted(fep.roles) != graph_roles:
            add("major", "ROLES_DIFFER", f"{fep.id} expects roles {sorted(fep.roles)} ({fep.permission}) but {gep['id']} authorizes {graph_roles}",
                [fep.id, gep["id"]], "UI offers actions the backend refuses (403) or hides ones it allows", "align frontend permissions with graph permissions")
        if bool(fep.auth) != bool(gep["authorization"]["authenticated"]):
            add("major", "AUTH_DIFFERS", f"{fep.id} auth={fep.auth} but {gep['id']} authenticated={gep['authorization']['authenticated']}",
                [fep.id, gep["id"]], "the client sends/omits credentials contrary to the backend", "align the authentication requirement")
        sch = pkg.schemas.get(gep.get("request_schema_ref") or "")
        graph_body = {f["name"]: f for f in sch["fields"]} if sch else {}
        graph_query = set(op["input"]["optional_fields"]) | set(op["input"]["extra_fields"]) if gep["method"] == "GET" else set()
        for f in fep.fields:
            if f.source in ("form", "input") and gep["method"] != "GET":
                if f.name not in graph_body:
                    add("major", "FRONTEND_FIELD_NOT_IN_GRAPH", f"{fep.id} sends '{f.name}' but graph schema {sch['id'] if sch else '(none)'} does not define it",
                        [fep.id, gep["id"]], f"the backend rejects unknown fields (mass-assignment protection): requests carrying '{f.name}' get 422",
                        f"add '{f.name}' to the graph's request schema/operation input, or stop sending it")
            elif f.source == "query" and f.name not in graph_query:
                add("major", "FRONTEND_QUERY_NOT_IN_GRAPH", f"{fep.id} sends query parameter '{f.name}' that the graph does not define for {gep['id']}",
                    [fep.id, gep["id"]], "the filter is ignored/rejected by the backend", "define the filter in the graph operation")
        sent = {f.name for f in fep.fields if f.source in ("form", "input")}
        for f in fep.fields:
            gf = graph_body.get(f.name)
            if gf and gf["required"] and not f.required and f.source in ("form", "input"):
                add("major", "REQUIREDNESS_DIFFERS", f"'{f.name}' is required by graph schema {sch['id']} but optional in the frontend ({fep.id})",
                    [fep.id, gep["id"]], "the frontend may omit it and receive 422", f"make '{f.name}' optional in the graph or required in the frontend form")
        for name, gf in graph_body.items():
            if gf["required"] and name not in sent and gep["method"] != "GET":
                add("major", "GRAPH_REQUIRED_FIELD_NOT_SENT", f"graph requires '{name}' for {gep['id']} but the frontend {fep.id} does not always send it",
                    [fep.id, gep["id"]], "requests omitting it get 422", f"make '{name}' optional in the graph or required in the frontend form")
        shape_ok = {"list": "many", "single": "one", "none": "none", "session": "one"}[fep.response_shape]
        if op["output"]["cardinality"] != shape_ok:
            add("major", "RESPONSE_CARDINALITY_DIFFERS", f"{fep.id} expects {fep.response_shape} but {op['id']} returns {op['output']['cardinality']}", [fep.id, op["id"]],
                "client parsing breaks", "align the response shape")
        if fep.response_shape == "session":
            add("major", "SESSION_RESPONSE_NOT_IN_GRAPH",
                f"frontend login expects {{token, user}} but {gep['id']} responds with {gep.get('response_schema_ref')} which has no token",
                [fep.id, gep["id"]], "without a token the frontend cannot authenticate any later request",
                "add a session response schema (token + user) to api.json",
                resolution_applied="login returns the session envelope {token, token_type, expires_in, user} and ALSO the user fields at top level, so both the graph schema and the frontend Session type are satisfied")

    # entities: names / enum values / required response fields
    for fid, fent in fe.entities.items():
        if fent.get("transient") or fid not in pkg.entities:
            continue
        resp = _response_schema_fields(pkg, fid)
        gattrs = {a["name"]: a for a in pkg.entities[fid]["attributes"]}
        for f in fent["fields"]:
            name = f["name"]
            if name not in resp:
                add("major", "FRONTEND_FIELD_MISSING_IN_RESPONSE", f"frontend {fid}.{name} is not in the graph response schema (graph fields: {sorted(resp)})",
                    [f"frontend:{fid}.{name}", f"graph:schema.{entity_key(fid)}"], f"the UI will show an empty/undefined '{name}'",
                    f"rename in the frontend or add '{name}' to the graph entity/response")
            elif f.get("type") == "enum" and gattrs.get(name, {}).get("enum_values") is not None:
                fv, gv = f.get("values", []), gattrs[name].get("enum_values", [])
                if fv and set(fv) != set(gv):
                    if {v.lower() for v in fv} == {v.lower() for v in gv}:
                        add("major", "ENUM_CASE_DIFFERS", f"{fid}.{name}: frontend uses {fv} but graph uses {gv} (same values, different case)", [f"frontend:{fid}.{name}", gattrs[name]["id"]],
                            "every write of this enum from the frontend is rejected (422); every read is displayed with the wrong case",
                            "normalise enum casing in one place (the graph is the source of truth)")
                    else:
                        add("major", "ENUM_VALUES_DIFFER", f"{fid}.{name}: frontend {fv} vs graph {gv}", [f"frontend:{fid}.{name}", gattrs[name]["id"]], "values cannot round-trip", "align enum values")
    # state machines
    for fsm in fe.state_machines.values():
        gsm = next((s for s in pkg.state_machines.values() if s["entity_ref"] == fsm.get("entity")), None)
        if gsm is None:
            continue
        ft = {(t["from"].lower(), t["to"].lower()) for t in fsm["transitions"]}
        gt = {(t["from"].lower(), t["to"].lower()) for t in gsm["transitions"]}
        for frm, to in sorted(ft - gt):
            out.append(Conflict(
                F, "major", "TRANSITION_NOT_IN_GRAPH", f"frontend allows {frm} -> {to} for {fsm['entity']} but {gsm['id']} does not",
                [fsm["id"], gsm["id"]], "the UI offers a transition the backend refuses with 409 (the backend never allows undeclared transitions)",
                "add the transition to the graph state machine or remove it from the frontend", ErrorKind.STATE_TRANSITION_ERROR))
    # error body: documented, resolved by a dual-shape envelope
    out.append(Conflict(F, "minor", "ERROR_ENVELOPE_SHAPE",
                        "the backend error contract is {error:{code,message,details}} while the frontend client reads top-level {message, errors}",
                        ["backend error contract", "frontend:src/lib/api/client.ts"], "without a shim the frontend would show only generic messages and no field errors",
                        "keep one canonical shape in the Frontend client", ErrorKind.API_CONTRACT_ERROR,
                        resolution_applied="every error body carries BOTH: {error:{code,message,details}, message, errors, request_id}"))
    return out


def detect_all(pkg: GraphPackage, db: DatabaseContract | None, fe: FrontendContract | None) -> IntegrationReport:
    rep = IntegrationReport()
    rep.conflicts += detect_graph_conflicts(pkg)
    rep.checked.append("graph")
    if db is not None:
        rep.conflicts += detect_database_conflicts(pkg, db)
        rep.checked.append("database")
    else:
        rep.skipped["database"] = "no database contract supplied"
    if fe is not None:
        rep.conflicts += detect_frontend_conflicts(pkg, fe)
        rep.checked.append("frontend")
    else:
        rep.skipped["frontend"] = "no frontend contract supplied for this project"
    return rep
