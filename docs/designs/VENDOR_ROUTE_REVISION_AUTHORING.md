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

The map, action controls, and revision rail remain in one vertical flow on
small screens. Map and rail selection stay synchronized through revision IDs.

## Validation evidence

- `tests/test_vendor_route_revision_authoring.py`
- `tests/test_vendor_project_workspace.py`
- `tests/architecture/test_vendor_project_workspace_boundary.py`
