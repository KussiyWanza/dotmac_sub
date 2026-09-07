# Project infrastructure scope

`operations.project_lifecycle` owns the optional `Project.infrastructure`
relationship. Migration 578 adds one link row per project, with exactly one of
seven native inventory foreign keys. The infrastructure kind is derived from
the populated FK; names and metadata are never relationship authority.

`network.infrastructure_catalogue` owns typed, bounded search and exact-reference
resolution for both customer filters and project selectors. Inventory remains
with existing owners; its implementation lives in the cross-domain service layer
at `app.services.infrastructure_catalogue` because NAS records are catalog-owned.
The standalone network package does not acquire a catalog dependency.
New selections must have the requested role
and be active where that inventory has an active flag. Historical labels remain
readable after archival. Catalogue results contain labels and context only.

Cable Rerun permits a customer, infrastructure, both, or neither while planning.
With a vendor-enabled template and either customer or infrastructure, the project
owner calls the existing `operations.installation_scope` flush-only participant
using `EnsureProjectScope`; that participant returns `ProjectScopeOutcome`.
Creation and editing use this same boundary. The project lock precedes the scope
lock; the unique project binding prevents duplicates. Inactive or mismatched
existing scopes fail closed. A repeat returns the existing ID without another
creation event. No customer is invented for plant work. Buildout and sold-work
entry paths remain supported.

Vendor assignment itself still belongs to the vendor lifecycle owner: an active
draft scope and active vendor are required. Selecting inventory does not assign
a vendor or authorize a quote/payment. Once work is assigned or published,
generic project edits cannot change the infrastructure target. A draft vendor
scope cannot lose its last customer/infrastructure/buildout referent.

## Page contract

Service-delivery staff select what a cable rerun is for on project create/edit.
The optional section sits before customer context, with type then typeahead.
It searches after two characters, returns at most 20 results, preserves submitted
selection on validation errors, and supports clearing and keyboard selection.
Loading, no matches, and lookup failure are distinct. New searches invalidate
old selections and stale responses. Labels render as text. Controls stack on
mobile and support dark mode. Create/save remains the primary action.

The lookup accepts `project:create` or `project:update`; customer read access is
not required. The customer endpoint retains `customer:read`. The lifecycle owner
revalidates submitted references. The project detail resolves the live inventory
label. Existing customer notification rules still require a linked customer.

## Migration and repair

578 is additive, creates an initially empty table, and does not scan or rewrite
projects. Use the repository Alembic lock timeout (5 seconds by default) and
deployment statement budget; retry the entire migration on lock contention.
Existing project scope must never be guessed from names, imported metadata, or
nearby customers. Operators review each affected project and save its selected
asset and vendor-enabled template through the ordinary edit command. That
creates its missing scope atomically and idempotently. Projects with neither
customer nor reviewed infrastructure remain unscoped.

Application rollback retains the additive schema. Downgrade refuses to remove
nonempty relationship evidence; use a reviewed forward fix instead. Validate
both the real 577-to-578 upgrade and fresh baseline-to-head on disposable
PostgreSQL/PostGIS, plus FK and exactly-one-target enforcement. SQLite tests are
fast behavior checks only, never migration acceptance.

UI checklist: information/action and responsive contracts are above. Financial,
bulk, dashboard, chart and network-device operation controls are N/A. Scope edits
emit typed `project.infrastructure_changed` evidence with old/new references
and the owner command ID; scope creation emits one
`installation_scope.created` event in the same transaction.
