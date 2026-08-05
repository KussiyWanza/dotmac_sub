"""Assemble the canonical network SOT domain from capability shards."""

from __future__ import annotations

from app.services.sot_registry.domains.network.device_operations import (
    SERVICES as DEVICE_OPERATIONS_SERVICES,
)
from app.services.sot_registry.domains.network.fiber_plant import (
    SERVICES as FIBER_PLANT_SERVICES,
)
from app.services.sot_registry.domains.network.foundation import (
    SERVICES as FOUNDATION_SERVICES,
)
from app.services.sot_registry.domains.network.network_control import (
    SERVICES as NETWORK_CONTROL_SERVICES,
)
from app.services.sot_registry.domains.network.ont_assignments import (
    SERVICES as ONT_ASSIGNMENTS_SERVICES,
)
from app.services.sot_registry.domains.network.outages_and_ip import (
    SERVICES as OUTAGES_AND_IP_SERVICES,
)
from app.services.sot_registry.domains.network.subscriber_state import (
    SERVICES as SUBSCRIBER_STATE_SERVICES,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="network",
    services=(
        *FOUNDATION_SERVICES,
        *FIBER_PLANT_SERVICES,
        *ONT_ASSIGNMENTS_SERVICES,
        *SUBSCRIBER_STATE_SERVICES,
        *DEVICE_OPERATIONS_SERVICES,
        *NETWORK_CONTROL_SERVICES,
        *OUTAGES_AND_IP_SERVICES,
    ),
    entrypoints=(
        "app.services.topology.*",
        "app.services.infrastructure_*",
        "app.services.router_management.*",
        "app.tasks.network_*",
        "app.tasks.router_sync",
        "scripts.network.audit_fiber_topology",
        "scripts.network.review_fiber_topology_identity",
        "scripts.network.review_fiber_topology_connectivity",
        "scripts.network.review_forwarding_topology",
        "scripts.network.stage_fiber_topology_kmz",
        "app.web.admin.network_*",
        "app.web.customer.connection",
        "app.api.me",
        "app.services.reseller_portal",
        "mobile",
    ),
    rule="Pollers and map collectors write observations; the fiber-topology "
    "owner validates identity and connectivity; network resolvers decide "
    "state; event services decide consequences.",
)
