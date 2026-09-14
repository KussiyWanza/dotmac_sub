# Inbox Customer Identification and Completion Gate

## Decision and scope

`communications.team_inbox_customer_completion_policy` owns immutable,
administrator-selected Customer completion-policy versions. Every new Inbox
conversation snapshots the latest version. A later settings change creates a
new row and affects only conversations created afterward. Migration 594 assigns
the initial version to existing active, unresolved conversations.

The initial policy requires `name`, `phone`, and `address`. The supported field
vocabulary also includes email, WhatsApp, organization, city/region, country,
DOB, gender, and NIN. The policy stores field keys rather than browser logic.

`communications.team_inbox_customer_completion` owns the resolution
`ActionReadiness` verdict and coordinates profile completion from Inbox into the
canonical Customer account and Party owners. Inbox metadata is observation or
compatibility context; it never satisfies the completion gate.

`customer.canonical_profile_patch` is the typed, flush-only Customer participant
used by that coordinator. It locks and updates the existing Subscriber and
primary service Address, stages `subscriber.updated`, and cannot create a
Customer. `party.registry` remains the owner for Party profile and contact-point
updates. The Inbox coordinator owns the surrounding atomic command, conflict
checks, audit evidence, and readiness refresh.

## Classification

- A conversation with an explicit Subscriber relationship is a Customer.
- A conversation with an active reviewed or completed intake Lead relationship
  is a Lead.
- Conflicting identity evidence is ambiguous; no identity is unresolved.
- A reviewed contact link always outranks newly observed display data.
- A unique eligible Customer found by an exact normalized canonical phone or
  email is authoritative. A provider profile/display name is observational
  evidence and cannot veto that unique match.
- When the same normalized phone or email identifies multiple eligible
  Customers, an exact normalized Customer-name match may narrow the candidates
  only when it leaves exactly one. Otherwise the result remains ambiguous and
  requires review.
- Messenger and Instagram subjects are opaque provider identities. They resolve
  only in `(channel, provider, provider account, external subject)` scope and
  are never interpreted as phone numbers.

Customer selection searches and links an existing Customer only. Inbox has no
Customer-creation command. Lead actions may link an existing Lead or create a
Party-backed Lead through the existing Sales owner.
Identify Contact persists the reviewed route as an additive canonical Party
contact point as well as linking the current conversation. It never replaces a
Customer's other phones, emails, or social identities. Representative selection
keeps the speaking Person Party and its endpoint separate from the represented
Customer while still setting the authoritative `subscriber_id`. A reviewed
identity-owner conflict remains blocked for human adjudication.

Immediately before manual Lead capture, the Inbox command reruns canonical
Customer resolution. A unique Customer is linked instead, ambiguity creates no
Lead, and only a genuine no-match may enter the Party-backed Lead owner.

## Resolution rule

For agent-originated direct, bulk, and macro transitions to `resolved`, the
status owner asks the completion owner for one authoritative verdict:

- Customer: every field in the conversation's snapshotted version must be
  complete on the canonical Customer/Party profile.
- Lead: profile completeness is advisory and never blocks resolution.
- Unresolved or ambiguous: identification must be completed before resolution.

Bulk resolution skips blocked conversations and returns blocker details. Macro
resolution records the failed action and cannot bypass the same gate. System
maintenance and AI lifecycle reasons retain their separately owned transition
rules.

The status owner provides one narrow expired-conversation path. When—and only
when—the locked conversation is WhatsApp and the canonical reply-window owner
returns `expired`, an authorized agent or manager may resolve without first
completing Customer identity/profile fields. The command requires one bounded
resolution reason and records that reason plus
`channel_state_at_resolution='expired'` on the immutable status event. The
Customer readiness verdict remains blocked and visible as context; it is not
weakened or rewritten. An active-window, unavailable, or non-WhatsApp
conversation continues to obey the normal verdict.

Expired resolution is internal-only. It does not send a message or template,
create an assignment or FIFO entry, start AI intake, or alter the reply-window
chronology. Assignment/queue release is a separate routing consequence of
expiry and never implies `resolved`.

## Canonical save and conflicts

The Inbox Customer completion command locks the conversation and linked
Customer, confirms the exact link, validates only explicitly submitted typed
fields, and writes Subscriber and Party/contact-point facts inside one owner
transaction. It rebuilds the Customer identity index before returning the
fresh readiness verdict. The drawer is then reloaded from canonical data.

When an existing non-empty value differs from a proposed value, the command
returns both values and requires explicit field-level replacement confirmation.
Phone and email changes run through normalization and existing identity
collision checks. No parallel conversation profile is written.

Each changed field stages audit evidence containing decision source, actor ID,
actor type, timestamp, conversation ID, Customer ID, Party ID when present,
previous value, new value, and correlation ID. Identity-selection owners retain
their existing selected Customer/Lead/Party provenance.

## Projection freshness and repair

The readiness verdict is transaction-current and is always recomputed from the
snapshotted policy plus canonical records. The UI does not persist a readiness
flag. A missing policy snapshot, missing linked Customer, unsupported historic
field key, or ambiguous identity fails closed and is its own drift signal.
Reopening the drawer or retrying the status command deterministically rebuilds
the verdict; structural Lead-link repair remains with
`communications.conversation_lead_relationships`.
`InboxConversation.subscriber_id` is the authoritative Customer association.
Automatic identity resolution and the reviewed repair command populate that
field transactionally; the readiness owner then changes classification without
any template or cached-readiness override.
