# Platform Adoption Ledger — dotmac_sub

**Status:** Rebaselined 2026-08-02 for slice S1 of the selective kernel-adoption
plan; amended the same day for slice S2 (dependency pinned — see "S2 acceptance
claim") and slice S3 (composition declared in `app/composition.py` — see "S3
acceptance claim"). The pin moved to `dotmac-kernel==0.1.0a23` on 2026-08-09 —
see "Pin history". Supersedes the
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
  so the six documented collisions (`parties`, `party_roles`, `roles`,
  `user_credentials`, `audit_events`, `domain_settings`) are unchanged.

Collision findings unchanged. The recon is re-run on every rebase because a
stale inventory would silently under-report the very risk the S7 ADR gate
exists to hold.


## Pin history

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

Every module in kernel `SUPPORTED_MODULES` (plus the kernel-internal `display`)
is classified below. Anything not listed (including any `dotmac_kernel._*`) is
kernel-private and forbidden outright.

| Kernel module | Class | Slice | Notes |
| --- | --- | --- | --- |
| `dotmac_kernel.money` | consume-pure | S2/S5 | `Money`/`Currency`/immutable `ExchangeRate` at typed Sub/ERP boundaries only. Sub billing columns (`Numeric(12,2)`, NGN) and invoice arithmetic are not rewritten |
| `dotmac_kernel.capabilities` | consume-pure | S3 | `CapabilityCatalogue`; every capability code names exactly one existing SOT domain owner. Never an entitlement or permission decision |
| `dotmac_kernel.profiles` | consume-pure | S3 | `DeploymentProfileSpec`/`DeploymentProfileRegistry` as dedicated-ISP composition preflight. Profile names never appear in business logic |
| `dotmac_kernel.assembly` | consume-pure | S3 | `ProductAssemblySpec` is composition metadata only; `app.main` remains the runtime owner |
| `dotmac_kernel.testing` (+ `.fakes`, `.harness`, `.provisioning`) | consume-pure | S2/S4 | Test-only: development/test dependency group and `tests/` — not imported under `app/`. Sub's PostgreSQL isolation canaries are retained. `dotmac_kernel.testing.licensing` is gated with S8 |
| `dotmac_kernel.features` | adapt | S3 | `FeatureManifest`/`NavItem` as declaration metadata only. `mount_features` is app-factory machinery and stays forbidden (no remount) |
| `dotmac_kernel.providers` / `.providers.provisioning` | adapt | S4 | `ProvisioningProvider` protocol/result types behind a thin adapter over the existing `access.radius_projection` owner. The adapter gets no owner row; vendor/OLT/ONT semantics stay product-owned |
| `dotmac_kernel.messaging` (+ `.envelope`, `.inbox`, `.models`, `.outbox`, `.platform`, `.platform_relay`, `.platform_worker`, `.relay`, `.worker`) | defer-db | S7+ | Defines `inbox_records`, `outbox_events`, `platform_inbox_records`, `platform_outbox_events` tables and relay workers. Never added beside `events.store` and `integration.*` during pure-contract phases; semantics may be used as conformance criteria only |
| `dotmac_kernel.entitlements` | defer-db | S8 | `tenant_entitlement_grants` table. Only after the operator-tenant bridge (S7) and capability catalogue (S3) are proven. Commercial product/module availability only — never subscriber financial-access, service-readiness, or RBAC |
| `dotmac_kernel.licensing` | defer-db | S8 | The verifier itself is DB-free, but adoption is gated on entitlements persistence and the S7 ADR; a Sub-owned WS8 receiver comes after both |
| `dotmac_kernel.db` | defer-db | S7 | Importing constructs the SQLAlchemy engine from `DATABASE_URL`. `app/db.py` remains Sub's session/transaction authority; any kernel engine use requires the S7 ADR |
| `dotmac_kernel.migrations` | defer-db | S7 | Kernel Alembic revisions `0001`–`0012`. Never added to Sub's `alembic.ini` (`script_location = alembic`, no `version_locations`) before the S7 ADR and migration canaries |
| `dotmac_kernel.audit` | defer-db | deferred | `AuditEvent` model collides with Sub's (`audit_events` table). Sub's writers stay `record_audit_event` + `AuditEvents.stage` (pinned by `tests/architecture/test_audit_writer_surfaces.py`); kernel audit adapts behind them after parity, never as a second writer |
| `dotmac_kernel.settings_models` / `.settings_resolver` / `.settings_admin` | defer-db | deferred | Kernel `DomainSetting` collides with Sub's (`domain_settings` table and class name). Sub's owner stays `app/services/settings_spec.py`; kernel settings adapt behind `resolve_value` parity, never as a second settings writer |
| `dotmac_kernel.models` | **partial (S7a)** | ADR-0009 | `Tenant`/`TenantDomain` ONLY, per ADR-0009 — neither table exists in Sub, so neither is one of the six collisions below. Every other name stays prohibited: Kernel Party/identity family (`tenants`, `tenant_domains`, `parties`, `party_persons`, `party_organizations`, `roles`, `party_roles`, `user_credentials`, `auth_sessions`). Sub identity is not replaced during this program; even post-S7, only `Tenant`/`TenantDomain` could enter via an ADR that amends this ledger |
| `dotmac_kernel.models_platform` | prohibited | — | `PlatformAdmin`/`PlatformSession` — Sub keeps its own staff identity (`app/models/system_user.py`, `app/models/auth.py`) |
| `dotmac_kernel.config` | prohibited | — | `app/config.py` remains Sub's settings owner; a second `Settings`/`validate_settings` authority is a drift source |
| `dotmac_kernel.security` | prohibited | — | Sub's credential/session/MFA crypto is owned by its auth stack (`app/services/auth_flow.py` et al.); no second hasher/token issuer |
| `dotmac_kernel.deps` / `.web_deps` / `.platform_auth` | prohibited | — | Kernel route guards and platform auth are a second RBAC/identity surface; Sub guards live in `app/services/auth_dependencies.py` |
| `dotmac_kernel.middleware.csrf` / `.observability` / `.rate_limit` / `.security_headers` / `.tenant` | prohibited | — | A second middleware stack. Note the exact class-name collision with Sub's `ObservabilityMiddleware` below. `TenantResolverMiddleware` could only enter via the S7 ADR amending this ledger |
| `dotmac_kernel.app_factory` | prohibited | — | No `create_app` cutover; `app.main` is the runtime owner. Mounting it would collide on `/admin` and `/static` |
| `dotmac_kernel.crud` | prohibited | — | Reference CRUD features are not adopted as Sub domain services |
| `dotmac_kernel.templating` / `.branding` | prohibited | — | Sub owns its Jinja environment, templates, and branding (`app/models/branding.py`, `templates/`) |
| `dotmac_kernel.identity` | prohibited | — | Helpers over the kernel Party model; Sub identity is out of scope |
| `dotmac_kernel.query` | prohibited | — | Trivial pagination/escape helpers with existing Sub equivalents; excluded to keep the surface exactly plan-shaped. May be promoted by a later ledger amendment |
| `dotmac_kernel.errors` / `.exceptions` / `.logging` | prohibited | — | Sub owns its error taxonomy (`app/errors.py`) and logging config (`app/logging.py`); kernel error handlers are app-factory wiring |
| `dotmac_kernel.display` | prohibited | — | Kernel-internal (`INTERNAL_MODULES`); forbidden by the kernel itself |

## Kernel import allowlist (`app/`)

The executable form of the table above, enforced by
`tests/architecture/test_kernel_import_boundary.py`. Only these modules may ever
be imported under `app/`, and only once the dependency lands (S2). Today the
count of kernel imports in `app/` is zero, and the guard already enforces this
list. This section and the test's allowlist must stay in exact sync (the test
parses this section).

- `dotmac_kernel.assembly`
- `dotmac_kernel.capabilities`
- `dotmac_kernel.features`
- `dotmac_kernel.models`
- `dotmac_kernel.money`
- `dotmac_kernel.profiles`
- `dotmac_kernel.providers`
- `dotmac_kernel.providers.provisioning`

`dotmac_kernel.models` is admitted for **two names only** — `Tenant` and
`TenantDomain` (ADR-0009). Every other name in it, including `Party`,
`PartyRole`, `Role` and `UserCredential`, stays forbidden, and a bare
`import dotmac_kernel.models` is refused because it reaches all of them. The
guard enforces the narrowing through `RESTRICTED_MODULE_NAMES`, not a comment.
Sub identity is not replaced by this amendment.

Rules the guard enforces beyond the module list:

- Bare `import dotmac_kernel` / `from dotmac_kernel import X` is forbidden in
  `app/`: the top-level package re-exports identity, audit, and entitlement
  names indiscriminately; imports must name the specific allowlisted submodule.
- `mount_features` may not be imported from `dotmac_kernel.features` (app-factory
  machinery).
- `dotmac_kernel.testing.*` is consume-pure for `tests/` and the dev dependency
  group only; it is not on the `app/` allowlist.

## Collision inventory (kernel 0.1.0a7 vs Sub at 0d045baa)

Verified by comparing the kernel's `models.py`/`models_platform.py`/feature
tables and migrations against Sub's `app/models/**` `__tablename__` set (531
tables) and `alembic/`.

### Python package names

No collision: Sub's code lives under `app/` (plus `alembic/`, `scripts/`,
`tests/`); the kernel imports as `dotmac_kernel`. Sub's `src/` contains only CSS.

### SQLAlchemy table names — six REAL collisions

Kernel tables that already exist, with different shapes and different owners, in
Sub's schema:

| Table | Kernel owner | Sub owner |
| --- | --- | --- |
| `parties` | `dotmac_kernel.models.Party` (platform identity) | `app/models/party.py::Party` (Sub party model) |
| `party_roles` | `dotmac_kernel.models.PartyRole` | `app/models/party.py` |
| `roles` | `dotmac_kernel.models.Role` | `app/models/rbac.py::Role` |
| `user_credentials` | `dotmac_kernel.models.UserCredential` | `app/models/auth.py::UserCredential` |
| `audit_events` | `dotmac_kernel.audit.AuditEvent` | `app/models/audit.py::AuditEvent` |
| `domain_settings` | `dotmac_kernel.settings_models.DomainSetting` | `app/models/domain_settings.py::DomainSetting` |

The colliding class names (`Party`, `PartyRole`, `Role`, `UserCredential`,
`AuditEvent`, `DomainSetting`) are also identical, so a careless import would
shadow a Sub model. This is why `dotmac_kernel.models`, `.audit`, and
`.settings_models` are not on the allowlist and why kernel metadata must never
reach a Sub engine: `Base.metadata.create_all` or autogenerate against a shared
`MetaData` would corrupt or duplicate live Sub tables. Any future kernel-table
adoption (S7) must resolve these six by schema separation or renaming in the
ADR before one migration runs.

Non-collisions worth recording: kernel `tenants`, `tenant_domains`,
`party_persons`, `party_organizations`, `auth_sessions`, `inbox_records`,
`outbox_events`, `platform_inbox_records`, `platform_outbox_events`,
`platform_admins`, `platform_sessions`, `platform_audit_events`, and
`tenant_entitlement_grants` do not exist in Sub today (Sub's `sessions`,
`integration_inbox`, `inbox_*` team-inbox tables, and `service_entitlements`
are different names with different owners).

### Alembic

- Both sides use the default `alembic_version` table. Sub's `alembic/env.py`
  widens `version_num` to `VARCHAR(255)` for its descriptive revision IDs and
  pre-creates the table (`ensure_alembic_version_table`). Composing kernel
  revisions into Sub's `version_locations` would put two independent heads in
  one version table — forbidden before the S7 ADR.
- Kernel revision IDs are `0001_initial_tenant_schema` … `0012_platform_outbox`
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
