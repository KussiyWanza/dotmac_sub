# Customer-subledger opening correction

Use this workflow only when reviewed financial evidence proves that an already
captured customer opening position is wrong. It never edits or deletes the
original opening, and it never creates or duplicates a payment. The owner adds
one append-only correction and the matching customer-position effect in the
same transaction.

The operator must be an active staff user holding
`billing:customer_subledger_opening:correct`. Preview first:

```bash
python -m scripts.billing.correct_customer_subledger_opening \
  --account-id ACCOUNT_UUID \
  --currency NGN \
  --corrected-opening-amount EXACT_AMOUNT \
  --reason "REVIEWED_REASON" \
  --review-reference "DURABLE_FINANCE_REFERENCE"
```

Have finance verify the before, after, and delta values. Apply only the exact
reviewed fingerprint:

```bash
python -m scripts.billing.correct_customer_subledger_opening \
  --account-id ACCOUNT_UUID \
  --currency NGN \
  --corrected-opening-amount EXACT_AMOUNT \
  --reason "REVIEWED_REASON" \
  --review-reference "DURABLE_FINANCE_REFERENCE" \
  --apply \
  --expected-preview-fingerprint REVIEWED_SHA256 \
  --command-id NEW_COMMAND_UUID \
  --actor "NAMED_OPERATOR" \
  --actor-system-user-id STAFF_UUID \
  --idempotency-key UNIQUE_OPERATION_KEY
```

The command fails closed if authority is inactive, the opening is absent, the
preview changed, the permission is missing, or the idempotency key conflicts.
Afterward, verify the correction record, posting group, calculated funding
balance, and any separately executed renewal. Do not edit the opening row or
insert another payment as a workaround.
