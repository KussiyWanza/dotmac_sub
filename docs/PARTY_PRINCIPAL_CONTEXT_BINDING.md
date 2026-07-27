# Party Principal and Organization-Context Binding

Status: additive schema and command boundary implemented; no data backfill or
runtime authentication/authorization cutover.

## Decision

`party.registry` owns the link from a security or compatibility record to its
canonical identity and organization context. `auth.rbac_catalog` owns role,
permission, and role-policy catalogs; `auth.subscriber_assignments` owns
subscriber grants; and `auth.permission_gate` owns request authorization.
Credential, session, MFA, token, and login services continue to own
authentication state.

The layers are deliberately separate:

| Layer | Canonical fact | Does not imply |
| --- | --- | --- |
| Identity | `SystemUser.person_party_id` or `ResellerUser.person_party_id` references one Person Party | Active login, role, permission, or organization access |
| Context | A linked `PartyMembership` names one Person, one Organization, membership type/status, and bounded scope | Authentication or permission by itself |
| Authentication | Existing credential/session/MFA/token models select an explicit principal | Canonical identity merge or organization authority |
| Authorization | Existing RBAC and permission gates decide allowed actions for the selected principal/context | Permission inheritance from Party role or relationship |

One Person may therefore have a SystemUser principal, a reseller principal, a
vendor context, a customer account, and linked-contact relationships without
duplicating identity or combining permissions implicitly.

## Migration 353

Migration `353_party_principal_context_bindings` is schema-only.

- `system_users` gains nullable `person_party_id` plus binding timestamp,
  source, and reason. One Person Party may own at most one SystemUser.
- `reseller_users` gains nullable `person_party_id`,
  `party_membership_id`, and binding evidence. The Person and membership must
  be populated together. The same Person may have distinct reseller contexts,
  but only one ResellerUser for that Person per reseller.
- `organization_memberships` and `field_vendor_users` gain a nullable
  `party_membership_id` plus binding evidence.
- Each compatibility row maps to at most one canonical PartyMembership and
  each populated binding requires complete, nonblank provenance.
- Native `vendor_users` is unused by runtime vendor authentication and gains no
  new link or authority.

The migration inserts or updates no row. It does not inspect a name, email,
phone, CRM person UUID, vendor string bridge, role label, or active flag to
infer identity or authority.

## Guarded writers

Only the following `app.services.party` commands write these links.

### SystemUser

`bind_system_user_principal` requires an active or quarantined Person Party.
It is idempotent only for the exact target, preserves original evidence, and
refuses duplicate principals and repoints. It does not activate the user,
create credentials, or assign a staff/agent Party role, RBAC role, or direct
permission.

The explicit local-admin bootstrap is the one seeding path that also creates a
Person Party: `scripts.seed.seed_admin` creates a fresh Person Party for an
unbound seeded administrator and delegates the reviewed binding to
`bind_system_user_principal` with operator-bootstrap provenance. It never
matches an existing Party by name or email. Existing, conflicting bindings
remain fail-closed.

### ResellerUser

`bind_reseller_user_principal` atomically records the Person and one existing
`reseller_admin` PartyMembership. The membership must name the same Person and
the Organization Party already bound to the row's Reseller profile. Missing,
partial, conflicting, or duplicate context fails closed.

The command neither creates nor activates the membership and does not change
`ResellerUser.is_active`, credential, MFA, session, token, reseller role,
managed-customer scope, catalog scope, commission, billing, or permission.

### FieldVendorUser

`bind_field_vendor_user_context` links the live vendor auth projection. The
existing FieldVendor-to-Vendor string UUID locates the reviewed organization
profile twin; it is not person identity evidence.

Binding requires:

1. the FieldVendorUser's SystemUser is already bound to a Person Party;
2. Vendor and FieldVendor are already aligned to one Organization Party;
3. one existing `vendor_user` PartyMembership names that Person and
   Organization; and
4. the FieldVendorUser is unbound or already aligned to that exact membership.

Missing profiles, invalid/orphan profile bridges, conflicts, or context
mismatch fail closed. No user, membership, vendor role, portal scope,
credential, token, or permission state changes. The unused native VendorUser
is deliberately not made authoritative.

### OrganizationMembership

`bind_organization_membership_context` links the legacy row to an existing
PartyMembership only when:

- its Organization is already bound to the same Organization Party; and
- legacy `owner`, `admin`, or `member` role agrees with the canonical
  membership type.

The carried CRM `person_id` remains provenance and is not compared to or
rewritten as a native Party UUID. `is_active`, role, and PartyMembership status
remain unchanged.

## Read-only audit

`party.principal_context_audit` and
`scripts/migration/audit_party_principal_contexts.py` report aggregate counts
for:

- bound/unbound SystemUser principals and invalid Person targets;
- reseller Person/membership alignment;
- legacy OrganizationMembership alignment;
- FieldVendorUser profile bridge, SystemUser Person, and membership-context
  debt; and
- installed schema state.

On PostgreSQL the operator script starts `REPEATABLE READ, READ ONLY` and rolls
back. Its output contains no name, email, phone, UUID, legacy person identifier,
binding reason, credential, token, role assignment, or permission.

## Authority migration and cutover gates

Old runtime owners remain unchanged:

- SystemUser/ResellerUser credential, session, MFA, token, and active state;
- SystemUser RBAC roles and direct permissions;
- ResellerUser-to-Reseller portal resolution;
- FieldVendorUser-to-SystemUser and FieldVendor relationships;
- unused native VendorUser rows, which have no runtime consumers;
- OrganizationMembership role and `is_active`; and
- native/field vendor UUID bridges.

The new owner is `party.registry` only for reviewed identity and canonical
context links. The shadow phase may populate those links from protected,
reviewed decisions and compare them through the aggregate audit. It may not
change login resolution.

Runtime cutover requires all of the following:

1. complete provenance for every in-scope link and no partial/conflicting
   binding;
2. zero in-scope reseller, organization-membership, FieldVendorUser, and
   SystemUser context debt;
3. explicit tests proving one selected principal/context at a time and no
   permission union across staff, reseller, vendor, subscriber, or contact
   identities;
4. parity tests for active/invited/suspended/ended membership state against
   existing login and authorization behavior;
5. credential, MFA, session, token, impersonation, managed-customer, vendor,
   and multi-organization portal tests;
6. migrated runtime readers with fail-closed handling for missing context; and
7. a documented rollback window before compatibility readers or bridges are
   retired.

Only after parity may legacy person UUID resolution, fake-subscriber principal
fallbacks, duplicated OrganizationMembership decisions, the unused VendorUser
path, or compatibility vendor bridges be retired. Migration 353 alone is not a
cutover and authorizes no production backfill or deployment.
