# Party, Role, and Relationship Source of Truth

**Status:** Approved; foundation through additive customer lifecycle implemented locally  
**Decision owner:** Michael  
**Decision date:** 2026-07-17  
**System of record:** Sub

## Decision

Sub owns one native identity for every real-world person or organization. A
party may hold several concurrent business roles and relationships. Subscriber,
reseller, vendor, partner, staff/agent, contact, and login records are not
independent identities.

CRM has no runtime role in party identity or lifecycle. Legacy CRM identifiers
may be retained only as import provenance in `party_external_references`; they
are never lookup authority, identity, a fallback, or permission evidence.

This decision is implemented incrementally. Migration 349 creates the native
foundation, migration 350 adds the nullable, provenance-bound Subscriber to
Party link, migration 351 adds PII-free backfill execution receipts, and
Migration 352 adds nullable Organization/Reseller/Vendor/FieldVendor Party
bindings, migration 353 adds Person-principal and membership-context bindings,
and migration 354 adds reviewed SubscriberContact and Inbox contact-point
projections. Migration 355 adds Party-first Leads, immutable origin capture,
and downstream account-alignment guards. None
of these migrations switches current subscriber, reseller, vendor,
organization, Team Inbox, or authentication reads. Each domain receives a
separate backfill, verification, cutover, and compatibility-path retirement.

## Vocabulary and ownership

| Concept | Meaning | Canonical owner |
| --- | --- | --- |
| Party | One real-world person or organization | `party.registry` |
| Role | A concurrent business role held by a party | `party.registry` |
| Relationship | A directional fact between two parties; never authorization | `party.registry` |
| Membership | A person's explicit organization context and bounded access scope | `party.registry` |
| Contact point | Reachability and verification/consent evidence | `party.registry` |
| Principal/credential | Authentication mechanism linked to a person | `auth.*` |
| Account/domain object | Lead, subscriber account, subscription, invoice, ticket, project, etc. | Its named domain owner |

`app.services.party` is the only native writer for the foundation records.
Routes, imports, webhooks, jobs, portals, and future backfills call that owner.
They do not write these tables independently.

## Core invariants

1. One real-world person or organization has one active canonical Party.
2. `party_type` is immutable after creation. Person/organization correction is
   a reviewed replacement/merge, not an in-place type flip.
3. Email, phone, WhatsApp number, or social handle alone is not identity proof.
   Shared contact details are legitimate, especially for reseller-managed
   customers.
4. A merged Party points at its canonical replacement and cannot remain active.
   The complete merge/repoint command is intentionally deferred until every
   target domain has a declared reconciler; migration 349 does not enable a
   partial merge.
5. Roles are concurrent and independently suspended or ended. There is no
   single global role or lifecycle status.
6. Relationships are directional. `A owner_of B` is stored once; callers use
   the declared direction rather than maintaining inverse duplicates.
7. Relationships never grant permissions. Only an explicit active Membership,
   an explicit bounded `access_scope`, and the authorization owner may grant
   access.
8. A contact point belongs to a Party. Verification and marketing consent are
   separate facts. Inbox routing is a projection over these facts.
9. Social contact identity is scoped by provider, connected provider account,
   and immutable provider subject ID. A display handle is not enough.
10. External references are provenance only. Losing an external system cannot
    make native identity unresolvable.

## Reseller versus partner

A **reseller** is a specific commercial distribution role. After explicit
onboarding and domain authorization, a reseller may receive:

- a managed-customer scope;
- assigned catalog/offers and pricing boundaries;
- commission or margin policy;
- collection and consolidated-billing responsibilities;
- reseller portal memberships and branding.

A **partner** is a collaboration agreement that does not itself sell or manage
Dotmac subscriber service. Every partner role requires one explicit type:

- `referral`: introduces prospects under a referral agreement;
- `technology`: provides or integrates technology;
- `infrastructure`: shares or provides infrastructure under contract;
- `strategic`: another reviewed strategic collaboration.

Partner roles grant no customer ownership, catalog/pricing authority,
commission, collection, billing, or portal permission by default. A generic
`partner` role is forbidden because it would become an ambiguous permission
shortcut.

A reseller is a partner in ordinary language, but it is stored only as the
specific `reseller` role unless the same organization also has a separate
partner agreement. For example, an organization may concurrently hold:

```text
reseller:default          active
vendor:default            active
partner:infrastructure    active
```

Each role has its own status and domain profile. Suspending the vendor role
must not suspend reseller operations. Reseller receivables/commission and
vendor payables remain separate financial positions even when the legal party
is the same.

## Person and organization relationships

Examples of controlled directional relationships include:

```text
Jane Doe --owner_of-----------------> ABC Networks Ltd
Jane Doe --billing_contact_for------> Customer Organization
Jane Doe --technical_contact_for----> Customer Organization
Jane Doe --employee_of--------------> ABC Networks Ltd
Dotmac   --account_manager_for------> Customer Organization
Customer --referred_by--------------> Referral Partner
Parent Co --parent_of---------------> Subsidiary Co
```

`party_relationships` describes these business facts. Where Jane can log in or
act for ABC Networks, `party_memberships` separately records her organization
context, membership type, status, and bounded access scope.

One person may therefore be a Dotmac agent, a vendor representative, a reseller
administrator, a linked billing contact, and a personal subscriber without
duplicate identity. Authentication must select an explicit principal/context;
permissions from those contexts never combine implicitly.

## Foundation tables

### `parties`

Native identity, party type, display name, active/quarantine/merge/archive
state, production/test/import classification, and merge target.

### `party_roles`

Concurrent roles and independent role lifecycle. `role_key=default` is required
for all roles except partner; partner requires one controlled agreement type.

### `party_relationships`

Directional descriptive relationships. They carry provenance and effective
dates but no authority scope.

### `party_memberships`

Person-to-organization membership and explicit access scope. The command owner
verifies that the endpoints are a Person Party and an Organization Party.

### `party_contact_points`

Normalized reachability, provider/account scope, immutable external subject ID,
verification, consent, primary designation, and provenance. Normalized values
are deliberately not globally unique.

### `party_external_references`

Unique external source/entity identifiers for migration and reconciliation.
No credential, secret, or identity decision belongs here.

## Subscriber account binding

`Subscriber` remains the service and billing account owned by its account,
subscription, access, and billing services. `Subscriber.party_id` identifies
the Person or Organization Party that owns that account; it does not move
account-state authority into `party.registry`.

The binding contract is deliberately narrow:

1. one Party may own several subscriber accounts, so `party_id` is not unique;
2. every populated link requires `party_bound_at`, `party_binding_source`, and
   `party_binding_reason` evidence;
3. `party.bind_subscriber_account` is the only writer and is idempotent for an
   exact retry;
4. a different existing target is refused until the reviewed merge/repoint
   command and all domain reconcilers exist;
5. active and quarantined Parties may retain account bindings, but merged or
   archived Parties cannot receive new ones; and
6. binding assigns no role, permission, contact point, account status,
   subscription state, billing state, access state, or authentication context.

Migration 350 leaves every existing row unbound. It does not infer Person
versus Organization from `company_name`, copy legacy contacts, or activate a
subscriber role. The reviewed identity worklist must decide those facts in a
later backfill slice.

### Admin single-account binding repair page

The customer-detail **Repair Party Binding** action is a narrow exception for
one currently unbound account. Its audience is an administrator resolving a
specific blocked customer workflow, not a bulk identity-cleanup operator.
`party.subscriber_binding_repair` owns the typed read context, action
eligibility, validation, transaction, and audit evidence; the route and form
only render that contract and submit one typed command.

The first viewport identifies the exact customer, states that the account is
unbound, explains the non-effects (no lifecycle, billing, contact, access, or
permission change), and offers one primary action. The reviewer either enters
an exact existing Party UUID or explicitly supplies a Party type and display
name to create a new Party. The UI never matches by name, email, phone, or
other contact data. Both paths require review evidence and an explicit
confirmation. A bound account renders the reason the action is unavailable.

The command locks canonical rows, calls `party.registry` for every native
write, stages PII-free audit metadata, and refuses a repoint. A different
existing Party remains a merge/repoint decision until that separately reviewed
workflow and its domain reconcilers exist.

## Current structures and target links

| Current structure | Target relationship | Migration state |
| --- | --- | --- |
| `Subscriber` | Customer/service account linked to Person or Organization Party | Nullable binding implemented; classification/backfill and read cutover pending |
| `SubscriberContact` | Person relationship or quarantined unresolved contact candidate | Nullable Person, relationship, and per-field contact-point projections implemented; review/backfill and read cutover pending |
| `Organization` | Organization profile linked one-to-one to Organization Party | Nullable binding implemented; backfill and role/read cutover pending |
| `Reseller` | Reseller commercial profile for Organization Party | Nullable binding implemented; role parity and read cutover pending |
| `Vendor` | Vendor commercial profile for Organization Party | Paired nullable binding implemented; role parity and read cutover pending |
| `FieldVendor` | Vendor auth projection linked to the same Organization Party as Vendor | Paired nullable binding implemented; membership cutover and string-bridge retirement pending |
| `ResellerUser` | Person auth principal plus explicit reseller membership context | Nullable Person/membership binding implemented; backfill and auth cutover pending |
| `FieldVendorUser` | SystemUser principal projected into an explicit vendor membership context | Nullable membership binding implemented; backfill and auth cutover pending |
| `VendorUser` | Unused imported legacy membership path | No new authority; retirement pending |
| `SystemUser` | Authentication/staff profile linked one-to-one to Person Party | Nullable binding implemented; backfill and auth cutover pending |
| `OrganizationMembership` | Legacy membership projected onto a native PartyMembership | Nullable binding implemented; backfill and read cutover pending |
| `InboxContactLink` | Rebuildable routing link to canonical contact point/Party | Nullable canonical point projection implemented; shadow parity and routing read cutover pending |
| `Lead` | Party-first sales opportunity with optional reviewed Subscriber account context | Nullable Party/account binding and origin capture implemented; legacy classification and read/report cutover pending |
| `LeadOriginCapture` | Immutable structured acquisition evidence | Implemented for new canonical writes; capture adapters and legacy review pending |
| `Quote` / `SalesOrder` | Account-specific commercial offer and order | Party/account alignment guards implemented; owners unchanged |
| `Subscription` | Service lifecycle linked through Subscriber | Owner unchanged; aggregate Party-link coverage audited |
| `Ticket` | Pre-sales or customer support case | Lead FK and Party/account alignment guard implemented; legacy FK validation pending |

No current adapter may pretend that the additive foundation has completed one
of these cutovers.

## Identity cleanup and merge policy

The 15,291 subscriber-table rows are a mixed population, not an active
subscriber count. Cleanup produces separate cohorts: canonical parties,
verified contacts, prospects/leads, customer accounts, active subscribers,
churned/suspended accounts, test/quarantined records, unresolved contacts, and
suspected duplicates.

Matching evidence is ranked, retained, and reviewable. Automatic merges require
a domain-approved high-confidence identity key. Shared email, phone, address,
name, reseller owner, or social handle never suffices on its own. Ambiguous
records are quarantined; they are not deleted or silently attached.

Merges retain redirects and audit evidence. Every domain reconciler must repoint
its foreign keys before the source Party becomes `merged`. Hard deletion is not
a cleanup mechanism.

## Migration sequence

1. **Foundation (implemented):** SOT, registry, native tables, command boundary,
   constraints, and tests. No domain reads or cutovers.
2. **Identity audit (implemented read-only):** evidence model, lifecycle and
   non-production classification, candidate clustering, and private dry-run
   worklists. See `docs/PARTY_IDENTITY_CLEANUP_AUDIT.md`.
3. **Subscriber binding (implemented additive):** nullable FK, required binding
   provenance, idempotent exact binding, and refusal of unreviewed repoints. No
   rows are backfilled and no existing reads change.
4. **Identity adjudication/backfill:** the reviewed, digest-bound planner and
   separately approved guarded executor are implemented. Migration 351 adds a
   PII-free execution receipt; no production data is migrated by the schema.
   A fresh audit, protected review, expiring approval, explicit execution
   authorization, and deployment remain pending. Merge/repoint commands remain
   deliberately absent. See `docs/PARTY_IDENTITY_ADJUDICATION_PLAN.md` and
   `docs/PARTY_IDENTITY_BACKFILL_EXECUTION.md`.
5. **Organizations and roles (additive binding implemented):** `Organization`,
   `Reseller`, `Vendor`, and `FieldVendor` now have nullable, provenance-bound,
   one-to-one links to an Organization Party. The Vendor/FieldVendor pair binds
   atomically and one Party may carry concurrent reseller, vendor, and typed
   partner roles. Backfill, runtime read cutover, role parity, and retirement of
   `Organization.account_type` and the vendor string bridge remain pending. See
   `docs/PARTY_ORGANIZATION_PROFILE_BINDING.md`.
6. **People and principals (additive binding implemented):** `SystemUser` and
   `ResellerUser` now have nullable, provenance-bound Person links.
   `ResellerUser`, `OrganizationMembership`, and `FieldVendorUser` bind only to
   an explicit, matching PartyMembership. The unused native `VendorUser` is not
   wired into the new boundary. Backfill, parity, runtime cutover, and legacy
   principal/context retirement remain pending. See
   `docs/PARTY_PRINCIPAL_CONTEXT_BINDING.md`.
7. **Contacts and Inbox (additive projection implemented):** reviewed
   `SubscriberContact` rows can bind to a Person Party, a directional contact
   relationship, and individual canonical contact points. Existing Inbox routes
   can bind to a matching canonical point through the Team Inbox owner. No
   identity is inferred, no route/read path changes, and verification, consent,
   authorization, notifications, billing, and subscription state stay with
   their existing owners. Backfill, shadow parity, reader cutover, and legacy
   field retirement remain pending. See
   `docs/PARTY_CONTACT_INBOX_PROJECTION.md`.
8. **Customer lifecycle (additive foundation implemented):** Leads can now
   identify a reviewed Party before a Subscriber exists; immutable structured
   origin distinguishes native Sub campaign responses from external ad-provider
   IDs. Quote, Sales Order, and Ticket commands enforce Party/account alignment
   while Subscription, billing, access, and support owners retain their state.
   The PII-free audit measures legacy debt; capture adapters, backfill, deferred
   FK validation, reporting cutover, and compatibility retirement remain
   pending. See `docs/PARTY_CUSTOMER_LIFECYCLE.md`.
9. **Cleanup:** retire legacy UUID resolution, single-valued organization role
   decisions, fake-subscriber principals, vendor twins, and parallel writers.

Every cutover records old owner, new owner, backfill evidence, shadow/parity
gate, rollback boundary, and compatibility-path retirement.
