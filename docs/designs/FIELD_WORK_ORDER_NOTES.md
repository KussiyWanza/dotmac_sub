# Field Work-Order Notes

Status: implementation contract

Owner: `operations.field_notes`

## User outcome

A technician can add an internal or external note to an assigned work order and
can tell whether that exact note was delivered, remains queued, or was rejected.
A refresh or navigation round-trip must not hide queued or failed note evidence.

## Authority and boundaries

- `operations.field_notes` owns native note creation, authorization, attachment
  linking, idempotency, and the committed creation output.
- `operations.work_orders` owns the active work order and assignment facts.
- `auth.permission_gate` owns the authenticated `SystemUser` evidence.
- The field API maps typed domain errors to HTTP and owns no write transaction.
- The mobile outbox is a durable delivery projection, not authority for a
  server-side note.

The public command accepts a typed `CreateFieldWorkOrderNote` and returns a
typed `FieldNoteCreationOutcome`. It enters `execute_owner_command` once on a
transaction-free session. The note, attachment links, fingerprint, and durable
owner output commit atomically.

## Idempotency and concurrency

The mobile app creates one UUID `client_ref` before enqueueing a note and sends
that same value on every retry. Idempotency is scoped to the authenticated
`SystemUser`. The normalized fingerprint contains the work-order public ID,
trimmed body, visibility, and sorted attachment IDs.

The command locks the active actor row before checking the key and then locks
the assigned work order before creating the note. The database has a unique
partial index on `(author_system_user_id, client_ref)`. An identical retry
returns the original note; changed material input with the same key fails with
`operations.field_notes.idempotency_conflict`.

Older clients may omit `client_ref`; the API creates one for rollout
compatibility. That compatibility path is not retry-idempotent and is retired
after supported mobile clients send their own stable reference.

## Mobile delivery states

- `delivered`: the API accepted the note and the server projection can replace
  the local row.
- `queued`: offline, network, rate-limit, or retryable server failure left the
  durable outbox row pending. The UI says **Queued for sync**.
- `failed`: a permanent rejection or exhausted retry budget parked the outbox
  row for review. The UI says **Sync failed** and shows its safe error.

Repository refreshes merge non-sent note outbox rows with server notes and
deduplicate them by `client_ref`. A queued or failed note therefore stays
visible instead of being overwritten by a server refresh.

## Staff web projections

Successfully delivered notes remain authoritative `FieldWorkOrderNote` rows.
The staff work-order page reads those rows by the work order's native public
identity. A project-task page reads them only through
`WorkOrder.project_task_id`; a ticket page reads them only through
`WorkOrder.origin_ticket_id`. The latter remains the sole native ticket-to-work
relationship, so a task's ticket link never becomes an inferred fallback.

These are read-time projections, not copied `ProjectTaskComment` or
`TicketComment` rows. Existing notes therefore appear without a backfill, and
the task/ticket comment owners retain their own editing, mention, notification,
and customer-publication semantics. Related-context entries identify their
originating work order and cannot be edited through task or ticket comment
commands.

Only staff adapters with exact work-order read access may render the projection
or stream an active note attachment. Internal and external-history labels are
always explicit. Neither label publishes a note to a customer portal. Queued or
failed mobile outbox entries are device-local and cannot appear in staff web
views until the API accepts them.

## Migration and release order

Revision `591_field_note_delivery_idempotency` additively adds nullable
`client_ref` and its partial unique index. Existing rows need no backfill.
Deploy the backend migration and API before releasing the mobile build that
sends note references. The concurrent PostgreSQL index build uses a five-second
lock timeout and a fifteen-minute statement timeout. A failed index build is
safe to retry; downgrade removes only the index and new nullable column.

Acceptance requires the predecessor-to-head PostgreSQL rehearsal, command
replay/conflict tests, mobile queued/failed projection tests, and normal CI.
