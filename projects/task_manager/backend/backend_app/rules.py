"""Rule evaluation on top of the spec: state machines, row scopes (IDOR protection), ownership, guards, frozen states.

Pure functions where possible; the few that read the database take the connection and repositories explicitly.
"""
from __future__ import annotations

from typing import Any

import psycopg
from psycopg import sql

from .errors import ConflictError, EntityNotFound, PermissionDenied, StateTransitionError
from .spec import SpecView


# ---- state machines -----------------------------------------------------------------------------------------------------
def allowed_transitions(sm: dict[str, Any], current: str) -> list[dict[str, Any]]:
    return [t for t in sm["transitions"] if t["from"] == current]


def check_transition(view: SpecView, sm: dict[str, Any], current: str, target: str, held_roles: frozenset[str]) -> dict[str, Any]:
    """Return the matching transition or raise a structured error. Undeclared transitions are never allowed."""
    match = next((t for t in sm["transitions"] if t["from"] == current and t["to"] == target), None)
    if match is None:
        raise StateTransitionError(f"Cannot change status from '{current}' to '{target}'.",
                                   details={"from": current, "to": target, "allowed": sorted({t["to"] for t in allowed_transitions(sm, current)})})
    if not (set(match["roles"]) & held_roles):
        raise PermissionDenied("Your role may not perform this status change.", code="TRANSITION_NOT_PERMITTED", details={"from": current, "to": target})
    return match


# ---- row scopes ---------------------------------------------------------------------------------------------------------
def scopes_for(view: SpecView, op_id: str, held_roles: frozenset[str]) -> list[dict[str, Any]]:
    """Scope nodes that apply to this caller for this operation."""
    return [r["scope"] for r in view.rules_for(op_id, "row_scope") if set(r["roles"]) & held_roles]


def scope_sql(view: SpecView, entity_id: str, node: dict[str, Any], user_id: Any, alias: str | None = None) -> tuple[sql.Composable, list[Any]]:
    """SQL fragment restricting `entity_id` rows to those visible to `user_id` under scope `node`."""
    ent = view.entities[entity_id]
    cols = {c["attr"]: c["column"] for c in ent["columns"]}
    if "field" in node:
        return sql.SQL("{c} = %s").format(c=sql.Identifier(cols[node["field"]])), [user_id]
    parent = view.entities[node["parent_entity"]]
    inner, params = scope_sql(view, node["parent_entity"], node["parent_scope"], user_id)
    frag = sql.SQL("{fk} IN (SELECT id FROM {pt} WHERE ").format(fk=sql.Identifier(cols[node["fk"]]), pt=sql.Identifier(parent["table"])) + inner + sql.SQL(")")
    return frag, params


def row_in_scope(view: SpecView, repos: dict[str, Any], conn: psycopg.Connection, entity_id: str, row: dict[str, Any], node: dict[str, Any], user_id: Any) -> bool:
    if "field" in node:
        return row.get(node["field"]) == user_id
    parent_id = row.get(node["fk"])
    if parent_id is None:
        return False
    parent = repos[node["parent_entity"]].get(conn, parent_id)
    return parent is not None and row_in_scope(view, repos, conn, node["parent_entity"], parent, node["parent_scope"], user_id)


# ---- ownership / guards / frozen states --------------------------------------------------------------------------------
def check_ownership(view: SpecView, op_id: str, row: dict[str, Any], user_id: Any, held_roles: frozenset[str]) -> None:
    for rule in view.rules_for(op_id, "ownership"):
        if set(rule["exempt_roles"]) & held_roles:
            continue
        if row.get(rule["field"]) != user_id:
            raise PermissionDenied(rule["message"], code=rule["error_code"])


def check_frozen(view: SpecView, op_id: str, row: dict[str, Any]) -> None:
    for rule in view.rules_for(op_id, "frozen_state"):
        if row.get(rule["field"]) in rule["states"]:
            raise ConflictError(rule["message"], code=rule["error_code"], details={"state": row.get(rule["field"])})


def check_transition_guards(view: SpecView, op_id: str, row: dict[str, Any], target: str) -> None:
    for rule in view.rules_for(op_id, "transition_guard"):
        if rule["to"] != target:
            continue
        missing = [f for f in rule["require_fields_set"] if row.get(f) is None]
        if missing:
            raise ConflictError(rule["message"], code=rule["error_code"], details={"missing": missing})


def not_found(code: str, entity_key: str) -> EntityNotFound:
    return EntityNotFound(f"The {entity_key.replace('_', ' ')} was not found.", code=code)
