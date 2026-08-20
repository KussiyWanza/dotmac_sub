# Support ticket comment attachment reconciliation

## Incident and authority

Earlier comment-upload adapters produced a valid private `StoredFile` UUID, but
the typed `AttachmentMeta` command omitted that field. Pydantic discarded the
UUID before `TicketComment.attachments` was persisted, leaving the filename and
storage key visible but no identity for the authorized streaming route.

`support.ticket_lifecycle` owns comment attachment metadata and its repair.
Routes, scripts, templates, direct SQL, and object storage do not own the
reconstruction decision.

## Preview

Run against one or more exact affected Ticket UUIDs. Preview is read-only and
reports repairable, complete, missing, malformed, and ambiguous items:

```bash
poetry run python -m scripts.migration.reconcile_ticket_comment_attachment_references \
  --ticket-id abaf2d5a-944f-4b62-ba9e-ce4345368a79
```

Review the output. Apply only when `missing_file_record`,
`ambiguous_file_record`, and `malformed_metadata` are understood. The repair
never guesses when more than one active metadata row matches.

## Apply

Use a stable idempotency key and reviewed operator evidence:

```bash
poetry run python -m scripts.migration.reconcile_ticket_comment_attachment_references \
  --ticket-id abaf2d5a-944f-4b62-ba9e-ce4345368a79 \
  --apply \
  --actor <operator-id> \
  --reason <reviewed-reason> \
  --idempotency-key <stable-key>
```

The owner locks the scoped comments, rechecks current metadata, writes only
exact matches, and stages `comment_attachment_reference_repair` audit evidence.
It does not copy objects, alter visibility, or expose private storage keys.

## Verification

Run preview again and require `repairable` and `repaired` to be zero while the
repaired items count under `already_complete`. Open the affected ticket as an
authorized staff user and verify that the attachment is now a link and that an
image or PDF streams inline.

## Failure behavior

- A missing storage key or file row remains unchanged for separate storage
  investigation.
- Multiple active matching rows remain unchanged until their identity is
  reviewed.
- Re-running an applied repair is a no-op for already complete attachments.
- Do not use direct SQL or insert a guessed UUID into comment JSON.
