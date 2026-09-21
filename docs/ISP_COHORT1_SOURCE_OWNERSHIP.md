# Cohort-isp-01 source ownership map

Sub's side of Governance cohort `cohort-isp-01`, "Foundation party and
customer". This document is the human-readable half of
`app/migration_source/`; the machine-checked half is
`app/migration_source/surfaces.py`, and
`tests/architecture/test_isp_cohort_source_writers.py` fails if the two
disagree about who writes what.

Read `docs/adr/0012-isp-cohort-source-readiness.md` first for the decision this
implements.

## Governance binding

| | |
|---|---|
| Programme | `pgm-dotmac-isp-replacement` |
| Accepted revision | `d91a87f6823bfd2afa6c2025bdb1af644331fa39` |
| Record | `programmes/dotmac-isp-replacement.json` in `dotmac_governance` |
| Decision | `docs/adr/0012-dotmac-isp-replacement-programme.md` (Accepted) |
| Source assembly | `asm-dotmac-sub-legacy` — **source-authoritative** |
| Target assembly | `asm-dotmac-isp` — candidate, independent database |
| Track | `track-isp-sub-cutover` |
| Cohort | `cohort-isp-01`, sequence 1, state **blocked** |
| Components | kernel (reuse), UI (reuse), `dotmac-party` (release), `dotmac-brand-profiles` (adopt), `dotmac-customers` (build), `dotmac-addresses` (build) |

Sub's readiness work produces inputs for `ctl-isp-006` (source dispositions and
idempotent replay), `ctl-isp-007` (zero-unexplained-drift shadow) and
`ctl-isp-009` (bidirectional writer ratchet to zero). Producing an input is not
verifying a control. Every one of those three is `blocked`, and the two open
decisions that gate the cohort — `dec-isp-002`, the production deployment
owner, and `dec-isp-003`, the enforceable legacy Sub transition rule — are
Michael's and are unresolved.

**Nothing in this repository opens the cohort.** Sub is the sole production
writer of every fact below until a separately authorised sealed switch.

## Tenant scope

Sub is a dedicated single-operator deployment (ADR 0009). The ISP operator
*is* the tenant, and the cohort's tables carry no `tenant_id` column; the
operator tenant is a deterministic identity installed as a transaction-local
GUC on every root transaction.

Tenant scope is therefore mandatory and non-nullable in the export contract
and is resolved from `tenancy.operator_tenant`, never from a caller argument.
A request naming any other tenant is refused rather than answered with an
empty result — an empty page and a refusal look identical to an importer, and
only one of them is safe.

None of these tables lives in a platform/control plane. All twelve are
operator-tenant data.

## The twelve entity types

| Entity type | Table | Model | Declared owner | Expected target component |
|---|---|---|---|---|
| `party` | `parties` | `Party` | `party.registry` | `dotmac-party` |
| `party_role` | `party_roles` | `PartyRole` | `party.registry` | `dotmac-party` |
| `party_relationship` | `party_relationships` | `PartyRelationship` | `party.registry` | `dotmac-party` |
| `party_membership` | `party_memberships` | `PartyMembership` | `party.registry` | `dotmac-party` |
| `party_contact_point` | `party_contact_points` | `PartyContactPoint` | `party.registry` | `dotmac-party` |
| `party_external_reference` | `party_external_references` | `PartyExternalReference` | `party.registry` | `dotmac-party` |
| `customer_account` | `subscribers` | `Subscriber` | `customer.accounts` | `dotmac-customers` |
| `customer_contact` | `subscriber_contacts` | `SubscriberContact` | **none declared** | `dotmac-customers` |
| `customer_address` | `addresses` | `Address` | `customer.accounts` | `dotmac-addresses` |
| `organization` | `organizations` | `Organization` | **none declared** | `dotmac-customers` |
| `organization_membership` | `organization_memberships` | `OrganizationMembership` | **none declared** | `dotmac-customers` |
| `brand_profile` | `brand_profiles` | `BrandProfile` | `customer.branding` | `dotmac-brand-profiles` |

The expected target component is an expectation recorded from the Governance
matrix. It is not a plan, an approval, or a claim that any module is released.

### Addresses are owned, four ways

An earlier version of this document recorded `customer_address` as having no
declared owner. **That was wrong**, and it mattered: it made a plain bypass of a
real owner look like unowned debt nobody could route around, and it very nearly
produced a second address service alongside the one Sub already has.

`docs/designs/SUBSCRIBER_SERVICE_LOCATION_SOT.md` splits address ownership
across four services, all declared in the SOT registry:

| Concern | Owner |
|---|---|
| Service address identity and text | `customer.accounts` (`app/services/subscriber.py`) |
| Coordinates and the map projection | `gis.spatial_sync` (`app/services/gis_sync.py`) |
| New location capture — field arrival, portal pin, agent | `customer.location_capture` (`app/services/location_capture.py`) |
| Capture adjudication and the verification ledger | `customer.location_verification` (`app/services/geocode_reconciler.py`) |

Those four are the **product-first extraction source** for `dotmac-addresses`.
dec-isp-007 asks that owner to hold "normalized address, geospatial data and
verification history" — which is precisely what these four already own between
them, so the extraction has proven implementations to draw on rather than being
greenfield.

The decision's question said addresses had "no named owner at all". True of the
target, false of Sub. The distinction is worth keeping straight, because
product-first extraction is only possible where an implementation already
exists.

#### Why three modules bypassed those owners — and what changed

Being declared was not enough to be callable. Until 2026-08-22 neither address
owner offered an operation a caller inside an existing transaction could use:

- `customer.accounts` exposed address creation only as `Addresses.create`,
  which **committed** and raised `HTTPException` — a transport error from a
  domain service, against this repository's own adapter rule.
- `gis.spatial_sync` exposed only committing full-table sweeps
  (`GeoSync.sync_addresses`), with the actual per-address write in a private
  helper.

So `customer_location_requests.py` constructed `Address` rows and their
`GeoLocation` projections itself, and `app/services/field/map_assets.py` kept a
third private copy of the EWKT converter. That produced two live defects:

1. **Reversed axes on every approved map pin.** `_point_wkt(latitude,
   longitude)` returns `POINT(longitude latitude)` — WKT axis order. Both
   `customer_location_requests` call sites passed
   `_point_wkt(address.longitude, address.latitude)`, so `Address.geom` was
   written with the two swapped while `Address.latitude`/`.longitude` stayed
   correct. `gis_sync.py` called it correctly, so the two disagreed in silence.
   A comment at the second site read "Match approve_request's existing arg
   order" — the defect propagated because matching a wrong call site looked
   like correctness.
2. **A projection with no geometry.** `GeoSync.sync_addresses` set
   `GeoLocation.latitude`/`.longitude` and never `GeoLocation.geom`, while the
   duplicate in `customer_location_requests` did. Spatial queries read `geom`.

Both are fixed by making the owners callable rather than by fixing the callers:

- `app.services.gis.point_wkt(*, latitude, longitude)` is **keyword-only**, so
  the positional reversal that caused defect 1 is now a `TypeError`. It is the
  only converter in the repository.
- `subscriber.create_address(db, payload, *, geocode=False)` is flush-only and
  raises `AddressOwnerError`; `Addresses.create` is the HTTP adapter over it.
- `gis_sync.project_address_point(db, address, *, latitude, longitude)` is
  flush-only and idempotent, and writes latitude, longitude **and** `geom` on
  both the `Address` and its `GeoLocation`. `GeoSync.sync_addresses` reuses
  that same operation, which is what closes defect 2 for the sweep as well.

`gis_sync.py` therefore enters the writer census for the first time. That is
the intended direction: the census counts files, not owners, and membership
rose while `customer_location_requests.py` fell from six write sites to one and
the cohort total fell from 101 to 99.

**The stored corruption is not repaired by this change.** Rows written before
the fix still hold swapped `Address.geom`, and projections created by the sweep
still hold a null `geom`. Repairing them is a separate, separately authorized,
measured run of the full sweep with before/after null, drift and digest
counts — deliberately not automatic, because a sweep that rewrites every
address is exactly the shape of operation that should not ride along in a code
merge.

## Writer census

Counted mechanically across every entry-point family by
`scripts/architecture/isp_cohort_writers.py` and frozen by **two** baselines,
because membership and magnitude are different events wanting different
remedies:

- `tests/architecture/isp_cohort1_writer_files_baseline.txt` — *which files*
  write cohort state. A file that starts writing is a design decision somebody
  has to defend.
- `tests/architecture/isp_cohort1_write_sites_baseline.txt` — *how much* each
  writes, plus an exact `TOTAL`. An existing writer going from three sites to
  four is usually a refactor.

Both are two-directional. The magnitude ratchet compares only files present on
both sides, so it never reports an appearance or a removal — membership is the
other guard's job, and each stays silent about the other's business. The exact
total catches the one case per-file counts cannot: a write moved between two
already-baselined files looks like an ordinary shrink-and-grow pair.

| Family | Files | Write sites |
|---|---|---|
| `service` | 19 | 86 |
| `web_presenter` | 5 | 13 |
| `cli_script` | 6 | 14 |
| `migration` | 6 | 8 |
| **total** | **36** | **121** |

`api_route`, `webhook_handler`, `web_route`, `task_worker`, `scheduled_job`,
`event_handler`, `websocket`, `importer`, `poller`, `app_module` and
`repository_root` produce **zero** counted writes. That is a real property of
this repository rather than a gap in the scan: the thin-adapter rule already
keeps routes and tasks out of direct persistence, and the census's sensitivity
tests prove it would count a route — or a webhook — that started writing.

`webhook_handler` is worth stating on its own, because it is the family whose
writers are hardest to notice by reading code: nothing in this repository calls
an inbound callback. Sub has eight of them under `app/api/`, and **none writes
or even directly references** a cohort-1 model or table. `crm_webhooks.py`, the
one that carries customer identity, hands its payload to
`crm_customers.observe_customer` — an observation, delegated to a service, which
is the boundary the SOT registry already declares.

### Classification on three axes

One enum kept forcing dishonest answers — an applied migration is not an
"authorized adapter", and a fixture seeder writes real rows while owning
nothing. Those were three independent questions being asked at once, so the
inventory now answers them separately.

**Authority** — what say the surface has over the fact:

| `AuthorityRole` | Files |
|---|---|
| `PARALLEL_WRITER` | 18 |
| `NO_AUTHORITY` | 13 |
| `DECLARED_OWNER` | 8 |
| `SCHEMA_LINEAGE` | 6 |
| `PROJECTION_WRITER` | 2 |
| `UNDETERMINED` | 0 |

**Boundary** — what it does there:

| `BoundaryRole` | Files |
|---|---|
| `PERSISTS` | 36 |
| `DELEGATES` | 8 |
| `READS` | 2 |
| `TRANSPORTS` | 1 |
| `OBSERVES` | 0 |
| `UNDETERMINED` | 0 |

**Reachability** — how it can still be reached against production:

| `Reachability` | Files |
|---|---|
| `INTERNAL_ONLY` | 23 |
| `ONLINE_REQUEST` | 9 |
| `APPLIED_ONCE` | 6 |
| `OPERATOR_COMMAND` | 4 |
| `BACKGROUND_JOB` | 3 |
| `NON_PRODUCTION` | 2 |
| `UNDETERMINED` | 0 |

The three are genuinely independent, and
`test_the_three_axes_are_orthogonal` proves it over the real inventory rather
than asserting it: for every ordered pair, knowing one value leaves at least
two possibilities open on the other. `PERSISTS` appears with all five writing
authorities; `NO_AUTHORITY` appears with five different boundary roles;
`INTERNAL_ONLY` appears with four different authorities. If that ever
collapses, the axes should be merged rather than kept apart for appearances.

Of the 36 writing surfaces, **28 can write production again**. The other eight
cannot: two touch only disposable databases and six are applied migrations
Alembic will not re-run. All eight stay in the ratchet, because a *new* one is
exactly what the guard should catch; none counts as something a cutover has to
displace, because none can be retired.

### Derived classification

The original eight-member vocabulary survives as a *computed* view over the
axes, so every document and control record written against it still means what
it meant — and there is no second place to edit, so the two cannot disagree.

| `SurfaceClassification` | Files |
|---|---|
| `LEGACY_PARALLEL_WRITER` | 26 |
| `AUTHORIZED_ADAPTER` | 8 |
| `AUTHORITATIVE_WRITER` | 8 |
| `DERIVED_PROJECTION` | 2 |
| `READ_ONLY_CONSUMER` | 2 |
| `TRANSPORT` | 1 |
| `OBSERVATION_COLLECTOR` | 0 |
| `UNKNOWN` | 0 |

`OBSERVATION_COLLECTOR` and `BoundaryRole.OBSERVES` are both zero, and that is
a finding rather than an oversight: **nothing collects an external observation
directly into a cohort-1 table**. Provider payloads terminate in the
Integration Inbox, which is outside this cohort, and
`app/services/crm_customers.py` only *interprets* a verified observation by
matching it to an existing account through exact retained provenance. The one
inbound callback carrying customer identity, `app/api/crm_webhooks.py`, is
inventoried as `TRANSPORTS` for the same reason.

### Disposition — what happens to each surface

Classification says what a surface is; it does not say what becomes of it, and
that is the question a cutover actually needs answered. Every surface carries
one, **including the adapters and readers that write nothing**: a route that
reads a customer account has to read it from somewhere once that account lives
in another application.

| `Disposition` | Files | Meaning |
|---|---|---|
| `ROUTE_THROUGH_OWNER_FIRST` | 14 | Must stop bypassing its declared owner *before* the cohort can be shadowed |
| `RETIRE_AFTER_CUTOVER` | 14 | Displaced by the target; must reach zero for `ctl-isp-009` |
| `REPOINT_TO_TARGET_API` | 11 | Reads or forwards a cohort fact; after the switch it must reach the target through a versioned contract |
| `HISTORICAL_NO_ACTION` | 6 | An applied migration; nothing to retire |
| `NON_PRODUCTION_NO_ACTION` | 2 | Writes only disposable databases |
| `UNDECIDED` | 0 | Needs a decision, and says which one |

There is deliberately no "stays as it is" disposition. Every surface here
touches cohort-1 state — that is the inclusion criterion — and once that state
lives in another application, no such touch survives unchanged. A member
meaning "nothing happens" would be the one anybody reached for to avoid
deciding.

**Every counted writer has an individual disposition; the readers take a
declared default.** 401 files reference cohort state and 47 are inventoried
here. Assigning an individual disposition to the other 356 would be fabrication
at scale — the reference census is a bounded reach, not an impact analysis, and
many of those files only mention a model in a type hint.

The default is `REPOINT_TO_TARGET_API`, and it is an answer rather than a gap
because only one shape is available: ADR 0012 gives the two applications
separate databases, sessions and transactions, so after the switch a file that
reads a cohort fact either reaches the target through a versioned contract or
stops reading it. What the default may never cover is a **writer** — "displace
this" is a decision about a specific line of code, and a guard fails the build
if a counted writer ever falls through to it.

The disposition is about the surface's **cohort-1 touch**, not the whole file.
`account_lifecycle.py` keeps owning subscription lifecycle long after its
Subscriber projection writes are displaced.

`ROUTE_THROUGH_OWNER_FIRST` is the one that gates `ctl-isp-007` rather than
`ctl-isp-009`: a shadow comparison run against a source with two writers cannot
tell drift from the second writer, so those fourteen have to be routed
through their owners before a comparison means anything.

### No undecided surfaces

All three open questions were answered by Governance on 2026-08-21 and
recorded in `programmes/dotmac-isp-replacement.json` at `d91a87f`:

| Surface | Decision |
|---|---|
| `app/services/mrr_snapshot.py` | `subscribers.mrr_total` does **not** migrate; the target recomputes monthly recurring revenue from its own Subscriptions. The writer is displaced with the rest of cohort 1. The export still carries the value as a declared derived field — a reconciliation that cannot see it cannot explain a difference, and carrying it is not the same as migrating it. |
| `app/services/customer_location_requests.py` | dec-isp-007: a product-first `dotmac-addresses` owner takes normalized address, geospatial data and verification history; Customers, Services and Billing hold typed purpose links rather than copies. |
| `app/services/web_system_restore_tool.py` | Customers owns account-recovery **intent**, and the existing cross-domain cascade is decomposed. |

`undecided_surfaces()` now returns empty, and the test asserting that is paired
with a sensitivity check so it cannot pass by the accessor quietly breaking.

**Two of the three answers create work rather than finishing it.** `addresses`
still has no *Sub* owner — dec-isp-007 names the target owner, and product-first
extraction needs a proven Sub implementation to extract from, so Sub must grow
that owner before the write can be routed and before the cohort can be
shadowed. And decomposing the recovery cascade is what makes the cohort-1 half
separable at all: today one restore touches invoices, payments, credentials,
RADIUS, IP assignments and ONT assignments in the same pass, and only the
account rows belong to cohort 1.

### Declared owners

| Path | Owner | Writes |
|---|---|---|
| `app/services/party.py` | `party.registry` | 21 |
| `app/services/customer_canonical_profile_patch.py` | `customer.canonical_profile_patch` | 20 |
| `app/services/subscriber.py` | `customer.accounts` | 14 |
| `app/services/gis_sync.py` | `gis.spatial_sync` | 3 |
| `app/services/web_customer_actions.py` | `customer.profile_commands` | 7 |
| `app/services/brand_profiles.py` | `customer.branding` | 2 |
| `app/services/subscriber_profile_cleanup.py` | `customer.profile_cleanup` | 2 |
| `app/services/crm_customer_name_repair.py` | `customer.name_remediation` | 1 |

`web_customer_actions.py` is named like a presenter and declared as an owner.
It stays in the `web_presenter` family because that is where a reader will
find it, and it is classified as an owner because the registry says so. Family
is location; classification is authority. Conflating them is how a real owner
gets mistaken for a stray adapter — or the reverse.

### Derived projections written onto cohort rows

| Path | Owner | Fact |
|---|---|---|
| `app/services/account_lifecycle.py` | `access.subscription_lifecycle` | Subscriber lifecycle status and its override columns |
| `app/services/mrr_snapshot.py` | **none declared** | `subscribers.mrr_total` |

Both must cross to the destination as *derived*, never as the account's own
state. `mrr_total` is the sharper risk: a money figure, computed elsewhere,
stored on the account row, written by a module the registry does not declare.
The target recomputes it; it does not trust it.

### Legacy parallel writers — the displacement list

Twenty-six files write a cohort fact some other owner is declared to own.
**Eighteen of them can do it again**, and that eighteen is the set `ctl-isp-009`
must ratchet to zero; the remaining eight are listed for completeness and
marked non-production. Fourteen now carry `ROUTE_THROUGH_OWNER_FIRST` and
fourteen `RETIRE_AFTER_CUTOVER`, following the 2026-08-21 decisions.

| Path | Bypasses | Entity |
|---|---|---|
| `app/services/account_deletion.py` | `customer.accounts` | account `metadata` |
| `app/services/billing_cleanup_remediation.py` | `customer.accounts` | `billing_mode` |
| `app/services/crm_portal.py` | `party.registry` | `crm_subscriber_id` |
| `app/services/crm_ticket_pull.py` | `party.registry` | `crm_subscriber_id` |
| `app/services/customer_location_requests.py` | `customer.accounts` | account `metadata` |
| `app/services/customer_portal_contacts.py` | `party.registry` | `subscriber_contacts` |
| `app/services/customer_portal_notifications.py` | `customer.accounts` | account `metadata` |
| `app/services/network_subscriber_bridge.py` | `customer.accounts` | constructs `Subscriber` |
| `app/services/nin_verifications.py` | `customer.accounts` | account `metadata` |
| `app/services/team_inbox_commands.py` | `party.registry` | constructs `PartyRelationship` |
| `app/services/web_admin_resellers.py` | `customer.accounts` | `reseller_id` |
| `app/services/web_customer_details.py` | `customer.accounts` | account `metadata` |
| `app/services/web_system_import_wizard.py` | `customer.accounts` | constructs `Subscriber` |
| `app/services/web_system_restore_tool.py` | `customer.accounts` | `is_active`, account `metadata` |
| `scripts/migration/backfill_crm_subscriber_links.py` | `customer.accounts` | raw DML |
| `scripts/migration/backfill_party_status.py` | `party.registry` | raw DML |
| `scripts/migration/import_crm_phase3.py` | `customer.accounts` | raw DML |
| `scripts/one_off/backfill_crm_subscriber_ids.py` | `customer.accounts` | raw DML |
| `scripts/seed/seed_test_fixtures.py` | — | fixtures only, non-production |
| `scripts/migration/kernel_lineage_rehearsal_canaries.py` | — | rehearsal canaries, non-production |
| `alembic/versions/045_contact_channels_without_name.py` | — | applied migration, non-production |
| `alembic/versions/116_add_billing_accounts.py` | — | applied migration, non-production |
| `alembic/versions/208_map_karu_customers_to_bts.py` | — | applied migration, non-production |
| `alembic/versions/267_brand_profiles.py` | — | applied migration, non-production |
| `alembic/versions/277_lifecycle_communications_sot.py` | — | applied migration, non-production |
| `alembic/versions/383_replaceable_backoffice_boundary.py` | — | applied migration, non-production |

### The `subscribers.metadata` finding

Seven modules write `subscribers.metadata`: account deletion, NIN
verification, portal notification read-state, CRM name repair, location
requests, the customer detail presenter and the system restore tool. No owner
declares its shape, no schema constrains it, and its keys are added by
whichever feature needed somewhere to put something.

This is the single most consequential fact in the cohort for migration
purposes, and it is why the export contract treats the blob as an **inventory
of keys plus a digest**, never as typed facts. A migration that reads
structure into that column is inventing a contract nobody owns, and it would
carry seven features' private conventions into the destination as though they
were data.

## Adapters and callers

Nine adapters reach cohort state and write none of it. They validate,
authorise, delegate to an owning service, and own only the transaction —
which is what `db.commit()` in a route means here.

| Path | Family | Guard |
|---|---|---|
| `app/api/subscribers.py` | `api_route` | `require_permission("customer:read"/"create"/"update"/"delete")` |
| `app/api/me.py` | `api_route` | authenticated customer session |
| `app/api/reseller.py` | `api_route` | reseller-scoped permission |
| `app/web/admin/customers.py` | `web_route` | `require_permission("customer:read"/"write")` |
| `app/web/admin/system.py` | `web_route` | admin permission |
| `app/web/admin/billing_payments.py` | `web_route` | billing permission — reads accounts only |
| `app/web/customer/routes.py` | `web_route` | authenticated customer session |
| `app/web/customer/location.py` | `web_route` | authenticated customer session |
| `app/tasks/nin_tasks.py` | `task_worker` | worker context; writes verification rows outside this cohort |
| `app/api/crm_webhooks.py` | `webhook_handler` | provider signature verification; hands the payload to a service and writes no cohort row |
| `app/services/crm_customers.py` | `service` | reads accounts by exact CRM provenance; creates and updates nothing |

## Reader reach and downstream consequences

Writers are the set a cutover has to displace. Readers are the set it has to
not surprise, and they are an order of magnitude larger: **401 files** name a
cohort-1 model or table somewhere in their body — measured by
`python -m scripts.architecture.isp_cohort_writers` on this consolidation
commit.

| Family | Files referencing cohort state |
|---|---|
| `service` | 222 |
| `web_presenter` | 48 |
| `migration` | 46 |
| `cli_script` | 46 |
| `app_module` | 21 |
| `web_route` | 10 |
| `event_handler` | 3 |
| `api_route` | 3 |
| `task_worker` | 2 |
| `webhook_handler` | 0 |

This is a deliberately coarse measure — a mapped-class name anywhere in the
module, or a table name in a string that also contains a SQL keyword — and it
is a **bounded reach, not an impact analysis**. Overstating its
precision would invite someone to treat it as complete, and it is not: a
module that reads a cohort fact through a service helper, without naming a
model, does not appear.

### External projections

Cohort-1 facts leave Sub through several transports. None of them owns the
fact, and every one of them will need reconciling after a later cutover:

| Transport | What of the cohort it carries |
|---|---|
| RADIUS (`app/services/radius*.py`) | Account identity and lifecycle projected into access state and credentials |
| CRM (`app/services/crm_*.py`) | Customer identity in both directions — inbound as observation, outbound as views and reporting |
| ERP (`app/services/erp_*.py`) | Account references on billing and domain synchronisation |
| UISP (`app/services/topology/uisp_sync.py`) | Subscriber references on network device synchronisation |
| Team inbox (`app/services/team_inbox_projection.py`, `team_inbox_contact_links.py`) | Contact-to-conversation links built from `subscriber_contacts` and party relationships |
| `customer_identity_index` | A local, rebuildable search index over identity — a cache, never a source |
| Notification delivery | Addressed from contact points and account contact columns |

Every one of these is a **transport or a projection**, not an authority. That
is the existing SOT position and this document does not change it; it records
them here because "who reads this after the switch" is the question a cutover
checklist forgets until something stops being delivered.

## Deliberate exclusions

Ten cohort-adjacent tables are outside the export contract, each recorded in
`surfaces.UNMAPPED_ADJACENT_TABLES` with its reason. In summary: `resellers`
and `reseller_users` belong to Governance cohort 7; `customer_identity_index`
is a rebuildable search index; `subscriber_channels` belongs with cohort 6
communications; `subscriber_nin_verifications` is regulatory evidence needing
its own disposition decision; `subscriber_custom_fields` has no declared
schema; and `carried_source_identity_adjudications`,
`party_identity_backfill_receipts` and the two
`subscriber_contact_*_projections` tables describe the migration rather than
constitute source state.

## Two tables with no counted writer

`organizations` and `organization_memberships` have **zero** counted writers.
No construction, no tracked mutation, no set-based DML and no raw statement
names them anywhere under `app/`, `scripts/` or the executable migration
lineage — apart from one applied migration writing back-office reference
columns on `organizations`.

This is a searched result, not UNKNOWN, and it is not proof. The census cannot
see a mutation through a local it could not bind to a cohort model, a generic
`setattr` helper, a SQLAlchemy event hook, or a writer outside the scanned
roots. It reads "no counted writer" for that reason. The disposition of these
rows — historical B2B account records with no live owner — is an open item for
`ctl-isp-006`.

## Unresolved

- `dec-isp-002` — production deployment owner for `asm-dotmac-isp`. Michael's.
- `dec-isp-003` — the enforceable legacy Sub transition rule. Michael's.
- The disposition of `organizations` / `organization_memberships` rows.
- An owner for `subscribers.mrr_total`, or an explicit decision that the
  target recomputes it and the column is not migrated.
- An owner and shape for `subscribers.metadata`, or an explicit decision that
  it crosses as an opaque inventory only.
- Whether `subscriber_nin_verifications` migrates, is retained in Sub as
  regulatory evidence, or is retired.
