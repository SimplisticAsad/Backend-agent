"""Generic parameterised repository over one table of the database contract.

SQL safety: every identifier (table, column, sort field) comes from the generated spec, never from the client; client
values are always bound parameters. Sort/filter fields supplied by the client are checked against allow-lists first.
"""
from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg import sql

from .errors import ValidationError


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class BaseRepository:
    def __init__(self, entity: dict[str, Any]) -> None:
        self.entity = entity
        self.table = sql.Identifier(entity["table"])
        self.columns: dict[str, dict[str, Any]] = {c["attr"]: c for c in entity["columns"]}
        self._select = sql.SQL(", ").join(sql.Identifier(c["column"]) for c in entity["columns"])
        self.id_has_default: bool = entity["id_has_default"]
        self.sortable: set[str] = set(entity["response_fields"])
        self.default_sort = "created_at" if "created_at" in self.columns else ("placed_at" if "placed_at" in self.columns else "id")

    # ---- reads ---------------------------------------------------------------------------------------------------------
    def get(self, conn: psycopg.Connection, row_id: Any, *, for_update: bool = False) -> dict[str, Any] | None:
        q = sql.SQL("SELECT {cols} FROM {t} WHERE id = %s").format(cols=self._select, t=self.table)
        if for_update:
            q += sql.SQL(" FOR UPDATE")
        return conn.execute(q, (row_id,)).fetchone()

    def get_by(self, conn: psycopg.Connection, attr: str, value: Any, *, case_insensitive: bool = False) -> dict[str, Any] | None:
        col = sql.Identifier(self._col(attr))
        where = sql.SQL("lower({c}) = lower(%s)").format(c=col) if case_insensitive else sql.SQL("{c} = %s").format(c=col)
        q = sql.SQL("SELECT {cols} FROM {t} WHERE ").format(cols=self._select, t=self.table) + where + sql.SQL(" LIMIT 1")
        return conn.execute(q, (value,)).fetchone()

    def list(self, conn: psycopg.Connection, *, conditions: list[tuple[sql.Composable, list[Any]]] | None = None, search: str | None = None,
             sort: str | None = None, order: str = "asc", limit: int = 100, offset: int = 0, lock: bool = False) -> tuple[list[dict[str, Any]], int]:
        parts: list[sql.Composable] = []
        params: list[Any] = []
        for frag, p in conditions or []:
            parts.append(sql.SQL("(") + frag + sql.SQL(")"))
            params += p
        if search:
            text_cols = self.entity["text_columns"]
            if text_cols:
                like = f"%{escape_like(search)}%"
                parts.append(sql.SQL("(") + sql.SQL(" OR ").join(sql.SQL("{c} ILIKE %s").format(c=sql.Identifier(self._col(c))) for c in text_cols) + sql.SQL(")"))
                params += [like] * len(text_cols)
        where = (sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts)) if parts else sql.SQL("")
        sort_attr = sort or self.default_sort
        if sort_attr not in self.sortable and sort_attr != self.default_sort:
            raise ValidationError(f"Cannot sort by '{sort_attr}'.", code="INVALID_SORT_FIELD", details={"allowed": sorted(self.sortable)})
        direction = sql.SQL("DESC") if order == "desc" else sql.SQL("ASC")
        total = conn.execute(sql.SQL("SELECT count(*) AS n FROM {t}").format(t=self.table) + where, params).fetchone()["n"]
        q = (sql.SQL("SELECT {cols} FROM {t}").format(cols=self._select, t=self.table) + where
             + sql.SQL(" ORDER BY {s} {d}, id ASC LIMIT %s OFFSET %s").format(s=sql.Identifier(self._col(sort_attr)), d=direction))
        rows = conn.execute(q, [*params, limit, offset]).fetchall()
        return rows, total

    # ---- writes --------------------------------------------------------------------------------------------------------
    def insert(self, conn: psycopg.Connection, values: dict[str, Any]) -> dict[str, Any]:
        values = dict(values)
        if "id" not in values and not self.id_has_default:
            values["id"] = uuid.uuid4()
        cols = [self._col(a) for a in values]
        q = sql.SQL("INSERT INTO {t} ({c}) VALUES ({p}) RETURNING {cols}").format(
            t=self.table, c=sql.SQL(", ").join(map(sql.Identifier, cols)), p=sql.SQL(", ").join(sql.Placeholder() * len(cols)), cols=self._select)
        return conn.execute(q, list(values.values())).fetchone()

    def update(self, conn: psycopg.Connection, row_id: Any, values: dict[str, Any]) -> dict[str, Any] | None:
        if not values:
            return self.get(conn, row_id)
        sets = sql.SQL(", ").join(sql.SQL("{c} = %s").format(c=sql.Identifier(self._col(a))) for a in values)
        q = sql.SQL("UPDATE {t} SET {s} WHERE id = %s RETURNING {cols}").format(t=self.table, s=sets, cols=self._select)
        return conn.execute(q, [*values.values(), row_id]).fetchone()

    def delete(self, conn: psycopg.Connection, row_id: Any) -> bool:
        return conn.execute(sql.SQL("DELETE FROM {t} WHERE id = %s").format(t=self.table), (row_id,)).rowcount > 0

    # ---- helpers -------------------------------------------------------------------------------------------------------
    def _col(self, attr: str) -> str:
        try:
            return self.columns[attr]["column"]
        except KeyError:
            raise ValueError(f"unknown attribute '{attr}' for {self.entity['id']}") from None  # programming error, never user-reachable

    def eq(self, attr: str, value: Any) -> tuple[sql.Composable, list[Any]]:
        return sql.SQL("{c} = %s").format(c=sql.Identifier(self._col(attr))), [value]
