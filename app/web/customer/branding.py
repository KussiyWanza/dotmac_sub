"""Shared branding context for customer portal templates."""

import logging
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from threading import Lock
from time import monotonic
from typing import TypedDict
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.services.customer_context import optional_customer_subscriber_id
from app.services.db_session_adapter import db_session_adapter

logger = logging.getLogger(__name__)

# Cache branding data to avoid creating a DB session on every template render
_BRANDING_CACHE_TTL = 30.0  # seconds


class _BrandingCache(TypedDict):
    ts: float
    stats: dict
    portal_name: str
    favicon: str


_branding_cache: _BrandingCache = {
    "ts": 0.0,
    "stats": {},
    "portal_name": "",
    "favicon": "",
}
_branding_cache_lock = Lock()


def _format_currency_amount(value: object) -> str:
    """Format portal currency amounts with grouped thousands and two decimals."""
    if value in (None, ""):
        amount = Decimal("0")
    else:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return str(value)
    return f"{amount:,.2f}"


_PORTAL_DISPLAY_TZ = ZoneInfo("Africa/Lagos")
_PORTAL_DISPLAY_TZ_LABEL = "WAT"


def _coerce_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_PORTAL_DISPLAY_TZ)


def _format_portal_datetime(
    value: object,
    fmt: str = "%b %d, %Y %H:%M",
    fallback: str = "-",
    include_tz: bool = True,
) -> str:
    """Format customer-facing datetimes in the portal display timezone."""
    dt = _coerce_datetime(value)
    if dt is None:
        return fallback
    suffix = f" {_PORTAL_DISPLAY_TZ_LABEL}" if include_tz else ""
    return f"{dt.strftime(fmt)}{suffix}"


def _format_portal_date(
    value: object,
    fmt: str = "%b %d, %Y",
    fallback: str = "-",
) -> str:
    """Format a calendar date without adding timestamp semantics."""
    calendar_date: date | None
    if isinstance(value, datetime):
        dt = _coerce_datetime(value)
        calendar_date = dt.date() if dt is not None else None
    elif isinstance(value, date):
        calendar_date = value
    elif isinstance(value, str):
        try:
            calendar_date = date.fromisoformat(value)
        except ValueError:
            dt = _coerce_datetime(value)
            calendar_date = dt.date() if dt is not None else None
    else:
        calendar_date = None
    if calendar_date is None:
        return fallback
    return calendar_date.strftime(fmt)


def register_customer_portal_filters(templates: Jinja2Templates) -> Jinja2Templates:
    """Register filters used by shared customer portal templates."""
    templates.env.filters["currency_amount"] = _format_currency_amount
    templates.env.filters["portal_date"] = _format_portal_date
    templates.env.filters["portal_datetime"] = _format_portal_datetime
    return templates


def _get_cached_branding() -> tuple[dict, str, str] | None:
    """Return cached branding (stats, portal_name, favicon) if valid, else None."""
    with _branding_cache_lock:
        if monotonic() - _branding_cache["ts"] < _BRANDING_CACHE_TTL:
            return (
                _branding_cache["stats"],
                _branding_cache["portal_name"],
                _branding_cache["favicon"],
            )
    return None


def _update_branding_cache(stats: dict, portal_name: str, favicon: str) -> None:
    """Update the branding cache."""
    with _branding_cache_lock:
        _branding_cache["ts"] = monotonic()
        _branding_cache["stats"] = stats
        _branding_cache["portal_name"] = portal_name
        _branding_cache["favicon"] = favicon


def customer_branding_context(request: Request) -> dict[str, object]:
    """Build branding context (favicon, sidebar stats, portal name) for customer portal templates."""
    # Check cache first to avoid unnecessary session creation
    cached = _get_cached_branding()
    if cached is not None:
        stats, portal_name, favicon = cached
    else:
        # Brand assets only — the customer portal must never receive admin
        # operational data (a previous admin-service call injected unscoped
        # system-wide counts into every portal page, including the
        # unauthenticated login screen; see the portal boundary architecture
        # test). brand_profiles is the canonical customer-facing brand owner.
        db = db_session_adapter.create_session()
        stats = {}
        portal_name = ""
        try:
            from app.services import brand_profiles

            brand = brand_profiles.resolve_brand(db)
            stats = {
                "sidebar_logo_url": brand.logo_url,
                "sidebar_logo_dark_url": brand.dark_logo_url,
                "favicon_url": brand.favicon_url,
            }
            portal_name = brand.legal_name or brand.product_name or ""
        except Exception:
            logger.debug("Failed to resolve brand for portal branding")
        finally:
            db.close()

        favicon = str(stats.get("favicon_url") or "").strip()
        _update_branding_cache(stats, portal_name, favicon)

    # Check restricted status per-request (user-specific, cannot be cached globally)
    restricted = False
    cached_brand = stats.get("brand") if isinstance(stats, dict) else None
    resolved_brand: dict[str, object] | None = (
        dict(cached_brand) if isinstance(cached_brand, dict) else None
    )
    notification_preview: dict[str, object] = {
        "recent_notifications": [],
        "recent_notifications_total": 0,
        "unread_notifications_count": 0,
        "has_recent_notifications": False,
    }
    try:
        from app.services.customer_portal_context import is_subscriber_restricted
        from app.services.customer_portal_notifications import get_notifications_preview
        from app.web.customer.auth import get_current_customer_from_request

        db = db_session_adapter.create_session()
        try:
            customer = get_current_customer_from_request(request, db)
            if customer:
                subscriber_id = optional_customer_subscriber_id(db, customer)
                if subscriber_id:
                    from app.services.brand_profiles import resolve_brand

                    brand = resolve_brand(db, subscriber_id=subscriber_id)
                    resolved_brand = brand.to_dict()
                    stats = {
                        **stats,
                        "sidebar_logo_url": brand.logo_url,
                        "sidebar_logo_dark_url": brand.dark_logo_url,
                        "favicon_url": brand.favicon_url,
                        "app_name": brand.product_name,
                    }
                    portal_name = brand.product_name
                    favicon = brand.favicon_url
                if subscriber_id and is_subscriber_restricted(db, subscriber_id):
                    restricted = True
                notification_preview = get_notifications_preview(db, customer)
        finally:
            db.close()
    except Exception:
        logger.debug("Failed to load customer portal request context")

    return {
        "sidebar_stats": stats,
        "branding_favicon_url": favicon,
        "portal_name": portal_name,
        **({"brand": resolved_brand} if resolved_brand else {}),
        "restricted": restricted,
        **notification_preview,
    }


def get_customer_templates() -> Jinja2Templates:
    """Return Jinja2Templates configured with customer branding context."""
    templates = Jinja2Templates(
        directory="templates",
        context_processors=[customer_branding_context],
    )
    return register_customer_portal_filters(templates)
