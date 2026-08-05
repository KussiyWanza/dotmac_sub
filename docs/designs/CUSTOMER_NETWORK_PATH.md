# Customer Network Path

Status: adopted (programme slices PR 1 — shared graph contract and ownership
cleanup — and PR 2 — the Customer 360 Network Path component with deep links,
repair destinations, and passive-fibre expansion).
This document is the checked-in contract for
`ui.customer_network_path_projection` and the shared network graph vocabulary
in `app/services/network_graph.py`. Presentation and information rules follow
`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`.

## Decision

`app.services.customer_network_path` is the read-only owner of the Customer
360 network path projection: the serving-endpoint presentation and the typed
graph view rendered for every subscription on the admin customer detail page.

It composes existing owners and decides nothing of its own:

- `network.access_path` keeps ownership of path identity, hop ordering, and
  typed breaks (`CustomerPath`, `AccessPathSummary`,
  `SubscriberTopologyTrace`). This projection never adds, removes, or reorders
  a hop, and never bridges a gap from names, geography, or proximity.
- Observation owners (`network.olt_observed_state` ONT facts,
  `network.radio_signal` RF freshness, forwarding declarations) keep ownership
  of each hop's state word and observation time, already layered into the
  trace by `network.access_path`.
- `ui.status_presentation` owns the label/tone/icon meaning of hop states,
  path gaps, serving-endpoint sources, and RF signal freshness. Passive plant
  carries the distinct `not_applicable` ("Passive") word — identity and
  continuity, never a fabricated up/down.
- `network.fiber_topology` owns the validated passive fibre plant trace
  (hops, evidence, splitter losses, typed gap codes) composed into the
  optional "Fibre plant details" expansion.

The projection answers exactly:

- Which assets serve this subscription, in which proven order?
- What did each hop's owner last observe, when, and how fresh is it?
- Where does the path stop being provable, and with which owner code?
- Which record proved the serving endpoint (live session, ONT assignment,
  UISP observation, provisioning), and is it partial?
- What is the ONT receive power / RF signal, as an owner-composed display
  string?
- Where are the customer and the geographically mapped assets on that proven
  path, and which path or coordinate gaps prevent a complete map?

Unknown, stale, unavailable, and not-applicable stay distinct. An unenriched
hop renders "unknown", never "up". A missing or aged observation is reported
with its age, never converted into a customer-down claim.

## Shared graph contract

`app/services/network_graph.py` defines the immutable vocabulary shared by
this projection and the future subject-centred network explorer surface:

- `NetworkGraphNode` — asset identity, owner state word, StatusPresentation,
  evidence, measurements, optional deep link.
- `NetworkGraphEdge` — ordered adjacency restated from an owner's path
  ("path_order"); no edge kind may be inferred.
- `NetworkGraphGap` — an owner break code and message rendered inside the
  path, with an optional canonical repair destination.
- `NetworkGraphEvidence` — evidence source, observed time, freshness word.
- `NetworkGraphMeasurement` — owner-composed reading display (optical power,
  RF signal) with unit and provenance.
- `NetworkGraphView` — the bounded subject-centred graph
  (`schema_version` 1).

Routes, templates, and HTMX fragments render these contracts (or their
`to_dict()` projections) and do not re-derive state, tone, labels, units, or
completeness.

`CustomerNetworkMapView` is the bounded geographic companion contract. It
combines the customer's primary service-address coordinates with the validated
subscription fiber trace and canonical asset coordinates. It emits points,
validated fiber-segment lines, an explicitly dashed electronic customer-to-POP
line when passive geometry is incomplete, and typed gaps. Geography never
creates connectivity: nearby cabinets or closures are not candidates, and a
missing coordinate or topology edge remains visible as a gap rather than being
bridged by the renderer.

## Inputs and boundaries

The owner reads:

- One resolved `CustomerPath` per subscription from `network.access_path`,
  projected twice through the owner's pure `summarize_customer_path` and
  `build_topology_trace` so the endpoint card, the graph view, and evidence
  consumers pay for one resolution.
- `StatusPresentation` projections from `ui.status_presentation`
  (`topology_hop_status_presentation`, `path_gap_presentation`,
  `access_endpoint_source_presentation`,
  `radio_signal_freshness_presentation`).

It performs no SSH, UISP, OLT, router, or ACS call, writes nothing, and
persists nothing. A failed resolution degrades to an explicit per-subscription
unresolved projection; an unavailable path must not take the customer record
with it.

## Links and repair destinations

The projection owns the mapping from hop kind to its canonical admin page
(`_NODE_LINKS`) and from break code to its canonical review destination
(`_gap_repair`): radio/AP conflicts go to the unmatched-radio ticket queue
(`/admin/support/tickets?ticket_type=unmatched_radio`), missing-ONT gaps go
to the ONT assignment flow scoped to the subscriber, and everything else goes
to `/admin/network/topology-gaps`. PON ports link to their adjacent proven
OLT's PON tab — taken from the trace order, never inferred from names. Every
href carries the permission its destination requires
(`href_permission` / `repair_permission`); renderers show the link only when
the viewer holds it via the `can()` template global, and the facts themselves
never vary by viewer.

## Surface contracts

- `templates/admin/customers/_network_path.html` holds the single
  `network_path_graph` renderer for any NetworkGraphView; the customer detail
  Active Path and the fibre plant fragment both use it.
  `templates/admin/customers/detail.html` renders `card.network_path`
  (NetworkGraphView dict) and `card.access_endpoint`
  (AccessEndpointProjection dict). Tone reaches the DOM only through the
  semantic `status-tone-*` / `status-panel-*` classes fed by owner-provided
  presentations; the template holds no state-to-colour or source-to-label
  branches. Owner-emitted notices (partial endpoint, AP unresolved) are
  warnings by construction and render with the warning tone token.
- Fibre plant expansion: a `<details>` block on fibre cards lazy-loads
  `GET /admin/customers/subscriptions/{subscription_id}/fiber-path`
  (permission `network:fiber:read`), rendered from
  `project_subscription_fiber_detail` via
  `templates/admin/customers/_fiber_path_panel.html`. The route authorizes
  and renders only.
- The ticket prefill keeps consuming the legacy
  `SubscriberTopologyTrace.to_dict()` shape (`card.topology_trace`), which
  remains available from the same single resolution.
- The Account-tab location map consumes `CustomerNetworkMapView.to_dict()`.
  It retains customer coordinate editing, fits to the connected path, renders
  validated segment endpoints as solid lines and the electronic-only fallback
  as dashed, and summarizes missing topology or coordinates. The template does
  not search for nearby infrastructure or infer edges.
- Remaining template tone decisions on the same card — the known-outage
  panel, RADIUS access block, and access-medium label — belong to
  `network.connection_health` / `network.outage_lifecycle` presentation
  migrations scheduled with the PR 2 path component, and are explicitly not
  re-owned here.

## Performance contract

- Customer detail resolves each eligible subscription's path exactly once;
  the projection itself adds no SQL statements beyond that resolution.
- Query growth is bounded per subscription: projecting N subscriptions costs
  at most N times the single-subscription budget plus a constant, guarded by
  `tests/test_customer_network_path.py::test_projection_query_budget_slope`.
- No live device polling during page rendering.

## Programme context

This slice is PR 1 of the network understanding programme (Customer 360
Network Path → Unified Network Explorer). Later slices add subscription
selection, per-hop deep links and repair destinations, passive-fibre
expansion, incident context, and the explorer surface — all rendering this
same graph contract. Incident scope history, per-subscription impact
evidence, and SLA accrual are separate owners defined by the programme design
and are not decided by this projection.

## Coordinated cutover and retirement

Retired in this slice:

- `detail.html` inline `node.state` → Tailwind colour mapping for path chips.
- `detail.html` inline endpoint-source label/colour branches.
- `detail.html` inline RF freshness styling and string composition.
- Template-side ONT receive-power formatting.

`web_customer_details._build_access_endpoint_projection` remains as a thin
adapter that shares an already-resolved path with the owner and returns its
typed `SubscriptionNetworkPath`.

## Verification

- `tests/test_customer_network_path.py` — graph view identity/order/state
  fidelity, presentation composition, RF display composition, geographic path
  fidelity and gap behavior, unresolved degradation, multi-subscription
  isolation, and the query-budget slope.
- `tests/test_customer_detail_access_endpoint.py` — serving-endpoint
  contract and template boundary needles.
- `tests/test_status_presentation.py` — presentation vocabulary.
- `tests/architecture/test_sot_manifest_contracts.py` and
  `tests/architecture/test_sot_registry_integrity.py` — contract and registry
  integrity.
