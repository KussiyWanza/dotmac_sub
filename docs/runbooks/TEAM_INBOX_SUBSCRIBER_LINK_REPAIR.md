# Team Inbox Subscriber-link repair

## Purpose

Repair active historical Inbox conversations whose `subscriber_id` is empty so
the Contact Details **Conversations** tab can group the exact customer's
threads. This process does not import CRM conversations and does not merge
conversation or message rows.

The repair uses the registered
`communications.team_inbox_contact_resolution` owner. Only one uniquely
resolved active Subscriber for the normalized channel contact is eligible.
Resolution aggregates direct Subscriber fields, `SubscriberContact`,
`SubscriberChannel`, `CustomerIdentityIndex`, verified `PartyContactPoint`, and
reviewed provider-scoped `InboxContactLink` evidence. Ambiguous, unmatched,
inactive-customer, incomplete-social-scope, and already-linked results remain
unchanged. The command never creates a Lead or replaces an existing Customer.

## Preview

Run from a non-production application host with read access to the intended
database:

```bash
poetry run python -m scripts.one_off.repair_team_inbox_subscriber_links --limit 1000
```

The report is PII-free and includes the exact SHA-256 digest, every eligible
conversation, and linked/projected, ambiguous, unmatched, suppressed, skipped,
conflict, and error counts. Review the UUIDs and counts. Increase the limit only
after confirming the bounded batch.

Before deploying migration 595, verify there is no duplicate active
`PartyContactPoint` tuple for `(channel_type, provider, provider_account_id,
external_subject_id)`. Index creation deliberately fails closed if such a tuple
is already assigned more than once. Count legacy active Messenger/Instagram
Inbox links without full provider scope as review debt; the migration backfills
only links already bound to an exact scoped Party contact point.

Deployment order is: apply migration 595; deploy the resolver/ingress code;
verify new WhatsApp, email, Messenger, Instagram, and Fiber conversations; run
this command in preview mode; and apply a reviewed batch only under a separate
production approval. The application never launches this repair automatically.

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
The command is never invoked by the migration or deployment process. Production
apply requires a separately approved, explicitly named target.

## Verification

1. Re-run the preview and confirm the repaired conversations are no longer
   eligible.
2. Open representative customers in Team Inbox Contact Details and confirm
   their separate conversation threads appear.
3. Confirm ambiguous and unmatched conversations remain unlinked.
4. Confirm conversation and message counts did not decrease.
5. Record the output and approval reference in the operator evidence store.

The operation is additive. A wrong reviewed association must be corrected
through the existing manual contact-link workflow; do not directly edit the
conversation table.

Application rollback must precede schema downgrade. Do not downgrade after two
same-looking social subjects have legitimately been stored under different
provider accounts until those rows are reviewed: restoring the retired global
`(channel_type, normalized_contact)` uniqueness index would otherwise fail or
collapse valid identities.

Reapplying a route already linked to the selected Subscriber reuses that route
and repairs only eligible unlinked conversations. A different selected
Subscriber does not replace the reviewed owner: it records a conflict and
requires explicit adjudication. A stale-route refusal requires a fresh preview;
never remove or bypass the active-route unique index.
