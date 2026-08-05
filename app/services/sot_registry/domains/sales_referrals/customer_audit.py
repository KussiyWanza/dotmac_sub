"""sales_referrals SOT declarations: customer audit."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="customer.lifecycle_audit",
        module="app.services.customer_lifecycle_audit",
        owns=(
            "PII-free customer lifecycle link convergence report",
            "Lead origin and downstream alignment debt classification",
            "Party-first referral capture and conversion debt classification",
            "sales-to-service delivery convergence classification",
        ),
        depends_on=(
            "party.registry",
            "communications.campaigns",
            "sales.lead_lifecycle",
            "sales.service",
            "sales.orders",
            "sales.fulfillment",
            "operations.service_order_lifecycle",
            "customer.experience_handoff",
            "access.subscription_lifecycle",
            "support.ticket_lifecycle",
        ),
    ),
)
