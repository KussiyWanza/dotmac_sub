"""network SOT declarations: foundation."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="network.identity",
        module="app.services.network.identity",
        owns=("cross-model network links", "device/entity identity"),
    ),
    SOTService(
        name="network.monitoring_inventory",
        module="app.services.network_monitoring",
        owns=(
            "monitoring inventory mutations",
            "monitoring device admission lifecycle transitions",
            "monitoring metric records",
            "alert rule and alert state mutations",
        ),
        depends_on=("network.identity",),
        notes=(
            "Device admission is a transition, not a flag. Every "
            "NetworkDevice.is_active change goes through "
            "set_network_device_active, which leaves polling "
            "eligibility, decays the derived live_status cache to "
            "unknown so no unpollable row keeps asserting reachability, "
            "and keeps the device visible in inventory marked inactive. "
            "Callers that flip the flag directly get half a "
            "deactivation and freeze a stale 'up' that vetoes outage "
            "detection. Router inventory (router_management) is an "
            "authoritative INPUT to the admission of the monitoring "
            "device it links — an auto-created device has no "
            "independent existence — but it requests the transition "
            "from this owner instead of writing the flag. Reachability "
            "observations never drive inventory lifecycle in either "
            "direction. Deactivating a device that still has customers "
            "attached raises an admin-facing data-integrity alert at "
            "the transition (resolved on re-admission) — a statement "
            "about the inventory record with a known blast radius, "
            "never an outage incident and never a customer-visible "
            "surface. "
            "Inventory absence must not open a customer-facing outage: "
            "an unpolled device supports no reachability verdict, which "
            "is why deactivation classifies as unknown."
        ),
    ),
)
