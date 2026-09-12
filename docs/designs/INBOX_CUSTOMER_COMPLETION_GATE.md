# Inbox Customer Identification and Completion Gate

## Decision and scope

`communications.team_inbox_customer_completion_policy` owns immutable,
administrator-selected Customer completion-policy versions. Every new Inbox
conversation snapshots the latest version. A later settings change creates a
new row and affects only conversations created afterward. Migration 596
assigns the initial policy version to EVERY existing `inbox_conversations`
row whose policy pointer was null -- not only active, unresolved ones. That
backfill is only data classification (recording which policy version a
conversation is subject to); it is harmless by itself and does not, alone,
retroactively enforce completeness against a legacy conversation. See
"Legacy resolution override" below for how enforcement is exempted for
conversations that predate the backfill.

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
- Without a reviewed link, an observed name may narrow an exact normalized
  phone match only by exact normalized Customer-name equality. A mismatch is
  ambiguous and requires the agent to choose an existing Customer or a Lead.

Customer selection searches and links an existing Customer only. Inbox has no
Customer-creation command. Lead actions may link an existing Lead or create a
Party-backed Lead through the existing Sales owner.

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

## Legacy resolution override

`communications.team_inbox_completion_override` owns a narrowly-scoped,
audited, single-use resolution override for conversations that existed
before the completion-policy backfill cutover. It is NOT blanket
grandfathering: every override is single-use, permissioned
(`support:inbox:completion_override`, admin-only), reasoned, and audited.

- **Eligibility marker.** `InboxConversation.completion_gate_precutover_at`
  is stamped exactly once, by the legacy-override migration, with one
  captured cutover instant reused for every backfilled row. It is never
  written by application code. The marker is eligibility, never
  authorization: it gates who may be *considered* for an override request,
  and never itself authorizes a resolution.
- **Grant.** An operator reviews the exact live missing-fields/canonical-values
  gap on one pre-cutover, unresolved, currently-blocked conversation and
  issues a single grant (`inbox_completion_override_grants`), fenced against
  the live evidence at grant time (a stale review is refused).
- **Consumption.** The grant is burned at most once, inside the same
  transaction that performs the resolution it unblocks, via
  `team_inbox_status._apply_status_transition` -- the same single choke
  point every direct, bulk, and macro resolution already shares. Any
  conversation activity, reopen, or expiry after the grant was issued
  re-arms the gate: the grant is refused and marked superseded/expired.
- **Audit evidence** is the grant row itself plus its foreign key to the
  `InboxStatusTransitionEvent` it resolved, proving the grant-to-resolution
  pair.

### Operator runbook (Phase 1: CLI only, no admin-portal route)

There is no admin-portal UI for this yet -- Phase 1's actual operational
need (the two currently-open legacy conversations for one affected
subscriber) is small enough that a CLI a staff member with real admin
access runs is sufficient. Issuing and spending a grant are two separate,
deliberate steps:

```
# 1. Preview -- read-only, prints the live gap for the conversation.
python scripts/support/issue_inbox_completion_override.py \
    --conversation-id <id>

# 2. Issue a single-use grant once the printed gap has been reviewed.
python scripts/support/issue_inbox_completion_override.py \
    --conversation-id <id> --apply \
    --reason-code legacy_cutover_review --reason "<why>" \
    --actor "<staff name/handle, audit label only>" \
    --actor-system-user-id <real SystemUser UUID> \
    --idempotency-key <unique key>

# 3. Spend the grant to actually resolve the conversation, before it
#    expires (default INBOX_COMPLETION_OVERRIDE_GRANT_WINDOW_HOURS=24).
python scripts/support/issue_inbox_completion_override.py \
    --conversation-id <id> --resolve --grant-id <grant id from step 2> \
    --actor-system-user-id <real SystemUser UUID>
```

`--actor-system-user-id` is resolved against real RBAC grants
(`support:inbox:completion_override`, admin-only in Phase 1) for both
`--apply` and `--resolve` -- `--actor` is an audit label only and proves
nothing by itself. `--resolve` additionally requires the acting
`SystemUser` to have a bound Party identity
(`SystemUser.person_party_id`); an unbound staff account is refused rather
than recorded as an anonymous actor. If the grant expires or the
conversation gets new activity/reopens between steps 2 and 3, step 3 is
refused (`override_expired`/`override_superseded`/etc.) and a fresh grant
must be issued -- this is expected, not a failure to work around: expiry
and supersession only ever leave a `pending` slot free for reissue, so
re-running step 2 is always the correct recovery.

### Known Phase-1 gap

Resolved conversations never reopen on new inbound activity: a reply
reopens or creates a new conversation rather than resurrecting the old one.
Of the legacy population, the overwhelming majority are already resolved
and are inert history that will never need an override. A NEW conversation
opened after cutover by a subscriber who is still missing required profile
fields carries no marker and cannot receive an override -- it hits the
strict gate with no exemption. Solving that gap (bulk remediation of the
affected subscribers' data vs. a frozen legacy-data roster) is a genuine
open business decision deferred to Phase 2; Phase 1 is the marker, the
override mechanism, the migration fix, and this reconciliation only.

## Projection freshness and repair

The readiness verdict is transaction-current and is always recomputed from the
snapshotted policy plus canonical records. The UI does not persist a readiness
flag. A missing policy snapshot, missing linked Customer, unsupported historic
field key, or ambiguous identity fails closed and is its own drift signal.
Reopening the drawer or retrying the status command deterministically rebuilds
the verdict; structural Lead-link repair remains with
`communications.conversation_lead_relationships`.
