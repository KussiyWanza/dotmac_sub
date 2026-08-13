# Network Map V2 parity

Status: experimental, read-only duplicate at `GET /admin/network/map-v2`.
The established `GET /admin/network/map` route, template, and behavior remain
unchanged.

## Comparison evidence

The CRM route and its directly rendered template were reviewed from the
`michaelayoade/dotmac_crm` `main` source corresponding to
`/admin/network/map`. Both deployed URLs require authentication, so this
comparison makes no claim about tenant data currently present in either
production environment.

| Network Map feature | CRM | Selfcare | Status | Required work |
|---|---|---|---|---|
| FDHs | Dedicated marker layer and counts | Dedicated marker layer | Complete | Reuse the existing projection |
| Splice closures | Dedicated marker layer and counts | Dedicated marker layer | Complete | Reuse the existing projection |
| Access points | Dedicated marker layer and counts | Dedicated marker layer | Complete | Reuse the existing projection |
| Feeder, distribution, and drop fibre | Distinct stored-route styles; also contains a straight endpoint fallback | Distinct stored-route styles with no fallback | Complete | Preserve stored geometry only; do not copy the CRM fallback |
| Explicit OLT layer | Dedicated layer | Existing plant projection, not used by the original page | Partial | Add the approved matched OLT projection to V2 |
| Base-station layer | Dedicated `site_role` layer | No authoritative Network Map projection | Missing | Keep visibly unavailable pending an owner decision |
| Service-building layer | Dedicated layer | Existing plant projection, not used by the original page | Partial | Add the existing projection to V2 |
| Live field technicians | Polls a separately permissioned live feed every 30 seconds | Not part of `network:map:read` | Missing | Keep unavailable; do not broaden V2 permissions or couple owners |
| Layer visibility | Individual toggles | Individual toggles | Complete | Preserve and extend for V2 overlays |
| Dynamic layer counts | Per-layer and visible totals | Static inventory totals | Partial | Add V2 loaded/visible counts |
| Layer groups and presets | OSP, backbone, edge, POP/sites, all, clear | No presets | Missing | Add V2-only presets |
| Text and coordinate search | Yes | Yes | Complete | Preserve and include V2 overlay fields |
| Asset popups/details | Canonical ID and available asset fields | Available asset fields and links | Partial | Add V2 OLT/building/topology details |
| Distance measurement | Local measurement overlay | No | Missing | Add a clearly labelled non-topology measurement tool |
| Nearest-FDH lookup | Server-side distance lookup | No | Missing | Add read-only nearest loaded FDH inspection; imply no route |
| Segment deep link | `segment_id` query support | Coordinate `focus` support only | Missing | Add `segment_id` support to V2 |
| Route planning and cost estimation | Browser estimate and a server response containing a straight line | No | Not applicable | Do not copy; the CRM implementation is not authoritative route geometry |
| Direct marker movement and role updates | Direct map mutation | No | Not applicable | Requires governed Selfcare commands and approval ownership |
| Direct duplicate merge | Direct map mutation and audit row | No | Not applicable | Requires a governed merge owner and conflict policy |
| QA and change-request navigation | Links from the map | Existing change-request link | Partial | Preserve navigation; do not duplicate workflow state in V2 |
| Explicit segment endpoints | Used as a fallback when geometry is absent | Canonical termination points exist but are not presented | Partial | Show endpoints and topology status without drawing fallback lines |

## V2 ownership and connectivity rules

`ui.network_map_projection` remains the read owner. V2 composes the established
`NetworkMapProjection` and `NetworkMapPlantProjection`; it creates no inventory,
GIS, technician, planning, or topology records.

- A fibre line is drawn only when the canonical plant projection supplies a
  validated stored `LineString`.
- Segment endpoint details come only from `FiberSegment.from_point_id` and
  `FiberSegment.to_point_id` and their canonical termination-point records.
- An endpoint is explicitly attached when it has a canonical `ref_id` or is
  shared by more than one active segment.
- Coordinate proximity never creates an attachment. Nearby endpoints with
  different canonical IDs remain disconnected.
- Missing or invalid geometry is labelled incomplete. V2 may focus its endpoint
  markers, but never draws a line between them.
- Measurement graphics are labelled as measurements and never enter the fibre
  layer or topology projection.

## Functional, data, and architecture gaps

Functional gaps addressed by V2 are the explicit OLT and service-building
overlays, loaded/visible counts, layer presets, expanded search selection,
measurement, nearest-FDH inspection, endpoint status, and segment deep links.

Data gaps remain operational inputs, not rendering defects: unmatched active
OLTs are counted but omitted because no approved monitoring-device/POP match can
position them; an empty FDH, closure, building, or fibre class may mean no
canonical records exist; and endpoint, splitter, tray, splice, port, or route
geometry completeness depends on canonical network data. This change performs
no import, backfill, migration, or production database write.

Architecture decisions are still required for base-station identity and
position, live-technician permission composition, governed asset movement,
connectivity/topology proposals and approvals, controlled duplicate merging,
QA remediation, route-planning ownership, and cost-estimation ownership. Those
features are not copied from CRM merely because its page exposes direct actions.

## Validation contract

Focused Python tests cover route isolation, matching permissions, template
separation, original-template immutability, explicit topology, missing geometry,
and nearby unrelated endpoints. Node tests cover coordinate and asset search,
nearest-FDH selection, layer classification, and endpoint identity behavior.
The original route remains the production Network Map; V2 is a separate
read-only preview and does not deploy or migrate data.
