# Network Operations Map

Status: adopted. This document is the checked-in page and ownership contract
for the comprehensive admin network map at `GET /admin/network/map`.

## Ownership

`ui.network_map_projection` owns the immutable, read-only map projection in
`app.services.network_map`. It composes existing owners and makes no inventory,
topology, device-health, customer-service, or session-lifecycle decision.

- `network.identity` owns infrastructure identity and persisted point
  coordinates; `network.fiber_topology` owns validated fiber route geometry.
- `network.device_state` owns device and ONT working/not-working verdicts,
  reasons, and semantic presentation.
- `network.radius_sessions` owns subscription-scoped connected, stale, offline,
  and inactive session observations, their exact binding, and `observed_at`.
- `customer.accounts` owns customer identity and mapped service addresses.
- `access.subscription_lifecycle` owns the customer subscription cohort.
- `ui.status_presentation` owns access-session labels, tones, and icons.
- `auth.permission_gate` owns `network:map:read` and `customer:read` capability
  decisions.

The owner returns `NetworkMapProjection`. The web adapter serializes that value
once through `to_template_context()`. The template does not query models,
inspect accounting rows, derive connectivity, map raw state to semantic colors,
or generate customer relationship URLs from imported identifiers.

## Dispatch plant subset

`ui.network_map_projection` also owns `NetworkMapPlantProjection`, the
read-only GeoJSON subset served by `GET /admin/network/map/plant-data` for the
dispatch live map. It reads only plant identity, geometry, and cached device
state: PoP/BTS sites, network devices, OLTs positioned through their matched
network-device and PoP relation, FDHs, closures, access points, service
buildings, and feeder/distribution/drop routes. Customers, ONTs, subscriptions,
and session resolution are explicitly outside this query. Active OLTs without a
matched, mapped network device are omitted and reported as `unmatched_olts`.
The endpoint requires `network:map:read`; the dispatch route retains only
`operations:dispatch:read` and omits plant controls for viewers without the
additional permission.

## Page contract

- Screen: `admin.network.operations_map`; incident/NOC investigation page.
- Audience and job: NOC and network-operations staff locating infrastructure,
  customers, and current access-session evidence geographically.
- Decision supported: identify the mapped subject, inspect its owner-provided
  operational/session state, and navigate to the canonical detail or exact
  infrastructure-scoped customer cohort.
- Primary entity: typed network-map feature with canonical UUID and persisted
  or validated geometry.
- Authoritative read owner: `ui.network_map_projection`.
- First viewport: map canvas, search, current owner-provided health/session
  summary, independently controllable layers, and the selected-feature panel.
- Actions: fit/refresh/copy coordinates are local map tools. Detail and cohort
  links are owner-projected secondary navigation actions with a required
  permission. This screen performs no business mutation.
- Sensitivity: customer names, addresses, and customer drill-downs require the
  map route plus the destination's `customer:read` enforcement. Customer links
  are absent when that capability is unavailable.
- Drill-downs: canonical infrastructure details, customer 360/network path,
  and exact location/cabinet customer-list filters.
- Responsive projection: the map remains the first work surface; health,
  inventory, legend, layers, and actions stack below it without changing state
  semantics.

## Customer access-session semantics

The projection selects one customer marker state from the customer's typed
subscription snapshots in this order: connected, stale, offline, inactive.
For equal states it retains the newest `observed_at`. The state remains visible
with its owner and observation time.

- `connected` renders the status owner's **Connected** positive presentation
  and enters the Connected layer.
- `stale` renders **Last seen** with warning semantics and enters the Not
  connected layer. It is never silently promoted to Connected.
- `offline` renders **Not connected** with neutral semantics.
- `inactive` remains a separate owner value and also renders the owner's neutral
  Not connected presentation.

The two customer layers are presentation cohorts owned by
`ui.network_map_projection`; they do not write customer or session state.
Geographic proximity never proves customer-to-infrastructure connectivity.
Location and cabinet cohort links consume the canonical customer-list filters.

## Availability, freshness, and failure behavior

- The projection is rebuilt on every request; it is not an authoritative cache.
- Session provenance carries `network.radius_sessions`, the binding word, and
  `observed_at`. Unknown timestamps remain absent rather than becoming current.
- Missing feature coordinates omit only that feature. Missing customer session
  evidence produces the owner's inactive/offline presentation; it does not
  manufacture a positive observation.
- A configured customer map limit bounds rendered customer markers while the
  page explicitly reports the rendered and total mapped-address counts.
- Malformed PostGIS line geometry fails the projection instead of guessing or
  drawing an inferred path.

## Migration and verification

Retired in this slice:

- `build_network_map_context() -> dict` and its free-form feature bags;
- direct `RadiusAccountingSession` inspection in the UI projection;
- the transport-level `is_online` boolean;
- JavaScript Online/Offline label, tone, and layer derivation;
- template-generated customer detail and infrastructure-cohort URLs.

Verification is provided by
`tests/test_customer_network_operations_map.py`,
`tests/test_network_map_support_structures.py`, and
`tests/architecture/test_network_map_projection_boundary.py`, plus the existing
network-map, device-state, template compilation, and repository architecture
suites.
