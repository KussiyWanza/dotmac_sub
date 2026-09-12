# Work-Order Expense Entry

## Decision

Authenticated staff who can open an exact admin work-order detail page may
create an expense claim from that page after a technician has been assigned.
Technician assignment is an expense-eligibility requirement, not an identity or
access rule: global dispatch-read access, or a matching reseller or region
scoped grant, permits both viewing the page and creating a claim. Field/mobile
expense entry retains its assigned-technician scope.

Every claim is bound to an active authoritative work order by the expense owner.
The admin browser supplies no work-order identifier: the route is authoritative.
Field/mobile/API callers supply only the typed public ID, which the owner
resolves under assignment/access rules. Internal database UUIDs, placeholders,
generic drafts, and standalone expense creation are not accepted.

The Field app never asks a technician to type that public ID. Entry from an
exact job carries the job identity into a locked field; entry from the Expenses
tab uses a searchable choice backed by the technician's typed assigned-job
projection, including its offline cache. The expense owner still revalidates
the selected work order when the request is submitted.

## Owners and boundaries

- `ui.work_order_expense_projection` owns the typed form, field errors,
  requester-owned claim list, category help, action eligibility, and honest ERP
  delivery labels.
- `operations.expense_categories` owns the live ERP category observation,
  including receipt requirements and per-claim maximums. Unavailable and empty
  are distinct and both disable submission.
- `operations.expense_requests` owns validation and the atomic submitted claim,
  line items, requester evidence, the selected approver link, the masked
  per-expense payment-destination snapshot, idempotency fingerprint, receipt
  metadata, work-order assignment eligibility, activity mark, and the
  manager-approval ERP release. It rejects submission when the work order is
  inactive, unauthorized, unknown, or has no assigned technician. Submission
  creates no ERP event.
- The admin route parses HTTP form and file values, enforces CSRF and the exact
  work-order read RBAC scope, releases its read transaction, and invokes
  the typed owner command. It does not commit or call ERP.
- Sub is authoritative for the Field manager's approval or rejection decision.
  The submitter selects one eligible approver from ERP; Sub maps that employee
  to one active SystemUser by normalized email and only that user may approve
  or reject. ERP accepts that trusted, evidenced decision without constructing
  a second approval chain. ERP remains authoritative for approver eligibility,
  its bank directory, account-name verification, reimbursement bank details,
  payment intent state, transfer execution, reconciliation, and the final paid
  fact. Cost-centre, ERP task, fleet vehicle, and receipt-number controls remain
  out of scope.

The form offers the technician's masked ERP bank profile by default. A
technician may instead enter a beneficiary name, ERP bank, and account number
for this expense only. Sub sends those values directly to ERP account
verification and receives an encrypted, short-lived token bound to the
requester, organization, and claim UUID. Sub persists and delivers only that
opaque token, bank label, account last four digits, verified beneficiary, and
verification timestamps; it never stores the raw account number. ERP encrypts
the verified account snapshot on the claim. Approval locks the snapshot, and
payment rejects any attempt to replace it. The override never updates the
employee's ERP profile.

Receipt bytes use the existing private attachment storage owner. Metadata is
staged flush-only inside the expense command transaction. A deterministic
per-line receipt client reference makes a repeated claim submission safe. The
existing general field receipt endpoint remains assigned-technician scoped.
Staff uploader identity is recorded on `FieldAttachment`; the legacy
subscriber-only `StoredFile.uploaded_by` field remains empty for these staff
uploads.

The reusable disclosure control is a semantic button with `aria-controls` and
synchronized `aria-expanded`. Activation opens the existing `details`, scrolls
the form into view, and focuses Purpose. Native `summary` mouse and keyboard
behavior remains available; permission-denied users receive a disabled button
and no submit surface.

## State and delivery semantics

The work-order card shows only claims created by the authenticated staff
identity. Local submission, durable delivery pending, delivered but awaiting
ERP acceptance, ERP accepted, rejected, failed/dead, approved, and paid facts
remain distinct. `sent` outbox evidence is not presented as ERP acceptance;
only an accepted event or ERP claim reference qualifies.
An unapproved local claim is labelled **Awaiting manager approval**, because no
ERP delivery is expected yet. A canceled or locally rejected claim is labelled
**Not sent to ERP**. **ERP delivery evidence is unavailable** is reserved for an
approved or paid claim that unexpectedly has neither an ERP reference nor a
durable release event.

Rejected and dead release events persist only the allowlisted integration
diagnostic code, HTTP status, and ERP request identifier alongside any existing
partial-delivery progress. Provider response text and validation input are not
persisted or displayed. Web and Field API projections render their explanation
from that typed diagnostic evidence; malformed or legacy evidence falls back to
a generic failure message.

Manager approval is the sole ERP release point. Its single durable event creates
or retrieves an idempotent ERP draft, maps stable Sub line IDs to ERP items,
streams each private receipt from storage, uploads only missing attachments, and
delivers the trusted approval after all uploads succeed. Receipt bytes/base64
never enter the database outbox; supported URL receipts remain claim-line data.
Payment waits for the accepted approval-release event.
Managers with the exact `operations:expense_request:pay` permission may stage a
payment command for an approved expense. The Field app never calls Paystack or
marks the claim paid. ERP creates and initiates the transfer, and Sub projects
`queued`, `pending`, `processing`, `indeterminate`, `failed`, `completed`, and
the resulting `paid` claim fact from ERP responses and polling.

Verification requires a live connection and expires after 30 minutes. Mobile
may save the ordinary non-sensitive draft, but it must not persist raw account
details, the destination token, or queue a verified submission for later
offline replay. An identical retry of an already-created request remains
idempotent after token expiry.

The field app's expense list follows the same requester-owned rule. Ownership
is resolved from any exact technician-profile, canonical Person Party, or
authenticated SystemUser link on the claim. Work-order completion or
reassignment does not remove the claim from the requester's history, and a
claim created by another staff identity is not exposed. Reading history does
not require a current active technician profile: the resolver starts from the
authenticated SystemUser and includes its canonical Person Party plus every
exact historically linked technician profile. Submission retains its separate
active-profile and assigned-work-order checks. List responses report the full
filtered count before pagination.

For a manager who is also a technician, the Field Expenses destination exposes
both `My requests` and `Approvals`. Approvals remain the default operational
view, while the requester-owned tab uses the same history resolver as every
other field technician; manager capability never hides personal history.
The manager approval list uses a typed owner query and labels every card
`Raised by` with the requester display identity resolved by
`auth.staff_provisioning`. A historical row without exact SystemUser identity
is resolved only when its persisted technician or Person link yields one exact
SystemUser; otherwise it is labelled unavailable. The client never infers a
requester from the current work-order assignment. The manager navigation does
not expose the Materials destination.

## Schema change

Revision `594_field_expense_destination` adds nullable selected-approver,
masked destination, opaque token, verification, expiry, and lock evidence so
existing claims remain readable during rollout. `requested_by_technician_id` on expense requests and
`uploaded_by_technician_id` on field attachments become nullable. System-user
and person identity remain mandatory for new web submissions. The downgrade
fails closed while any staff-created rows without technician links exist.

## Validation and recovery

The server requires an eligible selected approver, a valid ERP destination
token, purpose, claim date, a three-letter currency, and one to 50
positive-amount lines. Each line requires an active ERP category and a
description of at most 500 characters. A receipt URL and receipt upload are
individually optional alternatives; when the selected ERP category requires
receipt evidence, either one satisfies that rule. The browser never marks the
file input itself as required. It changes the shared Receipt marker and help
text when the category changes, then validates the URL-or-file choice as one
requirement. Category receipt and maximum rules are enforced again by the
command owner. Browser calculations and required markers are assistance only.

Private attachments must be active, owned by the same authoritative work order,
present in storage, within the receipt size limit, and have a supported MIME,
consistent size, and matching stored checksum. Receipt URLs must use a supported
HTTP(S) shape and cannot contain credentials, fragments, localhost, or literal
private-network addresses. Approval revalidates current category and receipt
evidence before staging delivery.

Non-sensitive text values and the stable claim client reference survive
validation errors.
Browsers cannot repopulate file inputs, so a selected receipt is cleared and an
explicit field error asks the user to reselect it. ERP category or sync
unavailability never fabricates a usable fallback. The account number is also
cleared and must be re-entered after an error.

A dead approval-release event may be recovered only through a typed preview and
explicit recovery command. The owner revalidates approval, work order, category,
receipt, and ERP state, refuses ambiguity or a stale preview, preserves the
original event, and appends linked replacement evidence using a versioned key.
No automatic historical backfill or replay exists; legacy pre-approval events
are left untouched and refused by the worker.

Alembic revision `587_field_request_requester_history` repairs older claims
whose durable SystemUser link can be proven from their technician profile,
legacy SystemUser-as-person identifier, or unique Person Party binding. It also
adds requester lookup indexes. Ambiguous claims remain unchanged and hidden;
the repair never changes approval, delivery, or payment state and never queues
an ERP claim. `operations.expense_requests` owns this bounded, idempotent
repair, with Alembic acting only as its deployment adapter.

## Non-goals

This slice does not add a second persistence model, update the employee's ERP
bank profile, expose full stored bank credentials, automatically retry indeterminate
transfers, historical claim backfill, or production cutover changes.
