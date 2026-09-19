# Material status polling transaction boundary

The ERP capability facade resolves its enabled binding and configuration from
the polling session. These reads may open a new SQLAlchemy transaction after
candidate discovery. Linked-material polling now releases that clean read
immediately after the provider returns, including empty responses, before the
existing material observation owner executes its complete write transaction.
Pending ORM mutations remain an error: the lifecycle adapter never commits them
as a supposed read-only cleanup. Failed records retain the existing rollback and
per-record error reporting. Flow ownership, typed observation commands, ERP
identity validation and idempotent allocation ownership are unchanged.

The regression exercises the real capability facade, binding/context lookup and
material owner with only the external runner transport replaced. It covers
unchanged, terminal and absent provider outcomes plus refusal of a dirty reader.
These are fast unit-lane checks; PostgreSQL acceptance remains CI-owned.
Deployment does not force issues, modify stock or replay ERP writes. Verify
observed/failed counts and last-reconciled freshness after approved deployment.
