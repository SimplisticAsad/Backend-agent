Read-only snapshot of the Frontend Agent's task_manager graphs (Frontend-Agent commit ae9883d, `projects/task_manager/graphs/`).

The Frontend repository is never modified by the Backend Agent. Re-snapshot with:

    cp <Frontend-Agent>/projects/task_manager/graphs/{graph_manifest,api,entities,roles,permissions,state_machines}.json projects/task_manager/frontend/graphs/

Note the Frontend fixture uses its own graph format (`nodes`, manifest `files`) and is labelled "hand-authored fixture":
it is not Graph-Agent output. The Backend Agent reports the differences as integration conflicts.
