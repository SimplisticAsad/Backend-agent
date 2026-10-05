# Integration analysis of the Graph, Database and Frontend repositories

Written **before** the Backend Agent was implemented, from reading the three repositories (Graph-making-agent `841625f`,
Database-Agent `9e6d47e`, Frontend-Agent `ae9883d`). The Backend Agent is built around the *verified* contracts below; where the
repositories contradict each other nothing is silently worked around — each case is listed with the decision taken.

Format: **CONFLICT · SOURCE A · SOURCE B · IMPACT · RECOMMENDED RESOLUTION** (and, for the backend, **DECISION**).

## A. Conflicts between the repositories themselves

### A1. Graph file format: Graph Agent vs Frontend Agent
- **SOURCE A** (Graph Agent): `graph_manifest.json` has `graphs: {name: file}` and `checksums`; each file is `{schema_version, project_id, <list>}`
  (`api.json` → `endpoints`, `schemas`; `backend.json` → `services`, `operations`; ids like `api.project.read`, `permission.project.create`).
- **SOURCE B** (Frontend Agent): `projects/task_manager/graphs/*` is labelled "hand-authored fixture": manifest has `files: {…}`, every file is
  `{graph, nodes: [...]}`, there is no `backend.json`, API nodes carry `auth/permission/errors`, ids differ (`api.project.get`, `permission.project.view`),
  enum values are upper case, entities use `name` where the Graph Agent uses `full_name`.
- **IMPACT** The Frontend repository's graphs cannot be used as the backend's source of truth, and the Frontend Agent has never been run on real Graph Agent output.
- **RECOMMENDED RESOLUTION** The Frontend Agent should load real Graph Agent packages (or the Graph Agent should export the Frontend's view).
- **DECISION** The backend reads the *Graph Agent* format as the product truth. The Frontend graphs are read only as a **client contract**
  (`app/contracts/frontend.py`), matched to graph endpoints by `(method, normalised path)` rather than by id.

### A2. Database Agent input vs Graph Agent output
- **SOURCE A** (Database Agent): its only input is `requirements/entities.json` = `{project, entities:[{name, fields:[{name,type,required,unique,references}]}]}` with types
  `integer|string|text|decimal|boolean|datetime|date|uuid`. Its README still says the *Frontend* Agent produces that file.
- **SOURCE B** (Graph Agent): `entities.json` = conceptual attributes with ids, types `uuid|string|text|integer|decimal|boolean|date|datetime|enum|json|email|url`, `enum_values`, `reference_to`.
- **IMPACT** There is no graph→database adapter; the Database Agent cannot yet build the database for a graph project, and its `generated/` directory is empty in the repository. enum/email/url/json have no DB-agent type.
- **RECOMMENDED RESOLUTION** Add a graph→`entities.json` adapter to the Database Agent (or a shared library); map enum→text+CHECK, email/url→text.
- **DECISION** The backend does **not** design a database. It consumes the Database Agent's *artifact layout* (`architecture.json`, `schema.sql`, `crud.sql`,
  `database_state.json`) and verifies it against the graph (`DATABASE_CONTRACT_CONFLICT`). For the example projects the committed contract under
  `projects/*/database/` is produced by `scripts/make_database_fixtures.py`, a clearly labelled **stand-in** that follows the Database Agent's documented conventions
  and whose SQL is executed on PostgreSQL before it is snapshotted. It is test infrastructure, not a competing design.

### A3. Database Agent CRUD functions vs what the API needs
- **SOURCE A** (Database Agent): `update_<x>` takes **every** writable column (full-row replace); `list_<x>s()` has no filter, search or pagination; ids in its example are integer identity.
- **SOURCE B** (Graph/API): `PATCH` partial updates, list filters/search, uuid ids.
- **IMPACT** Generated CRUD functions cannot implement the API as specified.
- **DECISION** Repositories use parameterised SQL on the tables (identifiers come from the verified contract, values are bound). The CRUD functions are inventoried
  (`entity_mapping.json`, `repository_manifest.json`) and executed by the repository tests, but not used. `id` must be a single-column primary key (checked).

### A4. Graph Agent's `downstream_contract.backend`
- **SOURCE A**: lists `entities, relationships, requirements, capabilities, workflows, backend, api, permissions, validations, state_machines`.
- **SOURCE B** (this task): also needs `project, actors, roles, dependencies, acceptance_criteria, assumptions, questions`.
- **DECISION** The backend loads the union and validates references across all of them. `roles` (needed for role inheritance and the user→role mapping) is missing from the Graph Agent's list.

## B. Conflicts between the Frontend contract and the Graph (reported in `integration_conflicts.json`, task_manager)

| Code | Frontend (SOURCE A) | Graph (SOURCE B) | Impact | Backend decision / recommended resolution |
|---|---|---|---|---|
| `SESSION_RESPONSE_NOT_IN_GRAPH` | login returns `{token, user}` | `api.user.login` → `schema.user` (no token) | no client could authenticate | Backend returns **both** shapes (`{token, token_type, expires_in, user}` + top-level user fields). Add a session schema to the graph. |
| `ERROR_ENVELOPE_SHAPE` | client reads top-level `message`, `errors` | task asks for `{error:{code,message,details}}` | generic messages only | Every error carries both shapes plus `request_id`. |
| `ENDPOINT_NOT_IN_GRAPH` | `GET /users` | no such endpoint | assignee pickers would 404 | **Not invented.** Fails cleanly with 404. Add `operation.user.list` to the graph. |
| `FRONTEND_FIELD_NOT_IN_GRAPH` | `POST /tasks` sends `description`, `assignee_id`, `due_date` | `schema.task.create` = `title, project_id` | requests carrying them get 422 | Mass-assignment protection stays on; the 422 names the fields. Fix graph or frontend. |
| `FRONTEND_QUERY_NOT_IN_GRAPH` | `GET /tasks?project_id=` | no filter defined | filter ignored | Define the filter in the graph. |
| `REQUIREDNESS_DIFFERS` | `description` optional on project create | required | omitted → 422 | Align the form with the graph. |
| `ENUM_CASE_DIFFERS` | `TODO / IN_PROGRESS / COMPLETED` | `todo / in_progress / completed` | every status write from the UI → 422 | Graph wins; the backend never guesses a case. |
| `FRONTEND_FIELD_MISSING_IN_RESPONSE` | `user.name`, `project.created_at`, `task.created_at` | `full_name`, (no created_at) | empty cells in the UI | Rename or extend the graph. |
| `TRANSITION_NOT_IN_GRAPH` | `COMPLETED → IN_PROGRESS` ("reopen", manager) | no such transition | UI offers an action the backend refuses (409) | The backend never allows an undeclared transition. |
| `ROLES_DIFFER` / `AUTH_DIFFERS` | compared per endpoint | | none found for task_manager | — |

## C. Gaps inside the Graph itself (reported as `GRAPH_GAP`)

| Code | Finding | Decision |
|---|---|---|
| `NO_USER_PROVISIONING` | no operation creates a user, yet login exists | `python -m backend_app.manage create-user` ships with every backend |
| `RULE_FIELD_NEVER_SETTABLE` | `validation.task.completed_needs_assignee` needs `assignee_id`, but no operation can set it | rule is enforced; the gap is reported |
| `AGGREGATE_WITHOUT_RESULT_SCHEMA` | `progress` / `stats` return the plain entity list | `progress` adds **additive** count fields (documented); `stats` is **not implemented** (501 + known-gap test) |
| `NO_RESET_TOKEN_STORE` | password reset exists, no table for tokens/sessions | stateless signed reset tokens bound to the password hash; in-process logout revocation list |

## D. Decisions forced by the task text

- **API versioning.** The task suggests `/api/v1` when the graph defines none. The Frontend dev proxy strips `/api` and forwards the *graph's* paths, so a version
  prefix would break the real client. Decision: serve graph paths as-is; recorded in `backend_api_contract.json` and the README.
- **Artifact names.** The task names the implemented contract both `backend_api_contract.json` and `api_contract.json`; both are written (identical).
- **Python.** The task asks for 3.12+; the environment runs 3.11. The code uses no 3.12-only syntax and declares `requires-python >=3.11`.
- **Pagination.** The Frontend client expects bare arrays and sends no paging parameters. Lists therefore stay bare arrays; `limit`/`offset`/`sort`/`order` are *additive*
  query parameters and the total count is returned in `X-Total-Count`.
