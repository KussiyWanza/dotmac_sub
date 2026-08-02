# Unified Network Explorer

Status: adopted (programme slices PR 3 — explorer shell, typed search, and
subject-centred bounded graphs; PR 4 — the on-demand operational inspector;
PR 5 — fibre/geographic/utilization layers; PR 6 — the coverage and drift
view). Presentation and information rules follow
`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`. The Customer 360 companion
contract is `docs/designs/CUSTOMER_NETWORK_PATH.md`.

## Decision

`app.services.network_explorer` is the read-only owner of the
`/admin/network/explorer` projection: typed cross-asset search and the
bounded neighbourhood graph around one selected subject. It restates existing
owners and decides nothing of its own:

- `ui.customer_network_path_projection` supplies subscription path views and
  the canonical asset deep-link map; the explorer reuses the shared
  `NetworkGraphView` contract (`app.services.network_graph`) as its only
  wire format.
- `network.forwarding_topology` supplies reviewed device adjacency; explorer
  edges of kind "forwarding" restate it verbatim.
- `network.device_state` supplies the binary working/not_working verdict for
  monitored devices; `network.olt_observed_state` facts supply ONT words.
- `network.outage_impact` supplies audience cohorts (attached, provisioned,
  or served subscription counts) rendered as explicit cohort nodes.
- `network.identity` inventory rows (OLT, PON, ONT, CPE, NAS, FDH, splitter,
  site) supply identity and declared relations only.
- `ui.status_presentation` supplies every label/tone/icon.

Site containment renders as a dashed "containment" edge and is never
presented as connectivity. No edge is manufactured from names, geography, or
proximity. Passive and identity-only assets stay `not_applicable`
("Passive"), monitored-but-unobserved stays `unknown` — the vocabulary never
collapses.

## Subjects and bounds

Subject tokens are `<kind>:<uuid>` in the URL (`?subject=...&q=...`), so any
view is shareable. Supported kinds: subscription, subscriber, ont, radio,
device (NetworkDevice, including APs), nas, olt, pon_port, fdh, pop_site.
ONT and radio subjects with an active subscription binding resolve to that
subscription's path view.

The explorer never loads the fleet: each builder loads one bounded
neighbourhood, groups fan-out beyond 25 into a cohort node linking to the
canonical list surface, and enforces a hard cap of 100 nodes per view with an
explicit "+N more" cohort — never silent truncation.

## Search

`search_explorer_subjects` is typed per kind (customer, subscription
login/IPv4, ONT serial, radio serial/MAC, OLT, NAS, device, FDH, site) with a
bounded per-kind limit. Customer-identity kinds (subscriber, subscription,
radio) are omitted entirely for viewers without `customer:read`, and
customer-centred subjects refuse to open for them; infrastructure cohort
counts remain aggregates.

## Surface contract

- Page gate: `network:device:read` (plus the network module gate). Per-node
  links keep their owner-declared `href_permission` and render through the
  `can()` global.
- `templates/admin/network/explorer/index.html` renders the projection: the
  vendored Cytoscape + dagre canvas maps owner tones to semantic design
  tokens (`--color-semantic-*`), and an always-rendered accessible list
  fallback shows the same nodes, relationships, and gaps as text.
- `GET /admin/network/explorer/api/graph?subject=...` returns the same
  `NetworkGraphView.to_dict()` JSON for on-demand recentring.
- Routes authorize and render only; no SQL in `app/web`.

## Operational inspector

Selecting a graph node opens the archetype-C on-demand inspector — an
overlay fetched from `GET /admin/network/explorer/inspect?subject=...`
(`build_inspector`, rendered by
`templates/admin/network/explorer/_inspector.html`). It composes, per
subject: identity and the asset deep link; the owner verdict with its
machine reason and observation time (`network.device_state` for devices,
`network.olt_observed_state` words for ONTs, `network.radio_signal` for
radios); owner-composed measurements (ONT optical power and temperature, RF
signal with freshness); bounded neighbourhood facts; the reverse
affected-customer cohort from `network.outage_impact` (count plus
online-now); live incidents from `network.outage_lifecycle` scoped to the
node, basestation, or cabinet, rendered through
`outage_status_presentation`; and a Customer 360 deep link
(`customer:read`-gated) where a subscription or subscriber is bound. The
customer detail Active Path reciprocates with an "Open in Explorer" link.
Customer-identity subjects refuse to inspect without `customer:read`.
Alarm-stream context beyond live incidents is a later slice.

## Fibre, geographic, and utilization layers

- Passive fibre: FDH and splitter subjects render identity/continuity
  neighbourhoods; subscription subjects offer the same lazy "Fibre plant
  details" expansion as Customer 360 (`network:fiber:read`), reusing the
  `/admin/customers/subscriptions/{id}/fiber-path` fragment — one renderer,
  one owner.
- Geographic: the explorer reuses the existing Leaflet surfaces instead of
  building another map owner — site inspectors link to
  `/admin/network/map` (`network:map:read`) and fibre-plant inspectors to
  `/admin/network/fiber-map` (`network:fiber:read`).
- Utilization: device inspectors list the top five links by owner-computed
  utilization ("62% of 1000 Mbps") from the declared topology links; the
  projection composes display strings only and loads them on demand inside
  the inspector, never during initial page render.

## Coverage and drift

`build_network_coverage` (rendered at `/admin/network/explorer/coverage`,
`monitoring:read`) is the topology-quality view. Coverage is calculated per
subscription — never from aggregate device counts — by composing the batched
gap classification that `network.access_path` keeps contractually in sync
with `resolve_customer_path`: active subscriptions, complete end-to-end
paths, coverage percentage, and completeness by access medium
(fibre/wireless/NAS-only/unknown), plus per-gap-code counts. Drift worklists
each carry their count, a `coverage_metric_presentation` word
(Clear/Needs review), and the canonical repair destination: subscriptions
without a complete path (topology-gaps), forwarding declarations without
current agreement (idempotent reconcile state counts: drift / missing
observation / invalid), monitored devices with no provisioning match,
the unmatched-radio review queue with ageing (oldest open days), ONTs with
no PON association (unconfigured-ONTs queue), and connected ONTs with no
splitter/FDH association (fibre trace). The projection counts and links; it
repairs nothing and never turns a worklist size into a service-down claim.

## Performance and safety

- No SSH, UISP, OLT, router, or ACS calls during rendering.
- Bounded queries per neighbourhood; the forwarding graph is projected once
  per request through `topology.affected.forwarding_graph_projection`.
- Saved visual positions are not read or written by this slice; nothing this
  page renders is persisted as network truth.

## Later slices

The operational inspector (device state detail, RF/optical, incidents,
reverse impact), fibre/geographic/utilization layers, and coverage/drift
views are later programme slices composing the same contract. Legacy
topology/map/weathermap surfaces are retired only after the programme's
parity gate and explicit approval.

## Verification

- `tests/test_network_explorer.py` — typed search and identity gating,
  subject views, grouping and the hard node cap, honest unknown/passive
  states, JSON safety.
- `tests/architecture/test_sot_registry_integrity.py` and
  `tests/architecture/test_sot_manifest_contracts.py` — contract integrity.
- `tests/architecture/test_thin_wrappers.py` — the routes stay thin.
