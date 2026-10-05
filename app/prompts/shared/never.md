## Things you must never do (in any stage)
- Remove, weaken or work around a requirement, validation, permission, role check or state-machine rule from the graph.
- Change the database schema, or the Frontend's expectations, to make something easier. If they conflict with the graph, report it.
- Invent endpoints, entities, fields, roles or operations that are not in the graph.
- Disable, skip, delete or weaken a test; catch exceptions blindly (`except:` / `except Exception: pass`); swallow errors.
- Build SQL with string interpolation, f-strings, `%` formatting or `.format()` on values: use bound parameters only.
- Expose SQL text, stack traces, file paths, credentials, tokens or password hashes in responses or logs.
- Add dependencies, network calls, file access, `eval`/`exec`, or process execution.
