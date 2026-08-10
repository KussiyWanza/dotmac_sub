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

Creating a Project with a linked Subscriber requests one customer email through
the communication-intent owner. The email identifies the Project, its reference,
and initial status, uses an idempotent Project key, and requests no other channel.
Projects without a linked Subscriber do not request an email. Queue failure is
isolated in the approved owner savepoint and recorded as durable Project audit
evidence without rolling back Project creation.

Every genuine Project or ProjectTask status transition for a subscriber-linked
Project requests one customer communication intent from the lifecycle owner.
The typed consequence carries the native aggregate identifiers, exact previous
and new status enums, Subscriber identity, and lifecycle command identifier.
Ordinary transitions default to email and identify both statuses. Completion
transitions retain their existing milestone-specific messages and suppress the
ordinary message, so one transition never produces both notification shapes.
Non-status edits and Projects without a Subscriber request no status message.
The lifecycle command identifier scopes deduplication so a retry cannot create a
duplicate while a later legitimate transition over the same status pair remains
deliverable. Queue failure rolls back only the optional participant savepoint;
the transition remains authoritative and the owner records durable
`customer_status_notification_failed` audit evidence.

When a Project enters `completed`, `operations.project_lifecycle` may also queue
one internal finance email per resolved recipient. The projects-domain
`project_completion_finance_email_enabled` setting gates the automation. The
`project_completion_finance_email_recipients` list may name explicit recipients;
otherwise the owner resolves active staff through the configured
`project_completion_finance_permission_key` permission, defaulting to
`finance:ap:read`. The consequence records `project_completed_finance`
Notification rows with per-project/per-recipient dedupe keys and never embeds a
hardcoded email address. It runs inside the same optional completion consequence
savepoint as customer completion messaging, so failure cannot roll back the
authoritative Project transition.

When an existing task gains an assignee through the lifecycle update command,
the owner queues one in-app notification and one email for each newly added
active staff member whose
assignment identifier resolves to either their `SystemUser` or canonical Person
identity. Staff who were already assigned are not emailed again. Removing an
assignee, changing any non-assignment task field, or assigning an unresolved or
inactive identity does not queue these notifications. This consequence is part
of the same owner transaction. Queue failures are
isolated in the approved owner savepoint and recorded as durable project-task
audit evidence after rollback, so the reassignment itself remains valid.

Project-level assignment and task-assignment consequences now use the shared
staff audience resolver. Direct users and active members of an assigned Service
Team receive an in-app notification and, when an email address exists, a queued
email. Comment mentions use the same individual and Service Team group
semantics across projects and tasks. The retired Site Project Coordinator
column remains readable on historical records but is absent from new-project
input and is no longer populated by regional or assignment rules.

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

The dispatch work-order list exposes an explicit primary create action and keeps
the creation form collapsed until requested. Each list row and each ticket,
project, or project-task work-order projection links to the canonical work-order
detail route by `public_id`. The detail projection composes the subscriber,
ticket, project, project task, schedule, and current assignment evidence through
read owners. It places technician selection in a distinct dispatch panel after
creation; neither the route nor template merges assignment into the create
command or writes relationship fields directly.

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

Project numbers use the shared locked `project_number` document sequence with
the projects-domain policy defaults `PROJ-`, four-digit padding, and start value
one. Before reserving a number, every native creation path locks that sequence
and advances it beyond the highest canonical `PROJ-<digits>` value already in
the project aggregate. This makes preserved imports authoritative inputs to the
counter and prevents a stale local sequence from restarting the series. The
476 cutover repairs the native `4` through `7` drift as `PROJ-1104` through
`PROJ-1107` and advances, but never rewinds, the sequence to at least 1108.
The 496 follow-up repairs numeric `8` through `10` rows created during that
cutover window. It locks numbering and project creation, assigns those rows in
numeric order after both the highest canonical suffix and any value already
reserved by the sequence when the migration runs, and then advances the
sequence without rewinding it.

State-changing commands stage audit and versioned domain-event evidence in the
same transaction as authoritative state. Events are delivered after commit by
the durable dispatcher. Retryable database concurrency failures retry the whole
command; validation, authorization, stale evidence, relationship ambiguity, and
idempotency conflicts do not.

ProjectTask relationship integrity is enforced only by
`operations.project_lifecycle`. Task mutations first observe the native task
scope, lock the authoritative Project, then lock and revalidate the task. An
archived Project or task fails closed. Parent and dependency targets must be
active, distinct tasks in the same native Project; parent chains and dependency
graphs cannot contain cycles. Moving a task between Projects is not a generic
edit and requires a separately designed transfer command.

Status transitions use a typed command containing the expected status,
requested status, actor context, and business reason. Stale expected status is
rejected. Generic task update rejects status writes, and admin/API status
adapters delegate to the typed transition command. A task cannot transition to
`done` while any authoritative dependency
is inactive or not itself `done`. Dependency replacement is atomic, replaces
the full reviewed set, records audit evidence, and emits
`project_task.dependencies_replaced` in the same owner transaction.

Project and task comment creation now participates in the same typed Project
owner boundary as attachment staging, audit evidence, and explicit-mention
notification staging. Task assignment and explicit project/task comment
mentions may stage one deduplicated `nextcloud_talk` row per mapped staff user.
The feature defaults disabled, and no Nextcloud HTTP occurs before the Project
transaction commits; delivery, room reuse, stale-room repair, and retry policy
belong to `communications.nextcloud_talk_staff`.
