# Fiber plant surface — build spec (from owners)

Fiber-plant ledger (archetype D) + canvas fiber layer, built as a projection of
the SOT owners (per `NETWORK_SOT_SERVICE_MAP.md`), not the legacy templates.

## Ledger data sources — owner `.list()` reads (source from owners, NOT the
## `web_network_fdh` / `web_network_splice_closures` adapters, which bypass the owners)

| Asset type (facet) | Owner read | Row status → tone |
|---|---|---|
| FDH cabinets | `network.splitters.FdhCabinets.list(db, region_id, name, is_active, order_by, order_dir, limit, offset)` | (no status; show splitter/spare counts) |
| Splitters | `network.splitters.Splitters.list(db, fdh_id, name, is_active, …)` + capacity via `fiber_plant_integrity.splitter_capacity(db, id).spare_outputs` | — |
| Strands | `network.fiber_services.FiberStrands.list(db, cable_name, status, segment_id, is_active, …)` | `fiber_strand_status_presentation(strand.status)` ✅ |
| Segments | `network.fiber_services.FiberSegments.list(db, segment_type, fiber_strand_id, is_active, …)` | — |
| Splice closures | `network.fiber_services.FiberSpliceClosures.list(db, …)` | — |
| Change requests | `fiber_change_requests.list_requests(db, status=None)` | `fiber_change_request_status_presentation(req.status)` ✅ |
| Support structures | **owner has NO list() — must add `FiberSupportStructures.list()`** | `fiber_support_lifecycle_presentation` / `fiber_support_inspection_presentation` ✅ |

Review/coverage/worklist are **separate workqueue surfaces** (already emit
server-owned states — don't remap): identity review
`fiber_topology_review.list_identity_review_queue(...)` (`review_state`), field
verification `fiber_topology_field_worklist.reconcile_fiber_field_worklist(db)`
(`verification_state`, `priority`), identity/connectivity coverage reconcilers.

## Done (this slice)
`fiber_*_status_presentation` added to `status_presentation.py` (the one
presentation owner) — the SoT tone contract the inventory owners lacked. Tested.

## Remaining fiber build (from this spec)
1. A consolidated `/admin/network/fiber-plant` **ledger** on `components/ui/ledger.html`
   with a type facet (FDH / Splitters / Strands / Closures / Change requests),
   each type sourced from its owner `.list()`, status via the presentations above,
   row drawer per type, actions from the owner command endpoints (change-request
   approve/reject; splitter/strand/closure CRUD through owners).
2. Owner list reads to add (flagged gaps): `FiberSupportStructures.list()`,
   and web exposure for `FiberSegments`/`FiberAccessPoint`.
3. Fold spatial assets (FDH, closures, access points, support structures,
   segments) into the **network canvas fiber layer** via
   `network_map.build_network_map_projection`; retire `fiber/map.html`.

## Geo vs ledger
- Canvas (fiber layer): FdhCabinet, FiberSpliceClosure, FiberAccessPoint,
  FiberSupportStructure, FiberTerminationPoint (lat/lng), FiberSegment (line).
- Ledger-only: Splitter/SplitterPort/assignment, FiberStrand, FiberSplice/Tray,
  FiberChangeRequest.

## Owner-bypass drift (cross-cutting — preserve in Knowledge)
`web_network_fdh.list_page_data` / `list_splitters_page_data` /
`web_network_splice_closures.list_page_data` query models directly instead of the
`FdhCabinets.list` / `Splitters.list` / `FiberSpliceClosures.list` owner reads
(strands correctly use the owner). The new ledger sources from the owners.
