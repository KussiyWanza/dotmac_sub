# Prepaid Billing Calendar Reconciliation

Status: staging-validation candidate

## Decision and ownership

`financial.prepaid_service_renewals` owns the forward prepaid settlement
calendar decision. A lapsed settlement begins at midnight on the payment's
`Africa/Lagos` business date, advances through the subscription's typed billing
cadence, and persists the resulting boundaries as UTC instants.

`financial.prepaid_billing_calendar_reconciliation` owns the reviewed repair
of historical periods created by the retired UTC-midnight calculation. The
admin routes and templates only project its classification and submit a signed,
actor-bound, fingerprinted command. They do not calculate eligibility or dates.

## Safe cohort

Automatic confirmation is available only when all of these facts hold at
preview and again under lock:

- one active, fully paid, non-proforma invoice with zero balance;
- exactly one active `base_subscription` invoice line;
- an explicit supported cadence on one prepaid subscription;
- exactly one active succeeded payment allocation that fully funds the invoice,
  with a same-account, same-currency canonical settlement for the payment;
- no refund or reversal evidence for that payment;
- the invoice period exactly equals the retired UTC-midnight calculation for
  that payment instant and cadence;
- exactly one active entitlement sourced from the same invoice and line with
  the same current interval;
- `Subscription.next_billing_at` exactly equals the current invoice end;
- no service-extension entry and no other active entitlement or invoice that
  overlaps the proposed WAT interval.
- no quota bucket overlaps the current or proposed period; usage-period evidence
  requires a coordinated usage-owner review and is never shifted here.

Every failed guard produces a named blocked disposition. A blocked row is
investigation-only and has no automatic action. The operator cannot override a
guard in the UI.

Confirmation locks the account, invoice, subscription, base line, entitlement,
payment, allocation, and settlement, expires the ORM snapshot, then re-reads
and reclassifies the full chain. A changed fingerprint fails closed before any
calendar projection is written.

## Atomic consequence

One confirmed command changes only:

- `Invoice.billing_period_start` and `billing_period_end`;
- the base invoice line's period metadata;
- the exact sourced entitlement's `starts_at` and `ends_at`;
- the linked subscription's `next_billing_at`.

It does not change invoice total, balance, status, payment, settlement,
allocation, ledger entries, access state, or service status. The economic delta
is always zero. Before/after instants, timezone, actor, reason, command,
correlation, payment, entitlement, fingerprint, and idempotency evidence are
stored on the invoice and staged in audit and durable event rows in the same
transaction.

## Page contract

- Screen: `admin.prepaid_billing_calendar_reconciliation`; list/queue plus
  confirmation editor.
- Audience/job: finance managers inspect and correct exact historical prepaid
  calendar drift; auditors inspect the queue without write access.
- Primary entity: paid invoice, identified by invoice number with linked
  subscription identity.
- Read/action owner:
  `financial.prepaid_billing_calendar_reconciliation`.
- First viewport: purpose and timezone, safe/blocked/scanned counts, invoice,
  current WAT period, proposed WAT period, disposition, reason, and one review
  action for eligible rows.
- Permissions: `billing:reconciliation:read` for the queue and
  `billing:reconciliation:write` for preview and confirmation.
- Primary action: review one exact correction. There is no bulk action.
- Confirmation: before/after WAT values, customer calendar dates, zero economic
  delta, evidence identifiers, consequences, required reason, explicit check,
  signed actor-bound token, and ten-minute expiry.
- Pagination: 100 paid prepaid invoice candidates per server-side page, newest
  paid first, with previous/next controls until the bounded cohort is exhausted;
  the owner removes non-signature rows after resolving their exact instants.
- Empty state: no exact legacy signatures found on the current page. Blocked
  state: named disposition and manual-review label without a submit control.
- Freshness: computed from the current database snapshot; confirmation rechecks
  under lock and rejects a stale fingerprint.
- Mobile: table remains horizontally scrollable while retaining invoice,
  decision, dates, and action; confirmation uses stacked definition rows.
- Audit/observability: invoice evidence, audit event, durable domain event, and
  idempotency reservation are committed atomically.

## Promotion and reconciliation runbook

1. Validate the forward fix and this queue on the immutable `origin/dev` image.
2. Deploy only that dev image to the explicitly named staging host.
3. Inspect staging queue counts and several eligible and blocked samples.
4. Confirm test fixtures or approved non-production cases and verify the
   invoice, entitlement, anchor, event, and audit evidence.
5. Run the forward, reconciliation, architecture, migration, and browser/mobile
   checks on the exact staged commit.
6. Promote the staged commit only after acceptance. Production deployment and
   any production reconciliation remain separate explicit approvals.
7. In production, begin with individual reviewed cases. Do not add a bulk
   action unless a separate owner contract and safety review approve it.

The queue is temporary migration control-plane capability. Retire it after the
accepted reconciliation run and a complete verification scan reports no exact
legacy signatures; retain immutable audit and event evidence.
