"""Assemble the canonical provisioning_operations SOT domain from capability shards."""

from __future__ import annotations

from app.services.sot_registry.domains.provisioning_operations.core import (
    SERVICES as CORE_SERVICES,
)
from app.services.sot_registry.domains.provisioning_operations.vendor_delivery import (
    SERVICES as VENDOR_DELIVERY_SERVICES,
)
from app.services.sot_registry.domains.provisioning_operations.vendor_identity import (
    SERVICES as VENDOR_IDENTITY_SERVICES,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="provisioning_operations",
    setting_domains=(
        "provisioning",
        "projects",
        "inventory",
        "field",
    ),
    services=(
        *CORE_SERVICES,
        *VENDOR_IDENTITY_SERVICES,
        *VENDOR_DELIVERY_SERVICES,
    ),
    entrypoints=(
        "app.services.events.handlers.provisioning",
        "app.tasks.ont_provisioning",
        "app.web.admin.provisioning",
        "app.web.admin.projects",
        "app.web.vendor_portal",
        "app.api.vendor_portal",
        "app.api.projects",
        "app.api.field.*",
        "app.services.web_projects",
        "app.services.web_dispatch_work_orders",
        "app.services.work_order_commands",
        "field_mobile",
    ),
    rule="Provisioning callers resolve customer/network context through the "
    "shared context layer before executing workflow steps. Native project "
    "mutation adapters delegate to Projects.update for lifecycle consequences. "
    "Field clients consume completion_requirements from authenticated job "
    "detail and leave completion eligibility to the field transition service. "
    "Dispatch adapters delegate native work-order and assignment writes to "
    "operations.work_order_commands.",
)
