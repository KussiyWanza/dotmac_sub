"""Assemble the canonical financial_access SOT domain from capability shards."""

from __future__ import annotations

from app.services.sot_registry.domains.financial_access.billing import (
    SERVICES as BILLING_SERVICES,
)
from app.services.sot_registry.domains.financial_access.collection_operations import (
    SERVICES as COLLECTION_OPERATIONS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.collections import (
    SERVICES as COLLECTIONS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.customer_subledger import (
    SERVICES as CUSTOMER_SUBLEDGER_SERVICES,
)
from app.services.sot_registry.domains.financial_access.durable_timers import (
    SERVICES as DURABLE_TIMERS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.erp_billing import (
    SERVICES as ERP_BILLING_SERVICES,
)
from app.services.sot_registry.domains.financial_access.financial_core import (
    SERVICES as FINANCIAL_CORE_SERVICES,
)
from app.services.sot_registry.domains.financial_access.invoicing_tax import (
    SERVICES as INVOICING_TAX_SERVICES,
)
from app.services.sot_registry.domains.financial_access.payment_intents import (
    SERVICES as PAYMENT_INTENTS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.prepaid import (
    SERVICES as PREPAID_SERVICES,
)
from app.services.sot_registry.domains.financial_access.provider_payments import (
    SERVICES as PROVIDER_PAYMENTS_SERVICES,
)
from app.services.sot_registry.domains.financial_access.sales_funding import (
    SERVICES as SALES_FUNDING_SERVICES,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="financial_access",
    services=(
        *BILLING_SERVICES,
        *CUSTOMER_SUBLEDGER_SERVICES,
        *DURABLE_TIMERS_SERVICES,
        *COLLECTIONS_SERVICES,
        *SALES_FUNDING_SERVICES,
        *ERP_BILLING_SERVICES,
        *FINANCIAL_CORE_SERVICES,
        *PAYMENT_INTENTS_SERVICES,
        *INVOICING_TAX_SERVICES,
        *PREPAID_SERVICES,
        *COLLECTION_OPERATIONS_SERVICES,
        *PROVIDER_PAYMENTS_SERVICES,
    ),
    entrypoints=(
        "app.services.billing_automation",
        "app.services.collections.*",
        "app.web.admin.billing_*",
        "app.web.admin.reports",
        "app.api.billing",
        "app.services.payment_proofs",
        "app.services.web_reports_extended",
        "app.api.me",
        "mobile",
        "app.tasks.billing",
        "app.tasks.collections",
        "app.tasks.enforcement",
        "app.tasks.payment_reconciliation",
    ),
    rule="No caller infers access or balances from draft invoices, imported "
    "legacy fields, or ad hoc sums when ledger/access resolvers exist. "
    "Tax reports consume the tax-accounting projection, never label "
    "issued tax as collected cash, and never add different currencies. "
    "Tax account mappings and double-entry consequences are written only "
    "by Dotmac ERP from Sub's bounded source-fact feeds.",
)
