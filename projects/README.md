# Example projects

Each directory is one product. Inputs are committed; outputs (`backend/`, `artifacts/`) are what the agent generated from them.

| Project | Graphs | Database contract | Frontend contract |
|---|---|---|---|
| `task_manager` | Graph Agent output (identical to `Graph-making-agent/examples/projects/task_manager`) | stand-in, see below | snapshot of Frontend-Agent `projects/task_manager/graphs` |
| `ecommerce_store` | `python -m app.main generate --input tests/fixtures/ecommerce_detailed.txt --mock` in the Graph Agent | stand-in | – |
| `support_ticketing_system` | same, `tests/fixtures/ticketing.txt` | stand-in | – |

* `database/` is produced by `scripts/make_database_fixtures.py` (a labelled stand-in for the Database Agent: the real one needs an LLM and its own `entities.json` format;
  see `docs/INTEGRATION_ANALYSIS.md` §A2). The SQL is executed on PostgreSQL before it is snapshotted.
* `frontend/` is a read-only snapshot (see `frontend/SOURCE.md`); the Frontend repository is never modified.
* Regenerate everything: `for p in task_manager ecommerce_store support_ticketing_system; do python -m app.main integration-test --project projects/$p --mock; done`
