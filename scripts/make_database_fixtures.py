"""Stand-in for the Database Agent, used ONLY to create committed test fixtures.

The real Database Agent is LLM-driven and consumes its own `entities.json` format (not the Graph agent's), so there is no
generated database for the example projects. This script writes artifacts in the Database Agent's `generated/` layout
(architecture.json, schema.sql, crud.sql, crud_functions.json, database_state.json) following its documented conventions
(snake_case plural tables, `pk_/fk_/uq_/ck_/ix_` constraint names, `create_/get_/update_/delete_/list_` functions),
then EXECUTES them on a real PostgreSQL and snapshots the live catalog into database_state.json.

It is not part of the backend agent's pipeline: the backend never designs a database, it only consumes one.

    python scripts/make_database_fixtures.py projects/task_manager [...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.contracts.database import introspect  # noqa: E402
from app.graph.loader import load_graph_package  # noqa: E402
from app.testing.pg_cluster import apply_sql, temp_schema  # noqa: E402
import psycopg  # noqa: E402

PG_TYPES = {"uuid": "uuid", "string": "text", "text": "text", "email": "text", "url": "text", "integer": "integer",
            "decimal": "numeric(12,2)", "boolean": "boolean", "date": "date", "datetime": "timestamptz", "json": "jsonb"}
POSITIVE = {"price", "stock", "quantity", "total", "unit_price"}  # CHECK >= 0 (quantity > 0)
CASCADE_CHILD = ("_item", "_member", "_comment")


def plural(key: str) -> str:
    if key.endswith("y") and key[-2:-1] not in "aeiou":
        return key[:-1] + "ies"
    return key + ("es" if key.endswith(("s", "x", "ch", "sh")) else "s")


def singular_fn(key: str) -> str:
    return key


def build(pkg) -> dict:
    ents = {k.split(".", 1)[1]: e for k, e in pkg.entities.items()}
    tables, ddl, order, seen = [], [], [], set()

    def deps(key):
        return {entity_key for entity_key in (a["reference_to"].split(".", 1)[1] for a in ents[key]["attributes"] if a.get("reference_to")) if entity_key != key}

    pending = sorted(ents)
    while pending:
        progressed = False
        for key in list(pending):
            if deps(key) <= seen:
                order.append(key), seen.add(key), pending.remove(key)
                progressed = True
        if not progressed:
            raise SystemExit(f"cyclic entity references among {pending}; fixture generator does not model deferred FKs")

    for key in order:
        e, tname = ents[key], plural(key)
        cols, defs, fks, uniques, idx, checks = [], [], [], [], [], []
        for a in e["attributes"]:
            name, t = a["name"], PG_TYPES[a["type"]] if a["type"] != "enum" else "text"
            nullable = not a["required"] and name != "id"
            cols.append({"name": name, "type": t, "nullable": nullable, "source_field": name})
            d = f"{name} {t}"
            if name == "id":
                d += " DEFAULT gen_random_uuid()"
            if not nullable:
                d += " NOT NULL"
            if name in ("created_at", "placed_at") and a["type"] == "datetime":
                d += " DEFAULT now()"
            defs.append(d)
            if a["type"] == "enum":
                vals = ", ".join(f"'{v}'" for v in a["enum_values"])
                checks.append(f"CONSTRAINT ck_{tname}_{name} CHECK ({name} IN ({vals}))")
            if name in POSITIVE and a["type"] in ("decimal", "integer"):
                op = ">" if name == "quantity" else ">="
                checks.append(f"CONSTRAINT ck_{tname}_{name} CHECK ({name} {op} 0)")
            if a.get("unique") and name != "id":
                uniques.append([name])
            if a.get("reference_to"):
                rk = a["reference_to"].split(".", 1)[1]
                cascade = key.endswith(CASCADE_CHILD) and rk not in ("user", "product", "department")
                fks.append({"columns": [name], "ref_table": plural(rk), "ref_columns": ["id"], "on_delete": "CASCADE" if cascade else "RESTRICT", "on_update": "NO ACTION", "deferred": False, "rationale": ""})
                idx.append({"columns": [name], "unique": False, "reason": f"join/filter on {name}"})
        body = defs + [f"CONSTRAINT pk_{tname} PRIMARY KEY (id)"] + [f"CONSTRAINT uq_{tname}_{'_'.join(u)} UNIQUE ({', '.join(u)})" for u in uniques]
        body += [f"CONSTRAINT fk_{tname}_{f['columns'][0]} FOREIGN KEY ({f['columns'][0]}) REFERENCES {f['ref_table']} (id) ON DELETE {f['on_delete']}" for f in fks]
        body += checks
        ddl.append(f"CREATE TABLE {tname} (\n  " + ",\n  ".join(body) + "\n);")
        for i in idx:
            ddl.append(f"CREATE INDEX ix_{tname}_{i['columns'][0]} ON {tname} ({i['columns'][0]});")
        tables.append({"name": tname, "purpose": e["description"], "source_entities": [e["name"].replace(" ", "")], "columns": cols,
                       "primary_key": {"columns": ["id"]}, "foreign_keys": fks, "unique_constraints": uniques, "indexes": idx,
                       "dependencies": [plural(d) for d in sorted(deps(key))]})
    arch = {"tables": tables, "relationships": [], "creation_order": [plural(k) for k in order],
            "normalization": {"target": "3NF", "analysis": ["fixture generated from entities.json"], "violations": [], "intentional_denormalizations": []},
            "normalization_notes": []}

    fn_sql, fn_meta = [], []
    for key in order:
        e, t = ents[key], plural(key)
        writable = [a for a in e["attributes"] if a["name"] != "id"]
        params = ", ".join(f"p_{a['name']} {PG_TYPES.get(a['type'], 'text')}" for a in writable)
        names, vals = ", ".join(a["name"] for a in writable), ", ".join(f"p_{a['name']}" for a in writable)
        sets = ", ".join(f"{a['name']} = p_{a['name']}" for a in writable)
        ptypes = ", ".join(PG_TYPES.get(a["type"], "text") for a in writable)
        fn_sql += [
            f"DROP FUNCTION IF EXISTS create_{key}({ptypes});",
            f"CREATE FUNCTION create_{key}({params}) RETURNS {t} LANGUAGE sql AS $$ INSERT INTO {t} ({names}) VALUES ({vals}) RETURNING *; $$;",
            f"DROP FUNCTION IF EXISTS get_{key}(uuid);",
            f"CREATE FUNCTION get_{key}(p_id uuid) RETURNS SETOF {t} LANGUAGE sql AS $$ SELECT * FROM {t} WHERE id = p_id; $$;",
            f"DROP FUNCTION IF EXISTS update_{key}(uuid, {ptypes});",
            f"CREATE FUNCTION update_{key}(p_id uuid, {params}) RETURNS {t} LANGUAGE plpgsql AS $$ DECLARE r {t}; BEGIN "
            f"UPDATE {t} SET {sets} WHERE id = p_id RETURNING * INTO r; IF NOT FOUND THEN RAISE EXCEPTION '{key} % not found', p_id USING ERRCODE = 'P0002'; END IF; RETURN r; END $$;",
            f"DROP FUNCTION IF EXISTS delete_{key}(uuid);",
            f"CREATE FUNCTION delete_{key}(p_id uuid) RETURNS boolean LANGUAGE plpgsql AS $$ BEGIN DELETE FROM {t} WHERE id = p_id; RETURN FOUND; END $$;",
            f"DROP FUNCTION IF EXISTS list_{t}();",
            f"CREATE FUNCTION list_{t}() RETURNS SETOF {t} LANGUAGE sql AS $$ SELECT * FROM {t} ORDER BY id; $$;",
        ]
        fn_meta += [{"name": f"{op}_{key}", "table": t, "operation": op, "description": f"{op} {key}"} for op in ("create", "get", "update", "delete")]
        fn_meta.append({"name": f"list_{t}", "table": t, "operation": "list", "description": f"list {t}"})
    return {"architecture": arch, "schema_sql": "\n".join(ddl) + "\n", "crud_sql": "\n".join(fn_sql) + "\n", "crud_functions": fn_meta}


def main(paths: list[str]) -> int:
    for p in paths:
        pkg = load_graph_package(p)
        out = Path(p) / "database"
        out.mkdir(parents=True, exist_ok=True)
        art = build(pkg)
        with temp_schema(prefix="fixture") as (base, schema):
            apply_sql(base, schema, art["schema_sql"])
            apply_sql(base, schema, art["crud_sql"])  # functions must really execute
            with psycopg.connect(base, autocommit=True) as c:
                state = introspect(c, schema)
                state["schema"] = pkg.project_key
        (out / "architecture.json").write_text(json.dumps(art["architecture"], indent=2) + "\n")
        (out / "schema.sql").write_text(art["schema_sql"])
        (out / "crud.sql").write_text(art["crud_sql"])
        (out / "crud_functions.json").write_text(json.dumps(art["crud_functions"], indent=2) + "\n")
        (out / "database_state.json").write_text(json.dumps(state, indent=2, default=str) + "\n")
        (out / "SOURCE.md").write_text(
            "Fixture standing in for the Database Agent's `generated/` output.\n\n"
            "Produced by `scripts/make_database_fixtures.py` from this project's graphs (the real Database Agent needs an LLM and its own "
            "entities.json format). The SQL was executed on PostgreSQL and `database_state.json` is a live catalog snapshot.\n")
        print(f"{p}: {len(art['architecture']['tables'])} tables -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["projects/task_manager", "projects/ecommerce_store", "projects/support_ticketing_system"]))
