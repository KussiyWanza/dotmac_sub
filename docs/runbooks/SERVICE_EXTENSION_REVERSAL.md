# Service-extension reversal

Status: operational runbook

This runbook corrects an applied service extension through
`financial.service_extensions`. It does not authorize production execution;
Michael must name the target host and authorize deployment or operation first.

## Preconditions

1. Confirm the extension UUID, reason, scope, days, creator, applier, and current
   applied status on the admin detail page.
2. Confirm the operator has the dedicated `billing:extension:reverse`
   permission. Create/apply permission alone is insufficient. Deployment grants
   the new permission to the `admin` role only by default; any narrower custom
   role assignment must be explicit.
3. Record the reviewed correction reason. Do not paste customer identifiers,
   credentials, or private payloads into the reason.
4. Ensure migration `472_service_extension_reversals` and the matching
   application image are deployed before using the workflow.

## Preview

From the applied extension detail page, enter the reviewed reason and choose
**Preview reversal impact**. Review:

- the number of immutable grant intervals that will stop providing coverage;
- anchors that still equal the extension result and will be restored exactly;
- later or lower anchors that will be preserved rather than guessed through;
- terminal services whose anchors will remain unchanged; and
- active, future, and expired interval counts at preview time.

If any underlying entry, subscription lifecycle, or billing anchor changes
before confirmation, the owner rejects the stale fingerprint. Generate and
review a new preview; never bypass the conflict with direct SQL.

## Confirm and verify

Confirm once. An exact retry is idempotent and returns the stored reversal.
Verify on the detail page:

1. status is **Reversed**;
2. one canonical `billing.service_extension_reversed` activity item exists;
3. the reversal evidence counts match the reviewed preview;
4. the original grant interval sample remains visible as historical evidence;
5. reversed intervals no longer resolve as prepaid coverage or dunning shields;
6. later/lower/terminal preserved-anchor counts are reviewed as entity-scoped
   follow-up rather than mass-edited; and
7. normal prepaid/postpaid enforcement continues and decides any access
   consequence from current funding, grace, shields, and locks.

## Recovery

Do not delete reversal rows, restore aggregate status with SQL, or manufacture
replacement audit events. A failed owner command rolls back aggregate status,
anchor changes, reversal evidence, audit, and event rows together. A committed
reversal is itself immutable; any later business correction requires another
explicit owner design rather than a reversal-of-reversal shortcut.
