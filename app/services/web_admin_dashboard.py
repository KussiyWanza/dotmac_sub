"""Service helpers for admin dashboard routes."""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from threading import Lock, Thread
from time import monotonic
from urllib.parse import urlencode

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.models.domain_settings import DomainSetting, SettingDomain
from app.schemas.status_presentation import StatusTone
from app.services import admin_alerts as admin_alerts_service
from app.services import admin_attention as admin_attention_service
from app.services import admin_whats_new as admin_whats_new_service
from app.services import infrastructure_health as infrastructure_health_service
from app.services import settings_spec
from app.services import (
    subscriber as subscriber_service,
)
from app.services import (
    system_health as system_health_service,
)
from app.services import (
    web_admin as web_admin_service,
)
from app.services import web_system_health as web_system_health_service
from app.services.audit_adapter import audit_adapter
from app.services.audit_helpers import (
    build_recent_activity_feed,
    extract_changes,
    format_audit_datetime,
    format_changes,
    humanize_action,
    humanize_entity,
    load_audit_actor_subscribers,
    resolve_actor_name,
)
from app.services.ui_contracts import Kpi, StateValue

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="templates")
# In-process cache for the main /admin/dashboard global context.
# Contains SQLAlchemy ORM rows (recent_activity, recent_subscribers, active_alarms)
# that can't be JSON-serialized to Redis, hence the in-process cache.
_DASHBOARD_GLOBAL_TTL_SECONDS = settings.dashboard_global_cache_ttl_seconds
_dashboard_global_lock = Lock()
_dashboard_global_cached_at = 0.0
_dashboard_global_cache: dict[str, object] | None = None
_dashboard_global_refreshing = False
_DASHBOARD_GLOBAL_STALE_WHILE_REVALIDATE = (
    settings.dashboard_global_stale_while_revalidate
)
_DASHBOARD_GLOBAL_MAX_STALE_SECONDS = max(
    _DASHBOARD_GLOBAL_TTL_SECONDS,
    settings.dashboard_global_max_stale_seconds,
)


def _network_monitoring_int_setting(db: Session, key: str, default: int) -> int:
    raw = settings_spec.resolve_value(db, SettingDomain.network_monitoring, key)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _invoice_total(inv) -> float:
    return float(getattr(inv, "total", None) or getattr(inv, "total_amount", 0) or 0)


def _float_setting(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _setting_raw_value(setting: DomainSetting) -> object | None:
    return setting.value_json if setting.value_json is not None else setting.value_text


def _rollback_after_failed_query(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        logger.debug("Failed to roll back dashboard session", exc_info=True)


def _build_cached_ont_status_summary(db: Session) -> dict[str, int]:
    """Return owner-resolved ONT operation from persisted observations.

    The dashboard must not synchronously poll monitoring per OLT during initial
    render. The lifecycle owner evaluates observation age and returns only
    working/not-working counts; diagnostics may still show the raw evidence.
    """
    from app.services.network.ont_status import ont_status_summary

    thresholds = _build_health_thresholds(db)
    return ont_status_summary(
        db,
        low_signal_threshold_dbm=float(thresholds.get("ont_signal_warning_dbm") or -25),
    )


def _build_health_thresholds(db: Session) -> dict:
    """Resolve network/server health thresholds from settings."""
    keys = {
        "server_health_disk_warn_pct": "disk_warn_pct",
        "server_health_disk_crit_pct": "disk_crit_pct",
        "server_health_mem_warn_pct": "mem_warn_pct",
        "server_health_mem_crit_pct": "mem_crit_pct",
        "server_health_load_warn": "load_warn",
        "server_health_load_crit": "load_crit",
        "network_health_warn_pct": "network_warn_pct",
        "network_health_crit_pct": "network_crit_pct",
        "ont_signal_warning_dbm": "ont_signal_warning_dbm",
    }
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.network_monitoring)
        .filter(DomainSetting.key.in_(keys.keys()))
        .filter(DomainSetting.is_active.is_(True))
        .all()
    )
    values = {keys[row.key]: _float_setting(_setting_raw_value(row)) for row in rows}
    return {field: values.get(field) for field in keys.values()}


def _build_dashboard_billing_summary(db: Session) -> dict[str, float] | None:
    """Return the small billing aggregate needed by the admin overview.

    Revenue this month comes from the billing reporting read owner
    (BillingReporting.get_overview_stats); this service no longer sums payments
    itself. Pending/overdue receivables stay on the invoice_collectibility owner
    so the value matches the Overdue KPI exactly. Returns ``None`` when the read
    owner is unavailable so the KPI renders "Unknown" rather than a lying ₦0.
    """
    from app.services.billing.reporting import BillingReporting
    from app.services.invoice_collectibility import (
        invoice_balance_sum,
        open_invoice_filters,
        overdue_debt_filters,
    )

    try:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_end = (month_start + timedelta(days=32)).replace(day=1)
        overview = BillingReporting.get_overview_stats(
            db, period_start=month_start, period_end=month_end
        )
        return {
            "payments_this_month": float(overview["total_revenue"]),
            "pending_amount": float(invoice_balance_sum(db, open_invoice_filters())),
            "overdue_amount": float(
                invoice_balance_sum(db, overdue_debt_filters(now=now))
            ),
        }
    except Exception:
        logger.debug("Failed to load dashboard billing summary", exc_info=True)
        _rollback_after_failed_query(db)
        return None


def _build_online_customer_summary(db: Session) -> dict[str, int] | None:
    """Return active-session counts focused on customers, not raw sessions.

    Delegates to the radius_sessions read owner (online_summary); this service no
    longer queries RadiusActiveSession directly. Returns ``None`` when the read
    owner is unavailable so the KPI renders "Unknown" rather than a false 0.
    """
    try:
        from app.services.network import radius_sessions

        return radius_sessions.online_summary(db)
    except Exception:
        logger.debug("Failed to load online customer summary", exc_info=True)
        _rollback_after_failed_query(db)
        return None


def _build_recent_activities(
    recent_activity: list, subscribers_lookup: dict
) -> list[dict]:
    """Transform audit events into display-ready activity dicts."""
    recent_activities = []
    for event in recent_activity[:5]:
        activity_type = "info"
        action = getattr(event, "action", "")
        entity_type = getattr(event, "entity_type", "")
        entity_id = getattr(event, "entity_id", None)

        if "payment" in action.lower() or "invoice" in entity_type.lower():
            activity_type = "payment"
        elif "subscriber" in entity_type.lower():
            activity_type = "signup" if "create" in action.lower() else "activation"

        actor_name = resolve_actor_name(event, subscribers_lookup)

        time_str = format_audit_datetime(getattr(event, "occurred_at", None), "%H:%M")

        metadata = getattr(event, "metadata_", None) or {}
        changes = extract_changes(metadata, getattr(event, "action", None))
        change_summary = format_changes(changes)
        action_label = humanize_action(action)
        entity_label = humanize_entity(entity_type, entity_id)

        message = f"{actor_name} {action_label} {entity_label}"
        detail = change_summary or entity_label

        recent_activities.append(
            {
                "type": activity_type,
                "message": message,
                "detail": detail,
                "time": time_str,
            }
        )
    return recent_activities


def build_dashboard_kpis(
    *,
    total_subscribers: int,
    online_sessions_value: StateValue,
    devices_working: int,
    devices_total: int,
    payments_this_month: float,
    overdue_amount: float,
    total_alarms: int,
    billing_ok: bool,
) -> dict[str, Kpi]:
    """Project the dashboard headline tiles as Kpi contracts.

    Each ``cohort_url`` drills into the exact admin list that produced the number
    (KPI-parity), so a headline and its list can never diverge. The money tiles
    carry ``StateValue.unknown()`` when the billing read owner is down, so a
    decimal 0 never stands in for an unresolved value; the online tile carries
    whatever state the RADIUS read owner resolved.
    """
    money = (
        (lambda amount: StateValue.present(f"₦{amount:,.0f}"))
        if billing_ok
        else (lambda amount: StateValue.unknown())
    )
    return {
        "total_subscribers": Kpi(
            label="Subscribers",
            value=StateValue.present(total_subscribers),
            cohort_url="/admin/customers",
        ),
        "online": Kpi(
            # The destination lists session rows, so this headline counts the
            # exact same entity instead of distinct customers.
            label="Online Sessions",
            value=online_sessions_value,
            cohort_url="/admin/network/sessions",
            tone=StatusTone.positive,
        ),
        "network_devices": Kpi(
            label="Network Devices",
            value=StateValue.present(f"{devices_working} / {devices_total}"),
            cohort_url="/admin/network/monitoring",
            tone=StatusTone.info,
        ),
        "collections": Kpi(
            label="Collections This Month",
            value=money(payments_this_month),
            # This is the billing-overview collected figure (invoice settled
            # value over the calendar month) — the same number /admin/billing
            # displays. No itemized list sums to exactly that invoice-settled,
            # calendar-month aggregate (the payments list is a different entity
            # on a rolling window), so the drill-down is the overview that shows
            # the identical figure — KPI-parity by same-number, not a mismatched
            # list.
            cohort_url="/admin/billing",
            tone=StatusTone.positive,
        ),
        "overdue": Kpi(
            label="Overdue",
            value=money(overdue_amount),
            cohort_url="/admin/billing/invoices?" + urlencode({"status": "overdue"}),
            tone=StatusTone.warning,
        ),
        "alarms": Kpi(
            label="Alarms",
            value=StateValue.present(total_alarms),
            cohort_url="/admin/network/alarms",
            tone=StatusTone.negative,
        ),
    }


def _build_dashboard_global_context(db: Session) -> dict[str, object]:
    """Build the heavy, non-user-specific portion of the dashboard context.

    Result is cached in-process for ~60s (see _get_cached_dashboard_global_context).
    """
    from app.services import network_monitoring as network_monitoring_service

    # --- Server health ---
    server_health = system_health_service.get_system_health()
    thresholds = _build_health_thresholds(db)
    server_health_status = system_health_service.evaluate_health(
        server_health, thresholds
    )

    # --- Centralized stats ---
    sub_stats = subscriber_service.subscribers.get_dashboard_stats(db)
    net_stats = network_monitoring_service.network_devices.get_dashboard_stats(db)
    billing_summary = _build_dashboard_billing_summary(db)
    billing_ok = billing_summary is not None
    billing_values = billing_summary or {
        "payments_this_month": 0.0,
        "pending_amount": 0.0,
        "overdue_amount": 0.0,
    }

    # --- Network health (counts + ring status from the monitoring read owner) ---
    warn_pct = int(thresholds.get("network_warn_pct") or 90)
    crit_pct = int(thresholds.get("network_crit_pct") or 70)
    network_health = network_monitoring_service.network_health_summary(
        db,
        warn_pct=warn_pct,
        crit_pct=crit_pct,
        fallback_stats=net_stats,
    )
    olt_total = network_health["olt_total"]
    ont_total = network_health["ont_total"]
    ont_working = network_health["ont_working"]
    olts_total = network_health["olts_total"]
    olts_working = network_health["olts_working"]
    health_pct = network_health["health_pct"]
    health_status = network_health["health_status"]

    # --- Billing summary ---
    payments_this_month = billing_values["payments_this_month"]
    pending_amount = billing_values["pending_amount"]
    overdue_amount = billing_values["overdue_amount"]
    active_subscribers = sub_stats["active_count"]
    arpu = payments_this_month / active_subscribers if active_subscribers > 0 else 0

    # --- AR aging breakdown (canonical buckets from the billing read owner) ---
    ar_30 = 0.0
    ar_60 = 0.0
    try:
        from app.services.billing.reporting import BillingReporting

        aging_totals = BillingReporting.get_ar_aging_buckets(db)["totals"]
        ar_30 = float(aging_totals.get("1_30", 0) or 0)
        ar_60 = float(aging_totals.get("31_60", 0) or 0)
    except Exception:
        logger.debug("Failed to compute AR aging", exc_info=True)
        _rollback_after_failed_query(db)

    # --- Bandwidth from the network monitoring read owner ---
    bw_current = "0"
    bw_peak = "0"
    try:
        from app.services import network_monitoring as network_monitoring_service

        total_bps = network_monitoring_service.bandwidth_summary(
            db,
            window_seconds=_network_monitoring_int_setting(
                db, "dashboard_bandwidth_window_seconds", 600
            ),
        )["total_bps"]
        if total_bps > 1e9:
            bw_current = f"{total_bps / 1e9:.1f} Gbps"
        elif total_bps > 1e6:
            bw_current = f"{total_bps / 1e6:.0f} Mbps"
        else:
            bw_current = f"{total_bps / 1e3:.0f} Kbps"
    except Exception:
        logger.debug("Failed to compute bandwidth stats", exc_info=True)
        _rollback_after_failed_query(db)

    stats = {
        "total_subscribers": sub_stats["total_count"],
        "active_subscribers": active_subscribers,
        "subscribers_change": sub_stats.get("new_this_month", 0),
        "monthly_revenue": payments_this_month,
        "mrr": payments_this_month,
        "arpu": arpu,
        "revenue_change": 0,
        "system_uptime": net_stats["uptime_percentage"],
        "ar_current": pending_amount,
        "ar_30": ar_30,
        "ar_60": ar_60,
        "ar_90": overdue_amount,
        "suspended_accounts": sub_stats["suspended_count"],
        "orders_new": 0,
        "orders_qualification": 0,
        "orders_scheduled": 0,
        "orders_in_progress": 0,
        "orders_pending_activation": 0,
        "orders_completed_today": 0,
        "olts_working": olts_working,
        "olts_total": olts_total,
        "onts_working": ont_working,
        "onts_total": ont_total,
        "alarms_critical": net_stats["alarms_critical"],
        "alarms_major": net_stats["alarms_major"],
        "alarms_minor": net_stats["alarms_minor"],
        "alarms_warning": net_stats["alarms_warning"],
        "bandwidth_current": bw_current,
        "bandwidth_peak": bw_peak,
        "churn_rate": sub_stats["churn_rate"],
    }

    # --- Recent activity ---
    recent_activity = audit_adapter.list_events(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )

    # Actor identities resolved by the audit helper (the same owner the audit
    # page uses); the dashboard no longer queries Subscriber directly.
    subscribers_lookup = load_audit_actor_subscribers(db, recent_activity)

    recent_activities = _build_recent_activities(recent_activity, subscribers_lookup)

    # --- Who's Online (distinct customers with active RADIUS sessions) ---
    online_summary = _build_online_customer_summary(db)
    online_ok = online_summary is not None
    online_values = online_summary or {"customers": 0, "sessions": 0}
    online_customers = online_values["customers"]
    online_sessions = online_values["sessions"]

    # --- Monitoring device summary (for operations dashboard) ---
    monitoring_summary = {
        "devices_working": net_stats.get("working_count", 0),
        "devices_not_working": net_stats.get("not_working_count", 0),
        "devices_total": net_stats.get("total_count", 0),
    }

    # --- ONT status summary ---
    try:
        ont_service_summary = _build_cached_ont_status_summary(db)
    except Exception:
        logger.debug("Failed to load ONT summary for dashboard", exc_info=True)
        _rollback_after_failed_query(db)
        ont_service_summary = {
            "working": 0,
            "not_working": 0,
            "low_signal": 0,
            "total": 0,
        }

    # --- Unconfigured ONTs (autofind read owner) ---
    unconfigured_ont_count = 0
    try:
        from app.services.network import olt_autofind as olt_autofind_service

        unconfigured_ont_count = olt_autofind_service.pending_candidate_count(db)
    except Exception:
        logger.debug(
            "Failed to load unconfigured ONT count for dashboard", exc_info=True
        )
        _rollback_after_failed_query(db)

    # --- PON interface status summary ---
    try:
        pon_interface_summary = network_monitoring_service.pon_interface_summary(db)
    except Exception:
        logger.debug(
            "Failed to load PON interface summary for dashboard", exc_info=True
        )
        _rollback_after_failed_query(db)
        pon_interface_summary = {"up": 0, "down": 0, "unknown": 0, "total": 0}

    # --- PON outages (interfaces currently down) ---
    pon_outages: list[dict] = []
    try:
        pon_outages = network_monitoring_service.pon_outages(db, limit=10)
    except Exception:
        logger.debug("Failed to load PON outages for dashboard", exc_info=True)
        _rollback_after_failed_query(db)

    # --- Pending service orders (provisioning read owner) ---
    pending_orders = 0
    try:
        from app.services.provisioning_managers import service_order_dashboard_counts

        order_counts = service_order_dashboard_counts(db)
        pending_orders = order_counts["pending"] + order_counts["in_progress"]
        stats["orders_new"] = order_counts["pending"]
        stats["orders_in_progress"] = order_counts["in_progress"]
        stats["orders_completed_today"] = order_counts["completed"]
    except Exception:
        logger.error("Failed to load service order stats for dashboard", exc_info=True)
        _rollback_after_failed_query(db)

    # --- Attention items (admin_attention owns inclusion/severity/order) ---
    total_alarms = (
        net_stats["alarms_critical"]
        + net_stats["alarms_major"]
        + net_stats["alarms_minor"]
        + net_stats["alarms_warning"]
    )
    try:
        pending_location_requests = web_admin_service._count_pending_location_requests(
            db
        )
    except Exception:
        logger.error(
            "Failed to load pending location requests for dashboard", exc_info=True
        )
        _rollback_after_failed_query(db)
        pending_location_requests = 0
    admin_alert_summary = admin_alerts_service.dashboard_alert_summary(db)
    by_category = admin_alert_summary.get("by_category")
    infrastructure_alerts = (
        by_category.get("infrastructure") if isinstance(by_category, dict) else None
    ) or {}
    attention_items, network_attention_items = (
        admin_attention_service.build_attention_items(
            net_stats=net_stats,
            overdue_amount=overdue_amount,
            suspended_count=sub_stats["suspended_count"],
            pending_orders=pending_orders,
            ont_summary=ont_service_summary,
            unconfigured_ont_count=unconfigured_ont_count,
            pending_location_requests=pending_location_requests,
            pon_outage_count=len(pon_outages),
            infrastructure_alerts=infrastructure_alerts,
            ont_not_working_threshold=_network_monitoring_int_setting(
                db, "dashboard_attention_ont_offline_threshold", 5
            ),
        )
    )

    online_pct = (
        round((online_customers / active_subscribers) * 100, 1)
        if active_subscribers > 0
        else 0
    )
    whats_new_items = admin_whats_new_service.serialize_for_dashboard(
        admin_whats_new_service.get_visible_items(db, limit=4)
    )

    # Freshness of this snapshot travels with the cached context so the header
    # and any staleness signal reflect build time, not render time.
    refreshed_at = datetime.now(UTC)

    # Whos-online counts carry their own state: a failed RADIUS read renders
    # "Unknown", never a 0 that would read as "nobody online".
    online_customers_state = (
        StateValue.present(online_customers) if online_ok else StateValue.unknown()
    )
    online_sessions_state = (
        StateValue.present(online_sessions) if online_ok else StateValue.unknown()
    )

    dashboard_kpis = build_dashboard_kpis(
        total_subscribers=sub_stats["total_count"],
        online_sessions_value=online_sessions_state,
        devices_working=monitoring_summary["devices_working"],
        devices_total=monitoring_summary["devices_total"],
        payments_this_month=payments_this_month,
        overdue_amount=overdue_amount,
        total_alarms=total_alarms,
        billing_ok=billing_ok,
    )

    return {
        "stats": stats,
        "subscriber_stats": sub_stats,
        "network_stats": net_stats,
        "billing_stats": {"stats": billing_values},
        "dashboard_kpis": dashboard_kpis,
        "network_health": {
            "percent": health_pct,
            "status": health_status,
            "warn_pct": warn_pct,
            "crit_pct": crit_pct,
        },
        "recent_activity": recent_activity,
        "recent_activities": recent_activities,
        "recent_subscribers": sub_stats["recent_subscribers"],
        "active_alarms": net_stats["active_alarms"],
        "attention_items": attention_items,
        "network_attention_items": network_attention_items,
        "admin_alert_summary": admin_alert_summary,
        "pending_orders": pending_orders,
        "total_alarms": total_alarms,
        "sidebar_stats": web_admin_service.get_sidebar_stats(db),
        "server_health": server_health,
        "server_health_status": server_health_status,
        "online_count": online_customers,
        "online_customers": online_customers,
        "online_sessions": online_sessions,
        "online_customers_state": online_customers_state,
        "online_sessions_state": online_sessions_state,
        "online_customer_pct": online_pct,
        "monitoring_summary": monitoring_summary,
        "ont_service_summary": ont_service_summary,
        "pon_interface_summary": pon_interface_summary,
        "pon_outages": pon_outages,
        "vpn_tunnels": [],
        "whats_new_items": whats_new_items,
        "unconfigured_ont_count": unconfigured_ont_count,
        # Wall-clock time this snapshot was built. Travels with the cached
        # context so the header shows real freshness, not render time.
        "refreshed_at": refreshed_at,
        # Same instant as a freshness-bearing StateValue for the header.
        "data_freshness": StateValue.present(refreshed_at, as_of=refreshed_at),
    }


def _get_cached_dashboard_global_context(db: Session) -> dict[str, object]:
    """Return cached dashboard global context, recomputing on TTL expiry.

    Single-flight: first thread holds the lock during recompute so concurrent
    requests don't all stampede the remote DB after cache expiry.
    """
    global _dashboard_global_cached_at, _dashboard_global_cache

    now = monotonic()
    if (
        _dashboard_global_cache
        and (now - _dashboard_global_cached_at) < _DASHBOARD_GLOBAL_TTL_SECONDS
    ):
        return _dashboard_global_cache

    stale_cache = _dashboard_global_cache
    stale_age = now - _dashboard_global_cached_at
    if (
        stale_cache
        and _DASHBOARD_GLOBAL_STALE_WHILE_REVALIDATE
        and stale_age < _DASHBOARD_GLOBAL_MAX_STALE_SECONDS
    ):
        _start_dashboard_global_refresh()
        return stale_cache

    with _dashboard_global_lock:
        now = monotonic()
        if (
            _dashboard_global_cache
            and (now - _dashboard_global_cached_at) < _DASHBOARD_GLOBAL_TTL_SECONDS
        ):
            return _dashboard_global_cache
        fresh = _build_dashboard_global_context(db)
        _dashboard_global_cached_at = monotonic()
        _dashboard_global_cache = fresh
        return fresh


def _start_dashboard_global_refresh() -> None:
    """Start one non-blocking refresh while callers keep the stale snapshot."""
    global _dashboard_global_refreshing

    with _dashboard_global_lock:
        if _dashboard_global_refreshing:
            return
        _dashboard_global_refreshing = True
    try:
        Thread(
            target=_refresh_dashboard_global_cache,
            name="dashboard-global-cache-refresh",
            daemon=True,
        ).start()
    except Exception:
        with _dashboard_global_lock:
            _dashboard_global_refreshing = False
        logger.exception("Failed to start dashboard cache refresh")


def _refresh_dashboard_global_cache() -> None:
    """Refresh dashboard data using a session owned by the background thread."""
    global _dashboard_global_cache, _dashboard_global_cached_at
    global _dashboard_global_refreshing

    fresh: dict[str, object] | None = None
    try:
        from app.db import SessionLocal

        with SessionLocal() as db:
            fresh = _build_dashboard_global_context(db)
    except Exception:
        logger.exception("Background dashboard cache refresh failed")
    finally:
        with _dashboard_global_lock:
            if fresh is not None:
                _dashboard_global_cache = fresh
                _dashboard_global_cached_at = monotonic()
            _dashboard_global_refreshing = False


def prewarm_dashboard_global_cache() -> bool:
    """Populate the dashboard cache outside the first user's request path."""
    from app.db import SessionLocal

    try:
        with SessionLocal() as db:
            _get_cached_dashboard_global_context(db)
        return True
    except Exception:
        logger.exception("Dashboard cache prewarm failed")
        return False


def _resolve_dashboard_permissions(
    request: Request, db: Session
) -> tuple[bool, bool, bool]:
    """Resolve per-user show_financials/show_network/show_subscribers flags."""
    from app.services.auth_dependencies import has_permission

    auth = getattr(request.state, "auth", None) or {}
    user = getattr(request.state, "user", None)

    if auth.get("principal_id"):

        def _has(perm: str) -> bool:
            return has_permission(auth, db, perm)

        return (
            _has("billing:invoice:read")
            or _has("billing:payment:read")
            or _has("reports:billing:read"),
            _has("network:device:read")
            or _has("network:olt:read")
            or _has("network:ont:read")
            or _has("monitoring:read")
            or _has("reports:network:read"),
            _has("customer:read"),
        )
    if user:
        try:
            from app.services.auth_dependencies import user_role_names

            role_names = user_role_names(
                db, user.get("subscriber_id") or user.get("id", "")
            )
            if role_names is not None:
                is_admin = "admin" in role_names or "super_admin" in role_names
                return (
                    is_admin or "finance" in role_names or "billing" in role_names,
                    is_admin
                    or "noc" in role_names
                    or "network" in role_names
                    or "technician" in role_names,
                    is_admin or "support" in role_names or "sales" in role_names,
                )
        except Exception:
            pass
    return (True, True, True)


def dashboard(request: Request, db: Session):
    """Build the main admin dashboard context and return TemplateResponse.

    Heavy global queries are memoized via _get_cached_dashboard_global_context().
    Only per-user fields (current_user + permission flags) are computed inline.
    """
    show_financials, show_network, show_subscribers = _resolve_dashboard_permissions(
        request, db
    )
    current_user = web_admin_service.get_current_user(request)
    from app.services.auth_dependencies import can

    show_attendance = can(request, "attendance:self:use")
    global_ctx = _get_cached_dashboard_global_context(db)

    return templates.TemplateResponse(
        "admin/dashboard/index.html",
        {
            "request": request,
            "now": datetime.now(UTC),
            # Real freshness of the cached snapshot, not render time.
            "data_as_of": global_ctx.get("refreshed_at") or datetime.now(UTC),
            "active_page": "dashboard",
            "current_user": current_user,
            "show_financials": show_financials,
            "show_network": show_network,
            "show_subscribers": show_subscribers,
            "show_attendance": show_attendance,
            **global_ctx,
        },
    )


def dashboard_server_health_partial(
    request: Request,
    db: Session,
    worker_action_notice: dict[str, str] | None = None,
):
    server_health = system_health_service.get_system_health()
    thresholds = _build_health_thresholds(db)
    server_health_status = system_health_service.evaluate_health(
        server_health, thresholds
    )
    try:
        (
            infrastructure_services,
            worker_health,
            service_summary,
            infrastructure_snapshot,
        ) = _load_dashboard_infrastructure_health(db)
    except Exception:
        logger.exception("Failed to load dashboard infrastructure health")
        infrastructure_services = []
        worker_health = web_system_health_service._build_worker_health([])
        service_summary = {
            "total": 0,
            "up": 0,
            "degraded": 0,
            "down": 0,
            "unknown": 0,
        }
        infrastructure_snapshot = None
    return templates.TemplateResponse(
        "admin/dashboard/_server_health.html",
        {
            "request": request,
            "server_health": server_health,
            "server_health_status": server_health_status,
            "infrastructure_services": infrastructure_services,
            "worker_health": worker_health,
            "service_summary": service_summary,
            "infrastructure_snapshot": infrastructure_snapshot,
            "worker_action_notice": worker_action_notice,
        },
    )


def _load_dashboard_infrastructure_health(
    db: Session,
) -> tuple[
    list[infrastructure_health_service.ServiceStatus],
    dict[str, object],
    dict[str, int],
    infrastructure_health_service.InfrastructureHealthSnapshot | None,
]:
    """Read the shared scheduled snapshot; never run dependency probes here."""
    del db
    snapshot = infrastructure_health_service.load_health_snapshot()
    infrastructure_services = list(snapshot.services) if snapshot is not None else []
    worker_health = web_system_health_service._build_worker_health(
        infrastructure_services
    )
    service_summary = _build_infrastructure_service_summary(infrastructure_services)
    return (
        infrastructure_services,
        worker_health,
        service_summary,
        snapshot,
    )


def _build_infrastructure_service_summary(
    services: Sequence[object],
) -> dict[str, int]:
    from app.schemas.status_presentation import StatusTone
    from app.services.status_presentation import (
        infrastructure_service_status_presentation,
    )

    summary = {
        "total": len(services),
        "up": 0,
        "degraded": 0,
        "down": 0,
        "unknown": 0,
    }
    _tone_bucket = {
        StatusTone.positive: "up",
        StatusTone.warning: "degraded",
        StatusTone.negative: "down",
    }
    for service in services:
        tone = infrastructure_service_status_presentation(
            getattr(service, "status", None)
        ).tone
        summary[_tone_bucket.get(tone, "unknown")] += 1
    return summary


def dashboard_stats_partial(request: Request, db: Session):
    show_financials, show_network, show_subscribers = _resolve_dashboard_permissions(
        request, db
    )
    global_ctx = _get_cached_dashboard_global_context(db)
    return templates.TemplateResponse(
        "admin/dashboard/_stats.html",
        {
            "request": request,
            "show_financials": show_financials,
            "show_network": show_network,
            "show_subscribers": show_subscribers,
            **global_ctx,
        },
    )


def dashboard_activity_partial(request: Request, db: Session):
    recent_activity = audit_adapter.list_events(
        db=db,
        actor_id=None,
        actor_type=None,
        action=None,
        entity_type=None,
        entity_id=None,
        request_id=None,
        is_success=None,
        status_code=None,
        is_active=None,
        order_by="occurred_at",
        order_dir="desc",
        limit=10,
        offset=0,
    )
    recent_activities = build_recent_activity_feed(db, recent_activity, limit=10)

    return templates.TemplateResponse(
        "admin/dashboard/_activity.html",
        {"request": request, "recent_activities": recent_activities},
    )
