"""Database contract: what the Database Agent actually built (or promised to build).

Source artifacts (Database-Agent `generated/` layout):
  architecture.json     tables, columns (PostgreSQL types, nullable, source_field), PK/FK/unique/indexes
  schema.sql            executed DDL (one statement per `;`)
  crud.sql              CRUD functions/procedures
  database_state.json   catalog snapshot {schema, tables{name:{columns,constraints,indexes}}, functions}  (optional)

The contract can also be built from a live schema (`introspect`) - that is how the agent proves that schema.sql
really executes and matches architecture.json instead of trusting the files.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.errors import ContractError, ErrorKind, Issue

TYPE_FAMILIES = {
    "uuid": "uuid",
    "smallint": "integer", "integer": "integer", "int": "integer", "int2": "integer", "int4": "integer", "bigint": "integer", "int8": "integer",
    "serial": "integer", "bigserial": "integer",
    "numeric": "numeric", "decimal": "numeric", "real": "numeric", "double precision": "numeric", "float8": "numeric",
    "text": "text", "varchar": "text", "character varying": "text", "char": "text", "character": "text", "citext": "text",
    "boolean": "boolean", "bool": "boolean",
    "timestamptz": "timestamp", "timestamp": "timestamp", "timestamp with time zone": "timestamp",
    "timestamp without time zone": "timestamp",
    "date": "date",
    "json": "json", "jsonb": "json",
    "user-defined": "enum",
}

# graph attribute type -> acceptable PostgreSQL families (first = preferred)
GRAPH_TO_PG: dict[str, tuple[str, ...]] = {
    "uuid": ("uuid",), "string": ("text",), "text": ("text",), "email": ("text",), "url": ("text",),
    "integer": ("integer",), "decimal": ("numeric",), "boolean": ("boolean",), "date": ("date",),
    "datetime": ("timestamp",), "enum": ("text", "enum"), "json": ("json",),
}


def pg_family(pg_type: str) -> str:
    t = re.sub(r"\(.*?\)", "", pg_type.strip().lower())
    t = re.sub(r"\[\]$", "", t).strip()
    return TYPE_FAMILIES.get(t, t)


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    default: str | None = None  # None = no default
    source_field: str | None = None

    @property
    def family(self) -> str:
        return pg_family(self.type)

    @property
    def has_default(self) -> bool:
        return self.default is not None


@dataclass
class ForeignKey:
    columns: list[str]
    ref_table: str
    ref_columns: list[str]
    on_delete: str = "NO ACTION"


@dataclass
class Table:
    name: str
    columns: dict[str, Column]
    primary_key: list[str]
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    unique_constraints: list[list[str]] = field(default_factory=list)
    source_entities: list[str] = field(default_factory=list)
    check_constraints: list[str] = field(default_factory=list)


@dataclass
class Function:
    name: str
    arguments: str = ""
    returns: str = ""
    kind: str = "function"
    table: str | None = None
    operation: str | None = None


@dataclass
class DatabaseContract:
    tables: dict[str, Table]
    functions: list[Function] = field(default_factory=list)
    creation_order: list[str] = field(default_factory=list)
    schema_sql: str | None = None
    crud_sql: str | None = None
    schema_name: str | None = None
    origin: str = "unknown"

    def table_for_entity(self, entity_key: str) -> Table | None:
        """Match graph entity `entity.<key>` to a table: explicit source_entities first, then naming conventions."""
        pascal = "".join(p.capitalize() for p in entity_key.split("_"))
        for t in self.tables.values():
            if any(s == pascal or s.lower() == entity_key.lower().replace("_", "") or s == entity_key for s in t.source_entities):
                return t
        for cand in naming_candidates(entity_key):
            if cand in self.tables:
                return self.tables[cand]
        return None

    def functions_for(self, table: str) -> list[Function]:
        return [f for f in self.functions if f.table == table]


def naming_candidates(entity_key: str) -> list[str]:
    plural = entity_key + ("es" if entity_key.endswith(("s", "x", "ch", "sh")) else ("ies" if entity_key.endswith("y") and entity_key[-2:-1] not in "aeiou" else "s"))
    if plural.endswith("ies") and entity_key.endswith("y"):
        plural = entity_key[:-1] + "ies"
    return [plural, entity_key, entity_key + "s"]


# ---------------------------------------------------------------------------------------------------
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ContractError(Issue(ErrorKind.DATABASE_CONTRACT_ERROR, "artifact_missing", f"missing database artifact {path.name}", str(path)))
    except json.JSONDecodeError as e:
        raise ContractError(Issue(ErrorKind.DATABASE_CONTRACT_ERROR, "artifact_invalid", f"{path.name} is not valid JSON: {e}", str(path)))


_CREATE_FN = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE)\s+([a-z_][a-z0-9_]*)\s*\(", re.I)


def _split_top_level(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def parse_schema_defaults(schema_sql: str) -> dict[str, dict[str, str]]:
    """Best-effort column DEFAULT extraction from CREATE TABLE statements: {table: {column: default_expr}}.

    Used only when database_state.json is absent. GENERATED ... AS IDENTITY counts as a default.
    """
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)\s*\(", schema_sql, re.I):
        start = m.end()
        depth, i = 1, start
        while i < len(schema_sql) and depth:
            depth += {"(": 1, ")": -1}.get(schema_sql[i], 0)
            i += 1
        for item in _split_top_level(schema_sql[start:i - 1]):
            first = item.split(None, 1)[0].lower() if item.split() else ""
            if first in ("constraint", "primary", "foreign", "unique", "check", "exclude", "like"):
                continue
            col = item.split(None, 1)[0].strip('"')
            d = re.search(r"\bDEFAULT\s+(.+?)(?=\s+(?:NOT\s+NULL|NULL|CONSTRAINT|CHECK|REFERENCES|PRIMARY|UNIQUE|GENERATED)\b|$)", item, re.I | re.S)
            if d:
                out.setdefault(m.group(1), {})[col] = d.group(1).strip()
            elif re.search(r"GENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY", item, re.I):
                out.setdefault(m.group(1), {})[col] = "identity"
    return out


def load_database_contract(directory: str | Path) -> DatabaseContract:
    d = Path(directory)
    arch = _read_json(d / "architecture.json")
    schema_sql = (d / "schema.sql").read_text(encoding="utf-8") if (d / "schema.sql").exists() else None
    crud_sql = (d / "crud.sql").read_text(encoding="utf-8") if (d / "crud.sql").exists() else None
    state = _read_json(d / "database_state.json") if (d / "database_state.json").exists() else None

    defaults: dict[str, dict[str, str]] = {}
    if state:
        for tname, t in state.get("tables", {}).items():
            for c in t.get("columns", []):
                if c.get("column_default") is not None:
                    defaults.setdefault(tname, {})[c["column_name"]] = c["column_default"]
    elif schema_sql:
        defaults = parse_schema_defaults(schema_sql)

    tables: dict[str, Table] = {}
    for t in arch.get("tables", []):
        cols = {
            c["name"]: Column(c["name"], c["type"], bool(c.get("nullable", True)), defaults.get(t["name"], {}).get(c["name"]), c.get("source_field"))
            for c in t["columns"]
        }
        tables[t["name"]] = Table(
            t["name"], cols, list(t["primary_key"]["columns"]),
            [ForeignKey(list(f["columns"]), f["ref_table"], list(f["ref_columns"]), f.get("on_delete", "NO ACTION")) for f in t.get("foreign_keys", [])],
            [list(u) for u in t.get("unique_constraints", [])], list(t.get("source_entities", [])),
        )
    if state:
        for tname, t in state.get("tables", {}).items():
            if tname in tables:
                for con in t.get("constraints", []):
                    if con.get("type") == "UNIQUE" and con["columns"] not in tables[tname].unique_constraints:
                        tables[tname].unique_constraints.append(list(con["columns"]))
                    if con.get("type") == "CHECK":
                        tables[tname].check_constraints.append(con.get("definition", ""))
    funcs: list[Function] = []
    if state:
        funcs = [Function(f["name"], f.get("arguments", ""), f.get("returns", ""), f.get("kind", "function")) for f in state.get("functions", [])]
    elif crud_sql:
        funcs = [Function(m.group(2), kind=m.group(1).lower()) for m in _CREATE_FN.finditer(crud_sql)]
    crud_meta = d / "crud_functions.json"  # optional table/operation labels (the Database agent's CrudFunction list)
    if crud_meta.exists():
        meta = {f["name"]: f for f in _read_json(crud_meta)}
        for f in funcs:
            if f.name in meta:
                f.table, f.operation = meta[f.name].get("table"), meta[f.name].get("operation")
    return DatabaseContract(tables, funcs, list(arch.get("creation_order", [])), schema_sql, crud_sql,
                            (state or {}).get("schema"), origin=str(d))


# ---------------------------------------------------------------------------------------------------
_COLUMNS_Q = """
SELECT table_name, column_name, data_type, is_nullable = 'YES' AS nullable, column_default
FROM information_schema.columns WHERE table_schema = %s ORDER BY table_name, ordinal_position
"""
_CONSTRAINTS_Q = """
SELECT c.relname AS table_name, con.conname AS name, con.contype AS type, pg_get_constraintdef(con.oid) AS definition,
  ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum ORDER BY k.ord) AS columns,
  rc.relname AS ref_table,
  ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.attnum ORDER BY k.ord) AS ref_columns
FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_class rc ON rc.oid = con.confrelid WHERE n.nspname = %s ORDER BY c.relname, con.conname
"""
_FUNCTIONS_Q = """
SELECT p.proname AS name, pg_get_function_identity_arguments(p.oid) AS arguments, pg_get_function_result(p.oid) AS returns,
       CASE p.prokind WHEN 'p' THEN 'procedure' ELSE 'function' END AS kind
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s AND p.prokind IN ('f','p') ORDER BY p.proname
"""
_CTYPES = {"p": "PRIMARY KEY", "f": "FOREIGN KEY", "u": "UNIQUE", "c": "CHECK", "x": "EXCLUDE"}


def introspect(conn, schema: str) -> dict[str, Any]:
    """Catalog snapshot in the Database Agent's `database_state.json` shape."""
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cols = cur.execute(_COLUMNS_Q, (schema,)).fetchall()
        cons = cur.execute(_CONSTRAINTS_Q, (schema,)).fetchall()
        funcs = cur.execute(_FUNCTIONS_Q, (schema,)).fetchall()
    tables: dict[str, dict[str, list]] = {}
    for kind, rows in (("columns", cols), ("constraints", cons)):
        for r in rows:
            r = dict(r)
            t = tables.setdefault(r.pop("table_name"), {"columns": [], "constraints": [], "indexes": []})
            if kind == "constraints":
                r["type"] = _CTYPES.get(r["type"], r["type"])
            t[kind].append(r)
    return {"schema": schema, "tables": tables, "functions": [dict(f) for f in funcs]}


def verify_live_schema(snapshot: dict[str, Any], contract: DatabaseContract) -> list[Issue]:
    """Compare a live catalog snapshot with the contract's architecture. Empty list = schema.sql really builds it."""
    issues: list[Issue] = []
    live = snapshot["tables"]
    for name in sorted(set(contract.tables) - set(live)):
        issues.append(Issue(ErrorKind.DATABASE_CONTRACT_ERROR, "table_not_created", f"table '{name}' is in architecture.json but schema.sql did not create it", name))
    for name, t in contract.tables.items():
        if name not in live:
            continue
        live_cols = {c["column_name"]: c for c in live[name]["columns"]}
        for c in t.columns.values():
            lc = live_cols.get(c.name)
            if lc is None:
                issues.append(Issue(ErrorKind.DATABASE_CONTRACT_ERROR, "column_not_created", f"{name}.{c.name} missing in the live schema", f"{name}.{c.name}"))
            elif pg_family(lc["data_type"]) != c.family:
                issues.append(Issue(ErrorKind.DATABASE_CONTRACT_ERROR, "column_type_differs",
                                    f"{name}.{c.name}: architecture says {c.type}, live schema has {lc['data_type']}", f"{name}.{c.name}"))
        pks = [con["columns"] for con in live[name]["constraints"] if con["type"] == "PRIMARY KEY"]
        if [t.primary_key] != pks:
            issues.append(Issue(ErrorKind.DATABASE_CONTRACT_ERROR, "pk_differs", f"{name}: primary key {pks} != architecture {t.primary_key}", name))
    return issues
