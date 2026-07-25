# Invoice Batch and Reminder Safe Actions

Status: implemented

## Owners

- `financial.billing_automation` owns postpaid invoice-cycle execution,
  durable `BillingRun` lifecycle, per-subscription idempotency, invoice-owner
  delegation, run recovery, and run audit projection.
- `financial.prepaid_service_renewals` independently owns funded prepaid
  renewal. A manual invoice batch does not invoke that owner.
- `app.services.web_billing_invoice_batch` owns the staff review projection:
  exact billable subscription membership, currency-grouped base-charge impact,
  deterministic preview fingerprint, failed-run retry eligibility, and the
  shared confirmation form.
- `app.services.web_billing_invoice_bulk` remains the existing invoice bulk
  command/eligibility owner. `ui.invoice_bulk_action_projection` projects its
  review form and exact eligible/skipped membership.
- Jinja templates and admin routes are adapters only.

## Manual invoice batch contract

1. The operator selects a billing cycle and effective billing date.
2. Billing automation runs a side-effect-free dry resolution. It returns every
   billable postpaid subscription, affected account and invoice grouping,
   period, currency and base charge.
3. The review fingerprint binds the normalized cycle/date, optional failed
   source run, exact subscription membership, periods, amounts, currencies and
   eligible/skipped totals.
4. Confirmation requires an authenticated staff principal and the shared
   explicit confirmation control.
5. Current owner facts are recomputed immediately before launch. Drift fails
   closed and requires another review.
6. The authoritative execution creates a durable `BillingRun` with
   `launch_kind`, `requested_by`, `preview_fingerprint`, and optional
   `source_run_id`.

Manual postpaid invoice generation and prepaid renewal are deliberately
separate. The former UI implied that one button only generated invoices while
the underlying orchestration also attempted funded prepaid renewals.

## Billing-run state and retry

`BillingRun.status` is the operational state machine:

- `running`: the durable run record exists and work is in progress.
- `success`: invoice processing completed.
- `failed`: execution stopped and carries failure evidence.

The abandoned-run reconciler moves stale `running` records to `failed`.
Only a failed run can be reviewed for retry. A retry creates a new run rather
than mutating or replaying the old row, and persists `source_run_id` so the
lineage is explicit. Invoice-line idempotency prevents duplicate billing for
the same subscription and period.

Billing execution is a durable, resumable owner-managed workflow rather than a
single database transaction. Invoice documents may already exist if a later
step fails; retry relies on canonical invoice-line keys and re-resolution.
`BillingRun` is the authoritative operational record. `AuditEvent` is a
rebuildable audit projection and is intentionally written after the run status
so audit-schema failure cannot falsely mark already-created invoices as failed.

## Invoice bulk and AR reminder review

Invoice-list issue/send/mark-paid/PDF actions and AR-aging reminder sends now
submit exact selected IDs to a server-rendered review page. The existing invoice
bulk owner returns eligible and skipped membership plus its scope token. The
shared action form transports count/token evidence and explicit confirmation;
execution rechecks membership and eligibility.

AR aging no longer posts directly to the confirmed send endpoint. Read-only
operators do not receive reminder controls.

## Retired paths

- Browser `window.confirm`, `onsubmit=confirm`, and Alpine confirmation policy.
- Direct invoice batch execution without a current preview fingerprint.
- Retry controls for successful or running batches.
- Old batch retry endpoint and JSON-only batch-preview contract.
- Direct issue/send/mark-paid/PDF endpoints bypassing the shared review page.
- Manual invoice batches implicitly invoking prepaid renewal.
- The unused `BillingRunSchedule` table, shadow
  `billing.billing_run_schedule_config`, save route and admin form. No
  scheduler consumed either value; `scheduler.registry` remains the sole
  cadence and enablement owner. Migration
  `419_billing_run_launch_evidence` performs the contract cutover.

## Verification

- Exact batch membership and fingerprint drift tests.
- Explicit-confirmation and actor tests.
- Failed-only retry and durable retry-lineage tests.
- Dry-run impact and side-effect guards.
- Invoice bulk exact-scope review tests.
- AR-aging permission/review template tests.
- Shared-form, no-browser-confirmation, SOT and architecture guards.
