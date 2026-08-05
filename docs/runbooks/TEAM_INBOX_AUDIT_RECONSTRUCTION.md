# Team Inbox audit reconstruction runbook

Owner: `communications.team_inbox_audit_reconstruction`

This runbook governs historical reconstruction after native lifecycle-event
cutover. It is not an automatic deployment step.

## Preview

Run the typed preview against the explicitly named target. Retain the complete
manifest, source watermark, SHA-256, counts by evidence grade, and unknown
exceptions. Preview is read-only and must cover the complete source set; do not
use sampled evidence for approval.

```bash
poetry run python -m scripts.one_off.team_inbox_lifecycle_audit preview
```

## Review gate

An operator reviews conflicts and unknowns and records an approval reference.
No actor, reason, timestamp, queue interval, or status may be inferred beyond
the evidence grade declared in the manifest. A changed source watermark or
manifest hash requires a new preview and review.

## Apply

Submit the typed apply command with the exact reviewed SHA-256, watermark,
operator UUID, approval reference, and idempotency key. Apply recomputes the
manifest inside the owner transaction, refuses drift, appends events, and
leaves unknown findings unapplied. Exact retries are constrained by source
identity and must not create duplicate evidence.

```bash
poetry run python -m scripts.one_off.team_inbox_lifecycle_audit apply \
  --reviewed-sha256 <sha256> \
  --source-watermark <watermark> \
  --actor-person-id <uuid> \
  --approval-reference <reference> \
  --idempotency-key <key> \
  --confirm APPLY_REVIEWED_TEAM_INBOX_AUDIT
```

## Verification

Verify event counts and source identities against the approved manifest,
assignment interval overlap, current status versus latest native event, and
unknown exception counts. Retain the manifest and execution outcome as
operator evidence. Historical events remain visually distinct from native
post-cutover events.

Inspect one conversation's redacted identifier-only timeline and drift report
with:

```bash
poetry run python -m scripts.one_off.team_inbox_lifecycle_audit timeline \
  --conversation-id <uuid>
```

## Failure and rollback

The event ledgers are append-only. A failed transaction rolls back as a unit.
After committed evidence exists, downgrade is refused; correction uses a
reviewed forward-fix event and never updates or deletes an event.
