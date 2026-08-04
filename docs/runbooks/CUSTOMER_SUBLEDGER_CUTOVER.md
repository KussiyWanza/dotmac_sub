# Customer subledger prepaid opening completion

This runbook operates ADR 0007 Phase 3 for the exact prepaid funding cohort.
The checked-in resolver and command owners, not shell arithmetic, own every
target, residual, posting, and approval.

## Invariants

- Run only the immutable image that completed the prescribed promotion train.
- Use one release-engineering session and stable, purpose-specific idempotency
  keys. Never run a second copy of the commands in parallel.
- The source opening for each migrated account is the mathematical net of its
  complete frozen Splynx transaction set. A complete empty set is zero.
- A customer created natively after the fixed handoff has an explicit zero
  history component and is advanced only by canonical Sub-native facts.
- Missing cohort coverage, duplicate or mismatched identity, malformed rows,
  and an unreconciled transaction net abort the whole artifact. There is no
  per-account unknown, default-zero fallback, or partial-subset materialization.
- The target at the reviewed instant is the frozen source position plus
  canonical native facts crossing the handoff. The opening posting is only the
  residual target minus the value already present in the subledger, so forward
  postings are not counted twice.
- Existing immutable openings and the irreversible authority record are never
  rewritten. A later mistake is corrected forward through a typed, reviewed
  reversal or adjustment owner.

All command examples use the deployed application container. The subledger
commands are under:

```text
python -m scripts.billing.billing_target_shadow
```

## 1. Build and review the complete source artifact

In the isolated audit restore, load the retained final Splynx snapshot and run
the checked-in reconstruction. Confirm that `audit_splynx_final_balances` was
produced from the complete source transaction set and that the fixed handoff is
`2026-06-18T00:00:00Z`.

Run `export_prepaid_funding_snapshot.py` with a timezone-aware review instant,
source label, output path, and OpenBao signing-key reference. The command must
return one signed target for every current prepaid funding candidate. Review:

- candidate count equals account-row count;
- blocker count is zero;
- the candidate and source-history fingerprints are stable;
- every migrated row is source-identity matched and transaction-reconciled;
- every empty history has zero transactions, null transaction net, and zero
  final position;
- all native-after-handoff components and total targets are plausible.

Any source-integrity error means stop and correct/rebuild the isolated source
snapshot. Do not create a partial manifest or edit a generated balance.

## 2. Materialize the exact reviewed targets

Dry-run `materialize_prepaid_funding_reconstruction.py` against the live cohort.
The live cohort hash must equal the signed hash. Review create, replace, and
unchanged counts, then apply with the exact normalized manifest hash, a
non-secret finance evidence reference, approving actor, and the required final
cutover acknowledgement.

This supersedes the active reconstruction baseline for the complete cohort.
For accounts that already have immutable subledger openings, verifier reads
continue from those openings; the new baseline does not rewrite historical
money. For a missing-opening account, the new baseline supplies its exact
history-derived target.

## 3. Preview and capture only missing openings

Run `preview-subledger-openings` with the exact deployed code SHA, schema
revision, and reviewed cutoff. The durable result contract must prove:

- the opening-required cohort contains exactly accounts that existed at
  subledger authority activation;
- `cohort_count = existing_opening_count + capture_eligible_count`;
- `covered_count = cohort_count`;
- source-incomplete and quarantined counts are zero;
- every existing opening is fingerprinted unchanged;
- each proposed residual equals verified target minus shadow position at the
  cutoff;
- `postings_manufactured=false` and `authority_moved=false`.

Review every non-zero residual and the aggregate positive/negative/zero totals.
Record separate operator and finance approvals on that exact fingerprint, then
run `capture-subledger-openings`. The owner atomically writes one immutable
opening row and one typed posting group per missing account. A failure rolls
back the complete capture.

## 4. Prove full-cohort parity

Run `verify-subledger-parity` for the approved window. The completion gate is:

- every opening-required account has exactly one opening, while later native
  accounts start from authoritative zero without a migration opening;
- per-account/currency/lane variance is zero;
- missing and duplicate openings are zero;
- every observation-window money fact has exactly one posting group;
- unwrapped and duplicate fact counts are zero;
- blocker and expected-difference counts are zero.

Any failure is fixed at its named owner and replayed through a new immutable
run. Do not waive or edit evidence.

## 5. Authority handling and post-completion proof

Production customer-subledger authority is already active. Do not call
`activate-subledger-authority` again; the existing irreversible record must be
unchanged. A fresh installation may activate only from an approved, zero-
blocker, complete-cohort parity run. An older run containing excluded accounts
is rejected even if it has work items.

Immediately prove:

- the authority record ID and activation instant are unchanged;
- opening-required count equals opening count, source-incomplete count is zero,
  and no candidate is blocked from billing functions;
- a known former opening-debt account resolves through the authoritative
  subledger and billing functions without a quarantine error;
- scalar and bounded-cohort positions equal the verified target;
- the next real money transition produces one authoritative typed posting;
- provider settlement value uses `PaymentSettlement.amount`, never gross
  gateway charge including provider fee.

Continue normal posting-coverage and per-lane monitoring. After the complete
proof, resolve the obsolete opening-debt work items and retire the one-time
Splynx history reader and partial-blocker workflow. Splynx remains historical
evidence, not runtime authority.
