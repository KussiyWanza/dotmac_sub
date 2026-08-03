# Customer subledger prepaid cutover

This runbook operates the ADR 0007 Phase 3 prepaid-position cutover. The
checked-in command owners, not this document or the shell, decide eligibility,
write openings, and activate authority.

## Preconditions

- Run only the immutable production image that passed the complete promotion
  train and schema verification.
- Confirm schema revision 457 is at head and all workers and Beat are healthy.
- Use one release-engineering session. Do not run a second copy of these
  commands in parallel.
- Keep the existing `prepaid-funding:opening-debt:` work items open for every
  evidence-quarantined account. Never assign zero or infer an opening from a
  catalog price.
- Choose a timezone-aware cutoff and an observation window beginning no earlier
  than the deployment of the owner-wrapped producer code being approved.

All examples use:

```text
python -m scripts.billing.billing_target_shadow
```

inside the deployed application container. Supply stable, unique idempotency
keys; never reuse a key for a different run or approval.

## 1. Record and review the opening proposal

Run `preview-subledger-openings` with the exact code SHA and schema revision.
Review the durable run's source/result fingerprints, cohort counts, per-currency
totals, every non-zero residual, and the complete quarantine set. The command
creates no posting and moves no authority.

Record operator approval, then finance approval, with
`approve-verification --role operator` and `--role finance`. Finance approval
fails until the operator approval exists. Neither approval can be replaced.

## 2. Capture only the approved fingerprint

Run `capture-subledger-openings` with the opening run ID, exact result
fingerprint, durable finance review reference, actor, and idempotency key. The
owner atomically writes one immutable opening row and one posting group per
eligible account. A staging failure rolls back the entire cohort.

Confirm:

- captured count equals the eligible count;
- positive, negative, and zero totals agree with the reviewed proposal;
- quarantined accounts received no opening;
- `authority_moved` remains false.

## 3. Record the post-opening parity gate

Run `verify-subledger-parity` with the approved observation window, current code
SHA, schema revision, and currency. Review the durable detail rows, not only the
aggregate counts.

The activation gate requires all of the following:

- `variance_count = 0` for every eligible account/currency;
- `unwrapped_fact_count = 0`;
- no missing or duplicate opening;
- no duplicate posting for an observation-window fact;
- `blocker_count = 0`;
- every quarantined account is still named and owns an open
  `prepaid-funding:opening-debt:` finance work item.

Any failure means stop, fix the owning producer or evidence, deploy the forward
fix through the complete train, and record a new run. Do not waive or edit the
run.

## 4. Approve and activate once

Record operator and finance approval on the exact parity run. Then run
`activate-subledger-authority` with its run ID, result fingerprint, durable
review reference, actor, and a fresh idempotency key.

Activation is irreversible. Exact replay returns the existing cutover; a
different run or fingerprint fails closed. After activation:

- default subledger reads combine historical shadow groups with new
  authoritative groups;
- every newly staged group is authoritative;
- explicit migration reads may still select one authority for forensic work;
- prepaid target-policy evaluation of an opening-quarantined account raises
  `collections.prepaid_policy.opening_position_quarantined` and names its work
  item fingerprint.

## 5. Verify after activation

Immediately verify the cutover record, a known eligible account, a quarantined
account, and the next real money transition. The eligible default position must
equal the verified legacy position; the quarantined account must fail closed;
the new posting must have authoritative authority and the correct typed source,
producer, effects, and instant.

For a provider settlement, compare the posting against the structural
`PaymentSettlement.amount`, not the gross `Payment.amount`. The gross charge may
include a provider fee that is cash/accounting evidence but is not customer
funding. Require scalar and bounded-cohort position parity after the first live
settlement, including a non-zero-fee settlement when the active gateway charges
one; a zero-fact or fee-free window alone does not close this semantic gate.

Continue monitoring posting coverage and per-lane parity. Recovery after
activation is a forward owner correction or code fix. Never delete the cutover,
rewrite an opening, restore a fallback balance reader, or manufacture a missing
posting from an unreviewed historical fact.
