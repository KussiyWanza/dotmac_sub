# Expense claim ERP cutover

## Authority and release point

- Sub owns the technician's expense request and the Field manager's approval or
  rejection decision.
- ERP owns the accounting claim, payment intent, transfer execution,
  reconciliation, and final paid fact.
- Submission is local only and creates no ERP outbox event.
- Manager approval is the sole release point. Approval and one versioned outbox
  event commit atomically; rejection remains local.
- The worker creates or retrieves an ERP draft, uploads every private receipt,
  and only then delivers the trusted approval. Payment initiation remains a
  separately authorized, ordered event.

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
5. Confirm the ERP accepts stable Sub claim and line IDs, the
   `exp-{request_id}-approved-release-v2` draft key, and receipt keys derived
   from contract version, expense, line, and attachment.
   For every new token-bearing canary, confirm the destination-verification
   `source_claim_id`, submitted `client_ref`, `FieldExpenseRequest.id`, draft and
   receipt `source_claim_id`, approval `source_claim_id`, and status-poll key are
   the same UUID. A mismatch must block manager approval before any ERP event is
   staged.
6. Confirm the previous expense sender is disabled before changing ownership.
7. Verify every technician email and intended approver email has one exact
   active match across Sub and ERP. Verify at least one eligible ERP approver,
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
2. Before ownership cutover, verify a submitted expense creates no outbox row.
3. Assign `expense_claim` ownership to `sub` through the reviewed production
   configuration procedure.
4. Submit one newly created canary expense and verify it is work-order-bound and
   creates no ERP outbox event.
5. Approve the canary in the Field app and verify exactly one release event is
   accepted, all required receipts are attached, and the ERP claim becomes
   `APPROVED`, not `PENDING_APPROVAL`. Confirm the ERP claim names the selected
   approver and contains the expected masked destination. Do not inspect or
   report the full account number.
6. With a dedicated payment-authorized manager, select **Pay expense** and verify
   one payment event is staged. Confirm ERP creates one payment intent and reports
   `PROCESSING` (or `COMPLETED` for an immediate success).
7. Exercise the webhook or polling path and verify `COMPLETED` changes the ERP
   claim and both Field views to `PAID`. Exercise an indeterminate sandbox result
   and verify no automatic duplicate transfer is attempted.

## No historical backfill

Do not enqueue or replay previously submitted or approved expenses during this
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
