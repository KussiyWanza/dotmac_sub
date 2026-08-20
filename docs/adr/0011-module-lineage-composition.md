# ADR 0011: Module lineages compose beside Sub's own, bound by Sub revisions

Status: accepted

Date: 2026-08-20

Decision owner: Michael

Affected systems and domains: `alembic/env.py`, `alembic.ini`, `scripts/deploy.sh`
migration step, `docs/PLATFORM_ADOPTION_LEDGER.md`, the network-module suite
adoption gate (starter ADR-0038), and every future installable `dotmac-*`
module Sub adopts.

## Context

Sub is the first cutover target for nine installable network modules (starter
ADR-0038: `dotmac-ipam`, `dotmac-network-inventory`,
`dotmac-network-observability`, `dotmac-network-topology`,
`dotmac-network-assurance`, `dotmac-network-control`, `dotmac-fiber-plant`,
`dotmac-network-access`, `dotmac-pon-access`). Each is a stateful tenant-plane
module owning one immutable `mod_*` schema and one migration lineage. Adopting
them means Sub composes an external Alembic lineage for the first time.

`docs/PLATFORM_ADOPTION_LEDGER.md` names "the S7 ADR" as the gate on eighteen
lines but never rules on it, and what it does say is scoped to the KERNEL's
public lineage, not to module lineages:

> Composing kernel revisions into Sub's `version_locations` would put two
> independent heads in one version table — forbidden before the S7 ADR.

The starter's own publication baseline names Sub's state as the blocker for a
different module programme in the same words, so this is not a network-only
question — `docs/inventories/declared-publication-baseline.json`, row
`dotmac-campaigns`:

> current Sub authority evidence still blocks adoption: Sub pins kernel a50,
> does not compose its lineage, and records consent, idempotency and outbox as
> unresolved S7+ owner collisions.

The kernel pin moved to a81 on 2026-08-20 (see the ledger's pin history), which
retires the first clause. This ADR addresses the second. It does not address
the owner collisions, which are per-domain cutovers and stay out of scope.

### Composing a module lineage is not composing the kernel lineage

Four differences, each verified against the a81 kernel and the built packages
rather than assumed:

1. **A module lineage is an independent root.** `ip_0001_ipam` has
   `down_revision = None` and `branch_labels = ("ipam",)`. It does not attach to
   Sub's chain, cannot renumber it, and cannot be renumbered by it.
2. **The revision-ID collision the ledger fears does not exist here.** Sub's
   prefix guard (`tests/architecture/test_migration_prefix_collisions.py`)
   globs `alembic/versions/*.py` and matches `^([0-9]+)_`. Module revision IDs
   are `ip_0001_ipam`, `ni_0001_...`, `fp_0001_...` — never numeric-prefixed,
   and never in that directory. The guard is not blind to a risk; there is no
   risk for it to see. The ledger's concern was the kernel's `0001_`–`0026_`
   files, which this ADR does not compose.
3. **Multiple heads are already the deployed reality.** ADR-0008 established
   this on 2026-08-07; re-verified at current positions rather than carried
   over, since its line numbers have since drifted —
   `scripts/deploy.sh:325` and `:770`, `Makefile:166` and `:239`, and
   `docs/runbooks/PRODUCTION_DEPLOYMENT.md:48` all run `alembic upgrade heads`,
   plural. Linearity is a test-imposed constraint on Sub's own chain, not a
   deployment one.
4. **Sub does not have to run kernel `0001` to satisfy what modules need.** The
   modules declare database EFFECTS, not revisions —
   `dotmac_ipam.manifest` has
   `requires=(TENANT_SCOPE_CATALOG_V1.name, MODULE_DATABASE_ROLES_V1.name)` —
   and the kernel documents a product supplying those from its own lineage as
   the intended case. `dotmac_kernel.prerequisites`, on
   `TENANT_SCOPE_CATALOG_V1`:

   > Kernel 0001 supplies both; so does ERP's `20260813_tenant_projection`, in
   > ERP's own lineage, without any kernel identity/RBAC/audit table existing
   > at all.

   And the reference assembly's `app/migration_bindings.py`:

   > ERP, which hosts `public.tenants` in its own lineage and structurally
   > cannot run kernel `0001`, writes a different file here and installs the
   > same modules.

   That is Sub's position exactly. Sub already hosts `public.tenants` and
   `public.tenant_domains` through migrations 508/509 under ADR-0009.

### The one real defect

`alembic/env.py:101` `_install_idempotent_schema_ops()` monkey-patches nine
module-level `alembic.op` functions so the post-squash chain tolerates state
the squashed `001` migration already built. It is installed unconditionally in
`run_migrations_online()`, after `context.configure` and before
`context.run_migrations()`, so it applies to EVERY revision in the run —
including a composed module lineage written years after the squash it exists to
paper over.

Every guard is schema-blind. `_table_exists`, `_columns_of`, `_index_exists`
and `_constraint_exists` all call `sa.inspect(op.get_bind())` with no `schema=`
argument, so they inspect the search-path schema. `_safe_create_table` accepts
`(table_name, *args, **kwargs)` and passes `schema=` through to the original
while testing existence without it.

Module DDL is fully schema-qualified — `ip_0001_ipam` calls
`op.create_table("addresses", ..., schema="mod_ipam")`. Six module table names
collide with a Sub `public` table of the same bare name:

| Bare name | Module owner | Sub owner |
| --- | --- | --- |
| `addresses` | `dotmac-ipam` | `app/models/subscriber.py` |
| `alerts` | `dotmac-network-observability` | `app/models/network_monitoring.py` |
| `pon_ports` | `dotmac-pon-access` | `app/models/network.py` |
| `ports` | `dotmac-network-inventory` | `app/models/network.py` |
| `sessions` | `dotmac-network-access` | `app/models/auth.py` |
| `vlans` | `dotmac-network-inventory` | `app/models/network.py` |

None is an authority collision — `mod_ipam.addresses` is an IP address and
Sub's `addresses` is a street address; `mod_netaccess.sessions` is a RADIUS
session and Sub's `sessions` is an auth session. The danger is purely
mechanical, which is what makes it dangerous: under today's `env.py`,
`op.create_table("addresses", schema="mod_ipam")` finds Sub's public
`addresses`, returns `None`, and the revision is stamped as applied with the
table absent. The lineage then believes it is up to date and every query
against `mod_ipam.addresses` fails at runtime with `UndefinedTable`.

That is a silent-corruption path, not a migration failure, and it is reachable
today by composing a single module.

## Decision

Sub composes module lineages BESIDE its own chain and supplies their
prerequisites from its own revisions. Sub does not compose the kernel's public
lineage, and this ADR grants no permission to.

1. **Prerequisite bindings are Sub's, and live in one named file.** Sub adds
   `app/migration_bindings.py`, the same decision the reference assembly makes
   in the same filename, binding `tenant_scope_catalog.v1` and
   `module_database_roles.v1` to Sub revisions. Binding is not belief:
   `require_prerequisites` re-proves each effect against the live catalog before
   the requiring migration runs, and the order canary requires the named
   revision to be present in `alembic_version`. A wrong entry fails at
   `alembic upgrade`, before any DDL.

2. **Two additive Sub migrations close the prerequisite gap.** Measured, not
   assumed — Sub satisfies neither spec in full today:
   - `tenant_scope_catalog.v1` wants `public.tenants` with the kernel column,
     key and index contract AND `public.app_current_tenant_id()` returning the
     `app.current_tenant` GUC as uuid. Sub has the table (508/509) and already
     sets that exact GUC name (`app/services/operator_tenant.py:85`), but
     defines no such function anywhere in `alembic/` or `app/`.
   - `module_database_roles.v1` wants `app_admin`, `app_user` and `platform_api`
     to exist and be grantable. No Sub migration creates any of the three.

3. **The idempotent schema-op wrappers become schema-aware and Sub-scoped.**
   Every guard takes the `schema=` kwarg its wrapped op already receives and
   passes it to the inspector. This is a correctness fix Sub needs whether or
   not it ever adopts a module — the wrappers silently no-op on any qualified
   DDL today — and it is the precondition for composing anything.

4. **The revision-0001 ratchet does not move.**
   `tests/integration/test_kernel_lineage_rehearsal.py::EXPECTED_FIRST_FAILURE`
   stays at `0001_initial_tenant_schema`. Nothing here composes the kernel
   lineage, so nothing here earns a ratchet movement, and the nine competing
   model declarations and lineage-head overlaps keep their existing
   dispositions.

### Rejected alternatives

**Run module lineages under a second Alembic config.** A separate
`alembic_modules.ini` with its own `env.py` and version table would isolate
modules from Sub's squash regime completely, and it is genuinely tempting given
what `_install_idempotent_schema_ops` does. Rejected because it makes the
version table plural, so "what schema is deployed" stops having one answer;
it doubles the deploy migration step and the lock-timeout policy
(`_set_migration_lock_timeout`, `ALEMBIC_LOCK_TIMEOUT`); and it leaves the
schema-blind wrappers unfixed for Sub's own future qualified DDL. It trades a
one-file correctness fix for a permanent second migration authority — the shape
this repository's SOT standard exists to prevent.

**Retire the wrappers entirely by fixing the post-squash chain.** Correct
long-term and explicitly contemplated by the `_install_idempotent_schema_ops`
docstring ("Post-squash migrations must call the top-level `op` schema methods
unless they implement equivalent live-schema guards themselves"). Rejected as a
prerequisite because it is a several-hundred-revision audit that blocks the
suite cutover behind unrelated work. Making the guards schema-aware is
compatible with retiring them later.

## Invariants

- Sub's own migration chain stays single-headed at promotion; module lineages
  are separate heads and are never parented onto it.
- No module revision is named by a Sub revision, and no Sub revision is named
  by a module revision. The only coupling is effect→revision in
  `app/migration_bindings.py`.
- A schema-qualified `op.*` call is guarded against the schema it names, or it
  is not guarded at all. A guard that inspects a different schema than the op
  targets is a defect, not a conservative default.
- `public` remains Sub's; every `mod_*` schema remains its module's. No Sub
  model, migration or service writes a `mod_*` table, and no module writes a
  `public` table.
- Composing a module does not activate FORCE RLS on Sub tables, move the
  revision-0001 ratchet, or admit any kernel module to `app/` beyond the
  ADR-0009 allowlist.
- Every prerequisite Sub binds is proven against the live catalog at upgrade
  time, never asserted in a document alone.

## Consequences

**Operational.** The deploy migration step is unchanged in shape — it already
runs `alembic upgrade heads`. Each composed module adds one head. Lock-timeout
policy applies to module DDL identically, since it is set on the connection
before `run_migrations`.

**Data.** The schema-aware fix changes behaviour for exactly one class of
statement: a qualified op whose bare name exists in `public`. Today those
silently no-op; afterwards they execute. No Sub revision issues qualified DDL
today, so the fix is a no-op against Sub's existing chain — which is also why
it can land ahead of any module work, and should.

**Compatibility.** Sub keeps hosting `tenants`/`tenant_domains` through
ADR-0009 rather than through kernel `0001`, so the ERP precedent is the one Sub
follows and the kernel-lineage collision inventory is untouched.

**Security.** Creating `app_admin`, `app_user` and `platform_api` introduces
cluster roles Sub does not have today. Passwords are never set in migrations
(the kernel spec is explicit). Grants land in the module's own revision; Sub's
migration only ensures the roles exist and are grantable.

**Team.** `app/migration_bindings.py` becomes a reviewed file: a diff there is
an authority statement about which Sub revision supplies a shared effect, and
should be read as one.

**Rejected-alternative cost, recorded honestly.** Making the wrappers
schema-aware leaves the squash-idempotency regime in place. Sub keeps a
monkey-patched `alembic.op` for the foreseeable future, and every future
contributor writing qualified DDL must know that. That is a real cost of not
taking the second-config option.

## Migration and cutover

- **Old owner and paths:** no owner. Module-lineage composition is unruled;
  `alembic/env.py:101` guards silently and schema-blind.
- **New owner and paths:** `app/migration_bindings.py` owns effect→revision;
  `alembic/env.py` owns schema-aware guarding; `alembic.ini` `version_locations`
  owns which lineages are composed.
- **Backfill/repair:** none. No existing row or applied revision changes.
- **Shadow or verification phase:** compose ONE module (`dotmac-ipam`, suite
  order 1) against a production-shaped scratch database, with `mod_ipam.addresses`
  as the named canary — it is the collision that would silently no-op today.
  Prove the table exists after upgrade, and prove the pre-fix `env.py` fails the
  same check.
- **Cutover gate and evidence:** the two prerequisite migrations are applied,
  `require_prerequisites` passes against the live catalog for both effects, the
  single-module rehearsal is green, and Sub's own chain is still single-headed.
- **Fallback retirement:** none in this ADR. Retiring Sub's local network
  writers is each module's own retirement gate in
  `docs/inventories/network-module-dispositions.toml`, sealed at the suite
  cutover, not here.
- **Schema contract step:** two additive Sub migrations —
  `public.app_current_tenant_id()`, and the three database roles. Both are
  forward-only and neither touches an existing table.

## Verification

- **Architecture:** a test asserts no Sub revision names a module revision and
  no module revision names a Sub revision; a test asserts every
  `ModuleManifest.requires` name in the composed set has a binding in
  `app/migration_bindings.py`.
- **Behaviour:** a canary creates a table in a `mod_*` schema whose bare name
  exists in `public` and asserts the table exists afterwards. This test fails
  against today's `env.py` — that is the point, and it should be written before
  the fix.
- **Migration:** `alembic upgrade heads` from the currently deployed revision
  against a production-shaped database, with one module composed, leaves Sub's
  chain single-headed and the module head applied.
- **Reconciliation:** `require_prerequisites` proves both bound effects against
  the live catalog; a deliberately wrong binding fails at upgrade before DDL.
- **Operational:** the deploy script's existing `upgrade heads` path is
  exercised with a composed module; lock timeout is observed on module DDL.
- **Isolation:** cross-tenant RLS canaries for the composed module's tables run
  against real PostgreSQL, per the existing testing model.

## Rollback or forward-fix

The schema-aware guard fix is reversible with no data consequence — it changes
which statements execute, and against Sub's current chain it changes nothing.

The two prerequisite migrations are additive and independently reversible: the
function can be dropped, the roles revoked and dropped if nothing has been
granted to them.

Composing a module lineage is the point of no easy return, and it is the
module's `downgrade()` that owns it — `ip_0001_ipam` drops its own tables and
schema. Removing a composed module means running its downgrade and removing its
`version_locations` entry; rows in `mod_*` are lost, which is why the suite
cutover carries its own rollback rehearsal (starter ADR-0038 §3) and this ADR
does not claim to cover it.

Nothing here touches applied Sub migrations or production data.

## Review and retirement

- Review date: at the network-suite cutover gate, or 2026-11-20, whichever is
  first.
- Retirement condition: superseded if Sub ever composes the kernel's public
  lineage, which would make the ERP-style binding indirection unnecessary and
  require re-ruling the collision dispositions.
- Supersedes or is superseded by: none. Extends ADR-0009 (operator-tenant
  bridge) and depends on ADR-0008's `upgrade heads` finding.
