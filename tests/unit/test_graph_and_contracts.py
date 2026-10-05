"""Graph loading/validation and the negative cases from the specification: each produces its own structured error kind."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.contracts.database import load_database_contract, parse_schema_defaults, pg_family
from app.contracts.frontend import load_frontend_contract, verify_client_expectations
from app.errors import ConflictType, ContractError, ErrorKind, GraphError
from app.graph.loader import load_graph_package
from app.graph.validator import validate_graph
from app.integration.conflicts import detect_all
from tests.conftest import FIXTURES, FRONTEND_REPO, mutate_graph, read_json, write_json


@pytest.mark.parametrize("name", FIXTURES)
def test_fixture_graph_packages_load_and_validate(project, name):
    pkg = load_graph_package(project(name))
    v = validate_graph(pkg)
    assert v.ok, [str(e) for e in v.errors]
    assert pkg.operations and pkg.endpoints and pkg.entities
    assert pkg.validation_report["status"] == "valid"


# ---- negative: missing / broken graphs -> GRAPH_ERROR ---------------------------------------------------------------------------------
def test_missing_project_is_a_graph_error(tmp_path):
    with pytest.raises(GraphError) as e:
        load_graph_package(tmp_path / "nowhere")
    assert ErrorKind.GRAPH_ERROR in e.value.kinds


def test_missing_graph_file_is_a_graph_error(project):
    p = project()
    (p / "graphs" / "api.json").unlink()
    with pytest.raises(GraphError) as e:
        load_graph_package(p)
    assert {i.code for i in e.value.issues} == {"file_missing"} and ErrorKind.GRAPH_ERROR in e.value.kinds


def test_invalid_json_and_missing_manifest_entry(project):
    p = project()
    (p / "graphs" / "roles.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(GraphError) as e:
        load_graph_package(p)
    assert "invalid_json" in {i.code for i in e.value.issues}
    p2 = project()
    mpath = p2 / "graphs" / "graph_manifest.json"
    m = read_json(mpath)
    del m["graphs"]["permissions"]
    write_json(mpath, m)
    with pytest.raises(GraphError) as e:
        load_graph_package(p2)
    assert "manifest_entry_missing" in {i.code for i in e.value.issues}


def test_edited_graph_with_stale_checksum_is_rejected(project):
    p = project()
    mutate_graph(p, "roles", lambda d: d["roles"][0].update(description="edited"), refresh_checksum=False)
    with pytest.raises(GraphError) as e:
        load_graph_package(p)
    assert "checksum_mismatch" in {i.code for i in e.value.issues}


def test_package_the_graph_agent_marked_unsafe_is_rejected(project):
    p = project()
    rpath = p / "validation" / "validation_report.json"
    r = read_json(rpath)
    r.update(status="invalid", safe_for_downstream=False)
    write_json(rpath, r)
    with pytest.raises(GraphError) as e:
        load_graph_package(p)
    assert "graph_agent_report_invalid" in {i.code for i in e.value.issues}


def test_project_id_mismatch(project):
    p = project()
    mutate_graph(p, "project", lambda d: d.update(project_id="project.other"))
    with pytest.raises(GraphError) as e:
        load_graph_package(p)
    assert "project_id_mismatch" in {i.code for i in e.value.issues}


# ---- negative: invalid references -> GRAPH_REFERENCE_ERROR ----------------------------------------------------------------------------
@pytest.mark.parametrize("graph,mutate,code", [
    ("backend", lambda d: d["operations"][0].update(entity_ref="entity.ghost"), "unknown_entity_ref"),
    ("api", lambda d: d["endpoints"][0].update(operation_ref="operation.ghost.create"), "unknown_operation_ref"),
    ("api", lambda d: d["endpoints"][0]["authorization"].update(roles=["role.ghost"]), "unknown_role_ref"),
    ("permissions", lambda d: d["permissions"][0].update(operation_refs=["operation.ghost.x"]), "unknown_operation_ref"),
    ("entities", lambda d: d["entities"][1]["attributes"][1].update(reference_to="entity.ghost"), "unknown_entity_ref"),
    ("state_machines", lambda d: d["state_machines"][0]["transitions"][0].update(operation_ref="operation.ghost.y"), "unknown_operation_ref"),
    ("acceptance_criteria", lambda d: d["acceptance_criteria"][0].update(operation_refs=["operation.ghost.z"]), "unknown_ref"),
])
def test_invalid_references_are_reference_errors(project, graph, mutate, code):
    p = project()
    mutate_graph(p, graph, mutate)
    v = validate_graph(load_graph_package(p))
    assert not v.ok
    assert code in {i.code for i in v.errors} and ErrorKind.GRAPH_REFERENCE_ERROR in {i.kind for i in v.errors}


def test_structural_graph_errors(project):
    p = project()
    mutate_graph(p, "api", lambda d: d["endpoints"].append({**d["endpoints"][0], "id": "api.dup.route"}))
    codes = {i.code for i in validate_graph(load_graph_package(p)).errors}
    assert "duplicate_route" in codes
    p2 = project()
    mutate_graph(p2, "state_machines", lambda d: d["state_machines"][0].update(initial_state="nonexistent"))
    assert "initial_state_unknown" in {i.code for i in validate_graph(load_graph_package(p2)).errors}
    p3 = project()
    mutate_graph(p3, "api", lambda d: d["endpoints"][0].update(path="/departments/{missing}"))
    assert "path_param_not_in_operation" in {i.code for i in validate_graph(load_graph_package(p3)).errors}


# ---- database contract -----------------------------------------------------------------------------------------------------------------
def test_database_contract_reads_the_database_agents_artifacts(project):
    db = load_database_contract(project() / "database")
    assert set(db.tables) >= {"users", "tasks", "projects"} and db.tables["tasks"].primary_key == ["id"]
    assert db.tables["users"].columns["id"].has_default and db.tables["users"].columns["id"].family == "uuid"
    assert ["email"] in db.tables["users"].unique_constraints
    assert any(f.ref_table == "projects" for f in db.tables["tasks"].foreign_keys)
    assert db.table_for_entity("project_member").name == "project_members"
    assert {f.name for f in db.functions} >= {"create_task", "list_tasks"}


def test_defaults_parser_reads_schema_sql_when_no_state_snapshot(project):
    p = project()
    (p / "database" / "database_state.json").unlink()
    db = load_database_contract(p / "database")
    assert db.tables["users"].columns["created_at"].has_default and db.tables["users"].columns["id"].has_default
    defaults = parse_schema_defaults("CREATE TABLE t (id uuid DEFAULT gen_random_uuid() NOT NULL, n integer, c timestamptz NOT NULL DEFAULT now(), CONSTRAINT pk PRIMARY KEY (id));")
    assert defaults == {"t": {"id": "gen_random_uuid()", "c": "now()"}}


@pytest.mark.parametrize("raw,fam", [("uuid", "uuid"), ("numeric(12,2)", "numeric"), ("timestamptz", "timestamp"), ("character varying", "text"), ("integer", "integer"), ("jsonb", "json")])
def test_postgres_type_families(raw, fam):
    assert pg_family(raw) == fam


def test_missing_database_artifacts_are_contract_errors(tmp_path):
    with pytest.raises(ContractError) as e:
        load_database_contract(tmp_path)
    assert ErrorKind.DATABASE_CONTRACT_ERROR in e.value.kinds


def _conflicts(project_dir, with_frontend=True):
    pkg = load_graph_package(project_dir)
    db = load_database_contract(project_dir / "database")
    fe = load_frontend_contract(project_dir / "frontend") if with_frontend and (project_dir / "frontend").exists() else None
    return detect_all(pkg, db, fe)


def _arch_edit(project_dir, fn):
    path = project_dir / "database" / "architecture.json"
    d = read_json(path)
    fn(d)
    write_json(path, d)
    (project_dir / "database" / "database_state.json").unlink(missing_ok=True)


def test_database_table_missing_is_a_critical_database_contract_conflict(project):
    p = project()
    _arch_edit(p, lambda d: d["tables"].__delitem__(next(i for i, t in enumerate(d["tables"]) if t["name"] == "tasks")))
    rep = _conflicts(p)
    c = next(c for c in rep.conflicts if c.code == "TABLE_MISSING")
    assert rep.status == "blocked" and c.severity == "critical" and c.type == ConflictType.DATABASE_CONTRACT_CONFLICT and c.kind == ErrorKind.DATABASE_CONTRACT_ERROR


def test_database_type_mismatch_uuid_vs_integer_is_blocking(project):
    p = project()

    def edit(d):
        t = next(t for t in d["tables"] if t["name"] == "projects")
        next(c for c in t["columns"] if c["name"] == "id")["type"] = "integer"

    _arch_edit(p, edit)
    c = next(c for c in _conflicts(p).conflicts if c.code == "TYPE_MISMATCH")
    assert c.severity == "critical" and "entity.project.id" in c.sources[0]


def test_missing_column_not_null_without_default_and_missing_constraints(project):
    p = project()

    def edit(d):
        t = next(t for t in d["tables"] if t["name"] == "tasks")
        t["columns"] = [c for c in t["columns"] if c["name"] != "title"]
        t["columns"].append({"name": "internal_flag", "type": "boolean", "nullable": False, "source_field": None})
        u = next(t for t in d["tables"] if t["name"] == "users")
        u["unique_constraints"] = []
        t["foreign_keys"] = []

    _arch_edit(p, edit)
    codes = {c.code for c in _conflicts(p).conflicts}
    assert {"COLUMN_MISSING", "UNKNOWN_REQUIRED_COLUMN", "UNIQUE_NOT_ENFORCED", "FOREIGN_KEY_MISSING"} <= codes


# ---- frontend contract ---------------------------------------------------------------------------------------------------------------------
def test_frontend_contract_loads_the_frontend_agents_own_format(project):
    fe = load_frontend_contract(project() / "frontend")
    ep = next(e for e in fe.endpoints if e.id == "api.task.update_status")
    assert (ep.method, ep.path, ep.roles) == ("PATCH", "/tasks/{id}/status", ["employee", "manager"])
    assert fe.expectations["list_shape"].startswith("bare JSON array")


def test_frontend_format_mismatch_is_a_frontend_contract_error(project):
    p = project()
    write_json(p / "frontend" / "graphs" / "api.json", {"endpoints": []})
    with pytest.raises(ContractError) as e:
        load_frontend_contract(p / "frontend")
    assert ErrorKind.FRONTEND_CONTRACT_ERROR in e.value.kinds


def test_frontend_expecting_a_missing_endpoint_is_reported_as_frontend_contract_error(project):
    """The prompt's example: the frontend expects POST /project while the graph defines POST /projects."""
    p = project()
    path = p / "frontend" / "graphs" / "api.json"
    d = read_json(path)
    next(n for n in d["nodes"] if n["id"] == "api.project.create")["path"] = "/project"
    write_json(path, d)
    c = next(c for c in _conflicts(p).conflicts if c.code == "ENDPOINT_NOT_IN_GRAPH" and "/project" in c.description)
    assert c.type == ConflictType.FRONTEND_API_CONFLICT and c.kind == ErrorKind.FRONTEND_CONTRACT_ERROR and c.severity == "major"
    assert "POST /projects" in c.description, "the report should point at the endpoint the graph does define"


def test_known_frontend_conflicts_of_the_task_manager_are_all_reported(project):
    codes = {c.code for c in _conflicts(project()).conflicts}
    assert {"ENDPOINT_NOT_IN_GRAPH", "FRONTEND_FIELD_NOT_IN_GRAPH", "ENUM_CASE_DIFFERS", "SESSION_RESPONSE_NOT_IN_GRAPH", "TRANSITION_NOT_IN_GRAPH",
            "FRONTEND_FIELD_MISSING_IN_RESPONSE", "ERROR_ENVELOPE_SHAPE", "REQUIREDNESS_DIFFERS", "FRONTEND_QUERY_NOT_IN_GRAPH"} <= codes
    shims = [c for c in _conflicts(project()).conflicts if c.resolution_applied]
    assert {c.code for c in shims} >= {"SESSION_RESPONSE_NOT_IN_GRAPH", "ERROR_ENVELOPE_SHAPE", "NO_USER_PROVISIONING"}


def test_client_expectations_are_verified_against_the_frontend_repository(tmp_path):
    if not FRONTEND_REPO.exists():
        pytest.skip("the Frontend-Agent checkout is not next to this repository")
    assert verify_client_expectations(FRONTEND_REPO) == []
    fake = tmp_path / "app/generation/templates/frontend/src/lib/api/client.ts"
    fake.parent.mkdir(parents=True)
    fake.write_text("export const x = 1;", encoding="utf-8")
    assert len(verify_client_expectations(tmp_path)) == 5


# ---- graph-internal contradictions block generation ------------------------------------------------------------------------------------------
def test_permission_that_contradicts_the_operation_roles_blocks(project):
    """'Permission says employee can update task' vs 'only manager' (the prompt's example) is a critical GRAPH_API_CONFLICT."""
    p = project()
    mutate_graph(p, "permissions", lambda d: next(x for x in d["permissions"] if x["id"] == "permission.task.update").update(roles=["role.manager", "role.employee"]))
    rep = _conflicts(p, with_frontend=False)
    c = next(c for c in rep.conflicts if c.code == "PERMISSION_ROLES_DIFFER_FROM_OPERATION")
    assert rep.status == "blocked" and c.severity == "critical" and c.type == ConflictType.GRAPH_API_CONFLICT and c.kind == ErrorKind.AUTHORIZATION_ERROR


def test_missing_permission_is_an_authorization_error(project):
    p = project()
    mutate_graph(p, "permissions", lambda d: next(x for x in d["permissions"] if x["id"] == "permission.project.delete").update(operation_refs=[]))
    rep = _conflicts(p, with_frontend=False)
    c = next(c for c in rep.conflicts if c.code == "PERMISSION_MISSING")
    assert c.kind == ErrorKind.AUTHORIZATION_ERROR and rep.status == "blocked"


def test_endpoint_roles_differing_from_the_operation_and_verb_mismatches_block(project):
    p = project()
    mutate_graph(p, "api", lambda d: (next(e for e in d["endpoints"] if e["id"] == "api.task.delete")["authorization"].update(roles=["role.employee"]),
                                      next(e for e in d["endpoints"] if e["id"] == "api.task.list").update(method="POST")))
    codes = {c.code for c in _conflicts(p, with_frontend=False).conflicts}
    assert {"ENDPOINT_ROLES_DIFFER_FROM_OPERATION", "QUERY_ON_NON_GET"} <= codes


def test_transition_role_that_cannot_call_the_operation_is_a_state_transition_error(project):
    p = project()
    mutate_graph(p, "backend", lambda d: next(o for o in d["operations"] if o["id"] == "operation.task.update_status").update(required_roles=["role.manager"]))
    mutate_graph(p, "api", lambda d: next(e for e in d["endpoints"] if e["id"] == "api.task.update_status")["authorization"].update(roles=["role.manager"]))
    mutate_graph(p, "permissions", lambda d: next(x for x in d["permissions"] if x["id"] == "permission.task.update_status").update(roles=["role.manager"]))
    c = next(c for c in _conflicts(p, with_frontend=False).conflicts if c.code == "TRANSITION_ROLE_CANNOT_CALL_OPERATION")
    assert c.kind == ErrorKind.STATE_TRANSITION_ERROR and c.severity == "critical"
