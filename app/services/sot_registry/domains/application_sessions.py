"""Canonical SOT declarations for the application_sessions domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="application_sessions",
    services=(
        SOTService(
            name="app_sessions.store",
            module="app.services.session_store",
            owns=(
                "Redis-backed session storage",
                "session principal indexes",
                "session revocation epochs",
            ),
        ),
        SOTService(
            name="app_sessions.customer_portal",
            module="app.services.customer_portal_session",
            owns=(
                "customer portal session creation",
                "customer portal session refresh/revoke",
                "impersonation/read-only portal session policy",
            ),
            depends_on=("app_sessions.store", "customer.identity_scope"),
        ),
        SOTService(
            name="app_sessions.auth",
            module="app.services.session_manager",
            owns=(
                "database auth-session listing",
                "database auth-session revocation",
            ),
            depends_on=("app_sessions.store",),
        ),
    ),
    entrypoints=(
        "app.web.customer.auth",
        "app.web.customer.routes",
        "app.api.auth",
        "app.web.admin.auth",
    ),
    rule="Routes authenticate and authorize; session services own storage, "
    "refresh, listing, revocation, and impersonation session policy.",
)
