# Catalog offer-version access-requirement authority

**Status:** Release 1 (2026-09-13). Release 2 is planned, later work — not
built by this change.

## Owner

`service_intent.offer_access_requirement` (`app/services/catalog/offer_access_requirement.py`)
owns exactly:

- access-classified offer-version admission
- the immutable access requirement for an exact offer version
- reviewed classification of legacy/unclassified versions
- mutation of an already-admitted offer version

It is a new, complete, fully-contracted `ServiceContract`
(`app/services/sot_registry/domains/service_intent_control_plane.py`), not an
expansion of `service_intent.catalog_policy`.

## Why a new owner, not an expansion of `service_intent.catalog_policy`

`service_intent.catalog_policy` (`app/services/catalog/policies.py`) owns
catalog policy lookup and offer policy interpretation — policy-set and
dunning-step CRUD. It has nothing to do with whether an offer version
requires last-mile network access. Folding this concern into that owner would
either fake compliance (declaring a concern that module doesn't actually
enforce) or force an unrelated scope expansion onto an owner whose contract,
tests, and callers already exist for a different purpose.

**`service_intent.catalog_policy` and `app/services/catalog/policies.py` are
deliberately left completely untouched by this change.** This is a permanent
design decision, not an oversight: the two concerns will keep evolving
independently, and conflating them would make either change unreviewable in
isolation.

## What changed (Release 1)

- New column `offer_versions.access_requirement`: a 3-value enum
  (`network_access` | `no_network_access` | `unclassified`), non-null, with a
  temporary server default of `unclassified` used ONLY to initialize
  historical rows in the migration's own DDL. There is no heuristic or
  automatic backfill — every existing row simply becomes `unclassified`.
- `OfferVersions.create` (`app/services/catalog/offers.py`) is a THIN
  ADAPTER: it builds an `AdmitOfferVersionCommand` and calls
  `offer_access_requirement.admit_offer_version`, which is the actual and
  only writer — offer lookup, catalog-default resolution, access-requirement
  admission validation, the `OfferVersion` INSERT, and the
  billing-governance audit participant all run inside ONE
  `execute_owner_command` boundary owned by the new module. The adapter does
  not construct the row itself. `unclassified` remains an accepted explicit
  value in Release 1 — the server-side default exists only for historical
  migration, never as an application-level fallback for a new row.
- New Offer audit calls carry `AuditActor` directly through the
  billing-governance participant. Existing scalar callers remain on that
  participant's separate retirement path; Offer does not add another scalar
  audit-actor caller. The audit enum has no machine-credential member, so an
  authenticated machine credential retains its `api_key` actor class and
  exact credential id instead of falling back to an anonymous `system` actor.
- The field is immutable outside the reviewed classification command:
  `OfferVersionUpdate` has no `access_requirement` field, and
  `OfferVersions.update` calls
  `offer_access_requirement.assert_access_requirement_immutable` as a
  defense-in-depth guard against a future edit reintroducing it there.
- A read-only, deterministic worklist
  (`offer_access_requirement.list_unclassified_offer_versions`, surfaced by
  `scripts/catalog/classify_offer_access_requirement.py` with no arguments)
  reports every remaining `unclassified` row, paginated, instead of raising
  one `AdminAlert` per row.
- `unclassified` is never read as, defaulted to, or treated like PPPoE or any
  connection-type fallback anywhere in the codebase — see the architecture
  guard below.
- Admission authorization has ONE decision owner:
  `offer_access_requirement.authorize_offer_version_admission`. It decides
  the compound `catalog:write AND (catalog:billing_write OR
  catalog:offer_version:admission)` rule AND the ERP staff leave-write
  restriction (`app/services/erp_staff_access.py`), never just the leaf
  permission grant. TWO call sites delegate to it rather than each deciding
  independently: `app/api/catalog.py`'s `_require_offer_version_admission`
  route dependency, and the command's own `verify_admission_authorization`
  (called from `_admit`, inside its transaction, twice — once after the
  advisory locks are acquired, once again immediately before the INSERT).
  Because both delegate to the same function, there is exactly one
  implementation of the rule to keep correct, not two that can silently
  disagree.
  The two admission routes are mounted on their OWN router
  (`app/api/catalog.py`'s `admission_router`), deliberately carrying NO
  blanket router-level dependency (round 12 finding 2 fix). Every OTHER
  mutating route in `app/api/catalog.py` (offers, offer-prices,
  add-on-prices, ...) still sits under `router`'s own pre-existing,
  admission-unrelated `catalog:write` gate
  (`require_method_permission("catalog:read", "catalog:write")`), whose
  `require_permission` independently applies the same `erp_staff_access`
  leave-restriction check via older, separate plumbing (a bare-string
  HTTPException detail, and an audit write it commits inline rather than
  staging) — that pre-existing mechanism is untouched and still correct for
  those routes, and it cannot produce a DIFFERENT verdict from the owner's
  own check (both call the identical `erp_staff_access.staff_write_
  restricted`). It simply never runs at all for `POST`/`PATCH
  /offer-versions`, because those two routes are exempt from that blanket
  gate: `_require_offer_version_admission` is their ONLY dependency, so the
  owner is the one and only decision-maker on both the route and the direct
  command, with nothing upstream that could refuse first via different
  plumbing.
  `AdmitOfferVersionCommand` takes a REQUIRED, typed `AdmissionPrincipal`
  (`StaffPrincipal` | `ApiKeyPrincipal` | `SubscriberPrincipal` |
  `MachineCredentialPrincipal` | `SystemAdmission`), validated at
  construction (`__post_init__`) to actually be one of those five. It is
  recorded for audit/attribution AND (for every member except
  `SystemAdmission` and, pending a separate migration, `Machine
  CredentialPrincipal`) re-verified against RBAC inside the command.
  `catalog:offer_
  version:admission` (`alembic/versions/610_offer_version_admission_
  permission.py`) is a genuine, narrower, OPT-IN alternative to `catalog:
  billing_write` — a caller holding either (in addition to `catalog:write`)
  satisfies the route — so the migration seeds only the permission row
  (mirroring 609's pattern exactly) and copies no grants: there is no
  existing-caller regression to prevent, because nobody's existing
  `catalog:billing_write` access is narrowed or removed.
  `app/api/catalog.py`'s `_admission_principal` resolves the attributable
  principal on BOTH routes (POST and PATCH, identically) to an authenticated
  `system_user`, `api_key`, OR `subscriber`. A subscriber principal IS a
  real, supported way to reach this route: a subscriber can be mapped to the
  `admin` role (or any role holding the compound admission permission) via
  the seeded role-assignment path (`scripts/seed/seed_rbac.py`,
  `app/services/subscriber_assignments.py`,
  `app/services/auth_flow.py`'s login role resolution) — production usage of
  that path is unverified, but it is not theoretical, and this resolver does
  not silently 403 it. Retiring that access is a deliberate, separate
  census/migration, not something implemented here.
  A kernel machine credential and a legacy local API key both authenticate
  as `principal_type == "api_key"` (backward compatible with every existing
  permission check); `credential_kind` on the auth dict
  (`"machine"`/`"legacy_api_key"`, stamped by `auth_dependencies._machine_
  principal`/`_api_key_principal`) is the only signal that tells them apart,
  and `_admission_principal` reads it to resolve `MachineCredentialPrincipal`
  versus `ApiKeyPrincipal`. `MachineCredentialPrincipal` is the exact,
  non-growing compatibility path, and SHADOW/WOULD-REFUSE MODE IS ITS
  TERMINAL STATE ON THIS BRANCH, not a staging step toward an enforcement
  flag: a cross-repository census established that the published Kernel
  Sub actually depends on (`dotmac-kernel==0.1.0a94`) cannot supply a
  verified machine principal carrying the identity, attribution, scope,
  expiry, and revocation evidence real enforcement would require, so
  `authorize_offer_version_admission` evaluates and logs this decision but
  never refuses on it — hard-enforcing with what this module can actually
  see today would be an uncensused, silent access retirement of a caller
  `origin/main` always authorized. Enforcement waits for a published Kernel
  successor contract (recorded in Knowledge:
  `dotmac-kernel-verified-machine-authentication-successor-contract`); it
  is not scheduled by an inventory or a scope migration this repository
  controls.
  `SystemAdmission` (an admission with no authenticated end-user context at
  all) has NO production construction site at all: `OfferVersions.create`'s
  `actor_id`/`actor_type` resolution FAILS CLOSED (raises a typed
  `OfferAccessRequirementError`) for any combination it doesn't recognize as
  `system_user`/`api_key`/`subscriber`, instead of silently defaulting to
  `SystemAdmission`; an internal/test caller that genuinely has no
  authenticated actor must construct `SystemAdmission(reason=...)` and pass
  it explicitly via the distinct `principal=` argument. This is proven by an
  AST-based (real `ast.Call` node inspection, not a substring search),
  test-enforced allowlist guard with its own planted-leak and near-miss
  sensitivity proofs (`tests/architecture/
  test_offer_access_requirement_boundary.py`) — a build-time/reviewed-call-
  site guarantee, not an unforgeable runtime credential.
- `(offer_id, version_number)` is enforced as a real DB-level unique
  constraint (`uq_offer_versions_offer_id_version_number`,
  `alembic/versions/611_offer_versions_unique_version_number.py`), not only
  by the advisory lock and pre-insert check. The pair is also immutable
  after admission: `OfferVersionUpdate` has neither field, and
  `OfferVersions.update` asserts this defense-in-depth, the same pattern as
  `access_requirement`'s own exclusion below.
- Admission accepts an optional `Idempotency-Key` header
  (`POST /offer-versions`), recorded in the shared `idempotency_keys` ledger
  (scope `offer_version_admission`): a retried admission that reuses the
  same key and the same request returns the original row instead of a
  `duplicate_version_number` conflict. A request with no header is not
  idempotent.

## Reviewed classification command

`offer_access_requirement.classify_offer_version_access_requirement` is the
only way an `unclassified` row becomes `network_access` or
`no_network_access`. It runs inside `execute_owner_command`
(`app/services/owner_commands.py`) — one root transaction, verified against
this module's `ServiceContract`.

- **Preview** binds the offer version id, its current classification, the
  proposed classification, the row's `updated_at`, and the review reference
  into a SHA-256 fingerprint. If this exact transition (offer version ->
  proposed target) was already recorded, the preview reflects that recorded
  transition and its STORED fingerprint (`already_applied=True`) instead of
  raising — this is what lets a genuine retry (same idempotency key, same
  inputs) reach the command's replay branch rather than being refused before
  it ever tries. A version already classified to a DIFFERENT target, or
  classified with no recorded row at all (e.g. admitted directly with a real
  value), still previews as a refusal.
- **Apply** requires the exact fingerprint, the authenticated principal's
  `SystemUser` id, a reason, a review reference, an idempotency key, and
  explicit confirmation (`--confirm` on the CLI).
- **Identity is never a free-text argument.** The CLI has no separate
  `--actor` field. The string recorded as `classified_by`, the audit actor,
  the event actor, and `authenticated_principal` is always
  `principal_label(authorized_system_user_id)` — derived server-side from
  the id RBAC actually verified, never from anything the caller can type.
- **Permission is re-verified fresh, inside the command's own transaction**
  (`_verify_classify_permission`), not trusted from an earlier, separately
  computed boolean. A grant revoked between an operator's preview and their
  apply is caught here. This NARROWS but does not fully ELIMINATE the
  look-then-act window: no RBAC row (`system_users`, `roles`,
  `role_permissions`, `permissions`) is locked, so a revocation committed in
  the instant between this re-check and the write's commit is not observed.
  This CLI's trust model is host/container shell access, not RBAC alone (see
  "RBAC" below) — closing that residual window would require row-locking
  the entire RBAC surface across five-plus tables, judged disproportionate
  to a trust-the-operator CLI boundary with no pre-authorizing route.
- **Refusals:** a stale preview (fingerprint mismatch), a missing offer
  version, a proposed target of `unclassified`, an idempotency key reused
  with different command inputs (`idempotency_conflict` — a typed error, not
  a raw unique-constraint violation), and any real-to-real or
  real-to-unclassified change — all fail closed.
- **Idempotent replay:** at most one row of
  `offer_access_requirement_classifications` ever exists per offer version,
  and `idempotency_key` is globally unique. An exact replay requires the SAME
  idempotency key AND matching offer version, target, preview fingerprint,
  reason, and authenticated principal — matching only some of those is a
  typed `idempotency_conflict`, not a replay.
- **Evidence:** one audit event (`stage_audit_event`, action
  `offer_access_requirement_classified`) and one versioned domain event
  (`EventType.catalog_offer_access_requirement_classified`) are staged in the
  same transaction, carrying bounded identifiers, the old/new value, the
  command/correlation ids, the review reference, and the authenticated
  principal.

## Update command

`offer_access_requirement.update_offer_version` (command
`UpdateOfferVersionCommand`, `OwnerCommandDefinition` name
`update_offer_version`, concern `mutation of an already-admitted offer
version`) is the ONE path that mutates an already-admitted `OfferVersion`
row's fields. `OfferVersions.update` (`app/services/catalog/offers.py`) is a
THIN ADAPTER over it — it builds the command and reads the result; it does
not mutate the row or complete the transaction itself.

- **One transaction, one owner.** Lookup, read-only validation, the
  in-transaction authorization recheck, the mutation, and the
  billing-governance audit participant all run inside ONE
  `execute_owner_command` boundary owned by this module (round 16 — before
  this, `OfferVersions.update` called `db.commit()` directly, so a caller
  with unrelated pending work in the same session had that work silently
  committed alongside the version update; `execute_owner_command` now
  refuses to run at all with a pending caller transaction instead).
- **Admission authority, re-verified, not assumed.** `UpdateOfferVersionCommand.principal`
  is a REQUIRED, typed `AdmissionPrincipal` — the same closed union
  admission uses (`StaffPrincipal` | `ApiKeyPrincipal` |
  `SubscriberPrincipal` | `MachineCredentialPrincipal` | `SystemAdmission`,
  validated at construction). It is re-verified, immediately before the
  mutation and inside this command's own transaction, against the exact same
  owner as admission: `verify_admission_authorization` delegates to
  `authorize_offer_version_admission` — the compound `catalog:write AND
  (catalog:billing_write OR catalog:offer_version:admission)` rule AND the
  ERP staff leave-write restriction. `app/api/catalog.py`'s `PATCH
  /offer-versions/{version_id}` route delegates the same way admission's
  `POST` route does: its own dependency (`_require_offer_version_admission`)
  authorizes admission only, and the command re-checks the identical owner
  immediately before it writes — one decision, not two that could drift
  apart.
- **Identity is immutable outside admission.** `(offer_id, version_number)`
  and `access_requirement` cannot be edited through this command:
  `_assert_offer_version_identity_immutable` and
  `assert_access_requirement_immutable` both fail closed against
  `OfferVersionUpdate`'s payload before any mutation is applied — defense in
  depth, since `OfferVersionUpdate` has neither field today.
- **Evidence.** A billing-critical change stages a billing-governance audit
  participant (`stage_billing_catalog_change`, action `version_updated`)
  carrying the resolved admission principal's audit evidence, in the same
  transaction as the mutation. A denial raises `OfferAccessRequirementError`
  and records leave-denial evidence (`record_leave_denial_evidence`) the
  same way admission does.

## RBAC: a claimed identity checked against real grants, not a free-text label

`catalog:offer_access_requirement:classify` (migration
`609_offer_access_requirement_classify_permission`) is:

- **Not `catalog:billing_write`.** It is a new, narrow permission.
- **Not seeded into any role.** The migration inserts the permission row only
  — no `role_permissions` row is added for any role.
- Checked via the real RBAC mechanism
  (`app.services.auth_dependencies.has_permission`) against an actual
  `SystemUser` row's roles — the CLI
  (`scripts/catalog/classify_offer_access_requirement.py`) requires
  `--actor-system-user-id` to name an active staff principal, and the
  permission check itself runs inside
  `offer_access_requirement._classify`, fresh, at apply time.
  `--actor-system-user-id` is the operator's CLAIMED identity, not a
  free-text display name: it is the ONLY identity input, and it is resolved
  against real RBAC grants, re-verified fresh inside the command's own
  transaction, before the owner ever treats the action as authorized. **This
  CLI does not itself verify who is really typing the command** — host or
  container shell access to run it at all is this script's actual
  authentication boundary, the same trust model documented in
  `scripts/billing/correct_customer_subledger_opening.py` and its siblings.
  What IS guaranteed: the string recorded as `classified_by`/audit
  actor/event actor is always derived from the RBAC-verified id
  (`principal_label`), never from an unverified claim, and a claimed id that
  does not hold the permission (or an admin/`*` wildcard grant) is refused.
  This is not a check performed once ahead of time and then trusted — see
  "Permission is re-verified fresh" above.

**Wildcard note (by design, not a gap):** this RBAC system already treats the
`admin` role and the `*`/domain wildcard grants as satisfying every
permission automatically. "Not seeded by default" means no explicit grant row
is added for this permission — a principal with an existing wildcard/admin
grant continues to pass, exactly as it does for every other permission in
this system. This is existing, unrelated RBAC behavior and is out of scope
here.

## Expand/contract plan

| Release | Admission | DB default |
| --- | --- | --- |
| **1 (this change)** | `access_requirement` required and explicit; `unclassified` accepted | `unclassified`, historical-row initialization only |
| **2 (later, not built here)** | `unclassified` rejected at admission | dropped |

Release 2 is out of scope for this change and must not be built as part of
it — see the brief that authorized this work.

## Migration safety

- The classification table's CHECK constraints enforce the one legal
  transition shape in the DATABASE, not only in service code:
  `previous_access_requirement` is always `unclassified`, and
  `new_access_requirement` is always one of the two real values.
- Downgrade LOCKS both tables (`ACCESS EXCLUSIVE`, inside the migration's own
  transaction) before counting rows, so a concurrent write cannot slip
  between the check and the destructive DDL.
- `SET LOCAL` (not a plain `SET`) scopes the migration's own lock/statement
  timeout to its own transaction, so it can never discard the
  operator-configured global override (`alembic/env.py`) for any later
  migration or statement.

## Architecture guard

`tests/architecture/test_offer_access_requirement_boundary.py` asserts that
`AccessRequirement` (the model enum) is referenced only inside:

- `app/services/catalog/offer_access_requirement.py` (the owner)
- `app/models/catalog.py` (the model/column)
- `app/schemas/catalog.py` (the schema)
- `alembic/versions/608_offer_access_requirement.py` (the migration)
- `scripts/catalog/classify_offer_access_requirement.py` (the CLI/worklist)
- `app/api/catalog.py` (thin error mapping)
- test files

It never leaks into connection-type, PPPoE, RADIUS, enforcement, or
`missing_login` code.
