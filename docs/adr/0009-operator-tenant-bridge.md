# ADR 0009: The operator is a tenant

Status: proposed

Date: 2026-08-09

Decision owner: Michael

Affected systems and domains: `docs/PLATFORM_ADOPTION_LEDGER.md` kernel import
allowlist, `app/models/domain_settings.py` scope columns, request context,
`alembic` chain, and every later kernel stateful-module adoption.

## Amendment, 2026-08-11: the state this ADR describes no longer exists

Measured read-only on production (`selfcare.dotmac.io`), two days after this
document was written:

```
tenants                              1 row
  8c7ae830-51fc-52ae-9818-d84b2a35e568  slug='operator'  name='Operator'

domain_settings                    577 rows
  scope_kind='tenant'              577   (100%)
  scope_kind='platform'              0
  tenant_id IS NULL                  0
  distinct tenant_id                 1 → the operator tenant
```

Two corrections follow, and both matter to anyone ratifying this.

**The migration this ADR criticises was reasoned, not careless.** The Context
below says the platform default "was chosen without a decision". Migration 507's
own docstring decides it explicitly and at length: Sub had no tenant at that
moment, so labelling rows `tenant` while `tenant_id` stayed NULL would have been
a scope claim the row's own data contradicted. 507 was correct when written. It
was migration 508 creating `tenants`, and the operator tenant being provisioned
at boot, that made it obsolete — not an oversight in 507.

**The correction this ADR calls for has already happened.** Every settings row
is tenant-scoped to the operator, stamped on write by
`app/models/domain_settings.py`. Ratifying therefore authorises no data
migration and costs nothing; what remains is schema convergence.

**How that convergence happens changed on 2026-08-11, and in Sub's favour.**
The original plan was `518_domain_settings_converge_on_kernel_shape`: move Sub
down to the kernel's shape, dropping the CHECK that migration 514 already
carries. Kernel `0.1.0a40` makes that unnecessary. Its migration
`0021_setting_scope_alignment` treats adoption as first-class — it detects an
existing `ck_domain_settings_scope_alignment`, verifies the constraint and the
platform default actually match, and **adopts** them, recording
`dotmac-kernel:0021:adopted-existing` as a constraint comment so a later
downgrade restores Sub's own predecessor rather than deleting it. It refuses to
adopt an unverified or incoherent constraint rather than assuming.

So Sub keeps the stronger invariant it already shipped, and 518 is retired
unmerged. This is the pattern the remaining collision dispositions should try
first: **make the kernel adopt the product's stronger invariant, rather than
levelling the product down to the kernel.**

One mechanical schema delta remains after that adoption decision:
`domain_settings.tenant_id` has no foreign key to `tenants.id`. Migration
`523_domain_settings_tenant_fk` adds only that relationship with
`ON DELETE CASCADE`. It changes no rows, retains the `platform` server default
and `ck_domain_settings_scope_alignment`, and fails if existing data contains
an orphan rather than silently deleting or re-attributing it.

The decision below stands unchanged. Only its premise has moved: it now records
what Sub already does rather than what Sub should start doing.

## Context

*(as written 2026-08-09, superseded in part by the amendment above)*

Sub has no tenant. The only `tenant_id` in its entire model layer arrived on
2026-08-09 in migration `507_domain_settings_scope_columns`, which added the
kernel's scope columns to `domain_settings` and defaulted every row to
**platform** scope.

That default is wrong for the world after 508 — see the amendment; it was the
right call for the world 507 shipped into. `dotmac_starter_mt`
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

## Relationship to the vendor control plane

Recorded because this decision creates a tenant identity locally, and it is
reasonable to ask whether the platform should be issuing it.

**It does not today, and the two sides name different things.**
`dotmac_vendor_control_plane` speaks `deployment_ref`/`deployment_id` — 68
references in its source against 7 for `tenant_id` — and kernel licensing binds
an envelope to a `deployment_ref`. Kernel entitlements, by contrast, key
`tenant_entitlement_grants` on `tenant_id`.

So a licence is deployment-scoped and a grant is tenant-scoped, and something
must join them. In the starter's reference receiver that projection happens in
the RECEIVING assembly. When Sub becomes a licence receiver (ledger S8, still
deferred) it will project a deployment-scoped licence into grants for its
operator tenant — and because Sub owns both the receiver and the tenant, a
locally chosen id is correct there rather than merely tolerable.

The risk this leaves is narrow: ADR-0003 gives the control plane "vendor-side
accounts, provisioning contracts, and (later) deployment lifecycle". If "later"
ever extends to ISSUING tenant identity, Sub's self-chosen id becomes something
to reconcile. Two properties keep that cheap, and both should be preserved
deliberately:

- **Sub never exposes the tenant id.** No API, contract or export emits it, so
  reconciliation would map a platform reference onto the local tenant rather
  than rewriting every `tenant_id`.
- **`Tenant.slug` is unique** and is the natural join column, being what a
  multi-ISP resolver would key on.

## Review and retirement

- Review date: when the first of the six colliding tables is scheduled, when a
  second operator is proposed, or when the vendor control plane begins issuing
  tenant identity — whichever is first.
- Retirement condition: superseded by an ADR that either admits further kernel
  models or establishes genuine multi-tenancy.
- Supersedes or is superseded by: amends the kernel import allowlist in
  `docs/PLATFORM_ADOPTION_LEDGER.md`; partially discharges the ledger's S7
  gate, whose remaining parts (kernel `db`, migration composition, the six
  colliding tables) stay closed.
