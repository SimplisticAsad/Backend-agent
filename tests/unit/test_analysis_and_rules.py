"""Operation classification, rule derivation (baseline), additive-only LLM merging, spec determinism."""
from __future__ import annotations

import json

import pytest

from app.analysis.classifier import classify_all
from app.analysis.rules import derive_rules, merge_rules
from app.analysis.spec import build_entity_mapping, build_spec
from app.analysis.typesys import parse_field_type, pydantic_annotation
from app.contracts.database import load_database_contract
from app.graph.loader import load_graph_package
from tests.conftest import FIXTURES, PROJECTS


def _load(name):
    pkg = load_graph_package(PROJECTS / name)
    db = load_database_contract(PROJECTS / name / "database")
    defaults = {(e["id"], a["name"]) for e in pkg.entities.values() for a in e["attributes"] if (t := db.table_for_entity(e["id"].split(".", 1)[1])) and t.columns[a["name"]].has_default}
    classes = classify_all(pkg, defaults)
    return pkg, db, classes, derive_rules(pkg, classes)


def test_classification_of_the_task_manager():
    pkg, db, classes, _ = _load("task_manager")
    kinds = {o: c.kind for o, c in classes.items()}
    assert kinds["operation.task.update_status"] == "transition"
    assert kinds["operation.project.progress"] == "aggregate"
    assert kinds["operation.user.login"] == "auth.login" and kinds["operation.user.reset_password"] == "auth.reset_password"
    assert kinds["operation.project.create"] == "create" and kinds["operation.task.list_assigned"] == "list"
    create = classes["operation.project.create"]
    assert create.params["server_fields"] == {"owner_id": "principal.id", "status": "first_enum"}, "owner is the caller; the enum starts at its first value"
    assert classes["operation.task.create"].params["server_fields"]["status"] == "initial_state"


def test_ecommerce_place_order_needs_a_handler_and_search_filters_are_engine_operations():
    pkg, db, classes, rules = _load("ecommerce_store")
    assert classes["operation.order.place"].kind == "custom" and "total" in classes["operation.order.place"].params["unmet"]
    assert classes["operation.product.search"].params["search"] is True and classes["operation.product.filter"].params["filters"] == ["category_id", "price"]
    assert classes["operation.product.list"].kind == "list"


def test_ticketing_implicit_filter_from_the_operation_name():
    _, _, classes, _ = _load("support_ticketing_system")
    assert classes["operation.user.list_agents"].params["implicit_filter"] == {"field": "role", "value": "agent"}
    assert classes["operation.ticket.assign"].kind == "update" and classes["operation.ticket.change_status"].kind == "transition"


def test_rules_derived_from_free_text_conditions():
    _, _, _, rs = _load("task_manager")
    by = {r["id"]: r for r in rs.rules}
    own = by["rule.ownership.task.employee_updates_own"]
    assert (own["field"], own["exempt_roles"], own["error_code"], own["status"]) == ("assignee_id", ["role.manager"], "TASK_NOT_ASSIGNED_TO_CALLER", 403)
    guard = by["rule.guard.task.completed_needs_assignee"]
    assert (guard["to"], guard["require_fields_set"], guard["error_code"]) == ("completed", ["assignee_id"], "TASK_NO_ASSIGNEE")
    assert by["rule.scope.task.list_assigned"]["scope"] == {"field": "assignee_id"} and by["rule.scope.task.list_assigned"]["roles"] == ["role.employee"]
    assert len([r for r in rs.rules if r["type"] == "ownership"]) == 1, "the permission condition and the validation describe the same rule: no duplicate"
    assert rs.unmapped == []


def test_frozen_state_rule_from_ticketing():
    _, _, _, rs = _load("support_ticketing_system")
    frozen = next(r for r in rs.rules if r["type"] == "frozen_state")
    assert frozen["states"] == ["closed"] and frozen["operations"] == ["operation.ticket.update"] and frozen["field"] == "status"


def test_implicit_data_subject_scoping_is_inferred_and_recorded_as_an_assumption():
    _, _, _, rs = _load("ecommerce_store")
    ids = {r["id"] for r in rs.rules}
    assert {"rule.scope.implicit.cart_item.user_id", "rule.scope.implicit.order.user_id"} <= ids
    assert any("cart_item" in a and "inferred" in a for a in rs.assumptions)
    _, _, _, rs2 = _load("support_ticketing_system")
    chain = next(r for r in rs2.rules if r["id"].startswith("rule.scope.implicit.ticket_comment"))
    assert chain["scope"] == {"fk": "ticket_id", "parent_entity": "entity.ticket", "parent_scope": {"field": "requester_id"}} and chain["roles"] == ["role.customer"]
    assert "operation.ticket_comment.create" in chain["operations"], "creating a child under somebody else's parent must be checked too"


def test_every_graph_validation_is_accounted_for_in_the_coverage_list():
    for name in FIXTURES:
        pkg, _, _, rs = _load(name)
        covered = {c["source"] for c in rs.coverage}
        assert set(pkg.validations) <= covered | {u["source"] for u in rs.unmapped}, name


def test_llm_rules_can_only_add_never_redefine_or_weaken():
    _, _, _, rs = _load("task_manager")
    good = {"id": "rule.llm.x", "type": "frozen_state", "entity": "entity.task", "operations": ["operation.task.update"], "field": "status", "states": ["completed"],
            "error_code": "TASK_DONE", "message": "done"}
    accepted, rejected = merge_rules(rs, [good])
    assert [r["id"] for r in accepted] == ["rule.llm.x"] and rejected == [] and accepted[0]["origin"] == "llm"
    clash = {**good, "id": "rule.ownership.task.employee_updates_own", "type": "ownership", "field": "assignee_id", "exempt_roles": ["role.employee", "role.manager"], "error_code": "X", "message": "m"}
    accepted, rejected = merge_rules(rs, [clash])
    assert accepted == [] and "collides with a baseline rule" in rejected[0]
    accepted, rejected = merge_rules(rs, [{"id": "rule.llm.y", "type": "allow_everything"}, {"id": "rule.llm.z", "type": "ownership"}])
    assert accepted == [] and len(rejected) == 2


@pytest.mark.parametrize("name", FIXTURES)
def test_spec_is_deterministic_and_maps_every_entity_to_a_table(name):
    a, b = _build(name), _build(name)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), "identical inputs must give an identical spec"
    pkg, db, *_ = _load(name)
    mapping = build_entity_mapping(pkg, db)
    assert all(m["database_table"] for m in mapping["entities"]) and len(mapping["entities"]) == len(pkg.entities)
    task = next((m for m in mapping["entities"] if m["graph_entity"] == "entity.task"), None)
    if task:
        assert (task["domain_model"], task["database_table"]) == ("Task", "tasks")


def _build(name):
    pkg, db, classes, rs = _load(name)
    return build_spec(pkg, db, classes, rs.rules, coverage=rs.coverage)


def test_spec_status_codes_and_paths():
    spec = _build("task_manager")
    ops = spec["operations"]
    assert ops["operation.project.create"]["endpoint"]["status_code"] == 201 and ops["operation.project.delete"]["endpoint"]["status_code"] == 204
    assert ops["operation.user.login"]["endpoint"]["status_code"] == 200 and ops["operation.user.logout"]["endpoint"]["status_code"] == 204
    assert ops["operation.task.update_status"]["endpoint"]["path_params"] == ["id"]


def test_field_type_grammar():
    assert str(parse_field_type("uuid!>user")) == "uuid!>user" and parse_field_type("uuid!>user").ref == "user"
    assert parse_field_type("email!*").unique and parse_field_type("string!").non_empty
    with pytest.raises(ValueError):
        parse_field_type("blob")
    ann, kw = pydantic_annotation(parse_field_type("string!"), request=True)
    assert ann == "str" and "min_length=1" in kw and "max_length=255" in kw
    assert pydantic_annotation(parse_field_type("enum"), ["a", "b"], request=True)[0] == "Literal['a', 'b']"
    assert pydantic_annotation(parse_field_type("decimal"), request=True)[1] == ["max_digits=12", "decimal_places=2"]
