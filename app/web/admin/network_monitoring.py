"""Admin network monitoring and alarms web routes."""

import uuid

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import web_network_alarm_rules as web_network_alarm_rules_service
from app.services import web_network_core_runtime as web_network_core_runtime_service
from app.services import web_network_monitoring as web_network_monitoring_service
from app.services import web_network_noc as web_network_noc_service
from app.services import web_network_noc_inspector as web_network_noc_inspector_service
from app.services.auth_dependencies import require_permission
from app.web.request_parsing import parse_form_data_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])

_format_duration = web_network_core_runtime_service.format_duration
_format_bps = web_network_core_runtime_service.format_bps


def _base_context(
    request: Request, db: Session, active_page: str, active_menu: str = "network"
) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": active_menu,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/topology-gaps",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def topology_gaps_page(
    request: Request,
    node_page: int = Query(1, ge=1),
    node_per_page: int = Query(50, ge=10, le=200),
    gap_page: int = Query(1, ge=1),
    gap_per_page: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    from app.services.topology.gaps import topology_gaps

    context = _base_context(request, db, active_page="monitoring")
    context["gaps"] = topology_gaps(
        db,
        node_page=node_page,
        node_per_page=node_per_page,
        gap_page=gap_page,
        gap_per_page=gap_per_page,
    )
    return templates.TemplateResponse("admin/network/topology_gaps.html", context)


@router.get(
    "/device-status-worklist",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def device_status_worklist_page(
    request: Request,
    reason: str | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    """Inventory-hygiene queue: devices whose admin status conflicts with what
    monitoring observes, grouped by reason and routed to an owning team.
    See docs/designs/DEVICE_OPERATIONAL_STATUS.md."""
    from app.services.device_operational_status import mismatch_worklist

    context = _base_context(request, db, active_page="monitoring")
    context["worklist"] = mismatch_worklist(
        db,
        reason=reason,
        search=search,
        page=page,
        per_page=per_page,
    )
    return templates.TemplateResponse(
        "admin/network/device_status_worklist.html", context
    )


@router.get(
    "/performance",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def performance_page(
    request: Request,
    tab: str = "wallboard",
    tier: str = "bts",
    window: str = "7d",
    db: Session = Depends(get_db),
):
    """Infrastructure performance & SLA dashboard (BTS / OLT / PON / AP).

    Three tabs: live wallboard, worst-performer ranking, SLA compliance. See
    docs/designs/INFRASTRUCTURE_SLA_PERFORMANCE.md.
    """
    from app.config import settings
    from app.services import web_network_performance as perf

    context = _base_context(request, db, active_page="monitoring")
    active_tab = tab if tab in {"wallboard", "ranking", "sla"} else "wallboard"
    context["active_tab"] = active_tab
    context["tiers"] = perf.TIERS
    context["windows"] = perf.WINDOWS
    context["sel_tier"] = tier if tier in perf.TIERS else "bts"
    context["sel_window"] = window if window in perf.WINDOWS else "7d"
    context["wallboard"] = perf.wallboard(db)
    context["sla_logging_enabled"] = bool(settings.sla_availability_log_enabled)
    if active_tab in {"ranking", "sla"}:
        context["ranking"] = perf.ranking(
            db, context["sel_tier"], context["sel_window"]
        )
    return templates.TemplateResponse("admin/network/performance/index.html", context)


@router.get(
    "/performance/trend",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def performance_trend_page(
    request: Request,
    element_type: str,
    element_id: str,
    name: str = "",
    db: Session = Depends(get_db),
):
    """Availability trend (Chart.js) for one element, from daily snapshots."""
    import uuid as _uuid

    from app.services import infrastructure_availability_snapshot as snap

    context = _base_context(request, db, active_page="monitoring")
    points: list = []
    if element_type in {"device", "pop_site", "pon_port"}:
        try:
            points = snap.trend(db, element_type, _uuid.UUID(element_id), days=365)
        except (ValueError, TypeError):
            points = []
    context["element_name"] = name or element_id
    context["element_type"] = element_type
    context["points"] = points
    return templates.TemplateResponse("admin/network/performance/trend.html", context)


@router.get(
    "/performance/export",
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def performance_export(
    tier: str = "bts",
    window: str = "7d",
    db: Session = Depends(get_db),
):
    from fastapi.responses import Response

    from app.services import web_network_performance as perf

    content = perf.build_ranking_csv(db, tier, window)
    safe_tier = tier if tier in perf.TIERS else "bts"
    safe_window = window if window in perf.WINDOWS else "7d"
    return Response(
        content=content,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f"attachment; filename=infra_sla_{safe_tier}_{safe_window}.csv"
            )
        },
    )


@router.get(
    "/outage-impact",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def outage_impact_page(
    request: Request,
    basestation_id: str | None = None,
    node_id: str | None = None,
    fdh_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Read-only outage impact preview: pick infrastructure -> affected
    active subscriptions. No incident is created (that's the outage console)."""
    import uuid as _uuid

    from app.models.network import FdhCabinet
    from app.models.network_monitoring import NetworkDevice, PopSite
    from app.services.topology.affected import (
        affected_customers,
        fdh_impact_branches,
        fdh_impact_rows,
        impact_breakdown,
        list_basestations,
        list_fdh_cabinets,
        list_network_nodes,
    )

    context = _base_context(request, db, active_page="monitoring")
    context["basestations"] = list_basestations(db)
    context["fdh_cabinets"] = list_fdh_cabinets(db)
    context["network_nodes"] = list_network_nodes(db)
    context["selected_basestation_id"] = basestation_id
    context["selected_node_id"] = node_id
    context["selected_fdh_id"] = fdh_id
    target = None
    result = None
    detailed_rows = None
    try:
        if basestation_id:
            pop = db.get(PopSite, _uuid.UUID(basestation_id))
            if pop is not None:
                target = f"Basestation: {pop.name}"
                result = affected_customers(db, basestation=pop)
        elif node_id:
            node = db.get(NetworkDevice, _uuid.UUID(node_id))
            if node is not None:
                target = f"Node: {node.name}"
                result = affected_customers(db, node=node)
        elif fdh_id:
            fdh = db.get(FdhCabinet, _uuid.UUID(fdh_id))
            if fdh is not None:
                label = fdh.code or fdh.name
                target = f"FDH Cabinet: {label}"
                result = affected_customers(db, fdh=fdh)
                detailed_rows = fdh_impact_rows(db, fdh)
    except (ValueError, TypeError):
        target = None
    context["target"] = target
    if result is not None:
        context["impact_count"] = result["count"]
        if detailed_rows is not None:
            context["impact_rows"] = detailed_rows
            context["impact_detail_mode"] = "fdh"
            context["impact_branches"] = fdh_impact_branches(detailed_rows)
        else:
            context["impact_rows"] = [
                {
                    "subscription_id": s.id,
                    "subscriber_name": (
                        f"{s.subscriber.first_name} {s.subscriber.last_name}"
                        if s.subscriber
                        else "—"
                    ),
                    "email": s.subscriber.email if s.subscriber else "",
                }
                for s in result["subscriptions"]
            ]
            context["impact_detail_mode"] = "basic"
            context["impact_branches"] = impact_breakdown(db, result)
    return templates.TemplateResponse("admin/network/outage_impact.html", context)


def _actor(request: Request) -> str | None:
    from app.web.admin import get_current_user

    user = get_current_user(request)
    if isinstance(user, dict):
        return user.get("email") or user.get("username")
    return getattr(user, "email", None) or getattr(user, "username", None)


def _cabinet_notice_context(request: Request, db: Session, fdh_id: str) -> dict | None:
    """Shared page context for the cabinet-notice console; None = unknown FDH."""
    import uuid as _uuid

    from app.models.network import FdhCabinet
    from app.services.network.outage_impact import resolve_fdh_audience

    context = _base_context(request, db, active_page="monitoring")
    try:
        fdh = db.get(FdhCabinet, _uuid.UUID(fdh_id))
    except (ValueError, TypeError):
        fdh = None
    if fdh is None:
        return None
    context["fdh"] = fdh
    context["fdh_id"] = str(fdh.id)
    context["audience"] = resolve_fdh_audience(db, fdh)
    return context


@router.get(
    "/cabinet-notice",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def cabinet_notice_page(
    request: Request,
    fdh_id: str,
    db: Session = Depends(get_db),
):
    """Draft a service notice to every active customer behind one FDH.

    Read-only entry point: shows the audience summary and the draft form.
    Nothing sends without the preview -> confirm POST flow below."""
    context = _cabinet_notice_context(request, db, fdh_id)
    if context is None:
        base = _base_context(request, db, active_page="monitoring")
        base["fdh"] = None
        return templates.TemplateResponse(
            "admin/network/cabinet_notice.html", base, status_code=404
        )
    return templates.TemplateResponse("admin/network/cabinet_notice.html", context)


@router.post(
    "/cabinet-notice",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def cabinet_notice_submit(
    request: Request,
    fdh_id: str = Form(...),
    subject: str = Form(default=""),
    body: str = Form(default=""),
    mode: str = Form(default="preview"),
    expected_count: int | None = Form(default=None),
    expected_scope_token: str | None = Form(default=None),
    expected_impact_token: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Preview or send a cabinet service notice.

    ``mode=preview`` classifies every distinct customer (eligible /
    missing-email / suppressed / unresolved) and binds the result into scope +
    impact tokens carried as hidden fields. ``mode=send`` recomputes the
    preview and dispatches only when both tokens and the eligible count still
    match — membership or content drift re-renders the fresh preview with a
    409 instead of sending on a stale audience. The command owner commits its
    own transaction; this adapter never commits (transaction-ownership
    contract)."""
    import uuid as _uuid

    from app.services.network.cabinet_notice import (
        CabinetNoticeConfirmation,
        CabinetNoticeDraft,
        CabinetNoticeDriftError,
        CabinetNoticeValidationError,
        preview_cabinet_notice,
        send_cabinet_notice,
    )

    context = _cabinet_notice_context(request, db, fdh_id)
    if context is None:
        return RedirectResponse("/admin/network/outage-impact", status_code=303)
    context["subject"] = subject
    context["body"] = body

    draft = CabinetNoticeDraft(fdh_id=_uuid.UUID(fdh_id), subject=subject, body=body)
    status_code = 200
    try:
        if mode == "send":
            result = send_cabinet_notice(
                db,
                draft,
                CabinetNoticeConfirmation(
                    confirmed=True,
                    expected_count=expected_count,
                    expected_scope_token=expected_scope_token,
                    expected_impact_token=expected_impact_token,
                ),
                actor=_actor(request),
            )
            context["result"] = result
        else:
            context["preview"] = preview_cabinet_notice(db, draft)
    except CabinetNoticeValidationError as exc:
        context["error"] = str(exc)
        status_code = 400
    except CabinetNoticeDriftError as exc:
        # Show the CURRENT preview next to the conflict so the operator
        # re-confirms against reality, never the stale audience.
        context["conflict"] = str(exc)
        try:
            context["preview"] = preview_cabinet_notice(db, draft)
        except CabinetNoticeValidationError as preview_exc:
            context["error"] = str(preview_exc)
        status_code = 409
    return templates.TemplateResponse(
        "admin/network/cabinet_notice.html", context, status_code=status_code
    )


@router.get(
    "/outages",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def outages_console(request: Request, db: Session = Depends(get_db)):
    """Manual outage console: declare against infrastructure, list/resolve open
    incidents. No auto-detection, no notification sending."""
    from app.models.network import FdhCabinet
    from app.models.network_monitoring import NetworkDevice, PopSite
    from app.services.network.service_impact import summarize_incident_impact
    from app.services.operational_escalation_delivery import delivery_audit_for_entity
    from app.services.status_presentation import outage_status_presentation
    from app.services.topology.affected import (
        list_basestations,
        list_fdh_cabinets,
        list_network_nodes,
    )
    from app.services.topology.outage import (
        is_stale_open,
        list_operator_open_incidents,
    )
    from app.services.topology.outage_tickets import infrastructure_link_for
    from app.services.topology.reachability import reachability_overview

    context = _base_context(request, db, active_page="monitoring")
    # Root-cause view of everything currently down: devices behind a down
    # parent are unreachable, not independent outages (one failure, not N).
    context["reachability"] = reachability_overview(db)
    context["basestations"] = list_basestations(db)
    context["fdh_cabinets"] = list_fdh_cabinets(db)
    context["network_nodes"] = list_network_nodes(db)
    rows = []
    for inc in list_operator_open_incidents(db):
        if inc.basestation_id is not None:
            pop = db.get(PopSite, inc.basestation_id)
            target = f"BTS: {pop.name}" if pop else "BTS"
        elif getattr(inc, "fdh_cabinet_id", None) is not None:
            fdh = db.get(FdhCabinet, inc.fdh_cabinet_id)
            target = f"FDH: {fdh.code or fdh.name}" if fdh else "FDH"
        elif inc.root_node_id is not None:
            node = db.get(NetworkDevice, inc.root_node_id)
            target = f"Node: {node.name}" if node else "Node"
        else:
            target = "—"
        rows.append(
            {
                "incident": inc,
                "target": target,
                "stale": is_stale_open(inc),
                "status_presentation": outage_status_presentation(inc.status),
                # Exposure vs confirmed split from network.service_impact —
                # audience membership is exposure, never automatic downtime.
                "impact": summarize_incident_impact(db, inc),
                # The one canonical infrastructure ticket, when bound.
                "infrastructure_link": infrastructure_link_for(db, inc.id),
                "delivery_audit": delivery_audit_for_entity(
                    db,
                    entity_type="outage",
                    entity_id=inc.id,
                ),
            }
        )
    context["incidents"] = rows
    return templates.TemplateResponse("admin/network/outages.html", context)


@router.post(
    "/outages/declare",
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def outages_declare(
    request: Request,
    basestation_id: str | None = Form(default=None),
    node_id: str | None = Form(default=None),
    fdh_id: str | None = Form(default=None),
    note: str | None = Form(None),
    db: Session = Depends(get_db),
):
    import uuid as _uuid

    from app.models.network import FdhCabinet
    from app.models.network_monitoring import NetworkDevice, PopSite
    from app.services.topology.outage import declare_outage

    declared = False
    try:
        pop = db.get(PopSite, _uuid.UUID(basestation_id)) if basestation_id else None
    except (ValueError, TypeError):
        pop = None
    if pop is not None:
        declare_outage(db, basestation=pop, declared_by=_actor(request), note=note)
        declared = True
    if not declared:
        try:
            node = db.get(NetworkDevice, _uuid.UUID(node_id)) if node_id else None
        except (ValueError, TypeError):
            node = None
        if node is not None:
            declare_outage(db, node=node, declared_by=_actor(request), note=note)
            declared = True
    if not declared:
        try:
            fdh = db.get(FdhCabinet, _uuid.UUID(fdh_id)) if fdh_id else None
        except (ValueError, TypeError):
            fdh = None
        if fdh is not None:
            declare_outage(db, fdh=fdh, declared_by=_actor(request), note=note)
            declared = True
    if declared:
        db.commit()
    return RedirectResponse("/admin/network/outages", status_code=303)


@router.post(
    "/outages/{incident_id}/resolve",
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def outages_resolve(incident_id: str, request: Request, db: Session = Depends(get_db)):
    import uuid as _uuid

    from app.services.topology.outage import resolve_outage

    try:
        resolve_outage(db, _uuid.UUID(incident_id))
        db.commit()
    except (ValueError, TypeError):
        pass
    return RedirectResponse("/admin/network/outages", status_code=303)


@router.get(
    "/detected-outages",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def detected_outages_console(
    request: Request,
    node_id: str | None = None,
    db: Session = Depends(get_db),
):
    """Classifier-driven outage console (design §P4a).

    PRIMARY source is the PERSISTED, debounced classifier incidents from the
    §7.6 reconcile loop (suspected/confirmed/clearing) — the debounced truth,
    not the raw live-computed verdict. The live P1/P2/P3 verdict is kept as a
    clearly-separate SECONDARY "candidates" view (raw, un-debounced). Optional
    ``node_id`` drills into one failure domain (per-customer P2 verdicts + P3
    branch alerts). No operator declaration, no notification sending here."""
    import uuid as _uuid

    from app.models.network_monitoring import NetworkDevice, PopSite
    from app.services.status_presentation import outage_status_presentation
    from app.services.topology.outage import (
        list_classifier_incidents,
        mttr_so_far_seconds,
    )
    from app.services.topology.outage_console import (
        active_outages,
        network_health_summary,
        outage_detail,
    )

    context = _base_context(request, db, active_page="monitoring")

    # Primary: persisted debounced classifier incidents (newest first).
    incident_rows = []
    for inc in list_classifier_incidents(db):
        node = (
            db.get(NetworkDevice, inc.root_node_id)
            if inc.root_node_id is not None
            else None
        )
        basestation = None
        if inc.basestation_id is not None:
            basestation = db.get(PopSite, inc.basestation_id)
        elif node is not None and node.pop_site_id is not None:
            basestation = db.get(PopSite, node.pop_site_id)
        incident_rows.append(
            {
                "incident": inc,
                "state": inc.status,
                "status_presentation": outage_status_presentation(inc.status),
                "detection_source": inc.detection_source,
                "affected_count": inc.affected_count,
                "confidence": inc.confidence,
                "classification": inc.classification,
                "node_name": getattr(node, "name", None),
                "basestation_name": getattr(basestation, "name", None),
                "suspected_at": inc.suspected_at,
                "confirmed_at": inc.confirmed_at,
                "mttr_so_far_seconds": mttr_so_far_seconds(inc),
            }
        )
    context["classifier_incidents"] = incident_rows

    # Secondary: raw live-computed candidates (un-debounced) + self-heal queue.
    context["summary"] = network_health_summary(db)
    context["active"] = active_outages(db)
    detail = None
    if node_id:
        try:
            detail = outage_detail(db, _uuid.UUID(node_id))
        except (ValueError, TypeError):
            detail = None
    context["detail"] = detail
    return templates.TemplateResponse("admin/network/detected_outages.html", context)


def _actor_id(request: Request):
    """Current operator's user id (uuid) for outage-notify audit, or None."""
    import uuid as _uuid

    from app.web.admin import get_current_user

    user = get_current_user(request)
    raw = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    try:
        return _uuid.UUID(str(raw)) if raw else None
    except (ValueError, TypeError):
        return None


def _incident_boundary_and_subscription_ids(db: Session, incident_id: str | None):
    """Resolve a notifiable classifier incident + affected subscription ids.

    Thin pass-through: the resolution lives in
    ``app.services.topology.outage_targets`` so the automated dispatcher
    (ADR 0004) and this preview cannot drift into notifying different people
    from the same incident.
    """
    from app.services.topology.outage_targets import (
        incident_boundary_and_subscription_ids,
    )

    return incident_boundary_and_subscription_ids(db, incident_id)


@router.get(
    "/detected-outages/notify",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def detected_outage_notify_preview(
    request: Request,
    incident_id: str,
    db: Session = Depends(get_db),
):
    """Preview the outage notification plan for one boundary (design §P4).

    Read-only: shows who *would* be emailed (area vs per-customer, opt-out
    suppressed, qualifies/debounced) and the current OUTAGE_NOTIFY_ENABLED
    state. Never sends — the confirm POST is the only dispatch path."""
    from app.services.status_presentation import outage_status_presentation
    from app.services.topology.outage_notifications import (
        plan_outage_notifications,
        recent_dispatches,
    )

    context = _base_context(request, db, active_page="monitoring")
    incident, boundary, sub_ids = _incident_boundary_and_subscription_ids(
        db, incident_id
    )
    context["incident"] = incident
    if incident is not None:
        context["incident_status_presentation"] = outage_status_presentation(
            incident.status
        )
    context["boundary"] = boundary
    context["incident_id"] = str(incident.id) if incident is not None else incident_id
    if incident is None or boundary is None:
        return templates.TemplateResponse(
            "admin/network/detected_outages_notify.html", context, status_code=404
        )
    context["plan"] = plan_outage_notifications(db, sub_ids, incident_id=incident.id)
    context["recent"] = recent_dispatches(db, incident.id)
    return templates.TemplateResponse(
        "admin/network/detected_outages_notify.html", context
    )


@router.post(
    "/detected-outages/notify",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def detected_outage_notify_send(
    request: Request,
    incident_id: str = Form(...),
    confirm: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Confirm + dispatch outage notifications for one boundary (design §P4).

    The ONLY path that dispatches. Requires an explicit ``confirm=send`` (a
    stray navigation never sends) and passes the operator's ``actor_id`` to the
    hard-gated dispatcher, which itself no-ops unless OUTAGE_NOTIFY_ENABLED is
    on. COMMITS the session so audit rows + the persisted debounce survive."""
    from app.services.status_presentation import outage_status_presentation
    from app.services.topology.outage_notifications import (
        dispatch_outage_notifications,
        plan_outage_notifications,
        recent_dispatches,
    )

    incident, boundary, sub_ids = _incident_boundary_and_subscription_ids(
        db, incident_id
    )
    if incident is None or boundary is None:
        return RedirectResponse("/admin/network/detected-outages", status_code=303)

    context = _base_context(request, db, active_page="monitoring")
    context["incident"] = incident
    context["incident_status_presentation"] = outage_status_presentation(
        incident.status
    )
    context["boundary"] = boundary
    context["incident_id"] = str(incident.id)
    if confirm != "send":
        # No explicit confirmation — re-show the preview, dispatch nothing.
        context["plan"] = plan_outage_notifications(
            db, sub_ids, incident_id=incident.id
        )
        context["recent"] = recent_dispatches(db, incident.id)
        context["needs_confirm"] = True
        return templates.TemplateResponse(
            "admin/network/detected_outages_notify.html", context
        )

    result = dispatch_outage_notifications(
        db, sub_ids, actor_id=_actor_id(request), incident_id=incident.id
    )
    db.commit()
    context["result"] = result
    context["plan"] = plan_outage_notifications(db, sub_ids, incident_id=incident.id)
    context["recent"] = recent_dispatches(db, incident.id)
    return templates.TemplateResponse(
        "admin/network/detected_outages_notify.html", context
    )


@router.get(
    "/monitoring",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def monitoring_page(
    request: Request,
    q: str | None = None,
    refresh: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
    db: Session = Depends(get_db),
):
    force_refresh = (refresh or "").strip().lower() in {"1", "true", "yes", "on"}
    if force_refresh:
        web_network_monitoring_service.dispatch_monitoring_refresh(
            request_id=getattr(request.state, "request_id", None)
        )

    page_data = web_network_monitoring_service.monitoring_index_context(
        db,
        format_duration=_format_duration,
        format_bps=_format_bps,
        query=q,
        page=page,
        per_page=per_page,
    )
    context = _base_context(request, db, active_page="monitoring")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/monitoring/index.html", context)


@router.get(
    "/monitoring/kpi",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def monitoring_kpi_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial: auto-refreshing KPI cards + alarm/outage summary."""
    context = {
        "request": request,
        **web_network_monitoring_service.monitoring_kpi_context(
            db,
            format_duration=_format_duration,
            format_bps=_format_bps,
        ),
    }
    return templates.TemplateResponse(
        "admin/network/monitoring/_kpi_partial.html", context
    )


@router.get(
    "/monitoring/bandwidth",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def monitoring_bandwidth_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial: auto-refreshing live network bandwidth + per-NAS throughput."""
    context = {
        "request": request,
        **web_network_monitoring_service.monitoring_bandwidth_context(db),
    }
    return templates.TemplateResponse(
        "admin/network/monitoring/_bandwidth_partial.html", context
    )


@router.get(
    "/alarms",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def alarms_page(
    request: Request,
    severity: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=10, le=100),
    db: Session = Depends(get_db),
):
    page_data = web_network_monitoring_service.alarms_page_data(
        db,
        severity=severity,
        status=status,
        page=page,
        per_page=per_page,
    )
    context = _base_context(request, db, active_page="monitoring")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/monitoring/alarms.html", context)


@router.get(
    "/alarms/rules/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def alarms_rules_new(request: Request, db: Session = Depends(get_db)):
    options = web_network_alarm_rules_service.form_options(db)
    context = _base_context(request, db, active_page="monitoring")
    context.update(
        {
            "rule": None,
            "action_url": "/admin/network/alarms/rules/new",
            **options,
        }
    )
    return templates.TemplateResponse(
        "admin/network/monitoring/rule_form.html", context
    )


@router.post(
    "/alarms/rules/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def alarms_rules_create(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = web_network_alarm_rules_service.parse_form_values(form)
    normalized, error = web_network_alarm_rules_service.validate_form_values(values)
    if not error and normalized is not None:
        error = web_network_alarm_rules_service.create_rule(db, normalized)
        if not error:
            return RedirectResponse(url="/admin/network/alarms", status_code=303)
    elif not error:
        error = "Please correct the highlighted fields."

    options = web_network_alarm_rules_service.form_options(db)
    rule = web_network_alarm_rules_service.rule_form_data(values)
    context = _base_context(request, db, active_page="monitoring")
    context.update(
        {
            "rule": rule,
            "action_url": "/admin/network/alarms/rules/new",
            **options,
            "error": error or "Please correct the highlighted fields.",
        }
    )
    return templates.TemplateResponse(
        "admin/network/monitoring/rule_form.html", context
    )


@router.post(
    "/monitoring/bulk-action",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:write"))],
)
def monitoring_device_bulk_action(
    request: Request,
    action: str = Form(""),
    device_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Execute a bulk action on selected monitoring devices."""
    stats = web_network_monitoring_service.execute_device_bulk_action(
        db, device_ids, action
    )
    return HTMLResponse(
        web_network_monitoring_service.render_bulk_result(stats, action)
    )


@router.get(
    "/noc",
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def noc_queue_page(
    request: Request,
    db: Session = Depends(get_db),
):
    """Unified NOC triage queue, projected from the monitoring owners."""
    context = _base_context(request, db, active_page="noc")
    context.update(web_network_noc_service.noc_queue_data(db))
    return templates.TemplateResponse("admin/network/noc/index.html", context)


@router.get(
    "/noc/inspect/{node_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("monitoring:read"))],
)
def noc_inspect(request: Request, node_id: uuid.UUID, db: Session = Depends(get_db)):
    context = {"request": request}
    data = web_network_noc_inspector_service.noc_inspector_data(db, node_id)
    context.update(data)
    return templates.TemplateResponse("admin/network/noc/_inspector.html", context)
