"""Deterministic answers of the MockLLMProvider, one function per prompt id.

The mock is NOT canned text for one project: it derives its answers from the structured context of the request (entities,
tables, columns, rules), so it works for any graph. Where it cannot do something faithfully it says so
("not_implemented"), exactly as a careful model would. No randomness, no network, no clock.
"""
from __future__ import annotations

import json
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], dict[str, Any]]


# ---- advisory stages --------------------------------------------------------------------------------------------------------
def graph_analysis(ctx: dict[str, Any]) -> dict[str, Any]:
    obs = [f"{ctx['summary']['operations']} operations across {ctx['summary']['entities']} entities; "
           f"{ctx['summary']['engine_operations']} implemented by the generic engine, {ctx['summary']['custom_operations']} need handlers."]
    risks = [{"ref": c["sources"][0], "risk": c["description"]} for c in ctx.get("conflicts", []) if c["severity"] in ("major", "critical")][:20]
    for u in ctx.get("unmapped", []):
        risks.append({"ref": u["source"], "risk": f"condition not mapped to an enforceable rule: {u['condition']}"})
    return {"observations": obs, "risks": risks}


def architecture(ctx: dict[str, Any]) -> dict[str, Any]:
    d = {}
    for m in ctx["modules"]:
        d[m["name"]] = m["purpose"]
    for s in ctx["services"]:
        d[s["name"]] = f"Application service for {s['id']}: {len(s['operations'])} operation(s)."
    for r in ctx["repositories"]:
        d[r["name"]] = f"Parameterised SQL access to table {r['table']}."
    return {"descriptions": d}


def domain_model(ctx: dict[str, Any]) -> dict[str, Any]:
    out = []
    for e in ctx["entities"]:
        inv = [f"{r}" for r in e.get("rules", [])]
        if e.get("state_machine"):
            inv.append(f"Status changes only along the declared transitions of {e['state_machine']}.")
        for u in e.get("unique", []):
            inv.append(f"{u} is unique.")
        out.append({"entity": e["id"], "invariants": inv})
    return {"entities": out}


def business_rules(ctx: dict[str, Any]) -> dict[str, Any]:
    return {"rules": [], "unresolved": [{"source": u["source"], "reason": "the mock provider does not interpret free-text conditions"} for u in ctx.get("unmapped", [])]}


def database_integration(ctx: dict[str, Any]) -> dict[str, Any]:
    notes = [{"ref": m["graph_entity"], "note": f"mapped to table {m['database_table']}; {len(m['crud_functions'])} CRUD function(s) inventoried, direct parameterised SQL is used for PATCH/filter semantics."}
             for m in ctx["entity_mapping"]["entities"] if m["database_table"]]
    return {"notes": notes, "required_database_changes": [c["recommended_resolution"] for c in ctx.get("conflicts", []) if c["type"] == "DATABASE_CONTRACT_CONFLICT"]}


def authorization(ctx: dict[str, Any]) -> dict[str, Any]:
    findings = []
    for r in ctx["rules"]:
        if r.get("origin", "").startswith("implicit"):
            findings.append({"ref": r["id"], "severity": "info", "finding": "Row privacy was inferred by a secure-default heuristic; the graph does not state it explicitly."})
    for u in ctx.get("unmapped", []):
        findings.append({"ref": u["source"], "severity": "warning", "finding": f"Condition is not enforced beyond roles and schemas: {u['condition']}"})
    return {"findings": findings}


def final_review(ctx: dict[str, Any]) -> dict[str, Any]:
    r = ctx["validation_report"]
    items = [f"conflict: {c}" for c in ctx.get("conflict_codes", [])] + [f"warning: {w}" for w in r.get("warnings", [])]
    return {"summary": f"Backend validation status: {r['status']} after {r.get('correction_attempts', 0)} correction attempt(s).", "open_items": items}


# ---- handlers (api_implementation) -------------------------------------------------------------------------------------------------
def _col(ent: dict[str, Any], attr: str) -> dict[str, Any] | None:
    return next((c for c in ent["columns"] if c["attr"] == attr), None)


def _find(ents: dict[str, Any], pred: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    return next((e for _, e in sorted(ents.items()) if pred(e)), None)


def _impl_place_order(op: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    ents, auth = ctx["entities"], ctx.get("auth_entity")
    order = ents.get(op["entity"])
    product = _find(ents, lambda e: _col(e, "price") and _col(e, "stock"))
    if not (order and product and auth and _col(order, "total")):
        return {"status": "not_implemented", "reason": "no order/product structure with price, stock and total found"}
    cart = _find(ents, lambda e: e["id"] not in (order["id"], product["id"]) and _col(e, "quantity") and any(c["ref"] == auth for c in e["columns"])
                 and any(c["ref"] == product["id"] for c in e["columns"]) and not any(c["ref"] == order["id"] for c in e["columns"]))
    line = _find(ents, lambda e: _col(e, "unit_price") and any(c["ref"] == order["id"] for c in e["columns"]) and any(c["ref"] == product["id"] for c in e["columns"]))
    if not (cart and line):
        return {"status": "not_implemented", "reason": "no cart or order-line entity found"}
    cart_user = next(c for c in cart["columns"] if c["ref"] == auth)["column"]
    cart_prod = next(c for c in cart["columns"] if c["ref"] == product["id"])["column"]
    order_user = next((c for c in order["columns"] if c["ref"] == auth), None)
    line_order = next(c for c in line["columns"] if c["ref"] == order["id"])["column"]
    line_prod = next(c for c in line["columns"] if c["ref"] == product["id"])["column"]
    status = _col(order, "status")
    if not order_user or not status:
        return {"status": "not_implemented", "reason": "order has no owner or status column"}
    initial = ctx["state_initial"].get(order["id"]) or (status["enum"] or ["pending"])[0]
    codes = {v["kind_hint"]: v for v in op.get("validations", [])}
    empty_code = codes.get("cart", {}).get("error_code", "CART_EMPTY")
    empty_msg = codes.get("cart", {}).get("message", "The cart is empty")
    stock_code = codes.get("stock", {}).get("error_code", "OUT_OF_STOCK")
    stock_msg = codes.get("stock", {}).get("message", "One or more products are out of stock")
    cols = [order_user["column"], status["column"], "total"]
    vals = ["principal.user_id", repr(initial), "total"]
    ship = _col(order, "shipping_address")
    if ship:
        cols.append(ship["column"])
        vals.append('body.get("shipping_address")')
    placed = _col(order, "placed_at")
    if placed and not placed["has_default"]:
        cols.append(placed["column"])
        vals.append("datetime.now(timezone.utc)")
    ph = ", ".join(["%s"] * len(cols))
    code = f'''body = body or {{}}
with self.db.transaction() as conn:
    cart = conn.execute('SELECT "{cart_prod}" AS product_id, quantity FROM "{cart["table"]}" WHERE "{cart_user}" = %s ORDER BY "{cart_prod}" FOR UPDATE', (principal.user_id,)).fetchall()
    if not cart:
        raise ConflictError({empty_msg!r}, code={empty_code!r})
    lines = []
    total = Decimal("0")
    for item in cart:
        product = conn.execute('SELECT id, price, stock FROM "{product["table"]}" WHERE id = %s FOR UPDATE', (item["product_id"],)).fetchone()
        if product is None or item["quantity"] > product["stock"]:
            raise ConflictError({stock_msg!r}, code={stock_code!r}, details={{"product_id": str(item["product_id"])}})
        lines.append((product["id"], item["quantity"], product["price"]))
        total += product["price"] * item["quantity"]
    order = conn.execute('INSERT INTO "{order["table"]}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) VALUES ({ph}) RETURNING *', ({", ".join(vals)})).fetchone()
    for product_id, quantity, price in lines:
        conn.execute('INSERT INTO "{line["table"]}" ("{line_order}", "{line_prod}", quantity, unit_price) VALUES (%s, %s, %s, %s)', (order["id"], product_id, quantity, price))
        conn.execute('UPDATE "{product["table"]}" SET stock = stock - %s WHERE id = %s', (quantity, product_id))
    conn.execute('DELETE FROM "{cart["table"]}" WHERE "{cart_user}" = %s', (principal.user_id,))
return {order["class_name"]}.model_validate(order)'''
    return {"status": "implemented", "code": code, "reason": "", "response_extra_fields": [], "response_class": "",
            "notes": ["payment_method is validated but not processed: the graph defines no payment integration", "rows are locked in a stable order (cart by product id) to avoid deadlocks"],
            "meta": {"kind": "checkout", "order": order["id"], "product": product["id"], "cart": cart["id"], "line": line["id"], "total": "total"}}


def _impl_progress(op: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    ents = ctx["entities"]
    parent = ents.get(op["entity"])
    if not parent:
        return {"status": "not_implemented", "reason": "operation has no entity"}
    child, fk, status = None, None, None
    for _, e in sorted(ents.items()):
        for c in e["columns"]:
            if c["ref"] == parent["id"]:
                st = next((x for x in e["columns"] if x["enum"] and any(v in ("completed", "done", "closed", "resolved") for v in x["enum"])), None)
                if st:
                    child, fk, status = e, c, st
                    break
        if child:
            break
    if not child:
        return {"status": "not_implemented", "reason": "no child entity with a completable status refers to this entity"}
    done = next(v for v in status["enum"] if v in ("completed", "done", "closed", "resolved"))
    cname = child["key"]
    code = f'''with self.db.transaction() as conn:
    rows = conn.execute('SELECT p.*, COALESCE(c.total, 0) AS {cname}_count, COALESCE(c.done, 0) AS completed_{cname}_count FROM "{parent["table"]}" p LEFT JOIN (SELECT "{fk["column"]}" AS parent_id, count(*) AS total, count(*) FILTER (WHERE "{status["column"]}" = %s) AS done FROM "{child["table"]}" GROUP BY "{fk["column"]}") c ON c.parent_id = p.id ORDER BY p.id', ({done!r},)).fetchall()
result = []
for row in rows:
    total, completed = row["{cname}_count"], row["completed_{cname}_count"]
    row["progress_percent"] = int(round(100 * completed / total)) if total else 0
    result.append(row)
return result'''
    return {"status": "implemented", "code": code, "reason": "",
            "response_extra_fields": [{"name": f"{cname}_count", "type": "integer"}, {"name": f"completed_{cname}_count", "type": "integer"}, {"name": "progress_percent", "type": "integer"}],
            "response_class": f"{parent['class_name']}ProgressResponse",
            "notes": [f"progress = completed {cname}s / all {cname}s per {parent['key']}; the fields are ADDITIVE to the graph schema (the graph defines no aggregate schema)"],
            "meta": {"kind": "progress", "parent": parent["id"], "child": child["id"], "fk": fk["attr"], "status_attr": status["attr"], "done": done,
                     "count_field": f"{cname}_count", "done_field": f"completed_{cname}_count"}}


def api_implementation(ctx: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for op in ctx["operations"]:
        suffix = op["id"].rsplit(".", 1)[1]
        if op["kind"] == "aggregate" and suffix == "progress":
            out[op["id"]] = _impl_progress(op, ctx)
        elif op["kind"] == "custom" and suffix == "place":
            out[op["id"]] = _impl_place_order(op, ctx)
        else:
            out[op["id"]] = {"status": "not_implemented", "reason": op.get("note") or "the response contract of this operation is not defined well enough to implement it faithfully"}
    for v in out.values():
        v.setdefault("code", "")
        v.setdefault("response_extra_fields", [])
        v.setdefault("response_class", "")
        v.setdefault("notes", [])
    return {"handlers": out}


# ---- tests for handlers ---------------------------------------------------------------------------------------------------------
def _sample(f: dict[str, Any]) -> Any:
    return {"string": "sample", "text": "1 Main Street", "integer": 1}.get(f["base"], "sample")


def testing(ctx: dict[str, Any]) -> dict[str, Any]:
    tests: dict[str, str] = {}
    for oid, h in sorted(ctx["handlers"].items()):
        meta, op = h.get("meta") or {}, ctx["operations"][oid]
        if meta.get("kind") == "checkout":
            tests["place_order"] = _checkout_tests(op, meta, ctx)
        elif meta.get("kind") == "progress":
            tests["progress"] = _progress_tests(op, meta, ctx)
    return {"tests": tests}


def _slug(ac_id: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in ac_id)


def _ac_tests(op: dict[str, Any], mapping: dict[str, str]) -> str:
    """One `test_ac_*` function per acceptance criterion of the operation. `mapping` picks the helper by the error code in the Then text."""
    import re

    out = []
    for ac in op.get("acceptance", []):
        then = " ".join(ac.get("then", []))
        m = re.search(r"error\s+([A-Z][A-Z0-9_]+)", then)
        helper = mapping.get(m.group(1), mapping["__error__"]) if m else mapping["__success__"]
        out.append(f'''

def test_ac_{_slug(ac["id"])}(world):
    """{ac["id"]}: Given {" and ".join(ac.get("given", []))} When {" and ".join(ac.get("when", []))} Then {then}"""
    {helper}(world)
''')
    return "".join(out)


def _checkout_tests(op: dict[str, Any], m: dict[str, Any], ctx: dict[str, Any]) -> str:
    ep, role = op["endpoint"], op["role_key"]
    body = {f["name"]: _sample(f) for f in op["request_fields"]}
    ents = ctx["entities"]
    auth = ctx["auth_entity"]
    user_col = next(c["attr"] for c in ents[m["cart"]]["columns"] if c["ref"] == auth)
    prod_col = next(c["attr"] for c in ents[m["cart"]]["columns"] if c["ref"] == m["product"])
    order_user = next(c["attr"] for c in ents[m["order"]]["columns"] if c["ref"] == auth)
    line_order = next(c["attr"] for c in ents[m["line"]]["columns"] if c["ref"] == m["order"])
    codes = {v["kind_hint"]: v["error_code"] for v in op.get("validations", [])}
    svc = op["service_key"]
    method = op["method_name"]
    empty_code, stock_code = codes.get("cart", "CART_EMPTY"), codes.get("stock", "OUT_OF_STOCK")
    acs = _ac_tests(op, {empty_code: "_empty_cart", stock_code: "_insufficient_stock", "__error__": "_empty_cart", "__success__": "_happy_path"})
    return f'''"""Checkout workflow ({op["id"]}): totals, stock, cart, atomicity, concurrency. Real PostgreSQL."""
import threading
import uuid
from decimal import Decimal

import pytest

from backend_app.engine import Principal
from backend_app.errors import ConflictError

pytestmark = pytest.mark.integration
PATH = {ep["path"]!r}
BODY = {body!r}


def _cart(world, user, qty, stock, price="10.00"):
    product = world.insert({m["product"]!r}, price=Decimal(price), stock=stock)
    world.insert({m["cart"]!r}, **{{{user_col!r}: user["id"], {prod_col!r}: product["id"], "quantity": qty}})
    return product


def _happy_path(world):
    user = world.user({role!r})
    p1, p2 = _cart(world, user, 2, 5, "10.00"), _cart(world, user, 1, 3, "4.50")
    r = world.client.post(PATH, json=BODY, headers=world.headers(user))
    assert r.status_code == {ep["status_code"]}, r.text
    order = world.get({m["order"]!r}, uuid.UUID(r.json()["id"]))
    assert order[{order_user!r}] == user["id"], "the order belongs to the caller"
    assert order["total"] == Decimal("24.50"), "total = sum(price * quantity)"
    lines = [row for row in world.rows({m["line"]!r}) if row[{line_order!r}] == order["id"]]
    assert len(lines) == 2 and sorted(l["quantity"] for l in lines) == [1, 2]
    assert {{l["unit_price"] for l in lines}} == {{Decimal("10.00"), Decimal("4.50")}}, "unit prices are frozen at order time"
    assert world.get({m["product"]!r}, p1["id"])["stock"] == 3 and world.get({m["product"]!r}, p2["id"])["stock"] == 2
    assert [r for r in world.rows({m["cart"]!r}) if r[{user_col!r}] == user["id"]] == [], "the cart is emptied"


def _empty_cart(world):
    user = world.user({role!r})
    before = world.dump()
    r = world.client.post(PATH, json=BODY, headers=world.headers(user))
    assert r.status_code == 409 and r.json()["error"]["code"] == {empty_code!r}, r.text
    assert world.dump() == before, "no data is changed"


def _insufficient_stock(world):
    user = world.user({role!r})
    ok = _cart(world, user, 1, 5)
    short = _cart(world, user, 3, 2)  # second line cannot be fulfilled
    before = world.dump()
    r = world.client.post(PATH, json=BODY, headers=world.headers(user))
    assert r.status_code == 409 and r.json()["error"]["code"] == {stock_code!r}, r.text
    assert world.dump() == before, "no order, no lines, no stock change, cart intact: the transaction rolled back as a whole"
    assert world.get({m["product"]!r}, ok["id"])["stock"] == 5


def test_placing_an_order_creates_lines_decrements_stock_and_empties_the_cart(world):
    _happy_path(world)


def test_an_empty_cart_cannot_be_ordered(world):
    _empty_cart(world)


def test_insufficient_stock_rolls_everything_back(world):
    _insufficient_stock(world)


def test_other_customers_carts_are_untouched(world):
    a, b = world.user({role!r}), world.user({role!r})
    _cart(world, a, 1, 5)
    pb = _cart(world, b, 1, 5)
    assert world.client.post(PATH, json=BODY, headers=world.headers(a)).status_code == {ep["status_code"]}
    assert [r for r in world.rows({m["cart"]!r}) if r[{user_col!r}] == b["id"]], "the other customer's cart must survive"
    assert world.get({m["product"]!r}, pb["id"])["stock"] == 5


def test_concurrent_checkouts_never_oversell(world):
    a, b = world.user({role!r}), world.user({role!r})
    product = world.insert({m["product"]!r}, price=Decimal("10.00"), stock=1)
    for u in (a, b):
        world.insert({m["cart"]!r}, **{{{user_col!r}: u["id"], {prod_col!r}: product["id"], "quantity": 1}})
    service = world.container.services.{svc}
    results = []

    def go(u):
        p = Principal(u["id"], u["email"], u["role"], "role." + u["role"], frozenset({{"role." + u["role"]}}))
        try:
            service.{method}(p, body=dict(BODY))
            results.append("ok")
        except ConflictError:
            results.append("conflict")

    threads = [threading.Thread(target=go, args=(u,)) for u in (a, b)]
    [t.start() for t in threads]
    [t.join(20) for t in threads]
    assert sorted(results) == ["conflict", "ok"], results
    assert world.get({m["product"]!r}, product["id"])["stock"] == 0
    assert len(world.rows({m["order"]!r})) == 1
{acs}'''


def _progress_tests(op: dict[str, Any], m: dict[str, Any], ctx: dict[str, Any]) -> str:
    ep, role = op["endpoint"], op["role_key"]
    acs = _ac_tests(op, {"__error__": "_progress_happy", "__success__": "_progress_happy"})
    return f'''"""Aggregate ({op["id"]}): per-parent counts and percentage computed in the database."""
import pytest

pytestmark = pytest.mark.integration
PATH = {ep["path"]!r}


def _progress_happy(world):
    boss = world.user({role!r})
    p1, p2 = world.insert({m["parent"]!r}), world.insert({m["parent"]!r})
    world.insert({m["child"]!r}, **{{{m["fk"]!r}: p1["id"], {m["status_attr"]!r}: {m["done"]!r}}})
    world.insert({m["child"]!r}, **{{{m["fk"]!r}: p1["id"]}})
    r = world.client.get(PATH, headers=world.headers(boss))
    assert r.status_code == {ep["status_code"]}, r.text
    by_id = {{x["id"]: x for x in r.json()}}
    a, b = by_id[str(p1["id"])], by_id[str(p2["id"])]
    assert (a[{m["count_field"]!r}], a[{m["done_field"]!r}], a["progress_percent"]) == (2, 1, 50)
    assert (b[{m["count_field"]!r}], b[{m["done_field"]!r}], b["progress_percent"]) == (0, 0, 0)
    assert "password_hash" not in r.text


def test_progress_reports_completed_over_total_per_parent(world):
    _progress_happy(world)
{acs}'''


HANDLERS: dict[str, Handler] = {
    "graph_analysis": graph_analysis, "architecture": architecture, "domain_model": domain_model, "business_rules": business_rules,
    "database_integration": database_integration, "authorization": authorization, "final_review": final_review,
    "api_implementation": api_implementation, "testing": testing,
}


def respond(prompt_id: str, context: dict[str, Any]) -> str:
    if prompt_id not in HANDLERS:
        # corrections and unknown prompts: the mock cannot diagnose anything, so it proposes no edits
        if prompt_id.endswith("_correction") and prompt_id != "structured_output_correction":
            return json.dumps({"diagnosis": "The mock provider cannot diagnose failures; no edit proposed.", "edits": []})
        raise KeyError(f"MockLLMProvider has no handler for prompt '{prompt_id}'")
    return json.dumps(HANDLERS[prompt_id](context), sort_keys=True)
