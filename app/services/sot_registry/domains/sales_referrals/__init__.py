"""Assemble the canonical sales_referrals SOT domain from capability shards."""

from __future__ import annotations

from app.services.sot_registry.domains.sales_referrals.acquisition import (
    SERVICES as ACQUISITION_SERVICES,
)
from app.services.sot_registry.domains.sales_referrals.customer_audit import (
    SERVICES as CUSTOMER_AUDIT_SERVICES,
)
from app.services.sot_registry.domains.sales_referrals.customer_handoff import (
    SERVICES as CUSTOMER_HANDOFF_SERVICES,
)
from app.services.sot_registry.domains.sales_referrals.lifecycle import (
    SERVICES as LIFECYCLE_SERVICES,
)
from app.services.sot_registry.domains.sales_referrals.referrals import (
    SERVICES as REFERRALS_SERVICES,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="sales_referrals",
    setting_domains=("workflow",),
    services=(
        *ACQUISITION_SERVICES,
        *CUSTOMER_HANDOFF_SERVICES,
        *LIFECYCLE_SERVICES,
        *CUSTOMER_AUDIT_SERVICES,
        *REFERRALS_SERVICES,
    ),
    entrypoints=(
        "app.api.me",
        "app.api.crm_referrals",
        "app.api.crm_webhooks",
        "app.api.crm_sales",
        "app.api.lead_capture_webhooks",
        "app.api.customer_experience",
        "app.web.customer.referrals",
        "app.tasks.referrals",
        "app.services.events.handlers.referral",
        "app.services.events.handlers.sales_lifecycle_projection",
        "app.services.web_sales",
        "app.services.web_referrals",
        "scripts.migration.audit_customer_lifecycle",
        "scripts.migration.reconcile_sales_lifecycle",
    ),
    rule="A prospect enters as a Party-bound Lead with captured origin, not a "
    "fake Subscriber. Staff author Lead-backed Quotes without conversion; "
    "Accepted Quote is the sole atomic account, SalesOrder, Project, Task, "
    "and configured WorkOrder conversion event. SalesOrder structurally "
    "owns one Project and installation scope; verified "
    "implementation requests service-order release after its evidence "
    "commits; successful provisioning activates service and its committed "
    "completion requests the CX handoff. Routes, webhooks, jobs, and "
    "handlers request outcomes from these owners and translate domain "
    "errors at the boundary. CRM and dotmac_mkt have no customer-lifecycle "
    "or attribution authority.",
)
