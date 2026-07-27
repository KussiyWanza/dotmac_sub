# Conversation participants

**Status:** shadow projection implemented; every consequence still deferred.
**Decision owner:** Michael.
**Owning service:** `communications.team_inbox_participants`
(`app/services/team_inbox_participants.py`).

## The gap this closes

`InboxConversation` modelled the internal side of a thread as sets —
`InboxConversationTeam` for which teams are involved, `InboxConversationAssignment`
for which agent owns it — and the customer side as a scalar:

```python
subscriber_id:    Mapped[uuid.UUID | None]
contact_address:  Mapped[str | None] = mapped_column(String(255))
```

So the inbox knew exactly which of our people were on a conversation and had no
representation of who theirs were. Two questions were therefore unanswerable:

- *Is this sender part of this thread?* — needed by any rule that would stop a
  reply joining a conversation on nothing more than a guessed `Message-ID`.
- *Who may receive this transcript?* — needed by any rule that would restrict
  export to legitimate parties.

Both had been deferred as policy questions. They were not: the data model made
the sensible answer unrepresentable, because a thread had one contact and
anyone else was an intruder by construction.

## The model

`inbox_conversation_participants` records that an endpoint took part in a
conversation, and how it was admitted.

### Endpoint before party

`party_contact_point_id` is **nullable**, with the same conditional
all-or-nothing evidence CHECK as `inbox_contact_links`.

Inbox owns the fact that an endpoint participated. `party.registry` owns who
that endpoint belongs to. A mandatory Party FK would recreate the original
problem: an unknown colleague, a new vendor, an ambiguous shared address or an
unreviewed contact would stay unrepresentable. It would also contradict
`docs/PARTY_CONTACT_INBOX_PROJECTION.md`, where the Party binding is explicitly
nullable and shadow-only.

### Admission source before relationship

Two separate columns, deliberately:

| Column | Meaning | Changes? |
| --- | --- | --- |
| `admission_source` | how the endpoint arrived — `inbound_from`, `inbound_to`, `inbound_cc`, `outbound_to`, `outbound_cc`, `operator_added` | never |
| `relationship_type` | what it turns out to be — `customer`, `contact`, `third_party`, `unknown` | Party may revise |

`copied` is not a relationship. A customer may be copied and a third party may
be the sender, so provenance cannot stand in for classification. Collapsing the
two would let a later reclassification rewrite how a participant originally
arrived, which is precisely the audit property the split protects.

Everything is admitted `unknown`. That is the honest default.

### Other properties

- **`To` is captured** as well as `From` and `Cc`. Our own mailboxes are
  excluded through `team_inbox_routing.owned_mailbox_addresses`, which reads the
  routing table — the register of what is ours — and includes *retired* routes,
  since a mailbox we stopped using is still not a customer and old headers
  carry it.
- **Membership is tested on the exact active endpoint.** Binding one contact
  point to a party must never silently admit every other address that party
  owns; a rule that widened on binding would grant thread access nobody
  reviewed.
- **First admission wins.** Re-observing an endpoint on a later message does
  not rewrite how it originally arrived.
- One active row per `(conversation, channel, endpoint, provider scope)`.
  Provider scope is carried because two Messenger threads on different Pages can
  share an opaque sender id.

## Coverage is bounded by the headers

The backfill reads `InboxMessage.from_address` / `to_addresses` /
`cc_addresses`. A conversation whose messages arrived without `To`/`Cc` yields
only its `From` endpoints.

Two consequences:

1. A parity figure over this projection must be read against the corpus that
   actually preserved headers, not against every conversation.
2. **If a CRM history import does not carry `To`/`Cc`, those conversations can
   never have participants** — and any participant-aware threading or export
   rule would then behave differently on imported versus native threads,
   permanently and invisibly. That is a decision for the import contract, and
   it has to be made before the import.

## Participation is not authentication

A spoofed `From` can claim an endpoint that is already a participant. Membership
is therefore necessary but not sufficient for any security-strength admission
rule, which must also weigh transport-authentication evidence.

That evidence is now retained at ingestion — `Authentication-Results`,
`ARC-Authentication-Results`, `Received-SPF`, `DKIM-Signature`, `ARC-Seal` and
the `Received` chain — stored raw and interpreted nowhere, because a parsed
verdict embeds the interpretation that has not been chosen. Capture could not
wait for the policy: nothing recovers an SPF result for a message already
accepted, so every message received before capture began is permanently
un-adjudicable.

## Pending decisions

None of these are implemented, and none should be inferred from this table
existing.

1. **Admission policy.** Whether an unrecognised sender may join a thread, and
   what weight transport authentication carries in that decision.
2. **Role taxonomy** beyond the default, and who may reclassify.
3. **Transcript permission and override.** Whether export needs its own
   permission separate from `support:ticket:update`, and whether recipients are
   restricted to participants. Note that an exceptional recipient must **not**
   silently become a historical participant — *"add this person to future
   correspondence"* and *"export once to this address"* are different decisions
   and must be audited differently.
4. **The Party programme.** Promoting `party_contact_points` off shadow status:
   adjudication, backfill, canonical write path, drift reconciliation, parity,
   reader cutover and legacy-reader retirement. A legacy `Subscriber` contact
   change is not automatically a Party identity change. Phone-channel contact
   resolution stays a scan until this lands — and becomes an indexed *candidate*
   lookup rather than a single hit, because party contact values are
   deliberately not globally unique (`docs/PARTY_ROLE_RELATIONSHIP_SOT.md`), so
   the resolver must keep its matched / ambiguous / suppressed / unmatched
   outcomes.

## Measuring before deciding

`conversation.transcript_exported` audit events (added with the export audit)
carry both `recipient_on_record` and `recipient_seen_on_thread`.

`recipient_on_record` is measured against the scalar `contact_address`, so a
genuine participant scores false and reads as an exception. Interpreting export
behaviour on that field alone would overstate how often operators send outside
the conversation, and a restriction policy judged on it would be judged on the
wrong number. Read `recipient_seen_on_thread` first, and report the sample size
rather than a bare percentage.
