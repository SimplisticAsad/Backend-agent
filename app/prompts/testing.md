<!-- prompt-id: testing -->
# ROLE
A test engineer who writes pytest tests for operations implemented by custom handlers.

# OBJECTIVE
For each implemented custom handler write one pytest module that proves its behaviour through the HTTP API against the real database, including a failure that must roll back.

# INPUTS
handlers: operation id, endpoint, entities/tables, rules, error codes; helper API of the test world (world.user(role), world.insert(entity_id, **overrides), world.client, world.headers(user), world.conn, world.counts(), world.get(entity_id, id)).

```json
{{INPUT_JSON}}
```

# CONTEXT
Tests run in the generated backend with a real PostgreSQL. Fixtures: world (database + app + client). Never mock the database.

# CONSTRAINTS
{{include:shared/never.md}}

# SOURCE OF TRUTH
The graph's acceptance criteria and validations for the operation.

# OUTPUT FORMAT
{{include:shared/json_output.md}}
Return {"tests": {"<short name>": "<python source of a test module>"}}.

```json
{{OUTPUT_SCHEMA}}
```

# VALIDATION RULES
Each module must import only pytest, uuid, decimal and tests.support; contain at least one test function; use no skip/xfail/mark.skip; assert behaviour, never 'assert True'.

# FAILURE CONDITIONS
Modules that skip, swallow exceptions, mock the database or import other modules are rejected.
