# Expense claim ERP cutover

## Authority and release point

- Sub owns the technician's expense request and the Field manager's approval or
  rejection decision.
- ERP owns the accounting claim, payment intent, transfer execution,
  reconciliation, and final paid fact.
- Submission and `expense_submit_v3` commit atomically. The worker creates or
  retrieves a hidden ERP draft, uploads every required receipt, invokes explicit
  submit, and requires ERP status `SUBMITTED` before exposing the claim.
- Manager approval or rejection and its separate `expense_approve_v3` or
  `expense_reject_v3` event commit atomically. Each decision event waits for
  accepted submission without consuming retry attempts.
- Sub remains authoritative for the manager decision. ERP remains authoritative
  for accounting, payment, reconciliation, and the final paid fact.

## Deployment prerequisites

1. Apply the existing `field_erp_sync_events` and `sync_flow_ownership` migration
   chain through repository head.
2. Enable the typed ERP outbox-delivery and expense-status capabilities.
3. Enable `erp.expense.form_context.v1` only after both ERP and Sub revisions
   supporting the approver/destination contract are deployed. Explicitly
   review and adopt the immutable `dotmac.erp` 1.4.0 manifest pin before
   enabling its new capability; deployment does not auto-adopt the pin.
4. Verify the ERP service identity has `sub:expense:write` for draft creation,
   private receipt upload,
   status, and Field manager decisions. Grant the separate exact
   `sub:expense:pay` scope only to the Sub integration identity that may request
   a transfer; do not grant broader human or finance-administration permissions.
5. Confirm the ERP accepts stable Sub claim and line IDs, receipt keys derived
   from contract version, expense, line, and attachment, and the one-UUID v3
   keys: `exp-{request_id}-submitted-v3`,
   `exp-{request_id}-approved-{decision_id}-v3`, and
   `exp-{request_id}-rejected-{decision_id}-v3`.
   For every new token-bearing canary, confirm the destination-verification
   `source_claim_id`, submitted `client_ref`, `FieldExpenseRequest.id`, draft and
   receipt `source_claim_id`, approval `source_claim_id`, and status-poll key are
   the same UUID. A mismatch must block manager approval before any ERP event is
   staged. Retain `release_approved_v2` behavior for already-staged legacy events.
6. Confirm the previous expense sender is disabled before changing ownership.
7. Verify every technician email and intended approver email has one exact
   active match across Sub and ERP. Verify the requester is absent from their
   own approver choices and at least one other eligible ERP approver remains,
   an active ERP bank directory, and each technician's intended default bank
   profile. An incomplete profile is allowed only when the technician uses a
   verified one-expense override.
8. Verify account resolution succeeds without creating a transfer. Confirm the
   response contains only a masked account and an opaque claim-bound token, and
   that Sub logs, drafts, tables, and outbox rows contain no raw account number.

## Controlled activation

1. Record and retain the pre-cutover legacy owner for
   `sync_flow_ownership.expense_claim` while deploying and validating the
   application change.
2. Before ownership cutover, verify submission fails closed without creating a
   request or an event.
3. Assign `expense_claim` ownership to `sub` through the reviewed production
   configuration procedure.
4. Submit one newly created canary expense and verify exactly one submission
   event is accepted, all required receipts are attached, and ERP reports
   `SUBMITTED`. Confirm no ERP `DRAFT` is visible to normal ERP users.
5. With the selected approver, who must be different from the requester, approve
   the canary in the Field app and verify the separate approval event is
   accepted and the same ERP claim becomes `APPROVED`, not `PENDING_APPROVAL`.
   Confirm the ERP claim names the selected
   approver and contains the expected masked destination. Do not inspect or
   report the full account number.
6. With a dedicated payment-authorized manager, select **Pay expense** and verify
   one `work-order-expense-payment.v1` event is staged, then delivered through
   the typed `initiate_expense_payment` capability. Confirm ERP creates one
   payment intent and reports `PROCESSING` (or `COMPLETED` for an immediate
   success). A queued Sub event is waiting for ERP and is not proof that ERP
   created an intent.
7. Exercise the webhook or polling path and verify `COMPLETED` changes the ERP
   claim and both Field views to `PAID`. Exercise an indeterminate sandbox result
   and verify no automatic duplicate transfer is attempted.

## No historical backfill

Do not enqueue or replay previously submitted, approved, or rejected expenses during this
cutover. Existing records are intentionally excluded. This branch
contains no migration, startup hook, scheduled scan, or repair command that
backfills them. Any future historical repair requires a separate reviewed scope.

This prohibition concerns ERP delivery. Revision
`587_field_request_requester_history` separately repairs exact local requester
identity links so staff can see claims they raised; it does not approve, enqueue,
or replay an expense claim. Before rollout, record the count of active expenses
with a null `requested_by_system_user_id`. After migration, investigate every
remaining row rather than inferring an owner. Verify a repaired claim appears
for its requester through `GET /api/v1/field/expense-requests` and that it
remains invisible to another requester.

## Dead-event recovery

Recovery is operator-initiated and always starts with
`GET /api/v1/field/manager/expenses/deliveries/{event_id}/recovery-preview`.
The preview revalidates the approved expense, active work order, current ERP
category rules, private attachment content, and ERP claim state. If the
evidence is unambiguous, submit its fingerprint to the matching `recover`
endpoint. Recovery preserves the dead event and appends a linked replacement
using `expense-delivery-recovery.v1`. Never recover without a fresh preview,
and never use this interface for a historical bulk backfill.

Legacy pre-approval `submit`, `approve`, or `reject` events are intentionally
left untouched and refused by the worker. They are not eligible for this
recovery command.

## Rollback

Restore the recorded pre-cutover legacy ownership of `expense_claim` to stop new
Sub deliveries, then disable the expense delivery/status capabilities. Do not
delete outbox evidence.
Investigate any pending event by its event ID before deciding whether to resume
delivery. Production configuration and ownership changes are operational work and
are not performed by this branch.
