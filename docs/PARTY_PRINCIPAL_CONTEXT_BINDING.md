# Party Principal and Organization-Context Binding

Status: migrations 353, 527, and 534 provide principal, credential, and session
projection foundations. The reviewed staff credential cohort is projected and
the deploy-1 Party-first reader is live; active legacy session projection and
the null-bridge retirement ratchet remain. Subscriber/reseller adoption, RLS,
and the wider compatibility retirement remain separate programmes.

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

## Migration 527: credential authentication projection

`party.credential_authentication_projection` owns the additive projection from
one `UserCredential` to the Person Party it authenticates, the installed
verifier binding that proves it, and Sub's operator tenant. The command writes
Party, binding, tenant, timestamp, source, and reason together or writes none.
The database CHECK rejects partial state, and the nullable unique constraint on
`(tenant_id, party_id, authentication_binding_id)` rejects a second credential
for the same Party and verifier without affecting untouched legacy rows.

`authentication_bindings.binding_key` and `mechanism_code` are immutable
deployment-global configuration identity; changing either installs a different
binding. PostgreSQL and the ORM both enforce that rule. Its `name` is only a
display label. Mechanism codes are plain strings whose membership is declared by one SOT domain: authorization
declares `local`, network access declares `radius`, and no owner declares `sso`
because Sub implements no SSO verifier. `AuthProvider.sso` is retained only as
a legacy persisted-enum compatibility value during R1; it grants no capability.

The native writer locks the credential, Person Party, binding, and legacy
principal before projecting. It requires:

1. the explicit operator tenant;
2. an active or quarantined Person Party;
3. the legacy principal's reviewed Party link to agree with that Person;
4. an active binding whose declared mechanism matches the credential provider;
5. complete nonblank evidence; and
6. no existing credential with the same tenant–Party–binding tuple.

An exact replay includes source and reason and preserves the original timestamp.
A changed Party, tenant, verifier, or evidence is a repoint and is refused.
Organization-owned Subscriber accounts remain Organization Parties: their
credentials require a separately reviewed human administrator rather than an
ownership-record rewrite.

`credential_convergence_report` deliberately exposes two different ledgers:
legacy principal readiness and completion/correctness of the new projection.
Neither number is allowed to stand in for the other. The report is aggregate
and PII-free.

## Staff authentication shadow evidence

`party.staff_authentication_shadow` owns the read-only comparison between
the retained SystemUser-keyed path and the proposed Party-keyed staff
authentication path. It does not own or change credential, MFA, lockout,
session, SystemUser, or Party state.

The typed report compares the four facts a reader cutover would change:
credential-to-principal resolution, MFA association, credential lockout, and
live database sessions. The operator adapter runs one PostgreSQL
`REPEATABLE READ, READ ONLY` snapshot and emits sorted aggregate JSON with no
identifier or contact value.

Five stable reasons block cutover independently:

- `party_disagreement`: the credential and principal name different Parties;
- `principal_unbound`: an active credential's staff principal has no Person
  Party binding;
- `party_owns_multiple_system_users`: a Party-keyed read would union separate
  staff principals' MFA and session state;
- `principal_holds_multiple_active_credentials`: Party-keyed lockout would
  have to choose between credential rows; and
- `projection_incomplete`: expected adoption debt that the approved executor
  clears.

`party_owns_multiple_system_users` is an invariant-breach sentinel, not an
ordinary population cohort. `uq_system_users_person_party_id` currently makes a
non-zero value structurally unreachable. The migrated-PostgreSQL canary pins
both the exact `UNIQUE (person_party_id)` catalog shape and its enforcement;
the report guard remains so a future lineage change cannot silently turn a
schema guarantee into an unchecked observation. Removing or relaxing that
constraint requires separate adjudication of Party-keyed MFA and session
semantics before any reader cutover.

The report is evidence for a later authorization decision, not that decision
itself. The legacy login path remains authoritative until every cohort is zero,
the credential convergence report is enforcement-ready, the GUC/RLS rehearsal
passes, and the reader cutover is separately reviewed.

Migration 527 performs no population change. Staff and subscriber adoption
remain separate approval-bound work: the existing Subscriber executor cannot
bind SystemUsers, and no command infers identity from email, name, username, or
other contact values.

The first real credential-projection caller is
`scripts/migration/execute_staff_party_credential_adoption.py`. Its public
boundary is fully typed: immutable plan item, plan, approval, phase, outcome,
refusal-code, staff-binding command, and credential-projection command
contracts carry UUIDs, enums, aware timestamps, counts, and SHA-256 values
rather than free-form bags. JSON exists only at the private-file adapter and is
normalized into those contracts before any owner is called.

The plan uses a discriminated typed union. Both actions name the exact
SystemUser, Person Party, credential, authentication binding, decision UUID,
and decision-evidence digest. `project_only` does not request a SystemUser
rebind, but its SystemUser id remains an expected-principal precondition: the
generic projection owner locks the credential and refuses unless its typed
legacy principal kind and id match. This permits one Party to hold credentials
against separate declared bindings without encoding today's one-credential
population as policy. Unknown or action-inapplicable
fields are refused, so a name, email, username, or other inferred identity
input cannot enter the plan unnoticed. The canonical plan digest is independent
of item order, the plan and approval files must be non-symlink mode-`0600`
files, and execution requires all of:

1. the exact plan digest typed on the command line;
2. a SHA-256 binding to the exact plan-file bytes;
3. an attributable approving SystemUser UUID;
4. approval timestamps no more than 24 hours apart;
5. exact principal-binding and credential-projection counts; and
6. an explicit `--execute` acknowledgement.

Every owner command carries the digest of the complete normalized approval,
including approver UUID, approval window, reason digest, plan digest,
plan-file digest, and exact count limits. Consequently a changed approval is
different replay evidence even when it reuses an approval UUID.

For `bind_principal_and_project`, `party.staff_principal_adoption` first
delegates the exact existing-SystemUser link to `party.registry` in one owner
transaction.
The adapter then invokes `party.credential_authentication_projection` in a
separate owner transaction. `project_only` is permitted only for an already
reviewed SystemUser link; the projection command carries `system_user` plus its
exact UUID, and the owner revalidates both under the credential lock. The adapter
does not commit, mutate ORM state, resolve a mechanism by guess, or hold a
batch transaction. A crash between phases leaves a valid staff identity link;
an exact retry replays that phase and resumes the credential projection. Every
item is revalidated against the unexpired approval before each owner call.

## Approved staff-session Party projection

`party.staff_session_projection` is the only writer for the approved legacy
`sessions.party_id` population. Its operator adapter is
`scripts/migration/execute_staff_session_party_projection.py`; it provides a
PII-free readiness report, deterministic private plan generation, and execution
of a separately approved plan.

The plan derives identity only from the deployed
`SystemUser.person_party_id` foreign-key binding. Every item carries the exact
session, SystemUser and Person Party UUID plus a digest of those facts and the
expected active/unrevoked state. Names, email addresses, usernames and token
material are not queried, emitted, or accepted by the strict contract.

Each execution is limited to 1,000 items. The separate approval binds the
normalized plan digest, exact plan-file bytes, attributable user UUID, reason
digest, exact item count, and an expiry window no longer than 24 hours. The
owner then locks Party, SystemUser and session in that order and repeats every
identity, eligibility and conflict check before writing. An exact existing
projection replays; changed state or a different Party refuses. Every applied
row and its audit event commit in the same owner transaction, while an
interrupted batch resumes through exact per-item replay.

Only active, unrevoked staff sessions are in scope. Revoked or non-active
historical null rows are preserved and never guessed or deleted. If the report
finds an active/unrevoked unmappable principal or projection disagreement, plan
generation refuses; remediation belongs to the canonical authentication/session
owner before a new plan is reviewed, not to a second revocation path in the
operator adapter.

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

New staff principals created through `auth.staff_provisioning` now create a
fresh Person Party and delegate the binding to
`bind_system_user_principal` in the same owner transaction. This covers both
ERP HR provisioning and reviewed local staff creation; it never matches an
existing Party by name or email. The explicit local-admin seeder follows the
same fresh-Party rule for bootstrap. Existing staff use the separately
approved UUID-only adoption plan described above. `party.staff_principal_adoption`
locks the reviewed Person Party and SystemUser, delegates the native write to
`bind_system_user_principal`, records PII-free audit evidence, permits only an
exact same-Party replay, and refuses conflicting or incomplete state.

Service-team source retirement does not create or bind Party identity and does
not adopt CRM membership. Existing staff binding remains a separate,
explicitly reviewed identity concern; a team migration is not authority to
match people or grant access.

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

For credentials specifically, cutover additionally requires every credential
to carry a complete projection, zero undeclared-mechanism/provider/Person/tuple
drift cohorts, shadow login parity, and a production-derived rehearsal proving
the tenant GUC and forced-RLS session contract. Migration 527 does not enable
RLS because doing so before that session contract would turn a loud migration
failure into fail-silent empty reads.

The 2026-08-13 predecessor slice installs the operator-tenant GUC on every
PostgreSQL SQLAlchemy root transaction and proves commit/rollback reapplication
plus pool cleanup in the PostgreSQL integration lane. That discharges the
application-side prerequisite only. FORCE RLS still cannot activate until a
later production-shape rehearsal proves synthetic representatives of every
observed role, credential, audit and business-capacity cohort remain visible
and byte-stable through the complete kernel-revision-0001 disposition, and
until this GUC-setting image is already the deployed predecessor. The rehearsal
uses the aggregate-only bundle in
`docs/runbooks/KERNEL_LINEAGE_MINIMIZED_REHEARSAL.md`; a full production data
copy is neither required nor accepted.

Only after parity may legacy person UUID resolution, fake-subscriber principal
fallbacks, duplicated OrganizationMembership decisions, the unused VendorUser
path, or compatibility vendor bridges be retired. Migration 353 alone is not a
cutover and authorizes no production backfill or deployment. Each later
bounded adoption requires its own registered owner, reviewed evidence, and
cutover gate.
