# Platform Adoption Ledger — dotmac_sub

**Status:** Rebaselined 2026-08-02 for slice S1 of the selective kernel-adoption
plan; amended the same day for slice S2 (dependency pinned — see "S2 acceptance
claim") and slice S3 (composition declared in `app/composition.py` — see "S3
acceptance claim"). The pin moved to `dotmac-kernel==0.1.0a50` on 2026-08-13
and to `dotmac-kernel==0.1.0a81` on 2026-08-20 — see "Pin history". Supersedes the
2026-07-19 Phase-0 draft, which was surveyed before the kernel was released and
against `origin/main` 7807afcd. No code, schema, or dependency change is
authorized by this document alone.
**Decision authority:** `dotmac_starter_mt` `docs/adr/0003-unified-deployment-profiles.md`
and the execution plan
`dotmac_starter_mt/docs/superpowers/plans/2026-08-02-dotmac-sub-kernel-improvements.md`
(non-authoritative intent; this repo's checked-in docs and registries govern).
**Companion sources of truth in this repo:** `docs/SOT_RELATIONSHIP_MAP.md` and
the executable aggregate `app/services/sot_registry/registry.py` — the
per-domain owners named there remain authoritative. This ledger classifies
kernel surfaces *against* those owners; it does not re-assign ownership.
**Recon basis:** repo state at `origin/dev` `0d045baae05c91fb9307772d7aaad181b928715f`,
surveyed 2026-08-02, against released `dotmac-kernel==0.1.0a7` (source of record:
`dotmac_starter_mt/packages/dotmac-kernel/src/dotmac_kernel/`, package
`__init__.py` `SUPPORTED_MODULES`/`INTERNAL_MODULES` manifest, now released as
`0.1.0a8`). Rebased onto `origin/dev` `e2ce6d02c` (2026-08-02), with the
collision inventory re-verified at each rebase rather than assumed to hold:

- against `d14543bde` and its 20 intervening commits: the only schema change is
  migration 456, which ALTERs the existing `ont_wan_service_instances` table
  (new columns + the `ontwanservicelifecycle` enum type) and adds no new
  tables; new model classes (`PostingProducer`, `PostingSourceKind`,
  `OntWanServiceLifecycle`) have no kernel counterpart.
- against `e2ce6d02c` and its 6 further commits (prepaid subledger authority
  cutover): migration 457 creates `customer_subledger_opening_positions` and
  `customer_subledger_authority_cutovers`; the new models add
  `customer_posting_groups`. None of those names collides with a kernel table,
  so the six then-documented collisions (`parties`, `party_roles`, `roles`,
  `user_credentials`, `audit_events`, `domain_settings`) were unchanged.
- against the party-identity integration batch through migration 527 and
  released kernel a50, the lineage-head inventory has nine table-name overlaps,
  classified in
  `dotmac_starter_mt/docs/inventories/sub-lineage-dispositions.md`. Two are the
  intentionally hosted kernel models `tenants` and `tenant_domains`; the other
  seven are competing model declarations. Kernel 0022 renamed its RBAC grant
  from `party_roles` to `party_role_grants`, so Sub's archetype-shaped
  `party_roles` is no longer a current model or lineage-head collision. The
  lineage still creates the old name at 0003 before renaming it at 0022, so that
  transient tenth name remains a disposition rather than disappearing from the
  migration review. The nine-name head set is exact in
  `tests/integration/test_kernel_lineage_rehearsal.py`; the seven-name model set
  is exact in `tests/architecture/test_kernel_table_collisions.py`. Any eighth
  competing declaration fails CI, while every lineage remeasurement must
  continue to include hosted tables, transient DDL names, and both repositories'
  migration histories.

- against released kernel a81 (Sub unchanged since the a50 measurement), the
  kernel's declared model tables intersect Sub's `app/models/**` in exactly the
  same seven names, and the nine current lineage-head overlaps are unchanged.
  Kernel revisions 0024-0026 add one new lineage-head table
  (`external_identity_bindings`), alter `auth_sessions`, and re-grant
  `platform_audit_events`; none of those names exists in Sub, so neither
  executable ratchet moves. The remeasurement was run against the a81 source
  tree, not assumed from the changelog.

The recon is re-run on every pin and Sub model change because a stale inventory
would silently under-report the very risk the S7 ADR gate exists to hold.

**2026-08-13 — S7 GUC predecessor, no RLS activation.** Sub's existing
session/transaction authority now applies its one deterministic operator tenant
to every PostgreSQL SQLAlchemy root transaction through a transaction-local
GUC. The owner lives in `app.services.operator_tenant`; the global
`after_begin` hook is a lifecycle adapter that also covers direct sessions in
tasks, workers, CLIs and scripts. The PostgreSQL canary proves reapplication
after both commit and rollback and proves the setting does not survive for the
same pooled connection's next transaction.

This does not import `dotmac_kernel.db`, activate FORCE RLS, compose the kernel
migration lineage, move the revision-0001 ratchet, backfill Party projections,
or authorise deployment. Because Sub deploys migrations before the new image,
this GUC behavior must be present in the deployed predecessor before a later
schema release can enable RLS safely.

**2026-08-14 — minimized production-shape lineage rehearsal.** The executable
revision-0001 ratchet can now consume a typed, aggregate-only evidence bundle
instead of a production database restore. The source exporter is repeatable-
read and read-only; its schema rejects values outside catalog digests, counts
and closed structural cohorts. The scratch lane verifies the exact source
revision and catalog fingerprints, materializes new synthetic cohort canaries,
runs the installed kernel lineage, and proves complete canary rows survive the
expected failure byte-stable. It copies no production UUIDs, identity/contact
fields, credential material, audit payloads, metadata values or timestamps.
The runbook is `docs/runbooks/KERNEL_LINEAGE_MINIMIZED_REHEARSAL.md`.

This makes the evidence collection safe enough to execute after both hosts are
explicitly named. It does not disposition any collision, move
`EXPECTED_FIRST_FAILURE`, authorize kernel composition, or replace the
per-table cutover decisions.


## Pin history

**2026-08-20 — `0.1.0a50` → `0.1.0a81`.** A deliberate catch-up repin, not a
new kernel consumption: Sub's imported surface is unchanged and every module
it actually consumes (`settings_resolver`, `settings_models`,
`setting_scopes`, `setting_value_types`, `settings_cache`, `settings_crypto`,
`secret_sources`, `capabilities`, `profiles`, `providers.provisioning`) is
byte-identical between a50 and a81. Only `assembly`, `features`, `models` and
the package `__init__` changed, and all four changed additively — no name Sub
imports was removed, renamed or given a new required argument.

Protected release run `32346291258` built, inspected, published and
registry-verified a81. The annotated tag `dotmac-kernel-v0.1.0a81` peels to
Starter main `8f99413826e5adf3d35379ebc6deb79bcb5c8242`. The lock records wheel
SHA256 `f3b82ed2f1a12897cf7e9b801c905f0f7018fbbb5f9045aa6bef02a3632665bb`
and sdist SHA256
`2c4fe080d0d2b31271ca0b0c2d435d0ad2d3a2fb81c25c591a1bc0b3774d3810`;
re-locking moved no other package.

Two breaking changes to a released surface were crossed and both are inert for
Sub. a61 replaced a60's implicit plane selector with the explicit
`ProductAssemblySpec.module_planes` contract (ADR-0028): `SUB_ASSEMBLY`
declares four `FeatureManifest`s, none of which declares
`supported_plane_sets`, so `validate_module_plane_selections` requires no
selection and the field keeps its empty default. a70 made `actor_type` (and
`actor_id` for every non-system actor) explicit on `write_audit_event` and made
`resolve_audit_actor` raise on the two former fallback shapes; `app/` calls
neither — `dotmac_kernel.audit` remains forbidden by the import guard and
`app/models/audit.py::AuditEvent` remains Sub's own writer.

The guarded transitive package surface grows from nineteen modules to
twenty-four. `planes` and `prerequisites` arrive through `assembly`/`modules`,
`external_identity` through `models`, `outbox_event_types` through `features`,
and the private `_transactions` through the a73 change that stopped
consent/delivery/idempotency/external-identity importing the eager kernel
database owner merely to open a SAVEPOINT. Each is LOADED, none is used: no
kernel middleware is mounted, no kernel endpoint is served, and the top-level
route prefix set is unchanged.

Sub still does not compose a kernel migration lineage, import a kernel
authority into `app/`, activate FORCE RLS, move the revision-0001 ratchet, or
transfer any business owner. The seven competing model declarations and the
nine current lineage-head overlaps are re-measured and unchanged; see the
collision inventory below.

**2026-08-13 — `0.1.0a42` → `0.1.0a50`.** Sub takes the first released
product-manifest contract rather than following the kernel's latest version by
default. Protected release run `31696869748` built, inspected, published and
registry-verified a50. The annotated tag `dotmac-kernel-v0.1.0a50` peels to
Starter main `461aff83d32d73166625be13e5214718f2ade9cf`. The lock records wheel
SHA256 `3030954c84c8ed4c4aae877412df4c1f3db0b2e4dd94895f1bd9a3a954fa77371`
and sdist SHA256
`87c0df99a33f4d4b79f3e22842166524b3dec9f077af7ad5757e6fb3600274f7`;
no unrelated locked dependency moves.

a44, a45 and a46 allocate module namespaces only. a47 is breaking because it
removes `sanitize_branding_css`; Sub imports neither that symbol nor any other
retired branding-CSS path. a50 adds the pure, import-safe
`ProductManifestSnapshot` contract consumed by Sub's release adapter. Sub does
not compose a new kernel migration lineage, import a kernel authority into
`app/`, or transfer any business owner. Its guarded transitive package surface
grows from eighteen to nineteen modules because the kernel's supported
top-level package now imports `dotmac_kernel.product_manifest`.

The model and lineage collision sets are unchanged from a42: the intervening
namespace allocations and product-manifest contract add no kernel models or
migrations. The exact seven competing model declarations and nine current
lineage-head overlaps remain executable ratchets. Kernel a51 is a later
documentation-only release and is not required by this contract, so Sub does
not repin merely to follow the newest package number.

**2026-08-12 — `0.1.0a40` → `0.1.0a42`.** The dependency half of audit R1,
after protected release run `31592573094` published and registry-verified a42
from Starter main `048662dbd944aca95b2e89f133b0c864c3fd5a59`. The annotated
tag `dotmac-kernel-v0.1.0a42` resolves to that exact commit. The lock records
wheel SHA256 `0678241f808b564cafd218fc86d45a096f685568aace90868a70b71c5d907106`
and sdist SHA256
`8658e05d78ecf2579187588f2af96c9f084bcfe977fcafd7f518248097ac25cb`;
no unrelated locked dependency moves.

The released wheel was then installed into a disposable Linux environment on
Observe. The integration candidate's complete migration chain reached
`524_audit_events_kernel_r1` on PostgreSQL 16 with PostGIS 3.4, and all 103
Postgres-backed integration tests passed. The rehearsal returned exit code 0
and removed its disposable resources. Promotion rebased the same additive
migration to `525_audit_events_kernel_r1` after Network Map V2 claimed revision
524. This is package-compatibility evidence, not a lineage, authority, merge,
or deployment claim.

**2026-08-13 promotion correction.** Inbox self-assignment subsequently claimed
revision 525 on `dev`. The promotion chain preserves that merged revision and
renumbers the unchanged audit expansion to 526 and the credential projection to
527. The 524/525 identifiers above are retained as historical rehearsal and
earlier-promotion evidence, not current-head claims.

a41 is breaking for consumers of the kernel `PartyRole` model, renamed to
`PartyRoleGrant` with no alias. Sub imports neither name, so its runtime import
surface is unaffected. The collision inventory does change: Sub's business
capacity table remains `party_roles`, while the kernel's current RBAC grant is
`party_role_grants`. The names no longer collide at lineage head, although the
kernel chain still creates the old name at 0003 before 0022 renames it. a42 adds
the polymorphic audit actor and request
forensics contract plus kernel migration 0023. Sub still does not import
`dotmac_kernel.audit` or compose the kernel lineage: migration 526 expands
Sub's own table and its existing `observability.audit_log` owner remains the
single writer during shadowing. This pin makes the immutable contract available
for compatibility and rehearsal; it does not claim an authority or lineage
cutover.

The release and lock evidence is machine-readable in
[`docs/audits/audit-r1-kernel-release.json`](audits/audit-r1-kernel-release.json).

**2026-08-11 — `dotmac-ui` `0.1.0a3` pinned (new distribution).** A SECOND
platform distribution enters this ledger. Nothing in this document's kernel
allowlist, module classification or collision inventory applies to it:
`dotmac-ui` has zero runtime dependencies, no models, no migrations and no
runtime behaviour — it publishes design tokens and a compiled stylesheet. Sub
is its second consumer (Academy was first).

Rationale, boundaries, and the two findings the adoption measured — Sub's
`src/css/` tree is dead code, and Sub's live token file shares the package's
role NAMES but not its values — are in
[`docs/adr/0010-adopt-shared-ui-contract.md`](adr/0010-adopt-shared-ui-contract.md).
Guarded by `tests/architecture/test_dotmac_ui_adoption.py`.

**2026-08-13 — `dotmac-ui` `0.1.0a3` → `0.1.0a7`.** UI contract 1 remains
compatible. The new consumed surface is inert, namespaced Jinja package data
plus its compiled `.dmui-empty-state*` CSS; no kernel, model, migration or
runtime dependency is introduced. Sub composes the published template root
into every live `Jinja2Templates` environment through its existing central
initializer. The six live callers of the byte-identical local include now emit
package-owned markup through a thin compatibility adapter; Sub's richer table
macro remains a separate, explicitly unreconciled contract. ADR-0010's dated
amendment records the boundary and verification.

**2026-08-11 — `0.1.0a27` → `0.1.0a40`.** This is the contract pin and
lineage rehearsal the gate needs, not migration composition or identity
adoption. Sub cannot rehearse a migration lineage it does not have, so the pin
precedes every per-table disposition.

Thirteen releases, and **none of the three breaking changes in them touch the
surface Sub imports**, which is why this is a pin bump rather than a migration:

- a37 requires `OutboundMessage(dispatch_id=..., category=...)`. Sub does not
  import `dotmac_kernel.delivery`.
- a33 renamed `InboxRecord` → `IdempotencyRecord` and moved it to
  `dotmac_kernel.idempotency_models`, with the tables and columns renamed to
  match. Its changelog is explicit that consumers of `messaging.process_once`
  need no source change and only consumers of the RECORD MODELS do; Sub imports
  neither.
- a31 widened the `fastapi` range upward. Sub pins 0.111.0, which is the floor
  and unchanged.

a38 also makes the assembly's platform surface, startup hooks/checks, and
product security policy explicit, and fixes the FastAPI 0.140 lazy-router guard
walker. Sub does not call `create_app`, mount kernel routes or middleware,
import kernel auth/RBAC, or add kernel `version_locations`.

What Sub does gain is a40's `0021_setting_scope_alignment`, which exists because
of Sub's own S7 rehearsal: it repairs the `scope_kind='tenant'` /
`tenant_id IS NULL` shape, and where a product already carries the exact CHECK
and platform default — Sub's migration 514 — it **verifies and adopts** them,
recording `dotmac-kernel:0021:adopted-existing` so downgrade restores Sub's
predecessor rather than deleting it. Sub keeps the stronger invariant it already
shipped.

The collision baseline was remeasured against this release rather than the
historical a27 figures: ten name collisions, of which four need no design, one
is a two-column reconciliation, and five need a union. See
`dotmac_starter_mt/docs/inventories/sub-lineage-dispositions.md`.

`SUB_ASSEMBLY` now records the product choices the a38 fields made expressible:
single-tenant topology, kernel platform surface off, no kernel startup hooks or
checks, and an empty kernel security policy because Sub's existing runtime owns
those concerns. This remains metadata; `app.main` remains the runtime owner.

The PostgreSQL canary
`tests/integration/test_kernel_a42_scope_adoption.py` invokes only the released
0021 migration body inside a rollback-only transaction against the real,
Sub-migrated schema. It proves adoption and downgrade preserve 514 while Sub's
`alembic_version` is unchanged. That is disposition evidence for the future S7
migration ADR, not a back door around it. It complements the full-lineage
ratchet, which currently stops at the expected 0001 collision and therefore
cannot yet exercise 0021.

**2026-08-09 — `0.1.0a23` → `0.1.0a27`.** Taken because the settings cutover
needs a value type that did not exist at a23.

`json` in the kernel is an OBJECT type — its `to_storage` rejects a non-`dict`,
and `register_specs` refuses a spec whose default its own type rejects. Four Sub
settings default to LISTS (`imports.import_history_log`,
`imports.import_jobs_log`, `audit.methods`, `audit.skip_paths`), so registering
Sub's specs against a23 fails closed at import. `list` shipped as a kernel
built-in in a27 (`dotmac_starter_mt` #78).

**It is a kernel built-in and not a Sub registration on purpose.** The
value-type registry is open, so Sub could have declared its own `list` on a
manifest; ERP would then have declared an incompatible one, which is the
vocabulary fork ADR-0008 exists to prevent. Recorded as an ADR-0006 amendment
in the starter ("build once; an extension point is not a licence"). Sub declares
no value-type vocabulary of its own, and migration
`512_open_setting_value_type_vocabulary` removed the database's closed list so
a kernel-declared type can be stored at all.

**Nothing in `app/` changes by the bump itself.** a24 (`inherits`), a25
(`stored_at`), a26 (ADR-0013 platform deployment facts, and the BREAKING rename
`ProductAssemblySpec.settings_overrides` → `setting_defaults`) and a27 (`list`)
were either additive defaults or in surfaces Sub did not invoke at that pin.
Sub's kernel surface remained the allowlist below. Note a26's rename is
breaking for anyone constructing a `ProductAssemblySpec` with
`settings_overrides`; `app/composition.py` does not.

a26 itself was never published — its release run failed — so a27 is the first
released version carrying either change. Nothing may pin a26.

**2026-08-09 — `0.1.0a13` → `0.1.0a23`.** Taken to make the settings cutover
testable. a13 PREDATES the settings re-base: at that pin,
`dotmac_kernel.settings_*` is the original from-scratch module — a five-member
`SettingDomain` enum behind a CHECK constraint — which is the module that was
replaced precisely because no product could adopt it. A parity harness built
against a13 would therefore diff Sub's resolver against a subsystem nobody
intends to adopt. a14 is the re-base; a15–a23 carry open value types, scope
depth, bulk reads, per-scope requirements, BYOK, the `KeyProvider` seam, and
ADR-0011's rule that resolution reads rows and defaults and never the
environment.

**Nothing in `app/` changed by the bump itself.** Every BREAKING entry across
a14–a23 is in the settings subsystem — `resolve()`, `Keyring.active`, per-scope
requirements, BYOK — and Sub imports none of it. Sub's kernel surface remains
`app/composition.py` (`assembly`, `capabilities`, `features`, `profiles`) plus
`Tenant`/`TenantDomain` under ADR-0009, all held by
`tests/architecture/test_kernel_import_boundary.py`.

An earlier version of this section recorded that "a14–a17 never existed as
artifacts". That was accurate on 2026-08-07 and is not now: a14 and a18 were
published on 2026-08-08, and a23 is the current release.

**2026-08-07 — `0.1.0a8` → `0.1.0a13`.** The kernel release carrying the
white-label foundation: the module registry and manifest declarations, D1's
per-module Postgres namespaces and migration lineages, tenant-entitlement
enforcement (`require_capability`), typed feature flags, and the platform
administration surface. a13 is a single release covering five development
iterations; a9–a12 were published, a14–a17 never existed as artifacts.

**Nothing in `app/` changed.** Sub consumes the kernel as CONTRACTS: the whole
surface is `app/composition.py` importing `assembly`, `capabilities`, `features`
and `profiles`, held there by `tests/architecture/test_kernel_import_boundary.py`.
The changes in a9–a13 that ENFORCE at runtime — `write_audit_event` rejecting an
undeclared audit action, and `require_permission` failing the boot on an
undeclared permission code — are guards on kernel call paths this repo does not
use, because Sub does not call `create_app` and does not import
`dotmac_kernel.deps` or `dotmac_kernel.audit`.

The one contract that could have broken did not: a13 retyped
`FeatureManifest.capabilities` as `str | CapabilitySpec`, and
`CapabilityCatalogue.from_manifests` coerces bare strings, so the declarations in
`app/composition.py` resolve exactly as before. `ProductAssemblySpec` gained
three fields, all defaulted. The lock moved the kernel entry and nothing else —
no transitive dependency changed.

Five sites move together, and a required check enforces that: four in
`pyproject.toml` (the PEP 621 `dependencies` entry, the Poetry main entry, the
Poetry dev-group `[testing]` entry, and the PEP 621 dev-group `[testing]` entry)
plus `KERNEL_PIN` in `tests/architecture/test_kernel_compatibility.py`.

**Not delivered by this bump:** Sub declares no `ModuleManifest`, so D1's
namespace and migration-lineage rules govern nothing here yet. They become
relevant if and when Sub extracts stateful modules.


## The adoption frame

```text
Sub authoritative intent
  -> existing owner command and transaction (execute_owner_command)
  -> existing event/integration delivery (events.store, integration.*)
  -> kernel-compatible provider/value/capability contract
  -> exact observed result or acknowledgement
  -> existing Sub reconciler and repair owner
```

- Sub remains authoritative for subscriber, subscription, billing, collections,
  service readiness, network intent, RADIUS/OLT/ONT/ACS, IPAM, topology, support,
  vendor-work, and official timeline state.
- The ISP **operator** deployment eventually maps to exactly one platform `Tenant`
  (S7 ADR gate). Subscribers, resellers, customer organizations, and staff are
  never platform tenants.
- **Kernel adapters get NO owner rows.**
  `app/services/sot_registry/registry.py` is the owner aggregate; an adapter over
  a kernel contract calls a registered Sub owner through
  `app.services.owner_commands.execute_owner_command` and is never itself
  registered as an owner, never commits, and never becomes a transaction authority.

## Classification of every kernel public module

Classes:

| Class | Meaning |
|---|---|
| **consume-pure** | DB-free public contract; import and use directly once the dependency lands (S2+) |
| **adapt** | Usable early, but only behind a Sub-owned adapter/declaration seam; parts of the module remain forbidden |
| **defer-db** | Touches kernel persistence (tables, engine, migrations) or is gated on the S7 operator-tenant/migration ADR; forbidden until that gate is green |
| **prohibited** | Would create a second owner, second identity, or second runtime stack in Sub; out of scope for this program |

The surfaces Sub has evaluated are classified below. A supported kernel module
that is not listed is still prohibited in Sub until a ledger amendment admits
it; kernel support is an API promise, not product adoption. Kernel-internal
modules (including any `dotmac_kernel._*` and `display`) are forbidden outright.

| Kernel module | Class | Slice | Notes |
| --- | --- | --- | --- |
| `dotmac_kernel.money` | consume-pure | S2/S5 | `Money`/`Currency`/immutable `ExchangeRate` at typed Sub/ERP boundaries only. Sub billing columns (`Numeric(12,2)`, NGN) and invoice arithmetic are not rewritten |
| `dotmac_kernel.capabilities` | consume-pure | S3 | `CapabilityCatalogue`; every capability code names exactly one existing SOT domain owner. Never an entitlement or permission decision |
| `dotmac_kernel.profiles` | consume-pure | S3 | `DeploymentProfileSpec`/`DeploymentProfileRegistry` as dedicated-ISP composition preflight. Profile names never appear in business logic |
| `dotmac_kernel.assembly` | consume-pure | S3 | `ProductAssemblySpec` is composition metadata only; `app.main` remains the runtime owner |
| `dotmac_kernel.testing` (+ `.fakes`, `.harness`, `.provisioning`) | consume-pure | S2/S4 | Test-only: development/test dependency group and `tests/` — not imported under `app/`. Sub's PostgreSQL isolation canaries are retained. `dotmac_kernel.testing.licensing` is gated with S8 |
| `dotmac_kernel.features` | adapt | S3 | `FeatureManifest`/`NavItem` as declaration metadata only. `mount_features` is app-factory machinery and stays forbidden (no remount) |
| `dotmac_kernel.providers` / `.providers.provisioning` | adapt | S4 | `ProvisioningProvider` protocol/result types behind a thin adapter over the existing `access.radius_projection` owner. The adapter gets no owner row; vendor/OLT/ONT semantics stay product-owned |
| `dotmac_kernel.messaging` (+ `.envelope`, `.models`, `.outbox`, `.platform`, `.platform_relay`, `.platform_worker`, `.relay`, `.worker`) | defer-db | S7+ | Defines `outbox_events` / `platform_outbox_events` and relay workers; inbox processing delegates to the idempotency owner below. Never added beside `events.store` and `integration.*` during pure-contract phases; semantics may be used as conformance criteria only |
| `dotmac_kernel.idempotency` (+ `.idempotency_models`, messaging `.inbox`) | defer-db | S7+ | Kernel `0.1.0a33` (ADR-0014) makes at-most-once execution one owner in `idempotency_records` / `platform_idempotency_records`. The tenant FK can reference Sub's admitted operator tenant, but that does not resolve ownership: Sub's `IdempotencyKey` (`idempotency_keys`, `(scope, key)`) and `TaskExecution` remain the owners until a dedicated cutover retires them |
| `dotmac_kernel.consent` / `.consent_models` | defer-db | S7+ | Kernel `communication_suppressions` now collides with Sub's same-named table and mature communication-eligibility owner. This is an extraction candidate, not permission to mount a second writer; disposition must preserve Sub parity and retire the local path explicitly |
| `dotmac_kernel.delivery` / `.delivery_models` / `.delivery_providers` / `.channel_policy` | defer-db | S7+ | Delivery adds `communication_deliveries` and a provider/consent decision path. No table collides today, but Sub's notification/outbox/delivery owners must be adapted and retired coherently before adoption; a protocol alone does not transfer authority |
| `dotmac_kernel.entitlements` | defer-db | S8 | `tenant_entitlement_grants` table. Only after the operator-tenant bridge (S7) and capability catalogue (S3) are proven. Commercial product/module availability only — never subscriber financial-access, service-readiness, or RBAC |
| `dotmac_kernel.licensing` | defer-db | S8 | The verifier itself is DB-free, but adoption is gated on entitlements persistence and the S7 ADR; a Sub-owned WS8 receiver comes after both |
| `dotmac_kernel.db` | defer-db | S7 | Importing constructs the SQLAlchemy engine from `DATABASE_URL`. `app/db.py` remains Sub's session/transaction authority; any kernel engine use requires the S7 ADR |
| `dotmac_kernel.migrations` | defer-db | S7 | Kernel Alembic revisions `0001`–`0023`. They are not added to Sub's `alembic.ini` (`script_location = alembic`, no `version_locations`). The released-a42 canary executes inherited revision 0021's body transactionally and leaves Sub's version table unchanged; composition still needs the S7 ADR and all current and transient collision dispositions |
| `dotmac_kernel.audit` | defer-db | deferred | `AuditEvent` model collides with Sub's (`audit_events` table). Sub's writers stay `record_audit_event` + `AuditEvents.stage` (pinned by `tests/architecture/test_audit_writer_surfaces.py`); kernel audit adapts behind them after parity, never as a second writer |
| `dotmac_kernel.settings_models` / `.settings_resolver` / `.settings_cache` / `.settings_crypto` / `.setting_scopes` / `.setting_value_types` / `.secret_sources` | adapt (consumed) | settings cutover | Sub declares product specs and retains product storage/transaction owners while the kernel supplies typed vocabulary, resolution, cache, crypto, and held-secret contracts. `DomainSetting` and `DomainSettingHistory` collide with Sub tables; app code may import only `SettingDomain` from `settings_models`, and migration composition remains closed |
| `dotmac_kernel.settings_admin` / `.setting_domains` | defer-db | deferred | Not imported by Sub. Admin/write authority remains in Sub services until a typed owner cutover removes the existing path; module availability alone is not a reason to add a second writer |
| `dotmac_kernel.models` | **partial (S7a)** | ADR-0009 | `Tenant`/`TenantDomain` ONLY, per ADR-0009. Sub migrations 508/509 host and provision those exact kernel models as the one operator tenant; they are admitted records, not duplicate Sub models. Every other name stays prohibited: kernel Party/identity (`Party`, `PartyRoleGrant`, `Role`, `UserCredential`, `AuthSession`, and related records) would collide with or replace Sub identity or authorization. This program does not do that |
| `dotmac_kernel.tenancy` | prohibited | — | Sub records `tenancy="single"` as composition metadata but does not install the kernel binding/resolver runtime; the operator-tenant service remains the owner |
| `dotmac_kernel.models_platform` | prohibited | — | `PlatformAdmin`/`PlatformSession` — Sub keeps its own staff identity (`app/models/system_user.py`, `app/models/auth.py`) |
| `dotmac_kernel.config` | prohibited | — | `app/config.py` remains Sub's settings owner; a second `Settings`/`validate_settings` authority is a drift source |
| `dotmac_kernel.security` | prohibited | — | Sub's credential/session/MFA crypto is owned by its auth stack (`app/services/auth_flow.py` et al.); no second hasher/token issuer |
| `dotmac_kernel.deps` / `.web_deps` / `.platform_auth` | prohibited | — | Kernel route guards and platform auth are a second RBAC/identity surface; Sub guards live in `app/services/auth_dependencies.py` |
| `dotmac_kernel.middleware.csrf` / `.observability` / `.rate_limit` / `.security_headers` / `.tenant` | prohibited | — | A second middleware stack. Note the exact class-name collision with Sub's `ObservabilityMiddleware` below. `TenantResolverMiddleware` could only enter via the S7 ADR amending this ledger |
| `dotmac_kernel.app_factory` | prohibited | — | No `create_app` cutover; `app.main` is the runtime owner. `SUB_ASSEMBLY.platform_surface_enabled=False` records the boundary, and mounting the factory would still collide on `/admin` and `/static` |
| `dotmac_kernel.crud` | prohibited | — | Reference CRUD features are not adopted as Sub domain services |
| `dotmac_kernel.templating` / `.branding` | prohibited | — | Sub owns its Jinja environment, templates, and branding (`app/models/branding.py`, `templates/`) |
| `dotmac_kernel.identity` | prohibited | — | Helpers over the kernel Party model; Sub identity is out of scope |
| `dotmac_kernel.query` | prohibited | — | Trivial pagination/escape helpers with existing Sub equivalents; excluded to keep the surface exactly plan-shaped. May be promoted by a later ledger amendment |
| `dotmac_kernel.errors` / `.exceptions` / `.logging` | prohibited | — | Sub owns its error taxonomy (`app/errors.py`) and logging config (`app/logging.py`); kernel error handlers are app-factory wiring |
| `dotmac_kernel.display` | prohibited | — | Kernel-internal (`INTERNAL_MODULES`); forbidden by the kernel itself |

## Kernel import allowlist (`app/`)

The executable form of the table above, enforced by
`tests/architecture/test_kernel_import_boundary.py`. Only these modules may ever
be imported under `app/`, and only once the dependency lands (S2). This section
and the test's allowlist must stay in exact sync (the test parses this
section).

The count of kernel imports in `app/` is no longer zero, which an earlier
version of this paragraph asserted: `app/composition.py` (S3),
`app/services/kernel_secret_source.py`, `app/services/operator_tenant.py`
and the settings cutover all import from this list.

- `dotmac_kernel.assembly`
- `dotmac_kernel.capabilities`
- `dotmac_kernel.features`
- `dotmac_kernel.models`
- `dotmac_kernel.money`
- `dotmac_kernel.profiles`
- `dotmac_kernel.providers`
- `dotmac_kernel.providers.provisioning`
- `dotmac_kernel.secret_sources`
- `dotmac_kernel.setting_scopes`
- `dotmac_kernel.setting_value_types`
- `dotmac_kernel.settings_cache`
- `dotmac_kernel.settings_crypto`
- `dotmac_kernel.settings_models`
- `dotmac_kernel.settings_resolver`

`dotmac_kernel.models` is admitted for **two names only** — `Tenant` and
`TenantDomain` (ADR-0009). Every other name in it, including `Party`,
`PartyRoleGrant`, `Role` and `UserCredential`, stays forbidden, and a bare
`import dotmac_kernel.models` is refused because it reaches all of them. The
guard enforces the narrowing through `RESTRICTED_MODULE_NAMES`, not a comment.

`dotmac_kernel.settings_models` is likewise admitted for **`SettingDomain`
only**. The resolver may use its own persistence classes internally against the
adopted table contract, but app code importing `DomainSetting` or
`DomainSettingHistory` would expose a parallel model/writer surface over two
same-named Sub tables. The same restricted-name guard rejects those imports.
Sub identity is not replaced by this amendment.

`dotmac_kernel.secret_sources` was added 2026-08-09 for the secret
classification ruled that day. It is a PLACE TO PUT material Sub read itself —
the kernel declares a one-method protocol and holds the result; it ships no
OpenBao client and performs no I/O. Sub's implementation is
`app/services/kernel_secret_source.py`, over the client in
`app/services/secrets.py`. ADR-0009 (`dotmac_starter_mt`): a secret is held,
never dereferenced, so nothing on a settings read path reaches OpenBao.

**Wired 2026-08-10.** The source had no caller for a day: it was declared,
tested and installed by nothing, so every reader still went to the database for
a `bao://` reference and dereferenced it mid-request. Two entry points install
it — `app.main._startup_preflight` for the API, and `app.celery_app`'s
`worker_process_init` for each prefork worker child, which runs no lifespan and
whose tasks decrypt device credentials — and the five readers take the held
value:

| secret | reader |
|---|---|
| `credential_encryption_key` | `app/services/credential_crypto.py::get_encryption_key` |
| `totp_encryption_key` | `app/services/auth_flow.py::_mfa_key` |
| `wireguard_key_encryption_key` | `app/services/wireguard_crypto.py::get_encryption_key` |
| `jwt_secret` | `app/services/auth_flow.py::_jwt_secret` |
| `radius_auth_shared_secret` | `app/services/radius_auth.py::authenticate` |

Three consequences worth stating, because each is a behaviour change:

- **A configured-but-unreachable OpenBao now fails the boot.** The install is
  gated on configuration (`is_openbao_configured`, no I/O), never on
  reachability — a reachability probe would skip the install during an outage
  and hand every reader a `None` that reads as "not configured", which for
  `credential_encryption_key` means storing device credentials in the clear
  behind a warning line. A deployment that configures no OpenBao holds nothing
  and keeps using its environment variables.
- **Precedence is environment, then held**, for all five. That preserves what
  four of them already did; `wireguard_key_encryption_key` had the settings row
  win over its variable, and both named the same OpenBao field.
- **Rotation refreshes the held set.** `credential_rotation_schedule` writes the
  new key into OpenBao, so it now calls `refresh_secrets()` — the kernel reloads
  on an explicit act and never on a timer.

**Specs retired 2026-08-10.** The five had specs and seeded rows that nothing
read — a control an operator can set that changes nothing. `radius
/auth_shared_secret` went with the read-path move; the other four
(`auth/jwt_secret`, `auth/credential_encryption_key`,
`auth/totp_encryption_key`, `network/wireguard_key_encryption_key`) went with
the trust-anchor slice below. They escaped
`tests/architecture/test_no_orphan_settings.py` only because each key NAME
still appears in `app/` — as the name its held secret is asked for by.

### The rule these follow

> `is_secret` on a settings spec means **confidential**: encrypt the value at
> rest, and settings-write may change it. A value whose **authority** matters —
> a trust anchor, a signing key, or a key that protects this same database — is
> not a setting at all. It is held material, loaded at boot from a path named in
> code.

The two are different properties and neither substitutes for the other.
Encryption at rest answers "a database dump must not yield this"; held material
answers "the surface this system exposes must not be able to change this".
ADR-0009 drew the second line for the boot five; this states the reason, so the
next secret-shaped setting is classified rather than argued about.

**`billing/prepaid_reconstruction_attestation_public_key_ref` moved by that
rule.** It is a public key, so confidentiality buys nothing; what it needed was
that only OpenBao access can replace it, since replacing it means forged
funding manifests verify. Its guard — "must be an OpenBao reference" — looked
like that protection and was not: it checked the value WAS a reference and
never WHICH reference, so settings-write could aim it at any key. It is now
`prepaid_attestation_public_key` in `OPTIONAL_SECRET_REFS`.

`OPTIONAL_SECRET_REFS` exists for exactly that shape: material needed by ONE
feature, where a deployment not using the feature has nothing to provision and
must still boot. The strict distinction survives — a missing PATH means not
provisioned, while an unreachable store, a bad token or a missing field still
raise (`secrets.resolve_openbao_ref_optional`). The required five stay
all-or-nothing.

The three settings modules were added 2026-08-10 for the settings cutover:
`settings_resolver` (resolution, and `register_specs` so Sub's 560 specs are
declared where the resolver looks), `settings_models` (the `DomainSetting`
the kernel reads, which is Sub's own table), and `setting_value_types` (the
declared value types, consulted at Sub's WRITE boundary). This is the pairing
migration `512` was written for: that migration removed the database's closed
list of value types, and this admits the registry that replaces it. Sub
declares no value types of its own — starter ADR-0006, "build once; an
extension point is not a licence".

`settings_cache` and `setting_scopes` were added 2026-08-10 for the settings
cache slice. The kernel owns settings caching — key, scope segment, TTL, and
what a write invalidates — and Sub supplies only a `CacheStore` transport
(`app/services/kernel_settings_cache_store.py`) plus one invalidation listener
on `DomainSetting`, which needs `SettingScope` to say whether a write was
tenant- or platform-scoped. The cache this replaced keyed on
`settings:{domain}:{key}` with NO scope segment — the cross-tenant leak
`dotmac_kernel.settings_cache` cites `dotmac_erp` for.

`dotmac_kernel.settings_crypto` was added 2026-08-10, the seam before the
schema. It encrypts a secret setting's value at rest; the kernel reads the
environment by default and ships no secret-store client, so a product whose
keys live in a store supplies a `KeyProvider`. Sub's is
`app/services/kernel_key_provider.py`, the exact sibling of
`kernel_secret_source`: loaded once at boot, held in memory, rotation an
explicit `refresh_keys()`.

It is NOT part of `SECRET_REFS`, whose load is all-or-nothing. A deployment
that has not created a keyring yet must still boot — every secret setting Sub
holds today is a `bao://` reference, so encryption becomes possible before it
becomes used. The provider therefore distinguishes a missing path (nothing
configured; returns nothing) from any other failure (raises, and the boot
fails), which is the distinction the kernel requires of a provider.

Nothing is encrypted by this alone. The write path, the conversion of existing
reference rows, and the readers that must move onto the kernel resolver are the
next slice.

Rules the guard enforces beyond the module list:

- Bare `import dotmac_kernel` / `from dotmac_kernel import X` is forbidden in
  `app/`: the top-level package re-exports identity, audit, and entitlement
  names indiscriminately; imports must name the specific allowlisted submodule.
- `mount_features` may not be imported from `dotmac_kernel.features` (app-factory
  machinery).
- `dotmac_kernel.testing.*` is consume-pure for `tests/` and the dev dependency
  group only; it is not on the `app/` allowlist.

## Collision inventory (kernel 0.1.0a81 vs Sub through migration 528)

The authoritative migration-lineage measurement has nine overlaps at current
lineage head plus one transient name that still needs a chain disposition; see
`dotmac_starter_mt/docs/inventories/sub-lineage-dispositions.md`. Comparing the
installed kernel's model declarations against Sub's `app/models/**`
declarations yields the seven competing-model overlaps below. Their exact
intersection and a sensitivity proof are enforced by
`tests/architecture/test_kernel_table_collisions.py`; the exact nine-table
lineage-head set is checked against a real Sub-migrated schema by
`tests/integration/test_kernel_lineage_rehearsal.py`.

### Python package names

No collision: Sub's code lives under `app/` (plus `alembic/`, `scripts/`,
`tests/`); the kernel imports as `dotmac_kernel`. Sub's `src/` contains only CSS.

### SQLAlchemy table names — nine at lineage head, seven competing models

`tenants` and `tenant_domains` are the two intentional hosted overlaps: Sub
migrations 508/509 create and provision them, while Sub imports the kernel's
`Tenant` and `TenantDomain` models. They have no competing Sub model. The seven
tables below do have models on both sides, with different authority surfaces:

| Table | Kernel owner | Sub owner |
| --- | --- | --- |
| `parties` | `dotmac_kernel.models.Party` (platform identity) | `app/models/party.py::Party` (Sub party model) |
| `roles` | `dotmac_kernel.models.Role` | `app/models/rbac.py::Role` (R1 nullable kernel identity projection; legacy readers remain authoritative) |
| `user_credentials` | `dotmac_kernel.models.UserCredential` | `app/models/auth.py::UserCredential` |
| `audit_events` | `dotmac_kernel.audit.AuditEvent` | `app/models/audit.py::AuditEvent` |
| `domain_settings` | `dotmac_kernel.settings_models.DomainSetting` | `app/models/domain_settings.py::DomainSetting` |
| `domain_setting_history` | `dotmac_kernel.settings_models.DomainSettingHistory` | `app/models/domain_setting_history.py::DomainSettingHistory` |
| `communication_suppressions` | `dotmac_kernel.consent_models.CommunicationSuppression` | `app/models/notification.py::CommunicationSuppression` |

The colliding class names (`Party`, `Role`, `UserCredential`,
`AuditEvent`, `DomainSetting`, and `DomainSettingHistory`) are also identical;
the suppression classes differ in name but still target one table. A careless
import can therefore shadow a Sub model even before metadata reaches an engine.
`dotmac_kernel.models` and `.settings_models` are name-restricted by the import
guard, while `.audit` and `.consent_models` remain forbidden. Kernel
`Base.metadata.create_all`, autogenerate, or composed migrations must never run
against Sub until all current and transient lineage overlaps have explicit
dispositions;
otherwise they would corrupt, duplicate, or silently take ownership of live
product tables.

Migration `528_roles_kernel_r1_additive` is an expand prerequisite, not a
collision disposition or lineage-ratchet movement. It widens `roles.name`, adds
the kernel timestamp defaults, nullable `tenant_id`/`slug`, the a42 cascade FK,
both kernel composite unique keys, and a complete-or-absent projection CHECK.
`auth.rbac_catalog` remains the sole row writer and derives the projection on
every role mutation; `roles.name` remains the authorization identity read by
Sub. Existing rows are not backfilled by DDL. Kernel reader cutover remains
blocked until the typed collision report and mismatch cohorts are clean, every
row is projected through a reviewed command, grant semantics have parity, and
the atomic revision-0001 rehearsal passes.

`party_roles` is the transient tenth name. Sub correctly retains that name for
its concurrent, temporal business capacities. Kernel a41 renamed its unrelated
RBAC grant to `party_role_grants`, so the two current models and lineage heads no
longer collide. Kernel revision 0003 still creates `party_roles` before 0022
renames it, however; migration composition must handle that intermediate state
explicitly rather than treating the final-name separation as a complete chain
disposition.

Hosted overlaps and non-competing names worth recording: kernel `tenants`,
`tenant_domains`,
`party_persons`, `party_organizations`, `auth_sessions`, `outbox_events`,
`platform_outbox_events`,
`platform_admins`, `platform_sessions`, `platform_audit_events`,
`party_role_grants`, and
`tenant_entitlement_grants` do not collide with a Sub model. Kernel revision
0024's `external_identity_bindings` (a81) joins that non-colliding list — Sub
has no table or model of that name, and `dotmac_kernel.external_identity`
stays off the `app/` allowlist. The first two are
intentionally hosted through the admitted kernel models; a40 renamed the inbox
tables to `idempotency_records` / `platform_idempotency_records`, which likewise
do not collide. `communication_deliveries` and `feature_flag_overrides` are also
non-colliding today. Sub's `sessions`, `integration_inbox`, `inbox_*`
team-inbox tables, and `service_entitlements` are different names with
different owners.

### Alembic

- Both sides use the default `alembic_version` table. Sub's `alembic/env.py`
  widens `version_num` to `VARCHAR(255)` for its descriptive revision IDs and
  pre-creates the table (`ensure_alembic_version_table`). Composing kernel
  revisions into Sub's `version_locations` would put two independent heads in
  one version table — forbidden before the S7 ADR.
- Kernel revision IDs are `0001_initial_tenant_schema` …
  `0026_platform_audit_log`
  (four-digit prefixes); Sub's files use three-digit-and-up prefixes
  (`001_squashed_initial_schema` …, 498 files plus `versions_archive`). The ID
  strings do not collide today, but Sub's numeric-prefix guard
  (`tests/architecture/test_migration_prefix_collisions.py`) only scans
  `alembic/versions/` and would not see kernel files — the S7 ADR must extend
  it before any composition.
- Sub's `alembic/env.py` also installs idempotent schema-op wrappers for its
  squash; kernel migrations were not written against that regime.
- Concrete corruption scenario, recorded so nobody "just tries it": kernel
  revision `0004_custom_fields` executes `op.add_column("parties", ...)` — run
  against Sub's database it would silently ALTER Sub's own live `parties`
  table (collision above), not a kernel table.

### Middleware

- **REAL class-name collision:** `ObservabilityMiddleware` exists in Sub
  (`app/observability.py`, installed in `app/main.py`) and in the kernel
  (`dotmac_kernel.middleware.observability`). Importing both in one module
  would shadow silently.
- Kernel `CSRFMiddleware` (double-submit `csrf_token` cookie + `X-CSRF-Token`
  header) overlaps Sub's own CSRF machinery (`app/csrf.py`); kernel
  `SecurityHeadersMiddleware`/`RateLimitMiddleware` overlap Sub's nginx/app
  layers; `TenantResolverMiddleware` presumes kernel `Tenant`/`TenantDomain`
  tables. All kernel middleware are prohibited (see table).

### Route prefixes

Sub mounts `/api/v1`, `/admin` (web routers), and `/static` in `app/main.py`.
The kernel app factory mounts `/static`, feature `/admin/*` surfaces, and the
platform auth router. `/admin` and `/static` are direct collisions — one more
reason `dotmac_kernel.app_factory` and `mount_features` stay prohibited.

### Settings owners

Sub's settings authority is `DomainSetting` + the `app/services/settings_spec.py`
registry/resolver (seed + cache). The kernel's settings stack targets a
same-named model and table (collision above). One writer rule: kernel settings
adapt behind the Sub resolver after parity, or not at all.

#### Amendment — setting-domain vocabulary conformance (ADR-0008)

`SettingDomain` is no longer a closed `enum.Enum` stored as the native
`settingdomain` PostgreSQL type. Per the fleet-wide standard that a vocabulary
whose members belong to modules is declared by those modules and validated by a
registry, the members moved OUT of the hosting layer:

- **Declared** on `DomainSOT.setting_domains`
  (`app/services/sot_registry/domains/`), so the SOT domain that owns the
  settings also owns the right to name them. Sub needed no new ownership
  structure for this — the canonical registry already existed, which is why the
  declaration is a field on it rather than a second list to drift. The
  annotated field on a frozen record is also the shape the governance schema-v3
  `declaration_field` gate resolves.
- **Validated** by `app/services/setting_domain_registry.py`, enforced at the
  WRITE boundary (an ORM listener on `DomainSetting`) rather than at
  construction — a resolver must be able to name a domain in order to reject
  it, and rows under a since-undeclared domain must still read.
- **Widened** by migration `502_open_setting_domain_vocabulary`:
  `VARCHAR(120)`, every value preserved, the enum type dropped only after
  `pg_depend` proves nothing else needs it, never `CASCADE`. This retires the
  class of migration that `144_vas_wallets`, `225_add_field_setting_domain` and
  `249_field_erp_sync_outbox` belong to — files whose entire content is an
  `ALTER TYPE ... ADD VALUE` on some module's behalf.

27 of the 28 members are declared. `subscription_engine` is deliberately NOT:
it had no spec, no route, no reader and no writer, and its concern moved to the
dedicated `subscription_engine_settings` table long ago. Its rows survive the
migration and become unwritable, which is the intended outcome for a dead
domain — the ERP equivalent was `operations`.

Consequences worth knowing before the kernel cutover:

- `SettingDomain` is an open `str` subclass, so `SettingDomain(x)` no longer
  raises on an unknown value and `is` comparisons against members are always
  false. Both were relied on — once each, both fixed and pinned by tests.
- `domain` serialises in OpenAPI as a plain string; the `SettingDomain`
  component is gone, so generated clients lose the enum. Hence `version:major`.
- This does not move Sub any closer to importing the kernel's settings modules:
  they stay `defer-db` and off the allowlist. It removes the vocabulary
  blocker, nothing else.

### Audit writers

Sub's two sanctioned surfaces are `record_audit_event`
(`app/services/audit_adapter.py`) and `AuditEvents.stage`
(`app/services/audit.py`), pinned by
`tests/architecture/test_audit_writer_surfaces.py`. Kernel
`write_audit_event`/`write_platform_audit_event` would be a second writer into
a colliding `audit_events` table — deferred behind the existing surfaces.

### Identity and session names

- Both Sub's portal auth (`app/services/web_auth.py`) and the kernel's
  (`dotmac_kernel.web_deps`) use an **`access_token` cookie** — a live conflict
  the moment any kernel web surface were mounted (it never is, see table).
  Sub's refresh-cookie name is setting-driven (`_refresh_cookie_name`).
- Sub's session table is `sessions` (`app/models/auth.py::Session`); the
  kernel's is `auth_sessions` (`AuthSession`) — no table collision, but two
  session authorities is exactly the second-identity path this ledger prohibits.

## Owner registry

The executable owner registry is `app/services/sot_registry/registry.py` (with
`docs/SOT_RELATIONSHIP_MAP.md` as its narrative map). All adoption slices leave
it authoritative and unchanged in meaning:

- Kernel adapters (S4+) are **not** owners and get **no rows** in the registry.
- `execute_owner_command` (`app/services/owner_commands.py`) remains the single
  transaction authority; adapters call registered owners and never commit.
- The S4 pilot boundary, `access.radius_projection`, is already registered and
  stays the projection owner behind any `ProvisioningProvider` adapter.

## S3 acceptance claim (declared 2026-08-02)

S3 adds exactly one Sub-owned composition module, `app/composition.py`, and
its executable acceptance, `tests/architecture/test_composition.py`. It is
metadata only: no route, middleware, permission, engine, migration, owner, or
transaction change (proven by a differential canary — the app imported with
and without the composition module is route-for-route and
middleware-for-middleware identical).

**What was declared** — the four coarse SOT domains the S4–S6/S8 slices need,
one `FeatureManifest` per domain (never one per service), five capability
codes, each naming exactly one existing owner registered in the canonical
aggregate:

| Module (manifest) | Capability code | Registered SOT owner |
| --- | --- | --- |
| `sub.network_projection` | `network_projection.radius` | `access.radius_projection` |
| `sub.backoffice_collaboration` | `backoffice_collaboration.material_release` | `operations.vendor_material_release` |
| `sub.backoffice_collaboration` | `backoffice_collaboration.vendor_advance` | `operations.vendor_advances` |
| `sub.billing_export` | `billing_export.erp_billing` | `integration.dotmac_erp_billing_adapter` |
| `sub.licensing_reception` | `licensing_reception.module_enablement` | `control.module_manager` |

Plus: a frozen `ProductAssemblySpec` (`dotmac-sub`) consumed by validation
only — `app.main` remains the runtime owner; a `CapabilityCatalogue` built
from the manifests (duplicate ownership fails closed, negative-tested); and
the versioned dedicated-ISP profile `sub-dedicated-isp` v`1.0.0`
(`DeploymentProfileSpec` + registry preflight) with independent axes —
required modules (the four above), forbidden modules
(`kernel.reference_features`, `kernel.messaging` — ledger-prohibited/defer-db
surfaces), the eight provider seams (`none` for unfilled seams,
`sub-owned-*` labels for Sub's in-repo owners, `nginx` ingress), locale
`en-NG`, currency `NGN`, legal authority `NG`, residency `NG`.

**What was deliberately NOT declared** — every other domain in the executable
SOT registry (~25 further domains). Expansion happens in later domain slices,
each with its own ledger amendment; generating a catalogue for the whole
repository at once is expressly rejected by the plan. Sub manifests stay pure
metadata: router/nav/seed fields are empty tuples (the starter's
router-carrying manifest use is app-factory machinery; `mount_features`
remains denied by the S1 guard).

**Capability codes are NOT entitlements or permissions** (plan boundary 5).
They are product vocabulary for releases and licences. They never feed RBAC,
subscriber financial-access, or service-readiness decisions, and profile
names never appear in business logic — enforced by a guard test that scans
`app/` (outside the composition module) for the profile code and every
capability code, with a red-sensitivity negative control.

**No-orphan rule** — every declared capability code must map, in
`app/composition.py::CAPABILITY_OWNERS`, to a service name that resolves in
`app/services/sot_registry/registry.py::service_relationship`; a code pointing at
a nonexistent owner is a test failure. The registry stays the ownership
authority; the composition module only references it and holds no owner rows.

**2026-08-13 release-bound consumer.** The release image now derives one
canonical product manifest from this exact `SUB_ASSEMBLY` and the checked-in
`VERSION` by calling the published a50 contract. The Docker build writes those
bytes to `/app/product-manifest.json`; because the file is inside the image, the
OCI digest transitively binds it. The one-time candidate workflow pulls that
exact digest, extracts the file, verifies it inside the exact image against the
assembly and version, records its `sha256:` digest in typed schema-v2 candidate
evidence, and uploads the canonical document beside that evidence. Parsing and
verification refuse rather than normalize. This creates release evidence for
the Vendor Control Plane catalogue adapter; it does not grant an entitlement,
publish a release-catalog attestation by itself, or add a second product
vocabulary owner.

## S2 acceptance claim (pinned 2026-08-02)

S2 pins **`dotmac-kernel==0.1.0a8`** exactly, superseding the plan text's
`0.1.0a7`: the first S2 attempt against a7 was blocked and reverted because
a7's published floors (`fastapi>=0.115`, `pydantic>=2.9`, Python `>=3.12,<3.14`)
conflicted with Sub's pins (`fastapi==0.111.0`, `pydantic==2.7.4`,
`>=3.11,<3.13`). The released a8 resolves every conflict without loosening one
Sub pin:

- **Floors widened:** `fastapi>=0.111,<0.116`, `pydantic>=2.7.4,<3.0`,
  `pydantic-settings>=2.2,<3.0`, Python `>=3.11,<3.14` — Sub's exact pins and
  Python range now satisfy the kernel directly; no python marker needed.
- **Extras split:** `cryptography>=42` moved to the `licensing` extra only; the
  `testing` extra now pulls only `httpx (>=0.27,<0.28)`, which Sub's own
  `httpx==0.27.0` already satisfies. Sub's `cryptography==42.0.8` pin is
  compatible (and `FakeLicenceSigner` works against it — proven by test).
- **`dotmac_kernel.testing` is DB-free:** every canary imports with
  `DATABASE_URL` and all DB-ish env stripped, so the test-kit canaries RUN
  (zero skips) instead of being skipped behind an extra.
- **`dotmac_kernel.profiles` is supported** as classified above; the
  collision-relevant model/migration surface is unchanged a7→a8, so the
  collision inventory below (verified against a7) stands.

Mechanics of the pin, per this ledger's rules:

- `[project.dependencies]` carries the exact `dotmac-kernel==0.1.0a8`;
  `[tool.poetry.dependencies]` enriches it with `source = "forgejo"`
  (`[[tool.poetry.source]]` `priority = "explicit"`, URL only — the credential
  lives in Poetry's auth store, never in Git).
- The `[testing]` extra is declared only in the dev dependency group;
  `dotmac_kernel.testing.*` usage is confined to `tests/` (the S1 guard keeps
  it out of `app/`).
- Resolution added only the kernel's own transitive closure —
  `pydantic-settings`, `argon2-cffi` (+ bindings), `typing-inspection` — and
  moved no existing Sub pin.
- `tests/architecture/test_kernel_compatibility.py` is the executable proof:
  no-DB import canaries for every allowlisted pure surface and the test kit,
  the exact-pin/named-index gate (an unreviewed range upgrade is a CI
  failure), pure value-contract behavior, and the app-unchanged canary (no
  `dotmac_kernel` module in `app.main`'s import graph, middleware stack,
  route endpoints, or top-level route prefixes). Zero skipped tests.

## S1 acceptance claim

Adding `dotmac-kernel==0.1.0a8` as a dependency, by itself:

- **runs no kernel migrations** — Sub's `alembic.ini` keeps
  `script_location = alembic` with no `version_locations`; kernel revisions are
  inert package data;
- **mounts no routes** — `create_app` is never called and `mount_features` is
  forbidden by the import guard; Sub's route inventory is unchanged;
- **constructs no engine** — every allowlisted module is import-safe by the
  kernel's own manifest (no `DATABASE_URL` read); `dotmac_kernel.db` and bare
  `import dotmac_kernel` are rejected by the guard;
- **changes no Sub transaction or owner** — `app/db.py`,
  `execute_owner_command`, and `app/services/sot_registry/registry.py` are
  untouched; no adapter holds an owner row.

The guard proving the import half of this claim is
`tests/architecture/test_kernel_import_boundary.py`, including a
negative-control test that fails the checker on a synthetic forbidden import.

## Explicitly out of scope for this ledger

- Any `tenant_id` column or any schema change (the S2 dependency pin above adds
  code to no runtime path).
- Renaming or merging `Subscriber`/`Organization`/`Party` into kernel identity.
- Any dual-write, second writer, second outbox/inbox, or writer replacement.
- Shared database or ORM imports with dotmac_erp or the vendor control plane.
