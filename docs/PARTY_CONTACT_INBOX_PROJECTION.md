# Party Contact and Team Inbox Projection

**Status:** Approved additive schema and command boundary implemented locally  
**Decision owner:** Michael  
**Decision date:** 2026-07-17  
**Systems of record:** `party.registry` for identity/contact facts;
`communications.team_inbox_contact_resolution` for reviewed Inbox contact links;
`communications.team_inbox_routing` for route lifecycle

## Outcome

Migration 354 gives a reviewed `SubscriberContact` row an optional Person Party
binding, then records exactly which canonical relationship and contact points
represent its legacy fields. It also lets an existing `InboxContactLink` point
to reviewed canonical reachability without changing how Inbox currently routes
or resolves a conversation.

This remains an additive reviewed projection rather than a data backfill.
Inbox customer-history reads may use an active reviewed contact-point binding
to group conversations for the exact Party. Existing legacy contact reads,
Inbox routing and thread admission, account state, subscription state,
billing-block enforcement, authentication, authorization, notification
eligibility, verification, and consent remain unchanged.

## Authority boundary

| Fact or decision | Canonical owner after migration 354 | Current-runtime position |
| --- | --- | --- |
| Person/Organization identity | `party.registry` | Additive Party foundation; reviewed bindings only |
| `SubscriberContact` to Person Party binding | `party.registry` | Nullable shadow link; legacy contact reads remain |
| Contact's descriptive relationship to subscriber Party | `party.registry` | Reviewed projection; never grants permission |
| Canonical contact-point value, provider scope, verification, and consent | `party.registry` | Existing Party fact; no legacy flag is copied into it |
| Legacy contact field to canonical contact-point evidence | `party.registry` | Nullable projection rows; one row per reviewed source field |
| Inbox channel/normalized-contact target and active-route lifecycle | `communications.team_inbox_routing` | Existing runtime authority remains unchanged |
| Inbox route to canonical contact point | `communications.team_inbox_contact_resolution` | Nullable reviewed projection; read by customer history, not routing or thread admission |
| `SubscriberContact.is_authorized` and login/access decisions | Existing customer/auth owners | A relationship or contact point never grants access |

Party code cannot write an Inbox row. Team Inbox validates canonical Party
facts but remains the single writer for its routing projection. This prevents a
second decision path while allowing the projection to be rebuilt from Party
facts and the authoritative Inbox route.

## Schema contract

Migration `354_party_contact_inbox_projections` is schema-only.

### Subscriber contact Person binding

`subscriber_contacts.person_party_id` is nullable and every populated link
requires `party_bound_at`, `party_binding_source`, and `party_binding_reason`.
The target must be a reviewed Person Party and the owning Subscriber must
already have a reviewed Party binding. The same Person cannot be attached to
two legacy contact rows for one subscriber account. Exact retries retain the
original evidence; a different target requires the future reviewed
merge/repoint workflow.

The binding does not copy a name, email, phone, social value, relationship
label, notes, authorization flag, billing-contact flag, or notification flag.

### Relationship projection

`subscriber_contact_relationship_projections` links one legacy contact row to
an existing `PartyRelationship`. The relationship must:

- start at the contact's reviewed Person Party;
- end at the owning Subscriber's reviewed Party;
- use `contact_for`, `billing_contact_for`, `technical_contact_for`, or
  `emergency_contact_for`; and
- be pending or active when first projected.

The projection is evidence, not authorization. `is_authorized`, membership,
RBAC, portal scope, and credentials do not derive from it.

### Contact-point projection

`subscriber_contact_point_projections` records one reviewed mapping per legacy
source field:

| Legacy field | Canonical channel |
| --- | --- |
| `email` | `email` |
| `phone` | `phone` |
| `whatsapp` | `whatsapp` |
| `facebook` | `facebook_messenger` |
| `instagram` | `instagram_dm` |
| `x_handle` | `x` |
| `telegram` | `telegram` |
| `linkedin` | `linkedin` |

The canonical point must be active, belong to the contact's Person Party, have
the expected channel, and match the normalized legacy field. Social points
also require provider, connected provider account, and immutable external
subject identity. A display handle may support a reviewed projection but can
never create identity by itself. Arbitrary `other_social` text is deliberately
unsupported and remains audit/review debt.

Verification and consent stay on `PartyContactPoint`. The projection does not
infer them from `receives_notifications`, `is_authorized`, or any legacy field.

### Inbox contact-point projection

`inbox_contact_links.party_contact_point_id` is nullable and every populated
link requires timestamp, source, and reason evidence. The Team Inbox binder
accepts only these current mappings:

| Inbox channel | Canonical channel |
| --- | --- |
| `email` | `email` |
| `whatsapp` | `whatsapp` |
| `facebook_messenger` | `facebook_messenger` |
| `instagram_dm` | `instagram_dm` |

The point must be active, have a routable Party, match the Inbox normalized
contact, and carry immutable provider scope for social channels. The existing
subscriber or reseller target must already have a routable reviewed Party. If
the point belongs to a linked Person rather than directly to the target Party,
an active controlled contact relationship must connect that Person to the
target.

`chat_widget` and `note` have no canonical contact-point mapping in this slice.
They remain explicit unsupported-channel counts; no opaque value is guessed
into a person. Binding preserves `subscriber_id`/`reseller_id`, `is_active`,
`source`, conversation state, and all current resolution behavior.

## Read-only audit

`scripts/migration/audit_party_contact_inbox.py` calls
`party.contact_inbox_audit` in a PostgreSQL repeatable-read, read-only
transaction and rolls back. Its JSON contains schema state and aggregate counts
only. It reports:

- bound, unbound, incomplete, and misaligned SubscriberContact Person links;
- missing or misaligned relationship projections;
- populated legacy fields, per-field projection coverage, unsupported
  `other_social` rows, and contact-point alignment debt;
- aggregate canonical verification/consent status and social-scope debt; and
- active/unbound or misaligned Inbox contact-point projections.

It emits no names, addresses, phone numbers, social identifiers, UUIDs, notes,
evidence text, credentials, permissions, verification details, or consent
details. It cannot create, bind, merge, repoint, route, verify, opt in/out, or
change authentication/authorization.

## Migration and cutover gates

1. **Install schema:** apply migration 354. It adds nullable columns/tables and
   writes no business rows.
2. **Measure:** run the read-only aggregate audit and retain the report as the
   baseline. Production execution requires Michael to explicitly name the host
   and authorize the operation.
3. **Adjudicate:** privately review contacts. Resolve duplicates and shared
   details; classify unsupported/test/unresolved rows. Never merge from a
   shared value alone.
4. **Bind Party facts:** use `party.registry` commands to bind the Person and
   separately reviewed relationship/contact points. Record evidence for every
   projection.
5. **Shadow Inbox:** use the Team Inbox binder for supported existing routes.
   It must neither create nor retarget a route.
6. **Prove parity:** for the approved cohort, demonstrate stable Party targets,
   channel/value agreement, social provider scope, no supported active route
   unexpectedly unbound, and identical current-versus-shadow resolution.
   Verification, consent, authorization, notification, billing, subscription,
   and Inbox lifecycle regressions must remain green.
7. **Cut over readers:** switch one named reader through the owning service in
   a separate change with observability and a rollback flag. The presence of a
   projection alone never changes reader precedence.
8. **Retire compatibility paths:** only after a sustained parity window and an
   explicit decision. Preserve provenance and do not hard-delete unresolved
   identity evidence as cleanup.

Before reader cutover, rollback means disabling the shadow population process;
all existing reads remain intact. After reader cutover, application rollback
must precede any schema rollback. The nullable projection data is retained so
the owner can diagnose and repair drift.
