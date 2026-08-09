# ADR 0009: The operator is a tenant

Status: proposed

Date: 2026-08-09

Decision owner: Michael

Affected systems and domains: `docs/PLATFORM_ADOPTION_LEDGER.md` kernel import
allowlist, `app/models/domain_settings.py` scope columns, request context,
`alembic` chain, and every later kernel stateful-module adoption.

## Context

Sub has no tenant. The only `tenant_id` in its entire model layer arrived on
2026-08-09 in migration `507_domain_settings_scope_columns`, which added the
kernel's scope columns to `domain_settings` and defaulted every row to
**platform** scope.

That default is wrong, and it was chosen without a decision. `dotmac_starter_mt`
ADR-0003 states:

> A single-tenant deployment provisions exactly one tenant and retains
> `Tenant`, tenant context, composite tenant constraints, and RLS. Single
> tenancy is a topology, not a second application architecture.

and, for this product shape specifically: *"the ISP operator is the platform
tenant and the ISP's subscribers are product-domain parties/customers inside
that tenant."* The operator IS a tenant. In the kernel's scope model `platform`
is the deployment-wide fallback BENEATH tenant — `tenant_id` is NULL only for
platform, and every other scope lives inside a tenant.

So the current schema asserts that Sub's settings belong to no tenant, which
contradicts the accepted model and is the "second application architecture"
ADR-0003 rules out. ADR-0003 also names the failure this sets up: multi-ISP
*"is not achieved by adding a tenant row around an otherwise single-tenant
schema."* Every setting currently sits at deployment-wide scope; if Sub ever
hosts a second operator, a setting that should have been operator #1's is
indistinguishable from one that should stay deployment-wide, and re-scoping
becomes a per-row judgement rather than a migration.

The general form matters more than settings. **The kernel is multi-tenant by
construction, so every stateful kernel module meets this same wall.** That is
the most likely reason so much of the adoption ledger is classified `defer-db`:
not that those modules are risky, but that Sub cannot host any of them without
a tenant.

There is no central tenant service to register with. `Tenant` lives in
`dotmac_kernel.models`; tenant management is `app/features/tenants` in the
starter assembly. `dotmac_vendor_control_plane` owns vendor-side accounts and
provisioning contracts and has zero `Tenant` usage — it is explicitly not a
product data plane. Each product assembly hosts its own tenants.

## Decision

**Sub provisions exactly one tenant, and that tenant is the ISP operator.**

- **Authoritative record:** `dotmac_kernel.models.Tenant` (table `tenants`),
  and `TenantDomain` (table `tenant_domains`) where domain binding is needed.
  Neither table exists in Sub today, so neither is one of the six colliding
  tables the ledger records.
- **Canonical writer:** a single provisioning path that creates the operator
  tenant if absent and is idempotent on every boot. Sub does not offer tenant
  CRUD; there is one tenant and it is not an operator-editable resource.
- **Resolver:** a request-scoped accessor returning the operator tenant. Sub
  does not resolve a tenant from the request (no host or header carries one) —
  it returns the single provisioned tenant.
- **Transport boundary:** unchanged. No kernel middleware is mounted by this
  ADR.

This amends the ledger's kernel import allowlist to admit **exactly two model
classes**, `Tenant` and `TenantDomain`, and nothing else from
`dotmac_kernel.models`. The ledger's existing sentence already contemplates
this and no more: *"even post-S7, only `Tenant`/`TenantDomain` could enter via
an ADR that amends this ledger."*

### Explicitly not decided here

- `dotmac_kernel.models.Party`, `PartyRole`, `Role`, `UserCredential` — Sub
  identity is not replaced. They remain prohibited.
- `dotmac_kernel.db` as session/transaction authority. `app/db.py` remains the
  owner. Admitting two model classes does not admit the kernel engine.
- `dotmac_kernel.migrations` composition. Sub writes its own migration for the
  two tables, in its own chain. See "Rejected alternatives".
- `middleware.tenant` (`TenantResolverMiddleware`). Sub has nothing to resolve
  from.
- The six colliding tables (`parties`, `party_roles`, `roles`,
  `user_credentials`, `audit_events`, `domain_settings`). Each needs its own
  ownership decision.

## Invariants

- Exactly one row in `tenants` in any Sub deployment. A second row is a defect,
  not a feature, until an ADR supersedes this one.
- Every tenant-scoped row Sub writes carries that tenant's id. No Sub row is
  created at platform scope once this lands, because Sub owns nothing that is
  deployment-wide above the operator.
- `Tenant` and `TenantDomain` are the ONLY classes importable from
  `dotmac_kernel.models`. The import guard enforces the narrowing, not a
  comment.
- Sub's identity model (`Party`, `Role`, `UserCredential`) is untouched, and
  the class-name collisions with the kernel's remain unimportable.

## Consequences

**Operational.** Provisioning runs on boot and is idempotent. A deployment that
starts with no tenant gets one; a deployment that already has one is unchanged.
No operator action, no runbook step.

**Data.** `domain_settings` rows move from platform scope to the operator
tenant's scope. This is a backfill of `tenant_id` and `scope_kind` over one
table, small today because nothing else is tenant-scoped yet — which is
precisely why doing it now is cheap and doing it after further adoption is not.

**Compatibility.** Sub reads none of the scope columns today, so the backfill
changes no behaviour. It changes what the schema *asserts*, which is the point.

**Multi-ISP.** This does not deliver multi-tenancy and must not be described as
doing so. ADR-0003 is explicit that shared multi-ISP requires a tenant-safety
programme across every table, worker, cache, object, index, credential, network
operation, export and webhook. What this ADR buys is that the first operator's
data is attributed to an operator, so that programme starts from a correct
model instead of a re-scoping exercise.

### Rejected alternatives

**Keep platform scope (status quo).** Cheapest, and it is what migration 507
currently does. Rejected because it encodes "Sub has no tenant" into the schema
against the accepted model, and because the re-scoping bill grows with every
row and every module added afterwards.

**Compose the kernel's migration lineage now.** Rejected on evidence already in
the ledger: kernel revision `0004_custom_fields` executes
`op.add_column("parties", ...)`, which against Sub's chain alters **Sub's**
`parties` table. Migration composition cannot precede resolution of the six
colliding tables; it follows them. Sub therefore writes its own migration for
`tenants`/`tenant_domains` rather than importing `dotmac_kernel.migrations`.

**Adopt `dotmac_kernel.db` at the same time.** Rejected as scope. Two model
classes can live in Sub's existing `Base` and engine; the session/transaction
authority question is independent and deserves its own decision.

## Migration and cutover

- **Old owner and paths:** no owner — Sub has no tenant concept. Settings
  scope columns default to `platform` (`507_domain_settings_scope_columns`).
- **New owner and paths:** `dotmac_kernel.models.Tenant`; a Sub-owned
  provisioning function; a Sub-owned accessor for the operator tenant.
- **Backfill/repair:** set `tenant_id` to the operator tenant and `scope_kind`
  to `tenant` for every `domain_settings` row. Idempotent and re-runnable.
- **Shadow or verification phase:** none required — no reader consumes the
  scope columns yet, so there is no behaviour to shadow. This is the one step
  in the adoption where that is true, which argues for doing it first.
- **Cutover gate and evidence:** a real PostgreSQL migration test proving one
  tenant row exists, every `domain_settings` row carries it, and no row remains
  at platform scope.
- **Fallback retirement:** the `platform` default on `scope_kind` is replaced
  by the operator tenant's scope; no fallback survives.
- **Schema contract step:** `tenants` and `tenant_domains` created by a Sub
  migration in Sub's chain, matching the kernel's model definition so the
  kernel's ORM can read them.

## Verification

- Architecture: the import guard admits `Tenant`/`TenantDomain` and continues
  to reject every other name from `dotmac_kernel.models`, with a sabotage proof
  for `Party`.
- Architecture: a check that no Sub code writes a `domain_settings` row at
  platform scope.
- Migration: PostgreSQL predecessor-to-candidate — rows exist at platform scope
  before, carry the operator tenant after, values unchanged.
- Behaviour: provisioning is idempotent across two boots and creates exactly
  one tenant.
- Operational: a deployment with an existing tenant is not modified.

## Rollback or forward-fix

Reversible. The backfill can be undone by returning rows to platform scope, and
the two tables can be dropped, because nothing reads them yet — the same
property that makes this cheap to do now. Once a later slice makes a second
table tenant-scoped, rollback stops being clean; that is the point at which
this decision hardens.

## Review and retirement

- Review date: when the first of the six colliding tables is scheduled, or when
  a second operator is proposed, whichever is first.
- Retirement condition: superseded by an ADR that either admits further kernel
  models or establishes genuine multi-tenancy.
- Supersedes or is superseded by: amends the kernel import allowlist in
  `docs/PLATFORM_ADOPTION_LEDGER.md`; partially discharges the ledger's S7
  gate, whose remaining parts (kernel `db`, migration composition, the six
  colliding tables) stay closed.
