# Reviewed migrated prepaid opening repair

**Owner.** `financial.customer_subledger_opening_positions` captures the opening.
`financial.prepaid_service_renewals` executes any separately reviewed missed
service period. The operator CLI is an adapter and never decides or writes money
directly.

**Use only when.** One migrated prepaid account existed at customer-subledger
authority activation, has retained Splynx identity, has no opening position, and
Finance has approved a complete-history position at the original authority
cutoff. Native accounts use the existing native-account completion path. A
cohort-wide source problem uses the sealed full-cohort reconstruction workflow.

Deployment does not run this repair. Every write below is a separate explicit
operator action.

## Required evidence

Keep the reviewed evidence document in the approved controlled system. It must
identify the account by internal UUID, show the complete ledger calculation at
the original authority cutoff, and record Finance's decision. Record only its
durable reference and SHA-256 digest in Sub; do not copy customer identity or
private financial documents into command arguments or logs.

The opening amount is the position **at the original cutoff**. Do not add later
credits or subtract later renewals: canonical later facts remain later facts and
the balance resolver applies them once.

## 1. Record the no-write preview

```bash
poetry run python -m scripts.billing.billing_target_shadow \
  preview-migrated-account-opening \
  --account <account-uuid> \
  --position-at <original-authority-cutoff-iso8601> \
  --legacy-position <reviewed-cutoff-position> \
  --source-evidence-ref <controlled-review-reference> \
  --source-evidence-sha256 <reviewed-document-sha256> \
  --code-version <deployed-commit> \
  --schema-version <deployed-alembic-head> \
  --idempotency-key <unique-preview-key>
```

This command writes verification evidence only. Review the account UUID,
currency, legacy position, current shadow position, opening delta, evidence
reference and digest, source fingerprint, and result fingerprint. Any mismatch
requires a new evidence document and a new idempotency key; never edit a stored
run.

## 2. Record the two approvals

The operator approves first. Finance then approves the same immutable run and
result fingerprint through `approve-verification`. Approval fails while the run
has blockers. Use real actor identities and the Finance approval time.

## 3. Capture the exact reviewed opening

```bash
poetry run python -m scripts.billing.billing_target_shadow \
  capture-subledger-openings \
  --run <verification-run-uuid> \
  --result-fingerprint <reviewed-result-sha256> \
  --reference <finance-approval-reference> \
  --actor <operator-identity> \
  --idempotency-key <unique-capture-key>
```

Capture locks the account and recomputes its identity, authority cutoff, and
shadow position. Changed evidence fails closed. Success creates one immutable
opening, one matching posting group, and one audit event in the same transaction.
Exact replay creates nothing twice.

## 4. Preview and execute a missed service period

First run `preview-prepaid-service-renewal` with the exact subscription, period,
amount, and currency. Confirm `allowed=true`, the before/after balances, and the
preview fingerprint. Then execute only that fingerprint:

```bash
poetry run python -m scripts.billing.billing_target_shadow \
  execute-reviewed-prepaid-service-renewal \
  --subscription <subscription-uuid> \
  --starts-at <period-start-iso8601> \
  --ends-at <period-end-iso8601> \
  --amount <reviewed-renewal-amount> \
  --currency NGN \
  --preview-fingerprint <reviewed-preview-sha256> \
  --evidence-ref <finance-approval-reference> \
  --actor <operator-identity> \
  --idempotency-key <unique-renewal-key>
```

The command re-previews under lock. If funding or terms changed, it refuses the
write. Success atomically creates the canonical debit, entitlement, billing
anchor advancement, renewed outcome, subledger posting, and applicable service
restoration.

## Acceptance checks

- The account is absent from prepaid opening-source quarantine.
- Canonical funding equals the approved opening plus later native facts.
- The reviewed renewal has one debit, one active entitlement, and one posting.
- `next_billing_at` equals the entitlement end.
- Admin and customer projections show the same available balance.
- Exact command replay creates no duplicate opening, debit, entitlement, or
  renewed outcome.

Never use raw SQL, edit a balance or billing date, create a fake payment, or
create an invoice to represent this repair.
