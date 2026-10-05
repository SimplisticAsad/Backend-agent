# Backend Agent

The **Backend Agent** is the third implementation stage of an autonomous software-engineering platform:

```
User ─► Graph-Making Agent ─► project graphs ─┬─► Database Agent ─► PostgreSQL contract ─┐
                                              ├─► Backend Agent  ◄───────────────────────┘   (this repository)
                                              └─► Frontend Agent ─► client contract ──────────► checked against the backend
```

It takes a **validated Graph-Agent package** plus the **Database Agent's contract** (and, when available, the **Frontend Agent's client contract**) and produces a
**tested FastAPI + PostgreSQL backend**: domain model, repositories, application services, routes, request/response schemas, authentication, role *and*
object-level authorization, state machines, consistent errors, OpenAPI, structured logging, health checks — together with its own test suites, which it **executes against a
real PostgreSQL**, with bounded autonomous correction of implementation defects.

> The Backend Agent is **not** a product designer. The graph says *what*; the backend implements *how*. When the graph, the database and the frontend disagree, it
> **reports a structured conflict** instead of silently choosing a side (`artifacts/integration_conflicts.json`).

```bash
python -m app.main inspect          --project projects/task_manager
python -m app.main generate         --project projects/task_manager --mock
python -m app.main validate         --project projects/task_manager
python -m app.main test             --project projects/task_manager --mock
python -m app.main integration-test --project projects/task_manager --mock
```

Result of `integration-test` on the three committed example projects (real PostgreSQL 16, mock LLM, no network):

| Project | Status | Generated-suite tests passed | Notes |
|---|---|---|---|
| `task_manager` (with Frontend contract) | `passed_with_conflicts` | 432 (incl. 36 frontend-compatibility) | 12 major Graph↔Frontend conflicts, 2 major Graph gaps, 3 minor/shimmed — all documented |
| `ecommerce_store` | `passed_with_conflicts` | 245 | checkout workflow (transaction, row locking, rollback, concurrent-oversell test) implemented by a validated handler |
| `support_ticketing_system` | `passed_with_conflicts` | 239 (+1 known gap) | `ticket.stats` is a **known gap** (501): the graph gives it no usable result schema |

`passed_with_conflicts` means: *every executed test passed, and the integration conflicts listed in the report remain open*. It never means "everything is fine".

---

## Contents
1. [What it does](#1-what-the-backend-agent-does) · 2. [Architecture](#2-architecture) · 3. [Repository integration](#3-repository-integration) · 4. [Graph Agent](#4-graph-agent-integration) ·
5. [Database Agent](#5-database-agent-integration) · 6. [Frontend Agent](#6-frontend-agent-integration) · 7. [API generation](#7-api-generation) · 8. [Domain architecture](#8-domain-architecture) ·
9. [Database access](#9-database-access) · 10. [Authentication](#10-authentication) · 11. [Authorization](#11-authorization) · 12. [Testing](#12-testing) · 13. [Integration testing](#13-integration-testing) ·
14. [Correction loop](#14-correction-loop) · 15. [Conflict detection](#15-conflict-detection) · 16. [CLI](#16-cli) · 17. [Configuration](#17-configuration) · 18. [Security](#18-security) ·
19. [Artifacts](#19-artifacts) · 20. [Traceability](#20-traceability) · 21. [Incremental regeneration](#21-incremental-regeneration) · 22. [Known limitations](#22-known-limitations) · 23. [Future architecture](#23-future-architecture)

## Quick start

```bash
pip install -r requirements.txt          # fastapi, pydantic v2, psycopg 3 (+pool), pyjwt, httpx, pytest  (Python >= 3.11)
python -m app.main inspect --project projects/task_manager
python -m app.main integration-test --project projects/task_manager --mock     # needs PostgreSQL binaries OR TEST_DATABASE_URL
pytest                                                                          # the agent's own suite (offline; DB tests start a throw-away cluster)
```

`integration-test` needs a **dedicated** PostgreSQL. Either export `TEST_DATABASE_URL=postgresql://user:pw@localhost:5432/<name containing "test">`
(see [`docker-compose.yml`](docker-compose.yml)), or let the agent start a throw-away local cluster from the PostgreSQL binaries on the machine. No Docker daemon is required.

---

## 1. What the Backend Agent does

Controlled pipeline owned by Python (`app/pipeline/orchestrator.py`); the LLM only works inside bounded, validated stages:

```
LOAD PROJECT → INSPECT GRAPH → VALIDATE GRAPH (stop if invalid) → DATABASE CONTRACT → FRONTEND CONTRACT
→ CONFLICT DETECTION (stop on critical) → ANALYSIS → RULES (+LLM, additive) → HANDLERS (LLM, AST-checked) → ARCHITECTURE/SPEC
→ CODE GENERATION → TEST GENERATION → STATIC VALIDATION → START DATABASE → RUN SUITES → CORRECTION LOOP → FINAL VALIDATION
```

Every stage is logged (`stage, status, duration, errors, correction attempts, artifact paths`) to `artifacts/run_logs/stages.jsonl` with secrets scrubbed.

## 2. Architecture

The repository separates **agent infrastructure**, **generated code** and **manual configuration** (task item 63):

```
app/                                   AGENT (this repo)
  main.py                              CLI
  graph/        loader, model, validator   graph_manifest + 17 graphs, checksums, IDs and reference validation
  contracts/    database.py, frontend.py   what the Database / Frontend agents actually built / expect
  integration/  conflicts.py               GRAPH_API / DATABASE_CONTRACT / FRONTEND_API conflicts and GRAPH gaps
  analysis/     classifier, rules, spec, typesys, analysis   deterministic: operations → behaviours, free text → rule DSL → BackendSpec
  llm/          base, mock, http_providers, client, prompt_loader, mock_handlers
  prompts/      14 prompt files + shared fragments           every LLM call renders an explicit file
  generation/   generator.py, test_generator.py, handler_checks.py, runtime/ (static backend runtime), test_templates/
  pipeline/     orchestrator, llm_stages, patch, correction, failures, testrun, static_validation, artifacts
  validation/   report.py                  backend_validation_report.json is computed from executed results
  testing/      pg_cluster.py             dedicated test PostgreSQL (TEST_DATABASE_URL or throw-away cluster)
projects/<name>/                       INPUT + OUTPUT per project
  graphs/ validation/                  Graph Agent package (input)
  database/                            Database Agent contract (input; fixtures for the examples)
  frontend/                            Frontend Agent contract snapshot (optional input)
  backend/                             GENERATED backend (own pyproject, tests, openapi.json, db_contract/)
  artifacts/                           analysis, conflicts, manifests, validation report, corrections
```

The **generated backend** (`projects/<name>/backend/backend_app`) follows clean architecture:

```
api/ (routes, schemas, deps)  →  application/services (one method per graph operation)  →  engine + rules (business logic)
                              →  infrastructure/repositories (parameterised SQL)  →  PostgreSQL (psycopg_pool)
domain/ (entities, exceptions)  auth.py  security.py  errors.py  middleware.py  logging_setup.py  config.py  db.py  manage.py
```

**Design decision — spec-driven engine + generated adapters.** What varies between products is *data* (entities, operations, roles, rules, state machines), so the
resolved `BackendSpec` (`backend_app/generated/spec.json`) is interpreted by a small, heavily tested engine; what must be readable and traceable per product is generated as
real modules (domain entities, request/response models, repositories, services, routes, OpenAPI operation ids). This makes generation byte-deterministic and keeps the security-critical
logic in one reviewed place. Operations the engine cannot express (workflows, aggregates) are implemented by **LLM-written handler bodies** that pass a strict AST policy
and are verified by LLM-written, AST-checked tests that run against PostgreSQL.

## 3. Repository integration

Before any code was written the three repositories were inspected; the findings are in **[`docs/INTEGRATION_ANALYSIS.md`](docs/INTEGRATION_ANALYSIS.md)**
(*CONFLICT · SOURCE A · SOURCE B · IMPACT · RECOMMENDED RESOLUTION*). The most important facts:

- The Frontend repository's fixture graphs use a **different format** from real Graph-Agent output; the Database Agent consumes yet **another** `entities.json` format and has no graph adapter.
- The Database Agent's generated CRUD functions (`update_*` is full-row, `list_*` has no filters) cannot implement PATCH/filter semantics → repositories use parameterised SQL on the verified tables.
- 12 major Graph↔Frontend conflicts exist for `task_manager` (login without token, enum case, undefined fields/endpoints, ...) plus 1 shimmed error-shape difference, and 4 gaps inside the graph (no user creation, assignee never settable, aggregate without schema, no reset-token store).

**The Frontend and Database repositories are never modified.** The backend only *reads* them and reports.

## 4. Graph Agent integration

`app/graph/loader.py` reads `graph_manifest.json`, then the 17 graphs the backend needs (the union of the manifest's `downstream_contract.backend` and the graphs the task lists), verifying
manifest SHA-256 **checksums**, `project_id` consistency and — if present — the Graph Agent's own `validation_report.json` (`status: valid`, `safe_for_downstream`).
`app/graph/validator.py` then checks ids, entity/operation/API/permission/workflow/state-machine/acceptance/dependency references. **An invalid package stops the pipeline**;
nothing is generated. Graph text is only ever passed to the LLM as JSON *data* (see [Security](#18-security)).

## 5. Database Agent integration

`app/contracts/database.py` reads the Database Agent's `generated/` layout (`architecture.json`, `schema.sql`, `crud.sql`, `database_state.json`, optional `crud_functions.json`) and can
introspect a live schema. `integration/conflicts.py` verifies the contract against the graph: missing table/column (critical), type-family mismatch (critical, e.g. graph `uuid` vs DB `integer`),
NOT NULL without default for something the graph never supplies (critical), missing UNIQUE/FK (major), enum not CHECKed (info). Generated repository tests then **execute** the contract's `schema.sql` and
`crud.sql` on PostgreSQL and compare the live catalog with `architecture.json` — the SQL is never assumed to work.
The graph→table/column mapping is `artifacts/entity_mapping.json`.

## 6. Frontend Agent integration

`app/contracts/frontend.py` reads the Frontend Agent's graphs plus the behaviours of its **generated API client** (`client.ts`): Bearer auth, bare-array lists, `204` handling, error bodies read as
top-level `{message, errors}`, `Session = {token, user}`, dev proxy stripping `/api`, dev origin `http://localhost:5173`. `verify_client_expectations` re-checks these against a Frontend checkout.
The generated `tests/frontend_compat/` suite calls the backend **the way that client does** (path/query/body construction, `compact()`, session fields, roles) and asserts shapes, statuses and error readability.
Known conflicts are not hidden: where the frontend sends something the graph does not define, the suite asserts the **documented** behaviour (a clear 422 naming the fields) so it cannot drift silently.
Backend-side compatibility shims are explicit and recorded: login session envelope, dual error envelope, bare-array lists.

## 7. API generation

`api.json` is the contract. For every endpoint the backend generates method, path, request model (Pydantic v2, `extra="forbid"`, length/format/enum limits), response model, authentication,
role check, operation → service → repository mapping and documented error responses. `operationId` **is** the graph endpoint id (`api.task.update_status`). Paths are served exactly as in the
graph (no `/api/v1`: the Frontend proxy strips `/api` and forwards graph paths; see the integration analysis). Routes are ordered most-specific-first (`/tasks/assigned` before `/tasks/{id}`).
Lists are bare arrays with additive `limit`/`offset`/`sort`/`order` (+ `query` where the operation defines search, + the operation's declared filters) and an `X-Total-Count` header; sort/filter fields are allow-listed.
Status codes: `201` create, `204` for operations without a result, `200` otherwise. `GET /health` and `GET /ready` (database reachable) are the only non-graph endpoints. In production the interactive `/docs` UI is disabled; `openapi.json` stays available.
`openapi.json` is exported and compared with the graph; the implemented contract is `artifacts/backend_api_contract.json` (`api_contract.json`).

Error contract (every non-2xx):
```json
{ "error": {"code": "TASK_NOT_FOUND", "message": "The task was not found.", "details": {}},
  "message": "The task was not found.", "errors": {"field": "message"}, "request_id": "…" }
```
`400` malformed · `401` unauthenticated · `403` forbidden · `404` not found (also for rows outside the caller's scope) · `409` conflict / invalid state transition · `413` oversized · `422` validation ·
`429` rate limited · `500` unexpected (generic) · `501` specified-but-not-implemented · `503` database unavailable. No SQL, stack trace, path or secret ever reaches a response.

## 8. Domain architecture

Backend domain entities mirror graph entities one-to-one (`entity.task` → `Task` → table `tasks`), with enums as `str, Enum`, hidden columns (`password_hash`) excluded from serialisation, and the
**state machines** (`todo → in_progress → completed`) enforced centrally: undeclared transitions are `409 INVALID_STATE_TRANSITION` (with the allowed targets), role-restricted transitions are
`403 TRANSITION_NOT_PERMITTED`. Business rules from `validations.json` / `permissions.json` are turned into a closed **rule DSL** (`ownership`, `row_scope`, `transition_guard`, `frozen_state`) by
deterministic patterns; conditions the patterns cannot map go to the LLM `business_rules` stage, whose output is **additive-only** (it can never redefine or weaken a baseline rule) and validated against the graph.
Anything still unmapped is listed in `backend_analysis.json` and the validation report — never dropped silently. Where the graph is silent about ownership (carts, orders, tickets), a documented
**secure-default heuristic** makes rows private to the user the backend assigns on create (and inherits that privacy for children); each inference is recorded as an assumption.

## 9. Database access

Direct, parameterised PostgreSQL access through `psycopg` 3 and a **connection pool** (`psycopg_pool`): bounded size, checkout timeout, `statement_timeout`/`lock_timeout`/`idle_in_transaction` limits,
`search_path` pinned to the Database Agent's schema, clean open/close with the application lifespan, readiness probe. Route handlers contain no SQL. Transactions are explicit
(`with db.transaction() as conn:` → COMMIT/ROLLBACK), with `SELECT … FOR UPDATE` before read-then-write (e.g. checkout locks cart rows then products in a stable order to avoid deadlocks).
Driver errors are mapped centrally (`23505`→409, `23503`→422/409, `23502/23514`→422, serialization/deadlock→409, timeout/outage/pool exhaustion→503). Identifiers come only from the verified contract
and allow-lists; values are always bound; a static scan (and the handler AST policy) rejects string-built SQL.

## 10. Authentication

Derived from the graph: the entity holding `email + password_hash + role` is the credential entity; `login/logout/request_password_reset/reset_password` are implemented as specified.
scrypt password hashing (stdlib), HS256 JWTs with `exp/nbf/iss/aud/jti` and an explicit algorithm list (`alg:none` and foreign keys rejected), role read from the **database** on every request,
tokens bound to the current password (a password change kills every older token), logout revocation list, constant-work login (no account-existence oracle), identical responses for unknown e-mails,
single-use reset tokens, per-client/per-account rate limiting (`429` + `Retry-After`). The graph defines **no operation that creates users**, so `python -m backend_app.manage create-user` ships with every backend
(password from `$USER_PASSWORD` or a prompt, never an argument). E-mail goes through an `EmailService` protocol (an in-memory outbox by default; no vendor coupling).

## 11. Authorization

Enforced **server-side**, in layers: (1) authentication, (2) role per operation from the graph (`role.inherits` honoured), (3) object-level rules — row scopes (`/tasks/assigned`, private carts/orders/tickets, comments inherit their ticket's privacy,
creating a child under someone else's parent behaves like a missing parent), ownership (`403` with the graph's error code, e.g. `TASK_NOT_ASSIGNED_TO_CALLER`, exempt roles such as managers), guards
(`TASK_NO_ASSIGNEE`) and frozen states (closed tickets). Order of checks leaks nothing: *exists → in scope (else 404) → ownership (403) → state machine/guards (409)*. Mass assignment is rejected by `extra="forbid"`;
server-owned fields (`owner_id`, `requester_id`, …) are assigned from the caller and a client-supplied different value is refused.

## 12. Testing

Each generated backend ships its own suite (`projects/<name>/backend/tests`), **spec-driven** so the same tests exercise any project, and runs against a **real PostgreSQL**
(`tests/support/world.py` builds valid rows, callers and requests from the spec; nothing is mocked):

| Suite | What it proves |
|---|---|
| `unit` | state machines (every declared and undeclared transition), rules, JWT/hash primitives (tampering, `alg:none`, audiences), error mapping without leakage, production config safety, decimal serialisation |
| `repository` | CRUD, pagination/sort, unique/FK/NOT NULL enforcement, delete-restrict, rollback, pool lifecycle, **live schema == `architecture.json`**, `crud.sql` executes |
| `api` | for every operation: success per allowed role, invalid body (missing/extra/typed/oversized/malformed), 401 (4 token shapes), 403 per denied role, malformed id, 404, 409, FK errors; filters/search/pagination; full auth flows; custom-workflow tests |
| `contract` | the running app vs the **raw** `api.json`: method, path, `operationId`, request/response fields/types/requiredness, `additionalProperties:false`, security scheme, documented errors, roles/permissions, state machines, no extra endpoints, every validation accounted for |
| `frontend_compat` | the Frontend client's calls, response shapes, error readability, enum/field/endpoint conflicts as documented |
| `bdd` | one Gherkin feature per acceptance criterion (`tests/bdd/features`) executed through the HTTP API; error codes named in criteria must exist; custom workflows must have dedicated scenarios |
| `security` | SQL injection (bodies, query, path, login; `pg_sleep` must not run), forged/expired/`alg:none`/foreign-secret tokens on **every** secured operation, role-claim escalation, mass assignment, IDOR/object-level rules, invalid transitions, guards, malformed/oversized/NUL input, error and secret leakage (responses **and** logs), CORS, request ids |
| `e2e` | the production entry point (`uvicorn backend_app.main:app`, `APP_ENV=production`) started as a separate process against the test database and driven over real HTTP: health/ready, docs disabled, login per role, every list endpoint per role, 401, CORS allow/deny, graceful shutdown |
| `db_failure` | outage (liveness stays up, readiness 503, every operation a safe 503), closed pool, pool exhaustion, rollback of a half-finished transaction, constraint violations |

**The suites have teeth.** `python scripts/mutation_check.py projects/<name>/backend` breaks one protection at a time in a copy of the generated backend (ownership, role check, row scope, state machine, transition guard, frozen state, token role/revocation, unknown-field rejection, sort allow-list, error sanitising, CORS, body limit, ...) and runs the suites.
Verified results: **task_manager 11 killed / 3 survived / 2 not applicable; support_ticketing_system 12 killed / 2 survived / 2 not applicable.** Every survivor was analysed: *login leaks password hash* and *no declared-size body limit* are **equivalent** mutants (a second defence still holds: the response model strips the hash, the streaming byte counter enforces the limit);
*no scope on read* survives in task_manager only because its graph defines no row scope on row-level operations (the same mutant is killed in the ticketing project). Mutants that target a rule type a project does not define are reported "not applicable", not as passes. The Backend Agent's **own** suite (`pytest`: 180 tests — 167 offline, 9 full PostgreSQL pipeline runs incl. the correction-loop scenarios, 4 live database-contract checks) covers the negative cases of the specification
(missing graph → `GRAPH_ERROR`, bad reference → `GRAPH_REFERENCE_ERROR`, missing table → `DATABASE_CONTRACT_ERROR`, missing frontend endpoint → `FRONTEND_CONTRACT_ERROR`, missing permission →
`AUTHORIZATION_ERROR`, invalid transition → `STATE_TRANSITION_ERROR`, database unavailable → `DATABASE_ERROR`, contract mismatch → `API_CONTRACT_ERROR`), determinism, prompts, the LLM client, patch safety and the correction loop.

## 13. Integration testing

`python -m app.main integration-test` runs generation + all suites against a dedicated PostgreSQL. Safety (task items 68–69): destructive tests refuse a database whose name does not contain `test`
or whose host is not local (unless `BACKEND_AGENT_TEST_DB_CONFIRM=<dbname>`); each run creates a private random schema (`bt_<hex>`) and drops it; the throw-away cluster uses trust auth on 127.0.0.1 and is removed on exit.
No PostgreSQL → the command exits `5` and the report says why; `test` instead **skips** the database suites with an explicit reason (status `partial`). The `e2e` suite boots the real production server and drives it over HTTP; a *browser* end-to-end run (Frontend app → Backend → Database) is
**not** performed: see [limitations](#22-known-limitations).

## 14. Correction loop

`app/pipeline/correction.py`: run suites → on failure classify (`IMPORT_ERROR, TYPE_ERROR, RUNTIME_ERROR, DATABASE_ERROR, VALIDATION_ERROR, AUTHORIZATION_ERROR, STATE_TRANSITION_ERROR, API_CONTRACT_ERROR,
FRONTEND_CONTRACT_ERROR, SECURITY_FAILURE, INTEGRATION_FAILURE, TEST_FAILURE`) → pick the specialised prompt (`runtime_correction`, `database_error_correction`, `api_contract_correction`, `security_correction`; security wins)
→ the LLM receives the failures, the relevant source files and the spec context and returns `{diagnosis, edits[]}` → edits are applied only through **guards** (`app/pipeline/patch.py`) → suites re-run.
Bounded: **at most `--max-corrections` (default 3)** correction rounds; it also stops early when no edit is applicable. Corrections **cannot cheat**: only `backend_app/**/*.py` may change (never tests, the DB contract or `spec.json`);
an edit that reduces the count of any protection token (`authorize(`, `check_ownership`, `PermissionDenied`, `extra="forbid"`, `max_length`, `algorithms=`, …), adds bare/blanket `except`, string-built SQL, wildcard CORS, `eval`, process execution or test skipping, or does not
compile, is refused (and rolled back). Verified with real injected defects: API/authorization failure, database error, frontend-contract mismatch (each fixed in one round), a defect that never converges (stops at 3), a "fix" that deletes the ownership check (refused), an attempt to edit a test (refused).
Corrections are logged in `artifacts/corrections.json`. They edit the generated tree; defects in the *agent's templates* must be fixed in the agent (regeneration overwrites generated files).

## 15. Conflict detection

`integration/conflicts.py` → `artifacts/integration_conflicts.json`: `{status: clean | conflicts_reported | blocked, summary, checked, skipped, conflicts[{type, severity, code, description, sources, impact, recommended_resolution, kind, resolution_applied}]}`.
Severity: **critical** → generation is blocked and nothing is generated (graph-internal contradictions such as endpoint/permission/operation role mismatches, command on `GET`, request schema ≠ operation input, a role value without a role, a protected
operation without a permission, an actor that cannot run its workflow, a transition role that cannot call the operation, a database that cannot hold the data); **major** → sources disagree but the graph wins and the backend is internally correct (reported with impact and fix);
**minor/info** → additive or shimmed. `--strict-frontend` promotes major Frontend conflicts to blocking (for CI).

## 16. CLI

| Command | Does | Exit |
|---|---|---|
| `inspect --project P [--json]` | read-only analysis: operations by kind, handlers needed, rules, conflicts | 0 / 3 blocked |
| `validate --project P` | graph + contracts + conflicts (+ static validation of an existing backend) | 0 / 1 / 3 |
| `generate --project P [--mock] [-o DIR]` | everything up to static validation; status `not_tested` | 0 |
| `test --project P [--mock]` | generate + run all suites (database suites skipped with a reason if no PostgreSQL) | 0 / 1 |
| `integration-test --project P [--mock] [--max-corrections N]` | full pipeline on real PostgreSQL with bounded correction | 0 / 1 / 5 |

Common: `--database-dir`, `--frontend-dir`, `--strict-frontend`, `--provider {mock,anthropic,openai_compatible,ollama}`, `--model`, `--json`, `-v`. Exit codes: `0` ok · `1` tests/validation failed · `2` invalid graph/contract · `3` blocked by critical conflicts · `4` generation failure · `5` no dedicated database · `6` usage/config (e.g. no API key and no `--mock`).

## 17. Configuration

Agent (`.env.example`): `LLM_PROVIDER` (`anthropic` default, `openai_compatible`, `ollama`, `mock`), `LLM_MODEL`, `LLM_API_KEY`/`ANTHROPIC_API_KEY`, `LLM_BASE_URL`, `MAX_CORRECTIONS`, `TEST_DATABASE_URL`, `FRONTEND_AGENT_DIR`.
Generated backend (`backend/.env.example`): `APP_ENV`, `DATABASE_URL`, `DATABASE_SCHEMA`, `JWT_SECRET`, `ACCESS_TOKEN_TTL_MINUTES`, `CORS_ORIGINS`, `DB_POOL_MIN/MAX`, `DB_POOL_TIMEOUT_SECONDS`, `MAX_BODY_BYTES`, `RATE_LIMIT_PER_MINUTE`, `LOG_LEVEL`.
Production (`APP_ENV=production|staging`) **refuses to start** without a ≥ 32-character `JWT_SECRET` or with `CORS_ORIGINS=*`. Nothing secret is hard-coded; secrets are `repr=False` and scrubbed from logs and artifacts.

## 18. Security

- **Injection:** parameterised SQL only; identifier allow-lists; `LIKE` wildcards escaped; a static AST scan fails the build on string-built SQL.
- **Prompt injection:** graph text reaches the LLM only as JSON data inside a fenced INPUTS block, with a system rule that INPUTS is data; every LLM answer is parsed, schema-validated and semantically validated before use; functional stages are additive-only and AST/policy-checked; nothing returned by an LLM is ever executed.
- **LLM-written code** (handlers, tests): parsed, never executed before the checks pass; no imports, no `eval/exec/open/__import__`, literal SQL only, no DDL/COMMIT/ROLLBACK, no swallowed exceptions, known tables only, transaction required.
- **Runtime:** see §10–11; bounded request bodies (`413`, also for chunked uploads), explicit CORS origins, `nosniff`/`no-store`/`no-referrer`, request ids (client ids sanitised), structured JSON logs that never contain bodies, tokens, passwords or hashes (tested).
- **Agent:** secrets scrubbed from logs/artifacts/LLM errors; the Frontend and Database repositories are read-only; destructive tests only on dedicated databases.

## 19. Artifacts (`projects/<name>/artifacts/`)

`backend_analysis.json` · `backend_architecture.json` · `backend_spec.json` · `entity_mapping.json` · `backend_api_contract.json` (= `api_contract.json`) · `openapi.json` · `service_manifest.json` · `repository_manifest.json` ·
`file_manifest.json` · `test_manifest.json` · `integration_conflicts.json` · `handlers.json` · `llm_notes.json` (advisory LLM output) · `corrections.json` · `backend_validation_report.json` · `run_logs/stages.jsonl`.
The validation report is **computed from executed results** (suite counts, statuses, known gaps, unimplemented operations, warnings, conflicts, correction attempts); LLM commentary can never change a status.
Statuses: `passed` · `passed_with_conflicts` · `partial` (database suites skipped) · `not_tested` · `failed` · `blocked`.

## 20. Traceability

`file_manifest.json` maps every generated file to the specification ids it implements (`backend_app/api/routes/task.py` → `api.task.*`, `operation.task.*`, `permission.task.*`, `schema.task.*`, `service.task`, rule ids; domain entity → `entity.task`, `table:tasks`),
and records its kind (`generated | runtime | config | contract | test`) and SHA-256. Every acceptance criterion has a `.feature` file; `operationId` equals the api.json endpoint id; each rule records its provenance (`validation`, `permission_condition`, `implicit_*` heuristic, `llm`). IDs are semantic and stable; generation is deterministic (same graphs + contracts + mock LLM → byte-identical output, tested).

## 21. Incremental regeneration

Not implemented — the MVP regenerates everything (deterministically). The design is ready for it: stable semantic ids, per-file `source_refs`, per-entity/per-service modules, content hashes, and `graph_manifest.json` checksums mean a graph diff can be mapped to affected files
(`source_refs ∩ changed ids`) and only those regenerated. Hand-written code must live outside `backend_app/` (the generated package is replaced wholesale).

## 22. Known limitations

- **No browser end-to-end test.** Frontend compatibility is tested by replaying the generated client's HTTP calls (and its error handling rules) against the real backend, and the real server is exercised over HTTP by the `e2e` suite; the React app is not launched.
- **The Database Agent's output for these graphs is a stand-in** (`scripts/make_database_fixtures.py`), because no graph→database adapter exists and the Database Agent needs an LLM. The contract loader/verification is real; a real Database Agent run should produce the same artifact layout.
- **Mock-LLM breadth.** The mock implements two handler patterns (cart→order checkout, parent→children progress) from structure, not names; other custom operations are reported `not_implemented` (501 + known-gap test) until a real model implements them — the real-model path (prompts, validation, retries) is covered by unit tests with HTTP transports but was not exercised against a live model here.
- **Heuristics are declared, not magic:** free-text→rule patterns and the implicit-ownership defaults are documented, recorded as assumptions, and conservative; unrecognised conditions are surfaced as *unmapped* rather than guessed.
- **In-process state:** logout revocation list and rate limiter are per process (multi-instance deployments need a shared store — the graph defines none). Password-reset tokens are stateless. The login limiter keys on the TCP peer address (`X-Forwarded-For` is deliberately not trusted), so behind a reverse proxy configure `RATE_LIMIT_PER_MINUTE` accordingly.
- **External integrations:** `project.json`'s `integration_requirements` are surfaced in `backend_analysis.json`, but the only adapter generated is the `EmailService` protocol (password reset); payments/storage/third-party APIs need an adapter written against a protocol like it. `payment_method` in checkout is validated but not processed.
- Filters on numeric/date fields are **equality** (the graph's `price` filter is ambiguous: reported, not guessed). Decimals are JSON numbers (the Frontend types them as `number`).
- The throw-away PostgreSQL cluster is removed on normal exit and on Ctrl-C; after `SIGTERM`/`SIGKILL` of a test run a leftover `/tmp/bagent-pg-*` directory/process may remain (stop it with `pg_ctl -D <dir>/data stop` and delete the directory).
- Corrections edit generated files; they are not replayed by `generate`.
- Python 3.11 compatible (target 3.12+); `requires-python >=3.11`.

## 23. Future architecture

Incremental regeneration from graph diffs; a shared Graph→Database adapter and a joint "platform contract test" that runs Graph → Database → Backend → Frontend in one pipeline with a browser E2E; pluggable stores for revocation/rate-limit/reset tokens once the graph models them;
asynchronous drivers behind the same repository protocol; more handler patterns (aggregates with a proper result schema, payments behind an adapter); and feeding the validation report back to the Graph Agent as structured change requests for the conflicts it lists.
