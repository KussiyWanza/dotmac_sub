# Inbox Customer Context and Lead Actions

**Status:** Implemented
**Decision owner:** Customer experience platform with Sales lifecycle participation

## Decision

The Inbox contact drawer is an authoritative customer-context projection. It
must never render customer-specific example values. Its typed section outcomes
are `available`, `empty`, `not_applicable`, `not_calculated`, `unavailable`, and
`restricted`. Only a successful owner query with no rows produces an empty
count of `0`. Optional missing scalars use `—`. A failed or forbidden query is
never represented as zero.

`communications.team_inbox_contact_context` composes exact Party/profile,
Subscriber or Reseller identity, Lead, active Ticket, recent conversation,
Project, and Project Task facts. Ticket, Project, and Task status inclusion
comes from their lifecycle
owners. Counts cover the full authoritative query while displayed collections
are bounded to five newest records. Database reads are transaction-current;
the drawer records its observation time and refreshes on every return to the
originating conversation. Each section has its own availability result.

The drawer exposes conversation history as a dedicated tab beside customer
details. Its badge is the full count of matching previous active and resolved
Inbox conversations, independent of which agent handled them. The typed match
scope uses an exact Subscriber first, then a reviewed Party contact-point or
Reseller relationship. Without broader reviewed identity, it uses only the
exact normalized inbound endpoint and, for provider-scoped social identifiers,
the same provider account scope. Conflicting identity or provider-scope
evidence returns `not_calculated` and never merges records. The tab lists the
five newest previous conversations with endpoint,
channel, status, and last activity date; selecting one returns to that exact
conversation through the server-owned `/admin/inbox?c=<conversation-id>`
destination. When more than five exist, the bounded result is stated explicitly
rather than presented as the complete count.

## Identity and action policy

`communications.inbox_lead_actions` accepts a `profile` or `lead` intent and
returns a typed UI action. The browser does not decide whether to reuse or
create identity.

1. A direct structural Lead link opens that Lead and never creates another.
2. An exact Party requires an explicitly selected active Pipeline. One eligible
   active Lead is linked and opened; several require explicit selection; none
   permits Lead creation for that exact Party.
3. Potential or conflicting contact matches require reviewed identity linking.
   Email, telephone, name, or social-handle equality never silently establishes
   or merges Party identity.
4. With no exact Party and no candidates, the authorized admin Lead form may
   create a quarantined prospect Party and Lead. Observed contact data remains
   unverified and grants no customer, Subscriber, authentication, consent, or
   authorization status.

Profile editing remains owned by the customer/Party profile owner. Lead fields
remain owned by Sales. The common resolver owns only eligibility, routing, and
the cross-owner coordination needed to create or attach a Lead.

## Provenance, cardinality, and atomicity

`communications.conversation_lead_relationships` owns
`inbox_conversation_lead_links`.

- A conversation has at most one active Lead link.
- A Lead may originate from or be associated with multiple conversations.
- Ordinary commands cannot replace an active link.
- Relinking is a separate reviewed repair decision; old evidence is retained.
- The row records exact conversation, Lead, Party, source, reason, actor,
  command identity, and time with restrictive foreign keys and indexes.

New-prospect Party creation, prospect role, explicit contact observations,
Lead origin, Lead creation, and conversation provenance run inside the single
`sales.lead_authoring` owner transaction. Existing-Party Lead creation and link
creation run inside `communications.inbox_lead_actions`. The relationship
participant flushes only and requires an active owner command. Exact retries
return the existing relationship; conflicting replays fail closed.
Existing-Party creation locks conversation, Party, Pipeline, then qualifying
Lead rows in that order. New-prospect authoring locks the conversation before
creating deterministic Party/Lead identities. Database uniqueness arbitrates
any remaining race; adapters retry only the complete owner command after
rollback.

## Authorization and return contract

Conversation access requires `support:ticket:read`. Profile, Lead, Ticket,
Project, and Task reads and writes retain their separate permission keys. The
drawer omits unauthorized actions and uses the approved restricted section
state only where disclosing the section is safe.

Return destinations are constructed server-side from the UUID of the active
originating conversation; arbitrary redirect URLs are not accepted. Successful
creation or linking returns through `/admin/inbox?c=<conversation-id>`, whose
lazy drawer request recomputes current authoritative context. A projection
failure after a successful mutation therefore permits a read retry without
replaying the command.

## Historical data and repair

Migration 475 is expand-only and performs no speculative backfill. Existing
completed Lead-intake invitations are exact provenance and may establish the
native relationship on an idempotent command replay. All other historical
conversations remain explicitly unlinked until a reviewed decision supplies an
exact Party and Lead. Contact values, names, timestamps, notes, and nearby
creation times are not repair evidence.

The relationship owner exposes a bounded read-only `drift_report` for completed
intake evidence missing its native link, Lead/Party mismatches, and conflicts
with current exact Party identity. Approved repairs use the idempotent
`link_existing_lead` coordinator with the `reviewed_repair` source, actor,
reason, and command identity; ordinary resolution cannot replace a link.
