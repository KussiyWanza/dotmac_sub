# Vendor delivery portfolio UI

## Scope

The admin vendor detail page exposes the operational relationship between one
native vendor and Dotmac. It remains a read-only portfolio: project lifecycle,
quotes, route revisions, as-built evidence, purchase invoices, material
releases, advances, and provider observations retain their existing owners.

Vendor creation, editing, portal-user administration, and deactivation are
outside this slice.

## Ownership

| Concern | Owner |
|---|---|
| Assigned installation-project lifecycle | `operations.vendor_project_lifecycle` |
| Quote, proposed-route, and as-built records | `operations.vendor_project_records` |
| Purchase invoice and payment observation | `operations.vendor_purchase_invoices` |
| Material release decision and issue observation | `operations.vendor_material_release` |
| Advance decision and payables observation | `operations.vendor_advances` |
| Current project delivery record selection | `ui.project_vendor_delivery_projection` |
| Latest active supply record selection | `ui.vendor_supply_projection` |
| Vendor portfolio filtering, KPIs, pagination, and field visibility | `ui.vendor_delivery_portfolio_projection` |
| Labels, semantic tones, and icons | `ui.status_presentation` |
| Vendor scope and field-level capabilities | `auth.permission_gate` |

The admin route supplies the authenticated vendor UUID and read capabilities.
It does not query project records or select current delivery records itself.

## Projection contract

`VendorPortfolioQuery` carries:

- the exact native vendor UUID;
- inventory, fiber-route, and accounts-payable read decisions;
- an optional exact installation-project status;
- an optional project name/code/number search;
- a bounded page size and offset.

`VendorDeliveryPortfolio` returns:

- exact-status KPI cohorts;
- stable status options from `ui.status_presentation`;
- a bounded page of assigned projects ordered by installation update time and
  UUID;
- current quote, route, as-built, and purchase-invoice summaries;
- latest active material-release and advance summaries;
- exact drill-down URLs;
- total count and previous/next page state.

KPI links reproduce the exact vendor and lifecycle-status cohort. The total KPI
counts every active project assigned to the vendor. The other KPIs use one
authoritative lifecycle value each (`approved`, `in_progress`, and `completed`)
rather than inventing cross-domain health labels.

## Permission contract

- `inventory:read` is required for the vendor detail page and exposes project,
  quote, as-built, and material facts.
- `network:fiber:read` exposes proposed-route facts.
- `finance:ap:read` exposes quote amounts, purchase invoices, payment
  observations, advances, and payables observations.

Protected fields and their drill-down URLs are absent when their capability is
absent. Templates do not reproduce permission rules.

## Current-record and provider semantics

The portfolio reuses `ui.project_vendor_delivery_projection` for current quote,
route, as-built, invoice, and payment selection. It does not call that
projection once per project; the page query eagerly loads the required records
and composes each row in memory.

`ui.vendor_supply_projection` selects at most one latest active material release
and advance per project using a stable created-time and UUID order. Provider
issue and payables states retain their explicit `unknown`, `unavailable`,
`not_applicable`, `present`, and `stale` semantics. Dotmac approval never
renders as stock issued or money paid.

## Empty, filtering, and pagination states

No assigned projects is a valid empty portfolio, not an error. A filtered empty
page explains that no projects match and links operational assignment back to
the vendor operations workspace.

Page sizes are bounded from 10 to 100. An out-of-range page is normalized to the
last available page. Search and exact status persist across pagination links.

## Validation

Focused tests cover:

- exact vendor scoping;
- current-record selection;
- latest supply selection;
- KPI-to-cohort parity;
- permission-based omission;
- provider freshness semantics;
- search, status, stable ordering, and pagination;
- empty portfolios;
- bounded query behavior;
- route delegation and template-only rendering;
- absence of commits, writes, and template-derived lifecycle rules.
