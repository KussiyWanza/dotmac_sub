# Vendor Operations Map vs Network Map

Status: vendor map slice implemented; network-map parity review pending.

## Vendor operations map

- Surface: `/vendor/projects/{project_id}`.
- Audience: authenticated vendor members working on their own project workspace.
- Primary jobs: draw proposed routes, submit route revisions, capture as-built
  traces, propose closures, and inspect nearby or wider-area reference plant.
- Scope today: vendor route context is vendor/project scoped; POIs are
  vendor-authenticated and proximity scoped.
- Filters implemented: route layer, route/status, reference-plant type, nearby
  radius, and all/none controls for layer/status/POI groups.
- POI types: FDH cabinets, splice closures, fiber access points, service
  buildings, and wireless masts.
- Hardening: typed filter contracts, inline errors, loading state, stale request
  cancellation, and server-side vendor POI type validation.

## Main network map

- Surface: admin network/fiber map routes and Network Map V2.
- Audience: internal users with network map/fiber permissions.
- Primary jobs: inspect authoritative network/fiber assets, governance proposals,
  topology context, and broader operational network state.
- Scope: internal permission-gated network view; not vendor-assignment scoped.
- Network Map V2 governed proposal asset types currently include FDH cabinets,
  splice closures, access points, and support structures.
- Separate fiber-plant maps include richer plant/topology views and planning
  behavior that should not be copied into the vendor portal without an explicit
  vendor contract.

## Current difference

- Vendor map is workflow-specific: it centers proposed/as-built vendor evidence
  and selected nearby or wider-area reference plant.
- Network map is authority/review-specific: it centers internal network plant,
  governance, and topology.
- Vendor POIs use the shared field-map asset service, but vendor filter behavior
  and vendor as-built/proposed route actions are not network-map behavior.
- Vendor map includes service buildings and wireless masts; Network Map V2's
  governed proposal list does not currently expose those same proposal types.
- Vendor map deliberately permits reference plant outside official project scope
  within bounded radius options. It does not yet distinguish official
  project-assigned plant because no authoritative project-to-plant relationship
  exists in the vendor assignment model.

## Review decision still needed

- Which POI vocabulary should be shared across vendor and network maps.
- Whether support structures belong in the vendor POI list.
- Whether service buildings and wireless masts belong in Network Map V2 governed
  proposals.
- Which filter UX should be shared as a component versus kept vendor-specific.
- What owner will define the project-to-plant assignment relationship needed for
  vendor POI scoping.
