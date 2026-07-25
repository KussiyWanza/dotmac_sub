# Projects source-of-truth completion

`operations.project_lifecycle` exclusively owns native `Project` and
`ProjectTask` identity, lifecycle, transitions, assignment, scheduling,
relationships, audit evidence, transactional events, and derived-state repair.
Public commands run once through `execute_owner_command` on a transaction-free
adapter session. They lock the project before tasks and use flush-only nested
helpers. Adapters map typed domain errors and never commit business state.

`operations.project_assignment_policy` owns rule matching and candidate
selection. Its output is advisory typed decision evidence. Only the project
lifecycle owner may apply manager, assistant-manager, service-team, primary task
assignee, or task-assignee collection changes. The shared ticket assignment
engine cannot write those fields.

`operations.work_order_commands` remains the writer of WorkOrder bindings. The
project owner validates the native Project-to-ProjectTask side; neither owner
may infer a relationship from a CRM identifier. CRM and other external
identifiers are provenance attributes only and are never decision or join keys.
Vendor installation-project lifecycle and workspace owners retain their
existing, non-overlapping authority.

`ui.project_list_projection` owns the complete typed list query: visibility
scope, search fields, filters, status vocabulary, stable sorting, pagination,
freshness, and action eligibility. Routes serialize inputs and templates render
the returned model without redefining these rules.

The project and task detail pages compose field work through
`operations.work_orders`. A project detail shows every native work order whose
authoritative `project_id` matches the project. A task detail shows zero or many
work orders whose authoritative `project_task_id` matches the task and offers a
task-originated create deep-link only when the project has a native subscriber.
The dispatch web adapter resolves the task deep-link back to the authoritative
task, project, and subscriber rows, locks those identifiers into the create
form, and delegates creation to `operations.work_order_commands`; URL-supplied
duplicate scope is never trusted. The command owner revalidates subscriber,
project, and task consistency at execution time and fails closed when the
project has no subscriber.

The project-task list bulk-loads active linked-work-order summaries for every
task on the current page through one `operations.work_orders` query. Its typed
projection renders Create Work Order for zero visits, Open Work Order for one
visit, and View N Work Orders for multiple visits. Dispatch-read permission
controls summaries and links; dispatch-write permission controls creation.
Project-task write permission alone grants neither capability. Detail pages use
the same read boundary and never reveal linked work-order identity without
dispatch-read permission.

The dispatch `project_task_id` query parameter is an authoritative native UUID
filter as well as an optional creation-prefill input. Filtering remains active
for read-only dispatch users and composes with search, status, lifecycle,
sorting, and pagination. Invalid identifiers fail closed and never fall back to
CRM identifiers. Dispatch KPI cards intentionally remain global queue context;
their labels, values, and cohort links all describe the same global cohorts and
therefore clear the task filter when opened.

Task-originated prefill carries the authoritative subscriber, project, and task
identifiers and labels, plus task title, description, safely mapped priority,
and Sub's explicit `install` work-type default. Operational fields remain
editable. Work-order creation and technician assignment are separate decisions:
creation never derives a technician from task assignees, and the existing
assignment/queue owner remains the next visible dispatch action.

Page contract: service-delivery and field-operations staff use the project/task
detail screens to determine whether field work has been issued and to open or
issue the next visit. `app.services.web_projects` owns the detail projection;
`operations.work_orders` owns linked work-order facts; and
`operations.work_order_commands` owns creation eligibility enforcement and the
write. The task panel is a secondary work surface, preserves one-to-many visits,
hides unauthorized creation, explains missing subscriber scope, renders an
empty state, and stacks without losing the task identity or next action on
mobile.

Project SLA clocks and normalized task-assignee rows are synchronous derived
projections. Drift is a missing, duplicate, or mismatched derived row. The
project lifecycle reconciler locks the native aggregate, reports drift, and
idempotently rebuilds the projection. Unknown or stale projection state fails
closed for mutation eligibility.

State-changing commands stage audit and versioned domain-event evidence in the
same transaction as authoritative state. Events are delivered after commit by
the durable dispatcher. Retryable database concurrency failures retry the whole
command; validation, authorization, stale evidence, relationship ambiguity, and
idempotency conflicts do not.
