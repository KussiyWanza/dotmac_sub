# Vendor Route-Revision Authoring

Status: implemented UI slice

## Screen contract

- Route: `/vendor/projects/{project_id}`
- Audience: an authenticated native vendor member who owns a quote for the
  installation project.
- Decision supported: define the physical route the vendor proposes to build,
  check its measured length, and decide when a draft is ready for staff review.
- Primary action: save the currently drawn route as a new draft revision.
- Secondary action: submit an existing draft revision for review.
- Out of scope: staff acceptance or rejection. Staff review remains a separate
  workflow.

## Ownership and authoritative data

`operations.vendor_project_workspace` owns the route-authoring read and action
projection and coordinates the typed create and submit commands.
`operations.vendor_project_records` remains the only writer for
`ProposedRouteRevision` rows and their lifecycle events. The HTML routes are
thin adapters: they validate form input, open no business transaction of their
own, and delegate to those owners.

The vendor map is deliberately scoped to the authenticated vendor. During
bidding it can show proposed revisions only from that vendor's quote. As-built
context is visible only when the same vendor is assigned to the project. The
admin route map retains its broader project-level view.

## Interaction and state behavior

- The vendor adds points by tapping the Leaflet map or using device
  geolocation, and can undo or clear the current trace.
- The route-authoring map exposes first-pass operations filters: vendors can
  toggle proposed routes, as-built routes, and closure proposals; narrow context
  by visible route/review status; and choose which reference-plant types are
  searched or loaded near their current device location.
- The filter panel shows a live summary of visible route/context features,
  reference plant, active layers, statuses, and point types, plus a compact
  legend for route lines, closure proposals, reference plant, and the current
  trace.
- The browser derives a GeoJSON `LineString` and an estimated length. The
  server validates geometry type, coordinate count, finite numbers, and
  longitude/latitude bounds before invoking the owner command.
- At least two points are required. Errors are shown inline and do not discard
  the trace.
- Saving always creates the next numbered draft revision; it does not overwrite
  prior route evidence.
- The revision rail is newest-first, uses the canonical proposed-route status
  presentation, shows staff notes where present, and can focus a saved line on
  the map.
- Only a draft revision exposes the submit action. Submission locks that
  revision for review; later changes require a new revision.
- Without a quote, the stable empty state directs the vendor to start one.
  With a quote but no revisions, the map remains usable and explains how to
  create the first draft.
- Point-of-interest search remains vendor-authenticated and proximity scoped by
  the existing field map endpoint. Assignment-to-plant scoping is a separate
  field-map boundary gap until that relationship exists.
- The reference-plant overlay uses the same selected point types and fixed
  radius options of 500 m, 1 km, 5 km, and 10 km. Clearing the overlay does not
  remove the route trace the vendor is drawing. Reference plant is explicitly
  not an official project assignment.
- Vendor route-authoring point filters are pinned to
  `VENDOR_ROUTE_AUTHORING_POI_TYPES` in `app.services.field.map_assets`; changes
  to the supported field-map asset vocabulary must update that contract, the UI,
  and test evidence together.
- As-built route capture uses the same Leaflet/static-script and inline error
  pattern as proposed-route authoring. Its submitted GeoJSON is normalized with
  the same WGS84 `LineString` validator before the signed submission preview is
  issued.

The map, action controls, and revision rail remain in one vertical flow on
small screens. Map and rail selection stay synchronized through revision IDs.

## Validation evidence

- `tests/test_vendor_route_revision_authoring.py`
- `tests/test_vendor_project_workspace.py`
- `tests/architecture/test_vendor_project_workspace_boundary.py`

## Operations map status

Implemented for the vendor project map:

- Vendor-scoped proposed route context.
- Assigned-vendor as-built route context.
- Closure proposal context where the vendor can submit closures.
- Layer filters for proposed routes, as-built routes, and closure proposals.
- Status filters for draft, submitted, accepted, rejected, pending, and applied
  map context.
- Vendor POI filters for FDH cabinets, splice closures, access points, service
  buildings, and wireless masts.
- Reference-plant loading from the vendor-authenticated field-map API with
  500 m, 1 km, 5 km, and 10 km radius options.
- Search against selected vendor POI types.
- Live filter summary, legend, inline errors, request loading state, and stale
  search/nearby request cancellation.
- All/none filter controls for layers, statuses, and reference plant types.
- Shared WGS84 `LineString` validation for proposed and as-built traces.

Hardened in this slice:

- Vendor map filter options are typed backend contracts rather than hardcoded
  template values.
- Vendor map asset API requests accept only vendor route-authoring POI types,
  reject blank type lists, and keep FastAPI radius/coordinate bounds.
- The template render test verifies the backend contracts produce the expected
  filter controls.

Still missing or awaiting a separate boundary:

- Assignment-to-plant scoping. The current vendor POI endpoint remains
  proximity-scoped because there is no authoritative project-to-plant
  relationship to enforce yet.
- Full browser/mobile acceptance on a host with the repository Playwright stack
  available.
- Later comparison with the main network map to decide what should be shared
  and what should stay vendor-specific; the current plain comparison is in
  `docs/designs/VENDOR_OPERATIONS_MAP_NETWORK_COMPARISON.md`.
