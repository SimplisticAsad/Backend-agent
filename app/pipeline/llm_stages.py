"""LLM-assisted stages. Python builds each context, validates each answer, and decides what is accepted.

  advisory   graph_analysis, architecture, domain_model, database_integration, authorization, final_review  -> notes in artifacts only
  functional business_rules (additive rule DSL), api_implementation (handler bodies), testing (tests for handlers)
Functional stages are validated strictly: invalid output is retried (bounded) and then REJECTED; nothing unvalidated reaches the code.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.analysis.rules import RuleSet, merge_rules
from app.generation.handler_checks import check_handler_body, check_test_module
from app.graph.model import GraphPackage
from app.llm.client import LLMClient, LLMOutputError

SCHEMAS: dict[str, str] = {
    "graph_analysis": '{"observations": ["string"], "risks": [{"ref": "string", "risk": "string"}]}',
    "architecture": '{"descriptions": {"<name>": "string"}}',
    "domain_model": '{"entities": [{"entity": "string", "invariants": ["string"]}]}',
    "business_rules": '{"rules": [{"id": "rule.llm.<name>", "type": "ownership|row_scope|transition_guard|frozen_state", "...": "type specific keys"}], "unresolved": [{"source": "string", "reason": "string"}]}',
    "api_implementation": '{"handlers": {"<operation id>": {"status": "implemented|not_implemented", "code": "string", "reason": "string", "response_extra_fields": [], "response_class": "string", "notes": [], "meta": {}}}}',
    "database_integration": '{"notes": [{"ref": "string", "note": "string"}], "required_database_changes": ["string"]}',
    "authorization": '{"findings": [{"ref": "string", "severity": "info|warning|critical", "finding": "string"}]}',
    "testing": '{"tests": {"<short_name>": "python source"}}',
    "final_review": '{"summary": "string", "open_items": ["string"]}',
}


def _dump(x: Any) -> str:
    return json.dumps(x, indent=1, sort_keys=True, default=str)


class LLMStages:
    def __init__(self, client: LLMClient) -> None:
        self.client = client

    def _call(self, pid: str, ctx: dict[str, Any], validate) -> Any:
        return self.client.call(pid, {"INPUT_JSON": _dump(ctx), "OUTPUT_SCHEMA": SCHEMAS[pid]}, ctx, validate)

    # ---- advisory ------------------------------------------------------------------------------------------------------------
    def graph_analysis(self, ctx: dict[str, Any], known_ids: set[str]) -> dict[str, Any]:
        def validate(d: Any) -> list[str]:
            p = _shape(d, {"observations", "risks"})
            if p:
                return p
            p += [f"observations must be strings" for o in d["observations"] if not isinstance(o, str)]
            for r in d["risks"]:
                if not isinstance(r, dict) or set(r) != {"ref", "risk"} or r["ref"] not in known_ids:
                    p.append(f"risk {r!r}: needs exactly ref (a known id) and risk")
            return p

        return self._call("graph_analysis", ctx, validate)

    def architecture(self, ctx: dict[str, Any]) -> dict[str, Any]:
        names = {m["name"] for m in ctx["modules"]} | {s["name"] for s in ctx["services"]} | {r["name"] for r in ctx["repositories"]}

        def validate(d: Any) -> list[str]:
            p = _shape(d, {"descriptions"})
            if p:
                return p
            return [f"unknown name {k!r}" for k in d["descriptions"] if k not in names] + [f"description of {k!r} must be a string" for k, v in d["descriptions"].items() if not isinstance(v, str)]

        return self._call("architecture", ctx, validate)

    def domain_model(self, ctx: dict[str, Any]) -> dict[str, Any]:
        ids = {e["id"] for e in ctx["entities"]}

        def validate(d: Any) -> list[str]:
            p = _shape(d, {"entities"})
            if p:
                return p
            out = [f"unknown entity {e.get('entity')!r}" for e in d["entities"] if e.get("entity") not in ids]
            out += ["every entity needs invariants as a list of strings" for e in d["entities"] if not isinstance(e.get("invariants"), list)]
            return out

        return self._call("domain_model", ctx, validate)

    def database_integration(self, ctx: dict[str, Any]) -> dict[str, Any]:
        refs = {m["graph_entity"] for m in ctx["entity_mapping"]["entities"]} | {m["database_table"] for m in ctx["entity_mapping"]["entities"] if m["database_table"]}

        def validate(d: Any) -> list[str]:
            p = _shape(d, {"notes", "required_database_changes"})
            if p:
                return p
            out = [f"note ref {n.get('ref')!r} is unknown" for n in d["notes"] if n.get("ref") not in refs]
            out += [f"{c!r}: proposing to let the backend alter the schema is not allowed" for c in d["required_database_changes"] if re.search(r"backend (should|must|can) (create|alter|drop)", str(c), re.I)]
            return out

        return self._call("database_integration", ctx, validate)

    def authorization(self, ctx: dict[str, Any], known_ids: set[str]) -> dict[str, Any]:
        def validate(d: Any) -> list[str]:
            p = _shape(d, {"findings"})
            if p:
                return p
            out = []
            for f in d["findings"]:
                if f.get("ref") not in known_ids:
                    out.append(f"finding ref {f.get('ref')!r} is unknown")
                if f.get("severity") not in ("info", "warning", "critical"):
                    out.append(f"bad severity {f.get('severity')!r}")
                if re.search(r"\b(remove|relax|weaken|disable)\b.*\b(check|role|auth|permission|rule)", str(f.get("finding", "")), re.I):
                    out.append("findings may not recommend weakening authorization")
            return out

        return self._call("authorization", ctx, validate)

    def final_review(self, ctx: dict[str, Any]) -> dict[str, Any]:
        def validate(d: Any) -> list[str]:
            p = _shape(d, {"summary", "open_items"})
            if p:
                return p
            claims_ok = re.search(r"\b(all|every)\b.*\b(pass|passed|green)\b", d["summary"], re.I) and ctx["validation_report"]["status"] != "passed"
            return ["the summary claims success but the validation report does not say 'passed'"] if claims_ok else []

        return self._call("final_review", ctx, validate)

    # ---- functional: business rules --------------------------------------------------------------------------------------------
    def business_rules(self, ctx: dict[str, Any], pkg: GraphPackage, baseline: RuleSet, spec_entities: dict[str, Any]) -> dict[str, Any]:
        unmapped_ids = {u["source"] for u in ctx["unmapped"]}

        def validate(d: Any) -> list[str]:
            p = _shape(d, {"rules", "unresolved"})
            if p:
                return p
            accepted, rejected = merge_rules(baseline, d["rules"])
            problems = list(rejected)
            for r in accepted:
                problems += _check_rule_refs(r, pkg, spec_entities)
            problems += [f"unresolved source {u.get('source')!r} is not an unmapped condition" for u in d["unresolved"] if u.get("source") not in unmapped_ids]
            return problems

        data = self._call("business_rules", ctx, validate)
        accepted, _ = merge_rules(baseline, data["rules"])
        return {"rules": accepted, "unresolved": data["unresolved"]}

    # ---- functional: handlers ---------------------------------------------------------------------------------------------------
    def api_implementation(self, ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
        op_ids = {o["id"] for o in ctx["operations"]}
        tables = {e["table"] for e in ctx["entities"].values()}

        def validate(d: Any) -> list[str]:
            p = _shape(d, {"handlers"})
            if p:
                return p
            out = [f"handler for unknown operation {k!r}" for k in d["handlers"] if k not in op_ids]
            out += [f"no handler decision for {k!r}" for k in sorted(op_ids - set(d["handlers"]))]
            for k, h in d["handlers"].items():
                if h.get("status") not in ("implemented", "not_implemented"):
                    out.append(f"{k}: status must be implemented or not_implemented")
                elif h["status"] == "implemented":
                    out += [f"{k}: {x}" for x in check_handler_body(h.get("code", ""), allowed_tables=tables)]
                    for f in h.get("response_extra_fields", []):
                        if not re.match(r"^[a-z][a-z0-9_]*$", str(f.get("name", ""))) or f.get("type") not in ("integer", "string", "decimal", "boolean"):
                            out.append(f"{k}: bad response_extra_fields entry {f!r}")
                    if h.get("response_extra_fields") and not re.match(r"^[A-Z][A-Za-z0-9]*$", h.get("response_class", "")):
                        out.append(f"{k}: response_class must be PascalCase when extra fields are declared")
            return out

        try:
            data = self._call("api_implementation", ctx, validate)["handlers"]
        except LLMOutputError as e:  # every handler is refused; the operations answer 501 until fixed
            return {oid: {"status": "not_implemented", "reason": f"handler stage failed validation: {'; '.join(e.problems[:3])}", "source": "llm", "stage": "api_implementation"} for oid in sorted(op_ids)}
        out: dict[str, dict[str, Any]] = {}
        for oid in sorted(data):
            h = data[oid]
            out[oid] = {"status": h["status"], "source": "llm", "stage": "api_implementation", "reason": h.get("reason", ""), "notes": h.get("notes", []), "meta": h.get("meta", {})}
            if h["status"] == "implemented":
                out[oid].update({"code": h["code"], "response_extra_fields": h.get("response_extra_fields", []), "response_class": h.get("response_class", "")})
        return out

    # ---- functional: tests for handlers --------------------------------------------------------------------------------------------
    def testing(self, ctx: dict[str, Any]) -> dict[str, str]:
        if not ctx["handlers"]:
            return {}

        def validate(d: Any) -> list[str]:
            p = _shape(d, {"tests"})
            if p:
                return p
            out = []
            for name, src in d["tests"].items():
                if not re.match(r"^[a-z][a-z0-9_]{0,40}$", name):
                    out.append(f"bad test module name {name!r}")
                out += [f"{name}: {x}" for x in check_test_module(src)]
            return out

        try:
            return dict(sorted(self._call("testing", ctx, validate)["tests"].items()))
        except LLMOutputError:
            return {}


def _shape(d: Any, keys: set[str]) -> list[str]:
    if not isinstance(d, dict):
        return ["the answer must be a JSON object"]
    if set(d) != keys:
        return [f"keys must be exactly {sorted(keys)}, got {sorted(d)}"]
    return []


def _check_rule_refs(r: dict[str, Any], pkg: GraphPackage, spec_entities: dict[str, Any]) -> list[str]:
    p: list[str] = []
    rid = r["id"]
    if not rid.startswith("rule.llm."):
        p.append(f"{rid}: rule ids must start with 'rule.llm.'")
    ent = spec_entities.get(r["entity"])
    if ent is None:
        return p + [f"{rid}: unknown entity {r['entity']}"]
    attrs = {c["attr"] for c in ent["columns"]}
    for op in r["operations"]:
        if op not in pkg.operations:
            p.append(f"{rid}: unknown operation {op}")
        elif pkg.operations[op].get("entity_ref") != r["entity"]:
            p.append(f"{rid}: operation {op} is not an operation on {r['entity']}")
    for role in r.get("exempt_roles", []) + r.get("roles", []):
        if role not in pkg.roles:
            p.append(f"{rid}: unknown role {role}")
    if r["type"] in ("ownership", "transition_guard", "frozen_state") and r["field"] not in attrs:
        p.append(f"{rid}: unknown field {r['field']}")
    if r["type"] == "transition_guard":
        p += [f"{rid}: unknown field {f}" for f in r["require_fields_set"] if f not in attrs]
        col = next((c for c in ent["columns"] if c["attr"] == r["field"]), None)
        if col and col["enum"] and r["to"] not in col["enum"]:
            p.append(f"{rid}: '{r['to']}' is not a value of {r['field']}")
    if r["type"] == "frozen_state":
        col = next((c for c in ent["columns"] if c["attr"] == r["field"]), None)
        if col and col["enum"]:
            p += [f"{rid}: '{s}' is not a value of {r['field']}" for s in r["states"] if s not in col["enum"]]
    if r["type"] == "row_scope" and not _scope_ok(r["scope"], spec_entities):
        p.append(f"{rid}: invalid scope node")
    if r["type"] == "ownership" and r.get("status", 403) not in (403, 404):
        p.append(f"{rid}: ownership violations must be 403/404")
    return p


def _scope_ok(node: Any, ents: dict[str, Any]) -> bool:
    if not isinstance(node, dict):
        return False
    if "field" in node:
        return set(node) == {"field"}
    return set(node) == {"fk", "parent_entity", "parent_scope"} and node["parent_entity"] in ents and _scope_ok(node["parent_scope"], ents)
