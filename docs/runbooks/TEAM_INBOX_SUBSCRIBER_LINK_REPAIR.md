# Team Inbox Subscriber-link repair

## Purpose

Repair active historical Inbox conversations whose `subscriber_id` is empty so
the Contact Details **Conversations** tab can group the exact customer's
threads. This process does not import CRM conversations and does not merge
conversation or message rows.

The repair uses the registered
`communications.team_inbox_contact_resolution` owner. Only one uniquely
resolved active Subscriber for the normalized channel contact is eligible.
Ambiguous, unmatched, and inactive-customer results remain unchanged.

## Preview

Run from a non-production application host with read access to the intended
database:

```bash
poetry run python -m scripts.one_off.repair_team_inbox_subscriber_links --limit 1000
```

The report is PII-free and includes the exact SHA-256 digest, eligible route
representatives, and ambiguous/unmatched/suppressed counts. Review the UUIDs
and counts. Increase the limit only after confirming the bounded batch.

## Apply

Apply requires a named target, attributable staff UUID, reason, approval
reference, exact preview digest, and explicit confirmation:

```bash
poetry run python -m scripts.one_off.repair_team_inbox_subscriber_links \
  --limit 1000 \
  --apply \
  --target <named-host-or-database> \
  --actor <staff-person-uuid> \
  --reason "<reviewed reason>" \
  --approval-reference <approval-id> \
  --expected-digest <preview-sha256> \
  --confirm APPLY_TEAM_INBOX_SUBSCRIBER_LINK_REPAIR
```

Do not apply when the preview contains unexpected volume or identity scope.
Re-run preview immediately before apply; digest drift must stop the operation.

## Verification

1. Re-run the preview and confirm the repaired exact routes are no longer
   eligible.
2. Open representative customers in Team Inbox Contact Details and confirm
   their separate conversation threads appear.
3. Confirm ambiguous and unmatched conversations remain unlinked.
4. Confirm conversation and message counts did not decrease.
5. Record the output and approval reference in the operator evidence store.

The operation is additive. A wrong reviewed association must be corrected
through the existing manual contact-link workflow; do not directly edit the
conversation table.
