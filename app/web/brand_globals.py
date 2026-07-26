"""Attach the static white-label ``brand`` value to every Jinja2 environment.

Brand fields are deployment-static (see :mod:`app.services.branding_config`), so
they belong as a Jinja *global* available to every template rather than a
per-request context processor. The web route modules each create their own
``Jinja2Templates`` instance and are imported lazily during app startup (see the
router specs in :mod:`app.main`), so patching ``Jinja2Templates.__init__`` once
before routers are imported makes ``brand`` available to all of them.

Templates should still guard with a default (``brand.primary_color if brand is
defined and brand else "#3b82f6"``) so error pages rendered by instances created
before this installer runs keep working.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

from app.services.branding_config import get_brand
from app.version import get_app_version

logger = logging.getLogger(__name__)

_installed = False


def _current_year() -> int:
    """Live current year for footers, so hardcoded years can't go stale."""
    return datetime.now(UTC).year


def _money_filter(amount: object, currency: str | None = None) -> str:
    """Jinja ``money`` filter: ``{{ amount | money }}`` / ``{{ amount | money(cur) }}``.

    Side-effect-free — it never touches the DB; without an explicit currency it
    renders with the default NGN symbol.
    """
    from app.services.display_format import format_money

    return format_money(amount, currency=currency)


def _app_datetime_filter(
    value: object,
    fmt: str | None = None,
    fallback: str = "-",
    include_tz: bool = True,
) -> str:
    """Admin/reseller sibling of the customer ``portal_datetime`` filter.

    Formats an aware / naive-UTC datetime in the fixed display timezone
    (Africa/Lagos / WAT), reusing the customer portal impl so both surfaces
    stay identical. ``fmt`` defaults to the portal's standard layout; pass
    ``include_tz=False`` for date-only formats where a timezone label is noise.
    """
    from app.web.customer.branding import _format_portal_datetime

    if fmt is None:
        return _format_portal_datetime(value, fallback=fallback, include_tz=include_tz)
    return _format_portal_datetime(value, fmt, fallback, include_tz)


def _attach_globals(templates: Jinja2Templates) -> None:
    from app.config import settings
    from app.services.auth_dependencies import action_permitted, can
    from app.services.display_format import currency_symbol
    from app.services.status_presentation import (
        account_status_presentation,
        appointment_status_presentation,
        device_operational_status_presentation,
        infrastructure_service_status_presentation,
        invoice_status_presentation,
        offer_status_presentation,
        payment_status_presentation,
        project_status_presentation,
        project_task_status_presentation,
        provisioning_task_status_presentation,
        quote_status_presentation,
        sales_order_status_presentation,
        service_order_status_presentation,
        subscription_status_presentation,
        system_job_status_presentation,
        ticket_status_presentation,
        work_order_status_presentation,
    )
    from app.web.customer.branding import (
        _format_portal_date,
        _format_portal_datetime,
    )

    # UI action gating: templates hide actions the principal cannot perform.
    # Reads the permission set require_permission cached on the request (no DB);
    # the route still authorizes. Usage: {% if can(request, "reports:billing:export") %}
    # or, for an Action contract: {% if action_permitted(request, action) %}
    templates.env.globals.setdefault("can", can)
    templates.env.globals.setdefault("action_permitted", action_permitted)

    templates.env.globals.setdefault(
        "infra_service_status_presentation",
        infrastructure_service_status_presentation,
    )
    templates.env.globals.setdefault(
        "service_order_status_presentation", service_order_status_presentation
    )
    templates.env.globals.setdefault(
        "appointment_status_presentation", appointment_status_presentation
    )
    templates.env.globals.setdefault(
        "work_order_status_presentation", work_order_status_presentation
    )
    templates.env.globals.setdefault(
        "system_job_status_presentation", system_job_status_presentation
    )
    templates.env.globals.setdefault(
        "device_operational_status_presentation",
        device_operational_status_presentation,
    )
    templates.env.globals.setdefault(
        "offer_status_presentation", offer_status_presentation
    )
    templates.env.globals.setdefault(
        "quote_status_presentation", quote_status_presentation
    )
    templates.env.globals.setdefault(
        "sales_order_status_presentation", sales_order_status_presentation
    )
    templates.env.globals.setdefault(
        "project_status_presentation", project_status_presentation
    )
    templates.env.globals.setdefault(
        "project_task_status_presentation", project_task_status_presentation
    )
    templates.env.globals.setdefault(
        "subscription_status_presentation", subscription_status_presentation
    )
    templates.env.globals.setdefault(
        "invoice_status_presentation", invoice_status_presentation
    )
    templates.env.globals.setdefault(
        "payment_status_presentation", payment_status_presentation
    )
    templates.env.globals.setdefault(
        "ticket_status_presentation", ticket_status_presentation
    )
    templates.env.globals.setdefault(
        "account_status_presentation", account_status_presentation
    )
    templates.env.globals.setdefault(
        "provisioning_task_status_presentation",
        provisioning_task_status_presentation,
    )
    templates.env.globals.setdefault("brand", get_brand())
    templates.env.globals.setdefault("current_year", _current_year)
    templates.env.globals.setdefault("app_version", get_app_version)
    templates.env.globals.setdefault("chat_live_enabled", settings.chat_live_enabled)
    templates.env.globals.setdefault("currency_symbol", currency_symbol)
    templates.env.filters.setdefault("money", _money_filter)
    templates.env.filters.setdefault("app_datetime", _app_datetime_filter)
    templates.env.filters.setdefault("portal_date", _format_portal_date)
    templates.env.filters.setdefault("portal_datetime", _format_portal_datetime)


def _backfill_loaded_template_instances() -> None:
    """Attach globals to template instances created before the init patch."""
    seen: set[int] = set()
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not namespace:
            continue
        for value in namespace.values():
            if not isinstance(value, Jinja2Templates) or id(value) in seen:
                continue
            seen.add(id(value))
            try:
                _attach_globals(value)
            except Exception:
                logger.debug(
                    "Could not backfill Jinja globals on existing template instance",
                    exc_info=True,
                )


def install_brand_jinja_global() -> None:
    """Patch Jinja2Templates so every instance exposes the ``brand`` global."""
    global _installed
    if _installed:
        return

    _original_init = Jinja2Templates.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        _original_init(self, *args, **kwargs)
        try:
            _attach_globals(self)
        except Exception:  # pragma: no cover - never break template setup
            logger.debug("Could not attach brand Jinja global", exc_info=True)

    Jinja2Templates.__init__ = _patched_init  # type: ignore[method-assign]
    _installed = True

    _backfill_loaded_template_instances()
