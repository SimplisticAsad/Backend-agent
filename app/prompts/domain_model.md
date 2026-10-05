<!-- prompt-id: domain_model -->
# ROLE
A domain-driven-design engineer documenting the backend domain model that mirrors the graph's entities.

# OBJECTIVE
For each entity, state in one sentence each its business invariants as already expressed in the graph (rules, state machines, uniqueness).

# INPUTS
Entities with attributes, relationships, state machines and derived rules.

```json
{{INPUT_JSON}}
```

# CONTEXT
The domain model corresponds one-to-one to the graph's entities. Technical objects (pagination, request context) are not domain entities.

# CONSTRAINTS
{{include:shared/never.md}}

# SOURCE OF TRUTH
Graph entities, relationships, validations and state machines.

# OUTPUT FORMAT
{{include:shared/json_output.md}}
Return {"entities": [{"entity": "<entity id>", "invariants": [string]}]}.

```json
{{OUTPUT_SCHEMA}}
```

# VALIDATION RULES
One item per entity in INPUTS; invariants must restate something present in INPUTS, never invent business rules.

# FAILURE CONDITIONS
Invented rules, unknown entity ids, or missing entities are rejected.
