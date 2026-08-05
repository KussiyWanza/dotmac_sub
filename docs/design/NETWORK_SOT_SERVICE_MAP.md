# Network UI — SOT service → surface map

Drives the network admin redesign from the **authoritative owners** in the SOT
registry (`app/services/sot_registry/registry.py`, `DOMAIN_SOT_RELATIONSHIPS`), not
from the legacy templates. Each redesigned surface is a **complete projection of
its owning services' contracts** — so we surface what the owner exposes, not
whatever fields a legacy page happened to pick. Companion to
`NETWORK_IA_RATIONALIZATION.md` and `ADMIN_UI_ARCHETYPES.md`.

**Registry scale (captured 2026-07-20):** 28 domains / 242 services. Network-relevant:
`network` **53**, `network_access_control_plane` **13**, `provisioning_operations`
**11**, `service_intent_control_plane` **9**, `vpn_remote_access` **4**, `geospatial` **2**.
The template count (218) hid this: **fiber alone is ~22 services** — a major
sub-domain, not one page.

## Surface → owning services (project these, don't reskin templates)

### Device ledger (D) + Device 360 (B)
- `network.device_projection` → `device_projection_reconcile` (the unified row — the ledger's source of truth)
- `network.device_state` → `device_operational_status` (server-owned tone; already consumed)
- `network.monitoring_inventory` → `network_monitoring` (NetworkDevice live/ping/SNMP for the 360 monitoring tab)
- `network.ont_runtime_status` → `network.ont_runtime_status`; `network.ont_status_refresh`
- `network.nas_inventory` → `nas.devices`; `network.nas_lifecycle` → `nas_lifecycle`; `network.nas_access_path_evidence`
- `network.routeros_sot` → `router_management.sot_policy` (router class)
- `network.connection_health` → `topology.connection_status`; `network.radius_sessions`
- `network.device_groups` → `network.device_groups` (a ledger facet)
- `network.operation_ledger`/`network.operation_dispatch` → `network_operations`/`network_operation_dispatch` (device actions + history — the 360 operations tab)

### Network canvas (C, layered)
- `network.forwarding_topology` → `network.forwarding_topology` (topology layer — already uplifted)
- `network.access_path` → `network.access_path`; `network.ont_topology_observations`
- **Fiber layer:** `network.fiber_topology`, `network.fiber_plant_integrity`, `network.fiber_physical_continuity`, `network.splitter_inventory`, `network.fiber_support_structures`, `network.fiber_access_attachments`
- **Geo layer:** `gis.geocoding` → `geocoding`; `gis.spatial_sync` → `gis_sync`

### NOC / monitoring triage (A)
- `network.outage_lifecycle` → `topology.outage`; `network.outage_impact` → `network.outage_impact` (inspector body)
- `network.connection_health`; `network.radius_sessions`
- `network.operation_ledger`/`operation_dispatch`
- `access.radius_state`/`access.radius_reject`/`access.session_enforcement` (auth/enforcement signal)

### Fiber plant ledger (D) — its own major surface (~22 services)
- Inventory/integrity: `network.fiber_topology`, `network.fiber_plant_integrity`, `network.splitter_inventory`, `network.fiber_support_structures`, `network.fiber_physical_continuity`, `network.fiber_source_staging`, `network.fiber_asset_changes` (`fiber_change_requests`)
- Identity/review: `network.fiber_identity_decisions`, `network.fiber_identity_review`, `network.fiber_identity_coverage`
- Connectivity: `network.fiber_connectivity_decisions`, `network.fiber_connectivity_review`, `network.fiber_connectivity_coverage`, `network.fiber_cutover_readiness`
- Field verification: `network.fiber_field_observations`, `network.fiber_field_verification_worklist`/`_jobs`/`_job_scope`/`_map`, `network.fiber_work_order_evidence_map`
- ONT assignment/cutover: `network.ont_assignment_commands`/`_identity`/`_cutover`/`_cutover_batches`/`_cutover_verification`/`_cutover_coverage`/`_constraint_authorization`, `network.ont_inventory_release`

### Provisioning (canvas board / ledger)
- `network.ont_provisioning_commands`/`network.ont_provisioning_execution`
- `network.control_plane_intent` → `control_plane_intent`; `service_intent_control_plane` (9 services)
- `provisioning_operations` (11 services)

### Access / RADIUS / FUP ledgers (D) — `network_access_control_plane` (13)
- `access.subscription_lifecycle`, `access.control_resolution`, `access.event_policy`, `access.walled_garden_policy`
- `access.radius_state`/`radius_reject`/`radius_target_registry`/`radius_projection`, `access.session_enforcement`
- `access.fup_rule_engine`/`fup_runtime_state`/`fup_usage_windows`/`fup_enforcement_sweep`

### IPAM ledger (D)
- `network.ip_pool_utilization` → `ip_pool_utilization_snapshot` (+ the IP management services under their own owners)

### VPN / remote access (D) — `vpn_remote_access` (4)

## Build rule
For each surface: read the owning services' public read contracts → project the
full set into the archetype (ledger columns / 360 tabs / canvas layers / triage
panes), status via the server tone contract, actions via each owner's Action/
command contract. A field the owner exposes but the legacy page dropped is a
**gap to surface**, not a precedent to copy.

## Honest scope note
Driven by the registry, "the network UI" is **~8–9 designed surfaces over ~90
service owners** — a multi-surface program. Done so far: Device ledger + Device
360 (backend + ledger + ONT/CPE detail chrome), topology canvas, the fold, the
flat sweep. Fiber (its own ~22-service surface), NOC, the access/RADIUS/FUP
ledgers, provisioning, and IPAM remain — each now has its owner list here.
