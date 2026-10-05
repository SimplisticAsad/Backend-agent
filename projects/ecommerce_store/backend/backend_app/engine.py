"""Operation engine: the generic implementation of graph operations (create/read/list/update/transition/delete).

Order of checks for an operation on an existing row (deliberate - it prevents information leaks):
    1. row exists                      -> 404 <ENTITY>_NOT_FOUND
    2. row is in the caller's scope    -> 404 (same as "missing": the caller may not learn that it exists)
    3. ownership rules                 -> 403 (graph-defined error code)
    4. frozen states / state machine   -> 409 / 403
    5. transition guards               -> 409 (graph-defined error code)
Authentication and role checks happen before the engine (api/auth dependencies); the engine enforces object-level rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from . import rules as R
from .config import Settings
from .db import Database
from .errors import EntityNotFound, PermissionDenied, ValidationError
from .repository import BaseRepository
from .spec import SpecView


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    email: str
    role_key: str
    role_id: str
    held_roles: frozenset[str]
    token_id: str = ""
    token_exp: int = 0


@dataclass
class ListQuery:
    filters: dict[str, Any] = field(default_factory=dict)
    search: str | None = None
    sort: str | None = None
    order: str = "asc"
    limit: int = 100
    offset: int = 0


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int


class Engine:
    def __init__(self, view: SpecView, db: Database, repos: dict[str, BaseRepository], settings: Settings) -> None:
        self.view, self.db, self.repos, self.settings = view, db, repos, settings

    # ---- dispatch -------------------------------------------------------------------------------------------------
    def run(self, op_id: str, principal: Principal | None, *, row_id: UUID | None = None, body: dict[str, Any] | None = None,
            query: ListQuery | None = None) -> Any:
        op = self.view.operations[op_id]
        kind = op["kind"]
        fn = {"create": self._create, "read": self._read, "list": self._list, "update": self._update, "transition": self._transition, "delete": self._delete}.get(kind)
        if fn is None:
            raise ValueError(f"engine cannot run operation kind {kind!r} ({op_id})")
        return fn(op, principal, row_id=row_id, body=body or {}, query=query or ListQuery())

    # ---- helpers --------------------------------------------------------------------------------------------------
    def _held(self, principal: Principal | None) -> frozenset[str]:
        return principal.held_roles if principal else frozenset()

    def _nf(self, op: dict[str, Any]) -> EntityNotFound:
        return R.not_found(op["not_found_code"], self.view.entities[op["entity"]]["key"])

    def _in_scope(self, op: dict[str, Any], principal: Principal | None, conn, row: dict[str, Any]) -> bool:
        if principal is None:
            return True
        for node in R.scopes_for(self.view, op["id"], principal.held_roles):
            if not R.row_in_scope(self.view, self.repos, conn, op["entity"], row, node, principal.user_id):
                return False
        return True

    def _load(self, op: dict[str, Any], principal: Principal | None, conn, row_id: UUID, *, lock: bool = False) -> dict[str, Any]:
        row = self.repos[op["entity"]].get(conn, row_id, for_update=lock)
        if row is None or not self._in_scope(op, principal, conn, row):
            raise self._nf(op)
        return row

    # ---- create -----------------------------------------------------------------------------------------------------
    def _create(self, op, principal, *, body, **_):
        ent = self.view.entities[op["entity"]]
        data = dict(body)
        cols = {c["attr"]: c for c in ent["columns"]}
        for attr, src in op["params"]["server_fields"].items():
            if src == "principal.id":
                supplied = data.pop(attr, None)
                if supplied is not None and (principal is None or supplied != principal.user_id):
                    raise ValidationError(f"'{attr}' is assigned by the server and cannot be set to another user.", code="FIELD_NOT_ALLOWED", details={"field": attr})
                if principal is None:
                    raise PermissionDenied()
                data[attr] = principal.user_id
            elif src == "initial_state":
                data[attr] = self.view.state_machines[ent["state_machine"]]["initial"]
            elif src == "first_enum":
                data[attr] = cols[attr]["enum"][0]
            elif src == "db_default_or_now" and not cols[attr]["has_default"]:
                data[attr] = datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            self._check_parent_access(op, principal, conn, data)
            return self.repos[op["entity"]].insert(conn, data)

    def _check_parent_access(self, op, principal, conn, data: dict[str, Any]) -> None:
        """Creating a child of a parent the caller may not see behaves exactly like a missing parent."""
        if principal is None:
            return
        for node in R.scopes_for(self.view, op["id"], principal.held_roles):
            if "fk" not in node:
                continue
            parent_id = data.get(node["fk"])
            parent = self.repos[node["parent_entity"]].get(conn, parent_id) if parent_id is not None else None
            if parent is None or not R.row_in_scope(self.view, self.repos, conn, node["parent_entity"], parent, node["parent_scope"], principal.user_id):
                pkey = self.view.entities[node["parent_entity"]]["key"]
                raise EntityNotFound(f"The {pkey.replace('_', ' ')} was not found.", code=f"{pkey.upper()}_NOT_FOUND")

    # ---- read / list ------------------------------------------------------------------------------------------------
    def _read(self, op, principal, *, row_id, **_):
        with self.db.transaction() as conn:
            return self._load(op, principal, conn, row_id)

    def _list(self, op, principal, *, query: ListQuery, **_):
        repo = self.repos[op["entity"]]
        allowed = {q["name"] for q in op["endpoint"]["query_params"] if q["kind"] == "filter"}
        conds = []
        if principal is not None:
            for node in R.scopes_for(self.view, op["id"], principal.held_roles):
                conds.append(R.scope_sql(self.view, op["entity"], node, principal.user_id))
        for name, value in query.filters.items():
            if name not in allowed:
                raise ValidationError(f"Unknown filter '{name}'.", code="INVALID_FILTER", details={"allowed": sorted(allowed)})
            if value is not None:
                conds.append(repo.eq(name, value))
        imp = op["params"].get("implicit_filter")
        if imp:
            conds.append(repo.eq(imp["field"], imp["value"]))
        limit = max(1, min(query.limit, self.settings.max_page_limit))
        with self.db.transaction() as conn:
            rows, total = repo.list(conn, conditions=conds, search=query.search if op["params"].get("search") else None, sort=query.sort,
                                    order=query.order, limit=limit, offset=max(0, query.offset))
        return Page(rows, total)

    # ---- update / transition / delete ---------------------------------------------------------------------------------
    def _update(self, op, principal, *, row_id, body, **_):
        if not body:
            raise ValidationError("At least one field must be provided.", code="EMPTY_UPDATE")
        with self.db.transaction() as conn:
            row = self._load(op, principal, conn, row_id, lock=True)
            R.check_ownership(self.view, op["id"], row, principal.user_id if principal else None, self._held(principal))
            R.check_frozen(self.view, op["id"], row)
            return self.repos[op["entity"]].update(conn, row_id, dict(body))

    def _transition(self, op, principal, *, row_id, body, **_):
        field_name = op["params"]["field"]
        target = body.get(field_name)
        if target is None:
            raise ValidationError(f"'{field_name}' is required.", code="VALIDATION_ERROR")
        sm = self.view.state_machines[op["params"]["state_machine"]]
        with self.db.transaction() as conn:
            row = self._load(op, principal, conn, row_id, lock=True)
            R.check_ownership(self.view, op["id"], row, principal.user_id if principal else None, self._held(principal))
            R.check_frozen(self.view, op["id"], row)
            R.check_transition(self.view, sm, row[field_name], target, self._held(principal))
            R.check_transition_guards(self.view, op["id"], row, target)
            return self.repos[op["entity"]].update(conn, row_id, {field_name: target})

    def _delete(self, op, principal, *, row_id, **_):
        with self.db.transaction(deleting=True) as conn:
            row = self._load(op, principal, conn, row_id, lock=True)
            R.check_ownership(self.view, op["id"], row, principal.user_id if principal else None, self._held(principal))
            R.check_frozen(self.view, op["id"], row)
            self.repos[op["entity"]].delete(conn, row_id)
        return None
