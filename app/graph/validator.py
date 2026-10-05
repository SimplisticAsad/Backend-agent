"""Backend-focused graph validation: IDs, references, API/operation/permission consistency.

An invalid package STOPS the pipeline; no backend code is generated from it. Semantic *conflicts* between sources
(graph vs database vs frontend) are not decided here; see app/integration/conflicts.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.errors import ErrorKind, GraphError, Issue
from app.graph.model import ID_RE, GraphPackage

_FRONTEND_OWNED = {"screen", "component"}
_PATH_PARAM = re.compile(r"\{(\w+)\}")
_KINDS = {"entity", "operation", "api", "schema", "role", "permission", "validation", "state_machine", "workflow",
          "service", "requirement", "capability", "ac", "assumption", "question", "actor", "relationship", "screen",
          "component", "project", "dependency"}


@dataclass
class GraphValidation:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise GraphError(self.errors)


def validate_graph(pkg: GraphPackage) -> GraphValidation:
    v = GraphValidation()
    ref = lambda code, msg, loc=None, refs=(): v.errors.append(Issue(ErrorKind.GRAPH_REFERENCE_ERROR, code, msg, loc, tuple(refs)))  # noqa: E731
    err = lambda code, msg, loc=None, refs=(): v.errors.append(Issue(ErrorKind.GRAPH_ERROR, code, msg, loc, tuple(refs)))  # noqa: E731
    warn = lambda code, msg, loc=None: v.warnings.append(Issue(ErrorKind.GRAPH_ERROR, code, msg, loc))  # noqa: E731

    # ---- ids: shape, kind prefix, uniqueness ----------------------------------------------------
    seen: dict[str, str] = {}
    for g in pkg.docs:
        if g in ("project",):
            continue
        objs = pkg.docs[g].get("services", []) + pkg.docs[g].get("operations", []) if g == "backend" else pkg.items(g)
        if g == "api":
            objs = list(objs) + pkg.docs["api"].get("schemas", [])
        for o in objs:
            i = o.get("id") if isinstance(o, dict) else None
            if not isinstance(i, str) or not ID_RE.match(i):
                err("invalid_id", f"{g}: malformed or missing id {i!r}", f"{g}.json")
                continue
            if i.split(".", 1)[0] not in _KINDS:
                err("unknown_id_kind", f"{g}: unknown id kind in {i!r}", f"{g}.json", [i])
            if i in seen:
                err("duplicate_id", f"duplicate id {i!r} (in {seen[i]} and {g})", f"{g}.json", [i])
            seen[i] = g
    if v.errors:
        return v  # reference checks are meaningless on broken ids

    ents, ops, schemas, eps = pkg.entities, pkg.operations, pkg.schemas, pkg.endpoints

    # ---- entities / attributes / relationships --------------------------------------------------
    for e in ents.values():
        names = set()
        for a in e.get("attributes", []):
            if a["name"] in names:
                err("duplicate_attribute", f"{e['id']} has duplicate attribute '{a['name']}'", "entities.json", [e["id"]])
            names.add(a["name"])
            if a["id"] != f"{e['id']}.{a['name']}":
                err("attribute_id_mismatch", f"attribute id {a['id']!r} does not match {e['id']}.{a['name']}", "entities.json", [a["id"]])
            rt = a.get("reference_to")
            if rt and rt not in ents:
                ref("unknown_entity_ref", f"{a['id']} references unknown entity {rt}", "entities.json", [a["id"], rt])
            if a.get("type") == "enum" and not a.get("enum_values"):
                err("enum_without_values", f"{a['id']} is an enum with no enum_values", "entities.json", [a["id"]])
        if "id" not in names:
            err("entity_without_id", f"{e['id']} has no 'id' attribute", "entities.json", [e["id"]])
    for r in pkg.relationships.values():
        for end in (r["source"], r["target"]):
            if end not in ents:
                ref("unknown_entity_ref", f"{r['id']} references unknown entity {end}", "relationships.json", [r["id"], end])

    # ---- backend services / operations ----------------------------------------------------------
    for s in pkg.services.values():
        for er in s.get("entity_refs", []):
            if er not in ents:
                ref("unknown_entity_ref", f"{s['id']} references unknown entity {er}", "backend.json", [s["id"], er])
    for o in ops.values():
        if o.get("entity_ref") and o["entity_ref"] not in ents:
            ref("unknown_entity_ref", f"{o['id']} references unknown entity {o['entity_ref']}", "backend.json", [o["id"], o["entity_ref"]])
        if o.get("service_ref") and o["service_ref"] not in pkg.services:
            ref("unknown_service_ref", f"{o['id']} references unknown service {o['service_ref']}", "backend.json", [o["id"]])
        for r in o.get("required_roles", []):
            if r not in pkg.roles:
                ref("unknown_role_ref", f"{o['id']} requires unknown role {r}", "backend.json", [o["id"], r])
        out = (o.get("output") or {}).get("entity_ref")
        if out and out not in ents:
            ref("unknown_entity_ref", f"{o['id']} output references unknown entity {out}", "backend.json", [o["id"], out])
        if o.get("access") == "restricted" and not o.get("required_roles"):
            err("restricted_without_roles", f"{o['id']} is 'restricted' but lists no required_roles", "backend.json", [o["id"]])
        ent = ents.get(o.get("entity_ref") or "")
        if ent:
            attrs = {a["name"] for a in ent["attributes"]}
            for f in (o["input"].get("required_fields", []) + o["input"].get("optional_fields", [])):
                if f != "id" and f not in attrs:
                    err("operation_field_missing", f"{o['id']} input field '{f}' is not an attribute of {ent['id']}", "backend.json", [o["id"]])

    # ---- api -------------------------------------------------------------------------------------
    for s in schemas.values():
        if s.get("entity_ref") and s["entity_ref"] not in ents:
            ref("unknown_entity_ref", f"{s['id']} references unknown entity {s['entity_ref']}", "api.json", [s["id"]])
    routes: dict[tuple[str, str], str] = {}
    for ep in eps.values():
        op = ops.get(ep["operation_ref"])
        if op is None:
            ref("unknown_operation_ref", f"{ep['id']} references unknown operation {ep['operation_ref']}", "api.json", [ep["id"], ep["operation_ref"]])
            continue
        for k in ("request_schema_ref", "response_schema_ref"):
            if ep.get(k) and ep[k] not in schemas:
                ref("unknown_schema_ref", f"{ep['id']} {k} references unknown schema {ep[k]}", "api.json", [ep["id"], ep[k]])
        for r in ep["authorization"].get("roles", []):
            if r not in pkg.roles:
                ref("unknown_role_ref", f"{ep['id']} authorizes unknown role {r}", "api.json", [ep["id"], r])
        norm = _PATH_PARAM.sub("{}", ep["path"])
        key = (ep["method"], norm)
        if key in routes:
            err("duplicate_route", f"{ep['id']} and {routes[key]} both define {ep['method']} {ep['path']}", "api.json", [ep["id"], routes[key]])
        routes[key] = ep["id"]
        for p in _PATH_PARAM.findall(ep["path"]):
            if p not in (set(op["input"].get("required_fields", [])) | set(op["input"].get("optional_fields", []))):
                err("path_param_not_in_operation", f"{ep['id']}: path parameter '{p}' is not an input of {op['id']}", "api.json", [ep["id"], op["id"]])
        if ep["method"] in ("GET", "DELETE") and ep.get("request_schema_ref"):
            warn("body_on_bodyless_method", f"{ep['id']}: {ep['method']} endpoint declares a request body schema")
    for o in ops.values():
        if o.get("exposed") and o["id"] not in pkg.endpoint_by_operation:
            err("exposed_operation_without_endpoint", f"{o['id']} is exposed but no endpoint implements it", "api.json", [o["id"]])

    # ---- permissions ----------------------------------------------------------------------------
    for p in pkg.permissions.values():
        if p["resource"] not in ents:
            ref("unknown_entity_ref", f"{p['id']} resource {p['resource']} is not an entity", "permissions.json", [p["id"]])
        for r in p.get("roles", []):
            if r not in pkg.roles:
                ref("unknown_role_ref", f"{p['id']} references unknown role {r}", "permissions.json", [p["id"], r])
        for o in p.get("operation_refs", []):
            if o not in ops:
                ref("unknown_operation_ref", f"{p['id']} references unknown operation {o}", "permissions.json", [p["id"], o])
    for r in pkg.roles.values():
        for inh in r.get("inherits", []):
            if inh not in pkg.roles:
                ref("unknown_role_ref", f"{r['id']} inherits unknown role {inh}", "roles.json", [r["id"], inh])
    for a in pkg.actors.values():
        for rr in a.get("role_refs", []) + ([a["role_ref"]] if a.get("role_ref") else []):
            if rr not in pkg.roles:
                ref("unknown_role_ref", f"{a['id']} references unknown role {rr}", "actors.json", [a["id"], rr])

    # ---- validations / state machines -----------------------------------------------------------
    for val in pkg.validations.values():
        t = val.get("target") or {}
        if t.get("entity_ref") not in ents:
            ref("unknown_entity_ref", f"{val['id']} targets unknown entity {t.get('entity_ref')}", "validations.json", [val["id"]])
        elif t.get("field") and pkg.attribute(t["entity_ref"], t["field"]) is None:
            err("validation_field_missing", f"{val['id']} targets unknown field {t['field']}", "validations.json", [val["id"]])
        for o in val.get("operation_refs", []):
            if o not in ops:
                ref("unknown_operation_ref", f"{val['id']} references unknown operation {o}", "validations.json", [val["id"], o])
    for sm in pkg.state_machines.values():
        if sm["entity_ref"] not in ents:
            ref("unknown_entity_ref", f"{sm['id']} references unknown entity {sm['entity_ref']}", "state_machines.json", [sm["id"]])
            continue
        attr = pkg.attribute(sm["entity_ref"], sm["field"])
        if attr is None:
            err("state_field_missing", f"{sm['id']} field '{sm['field']}' not on {sm['entity_ref']}", "state_machines.json", [sm["id"]])
        elif attr.get("type") == "enum" and set(sm["states"]) != set(attr["enum_values"]):
            err("state_enum_mismatch", f"{sm['id']} states {sm['states']} differ from enum {attr['enum_values']}", "state_machines.json", [sm["id"]])
        if sm["initial_state"] not in sm["states"]:
            err("initial_state_unknown", f"{sm['id']} initial_state not in states", "state_machines.json", [sm["id"]])
        for t in sm["transitions"]:
            if t["from"] not in sm["states"] or t["to"] not in sm["states"]:
                err("transition_state_unknown", f"{sm['id']} transition {t['from']}->{t['to']} uses an unknown state", "state_machines.json", [sm["id"]])
            if t.get("operation_ref") and t["operation_ref"] not in ops:
                ref("unknown_operation_ref", f"{sm['id']} references unknown operation {t['operation_ref']}", "state_machines.json", [sm["id"]])
            for r in t.get("role_refs", []):
                if r not in pkg.roles:
                    ref("unknown_role_ref", f"{sm['id']} references unknown role {r}", "state_machines.json", [sm["id"], r])

    # ---- workflows / acceptance criteria / dependencies -----------------------------------------
    for w in pkg.workflows.values():
        for step in w.get("steps", []):
            if step.get("operation_ref") and step["operation_ref"] not in ops:
                ref("unknown_operation_ref", f"{w['id']} step {step['id']} references unknown operation {step['operation_ref']}", "workflows.json", [w["id"]])
    for ac in pkg.acceptance_criteria.values():
        for k, table in (("operation_refs", ops), ("workflow_refs", pkg.workflows)):
            for r in ac.get(k, []):
                if r not in table:
                    ref("unknown_ref", f"{ac['id']} {k}: unknown {r}", "acceptance_criteria.json", [ac["id"], r])
    ids = pkg.all_ids
    for edge in pkg.items("dependencies"):
        for end in (edge["source"], edge["target"]):
            if end.split(".", 1)[0] in _FRONTEND_OWNED:
                continue  # screens/components are frontend graphs the backend does not load
            if end not in ids:
                ref("unknown_dependency_ref", f"dependency {edge['id']} references unknown id {end}", "dependencies.json", [edge["id"], end])
    return v
