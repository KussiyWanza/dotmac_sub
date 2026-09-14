# Legacy prepaid renewal tax-invoice correction

Owner: `financial.prepaid_service_renewals`

Use this runbook only when Finance has approved a documentary invoice for one
historical prepaid period that already has an exact base-only
`AccountAdjustment` debit and a matching active entitlement. It does not create
service twice and does not write a standalone tax adjustment.

Deploying the code performs no correction. Preview and apply are separate,
explicit operator actions on one named account.

## Required evidence

- internal account, subscription, adjustment, and entitlement UUIDs;
- exact service-period boundaries and the existing debit amount;
- the applicable tax-rate identity and exclusive-tax treatment;
- Finance-approved invoice total and remaining account credit;
- a durable Finance approval reference; and
- the authenticated operator's `user:<uuid>` actor identity.

Do not place customer names, email addresses, phone numbers, or private Finance
documents in command arguments, output, logs, or source control.

## 1. Run the read-only preview

Run on the explicitly approved application host with the deployed candidate:

```bash
poetry run python -m scripts.billing.billing_target_shadow \
  preview-legacy-renewal-tax-invoice-correction \
  --account <account-uuid> \
  --subscription <subscription-uuid> \
  --adjustment <adjustment-uuid> \
  --entitlement <entitlement-uuid> \
  --expected-invoice-total <approved-tax-inclusive-total> \
  --expected-remaining-credit <approved-credit-after-correction>
```

The preview is read-only and reports `financial_state_changed=false`. Require
all of the following before continuing:

- `disposition=exact_base_only_legacy_renewal` and `actionable=true`;
- the four UUIDs exactly match the approved evidence;
- period, currency, original debit, subtotal, tax, invoice total, credit before,
  and credit after exactly match Finance's approval;
- no competing invoice is reported; and
- both preview fingerprints are present.

Stop on `manual_review`, any mismatch, or any error. Do not alter records to make
the preview pass.

## 2. Apply the exact preview

Use a unique idempotency key and preserve it with the approval evidence:

```bash
poetry run python -m scripts.billing.billing_target_shadow \
  correct-legacy-renewal-tax-invoice \
  --account <account-uuid> \
  --subscription <subscription-uuid> \
  --adjustment <adjustment-uuid> \
  --entitlement <entitlement-uuid> \
  --expected-invoice-total <approved-tax-inclusive-total> \
  --expected-remaining-credit <approved-credit-after-correction> \
  --preview-fingerprint <preview-sha256> \
  --actor user:<operator-user-uuid> \
  --reason <finance-approval-reference> \
  --idempotency-key <unique-correction-key>
```

The command re-previews after locking. Evidence, tax, invoice, or balance drift
causes the whole transaction to roll back. Exact replay with the same command
returns the original invoice and creates no duplicate effects.

## Acceptance checks

- the returned invoice is paid, has zero balance due, and its subtotal, tax, and
  total match the preview;
- its period and base line match the original entitlement period;
- payment allocations equal the invoice total and cite canonical settled
  payment evidence;
- the selected adjustment has one structural reversal;
- the old entitlement is reversed and exactly one invoice-backed replacement is
  active for the same period;
- the subscription paid-through anchor remains supported by active coverage;
- the remaining credit equals the Finance-approved value;
- one audit event records the actor and approval reference; and
- one `prepaid_service.renewal_document_corrected` event records the linked
  evidence identities.

## Failure and recovery

Before commit, any failure is atomic: retry only after a fresh preview and only
when the evidence still matches. After a successful commit, do not run raw SQL,
delete the invoice, reactivate the old entitlement, or manually post a balancing
ledger entry. A later business reversal requires its own reviewed owner command
and Finance approval.
