# Platform Adoption Ledger — dotmac_sub

**Status:** Rebaselined 2026-08-02 for slice S1 of the selective kernel-adoption
plan. Supersedes the 2026-07-19 Phase-0 draft, which was surveyed before the
kernel was released and against `origin/main` 7807afcd. No code, schema, or
dependency change is authorized by this document alone.
**Decision authority:** `dotmac_starter_mt` `docs/adr/0003-unified-deployment-profiles.md`
and the execution plan
`dotmac_starter_mt/docs/superpowers/plans/2026-08-02-dotmac-sub-kernel-improvements.md`
(non-authoritative intent; this repo's checked-in docs and registries govern).
**Companion sources of truth in this repo:** `docs/SOT_RELATIONSHIP_MAP.md` and its
executable registry `app/services/sot_relationships.py` — the per-domain owners named
there remain authoritative. This ledger classifies kernel surfaces *against* those
owners; it does not re-assign ownership.
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
- **Kernel adapters get NO owner rows.** `app/services/sot_relationships.py` is the
  owner registry; an adapter over a kernel contract calls a registered Sub owner
  through `app.services.owner_commands.execute_owner_command` and is never itself
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
| `dotmac_kernel.models` | prohibited | — | Kernel Party/identity family (`tenants`, `tenant_domains`, `parties`, `party_persons`, `party_organizations`, `roles`, `party_roles`, `user_credentials`, `auth_sessions`). Sub identity is not replaced during this program; even post-S7, only `Tenant`/`TenantDomain` could enter via an ADR that amends this ledger |
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
- `dotmac_kernel.money`
- `dotmac_kernel.profiles`
- `dotmac_kernel.providers`
- `dotmac_kernel.providers.provisioning`

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

The executable owner registry is `app/services/sot_relationships.py` (with
`docs/SOT_RELATIONSHIP_MAP.md` as its narrative map). All adoption slices leave
it authoritative and unchanged in meaning:

- Kernel adapters (S4+) are **not** owners and get **no rows** in the registry.
- `execute_owner_command` (`app/services/owner_commands.py`) remains the single
  transaction authority; adapters call registered owners and never commit.
- The S4 pilot boundary, `access.radius_projection`, is already registered and
  stays the projection owner behind any `ProvisioningProvider` adapter.

## S1 acceptance claim

Adding `dotmac-kernel==0.1.0a7` as a dependency, by itself:

- **runs no kernel migrations** — Sub's `alembic.ini` keeps
  `script_location = alembic` with no `version_locations`; kernel revisions are
  inert package data;
- **mounts no routes** — `create_app` is never called and `mount_features` is
  forbidden by the import guard; Sub's route inventory is unchanged;
- **constructs no engine** — every allowlisted module is import-safe by the
  kernel's own manifest (no `DATABASE_URL` read); `dotmac_kernel.db` and bare
  `import dotmac_kernel` are rejected by the guard;
- **changes no Sub transaction or owner** — `app/db.py`,
  `execute_owner_command`, and `app/services/sot_relationships.py` are
  untouched; no adapter holds an owner row.

The guard proving the import half of this claim is
`tests/architecture/test_kernel_import_boundary.py`, including a
negative-control test that fails the checker on a synthetic forbidden import.

## Explicitly out of scope for this ledger

- Adding the dependency itself (S2), any `tenant_id` column, or any schema change.
- Renaming or merging `Subscriber`/`Organization`/`Party` into kernel identity.
- Any dual-write, second writer, second outbox/inbox, or writer replacement.
- Shared database or ORM imports with dotmac_erp or the vendor control plane.
