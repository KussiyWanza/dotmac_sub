"""Admin network management base web routes."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.audit import AuditActorType
from app.schemas.network_map_asset_changes import (
    NetworkAssetCoordinates,
    NetworkAssetDraft,
    NetworkAssetProposalReviewRequest,
    NetworkAssetProposalSubmitRequest,
    NetworkAssetReviewDecision,
    ReviewNetworkAssetProposalCommand,
    SubmitNetworkAssetProposalCommand,
)
from app.services import network_map_asset_changes
from app.services import web_network_core_devices as web_network_core_devices_service
from app.services.auth_dependencies import has_permission, require_permission
from app.services.db_session_adapter import db_session_adapter
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


def _network_map_actor(
    auth: dict[str, object],
) -> tuple[UUID, AuditActorType, str] | None:
    raw_id = str(auth.get("principal_id") or "").strip()
    try:
        actor_id = UUID(raw_id)
    except ValueError:
        return None
    actor_type = (
        AuditActorType.api_key
        if auth.get("principal_type") == "api_key"
        else AuditActorType.user
    )
    label = str(
        auth.get("display_name")
        or auth.get("name")
        or auth.get("email")
        or f"{actor_type.value}:{actor_id}"
    ).strip()
    return actor_id, actor_type, label[:160]


def _proposal_error_response(error: DomainError) -> JSONResponse:
    suffix = error.code.rsplit(".", 1)[-1]
    if suffix == "proposal_not_found":
        status_code = 404
    elif suffix in {
        "idempotency_conflict",
        "proposal_already_reviewed",
        "proposal_digest_mismatch",
        "stale_asset",
        "topology_review_required",
    }:
        status_code = 409
    elif suffix in {
        "independent_review_required",
        "invalid_actor",
        "invalid_scope",
    }:
        status_code = 403
    else:
        status_code = 422
    return JSONResponse(
        status_code=status_code,
        content={
            "error": error.code,
            "message": error.message,
            "details": error.details,
        },
    )


def _network_map_command_context(
    *,
    actor_id: UUID,
    actor_type: AuditActorType,
    scope: str,
    reason: str,
    idempotency_key: UUID,
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"{actor_type.value}:{actor_id}",
        scope=scope,
        reason=reason.strip(),
        idempotency_key=f"network-map-v2:{scope}:{idempotency_key}",
    )


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
    "",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:hub:read"))],
)
def network_hub(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """Network hub landing page."""
    return templates.TemplateResponse(
        "admin/network/index.html",
        _base_context(request, db, active_page="network"),
    )


def _build_device_query(
    *,
    device_type: str | None,
    type_filter: str | None,
    search: str | None,
    status: str | None,
    vendor: str | None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = 1,
    per_page: int | None = None,
    offset: int | None = None,
    limit: int | None = None,
):
    """Build the validated device ListQuery from loose route params.

    Accepts either page/per_page or the offset/limit that
    components/data/table_pagination.html emits. Falls back to defaults on
    out-of-contract params rather than erroring the page.
    """
    if limit:
        per_page = limit
        page = ((offset or 0) // limit) + 1
    selected_type = type_filter or device_type
    try:
        return web_network_core_devices_service.build_network_device_list_query(
            device_type=selected_type,
            status=status,
            vendor=vendor,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            per_page=per_page,
        )
    except ValueError:
        return web_network_core_devices_service.build_network_device_list_query(
            page=max(page, 1)
        )


@router.get(
    "/devices",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def devices_list(
    request: Request,
    device_type: str | None = None,
    type_filter: str | None = Query(default=None, alias="type"),
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int | None = Query(default=None),
    offset: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """List all network devices (SQL-paginated over the device projection)."""
    list_query = _build_device_query(
        device_type=device_type,
        type_filter=type_filter,
        search=search,
        status=status,
        vendor=vendor,
        sort_by=sort_by,
        sort_dir=sort_dir,
        page=page,
        per_page=per_page,
        offset=offset,
        limit=limit,
    )
    page_data = web_network_core_devices_service.devices_list_page_data(db, list_query)
    context = _base_context(request, db, active_page="devices")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/devices/index.html", context)


@router.get(
    "/devices/search",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def devices_search(
    request: Request,
    search: str = "",
    offset: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    list_query = _build_device_query(
        device_type=None,
        type_filter=None,
        search=search,
        status=None,
        vendor=None,
        offset=offset,
        limit=limit,
    )
    devices = web_network_core_devices_service.devices_search_data(db, list_query)
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.get(
    "/devices/filter",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def devices_filter(
    request: Request,
    device_type: str | None = None,
    type_filter: str | None = Query(default=None, alias="type"),
    search: str | None = None,
    status: str | None = None,
    vendor: str | None = None,
    sort_by: str | None = None,
    sort_dir: str | None = None,
    offset: int | None = Query(default=None),
    limit: int | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    list_query = _build_device_query(
        device_type=device_type,
        type_filter=type_filter,
        search=search,
        status=status,
        vendor=vendor,
        sort_by=sort_by,
        sort_dir=sort_dir,
        offset=offset,
        limit=limit,
    )
    devices = web_network_core_devices_service.devices_filter_data(db, list_query)
    return templates.TemplateResponse(
        "admin/network/devices/_table_rows.html",
        {"request": request, "devices": devices},
    )


@router.get(
    "/devices/create",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def device_create(request: Request, db: Session = Depends(get_db)):
    # Redirect to the more specific device creation pages.
    return RedirectResponse(url="/admin/network/core-devices/new", status_code=302)


@router.get(
    "/devices/{device_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:read"))],
)
def device_detail(
    request: Request, device_id: str, db: Session = Depends(get_db)
) -> Response:
    redirect_url = web_network_core_devices_service.resolve_device_redirect(
        db, device_id
    )
    if redirect_url:
        return RedirectResponse(url=redirect_url, status_code=302)

    return templates.TemplateResponse(
        "admin/errors/404.html",
        {"request": request, "message": "Device not found"},
        status_code=404,
    )


@router.post(
    "/devices/{device_id}/ping",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_ping(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Ping queued for device {device_id}."
        "</div>"
    )


@router.post(
    "/devices/{device_id}/reboot",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_reboot(request: Request, device_id: str, db: Session = Depends(get_db)):
    return HTMLResponse(
        '<div class="rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm dark:border-slate-700 dark:bg-slate-800 dark:text-slate-300">'
        f"Reboot request queued for device {device_id}."
        "</div>"
    )


@router.get(
    "/devices/{device_id}/reboot/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:device:write"))],
)
def device_reboot_preview(
    request: Request, device_id: str, db: Session = Depends(get_db)
):
    """Safe impact-preview step before the existing reboot command adapter."""
    context = _base_context(request, db, active_page="devices")
    context.update({"device_id": device_id, "affected": 1})
    return templates.TemplateResponse(
        "admin/network/devices/reboot_preview.html", context
    )


@router.get(
    "/map",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:map:read"))],
)
def comprehensive_network_map(request: Request, db: Session = Depends(get_db)):
    """Comprehensive network map showing all infrastructure and customers."""
    from app.services import network_map as network_map_service

    context = _base_context(request, db, active_page="network-map")
    projection = network_map_service.build_network_map_projection(db=db)
    context.update(projection.to_template_context())
    return templates.TemplateResponse("admin/network/map.html", context)


@router.get(
    "/map-v2",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:map:read"))],
)
def comprehensive_network_map_v2(
    request: Request,
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(require_permission("network:map:read")),
) -> HTMLResponse:
    """Render the isolated V2 map and its governed proposal projection."""
    from app.services import network_map as network_map_service

    context = _base_context(request, db, active_page="network-map-v2")
    base_projection = network_map_service.build_network_map_projection(db=db)
    v2_projection = network_map_service.build_network_map_v2_projection(
        db=db,
        base_projection=base_projection,
    )
    context.update(base_projection.to_template_context())
    context["network_map_v2"] = v2_projection.to_transport()
    proposals = network_map_asset_changes.list_proposals(db, limit=100)
    context["network_map_v2_governance"] = {
        **proposals.to_transport(),
        "can_propose": has_permission(
            auth,
            db,
            network_map_asset_changes.PROPOSE_PERMISSION,
        ),
        "can_review": has_permission(
            auth,
            db,
            network_map_asset_changes.REVIEW_PERMISSION,
        ),
        "actor_id": str(auth.get("principal_id") or ""),
        "supported_asset_types": [
            "fdh_cabinet",
            "splice_closure",
            "access_point",
            "support_structure",
        ],
        "unavailable_message": (
            "Governed changes are available only for authoritative passive-fibre "
            "point assets currently present in Selfcare. Empty asset layers may "
            "mean canonical data has not yet been migrated."
        ),
    }
    return templates.TemplateResponse("admin/network/map_v2.html", context)


@router.get(
    "/map-v2/proposals",
    response_model=None,
    dependencies=[Depends(require_permission("network:map:read"))],
)
def network_map_v2_proposals(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, object] | JSONResponse:
    """Return bounded proposal evidence for the V2 workbench."""
    from app.schemas.network_map_asset_changes import NetworkAssetProposalStatus

    try:
        parsed_status = NetworkAssetProposalStatus(status) if status else None
    except ValueError:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_status", "message": "Invalid proposal status."},
        )
    return network_map_asset_changes.list_proposals(
        db,
        status=parsed_status,
        limit=limit,
    ).to_transport()


@router.post(
    "/map-v2/proposals",
    response_model=None,
    dependencies=[
        Depends(require_permission(network_map_asset_changes.PROPOSE_PERMISSION))
    ],
)
def network_map_v2_submit_proposal(
    payload: NetworkAssetProposalSubmitRequest,
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(network_map_asset_changes.PROPOSE_PERMISSION)
    ),
) -> dict[str, object] | JSONResponse:
    """Submit a proposal without mutating a canonical network asset."""
    actor = _network_map_actor(auth)
    if actor is None:
        return JSONResponse(
            status_code=403,
            content={"error": "invalid_actor", "message": "A valid actor is required."},
        )
    if (payload.latitude is None) != (payload.longitude is None):
        return JSONResponse(
            status_code=422,
            content={
                "error": "incomplete_coordinates",
                "message": "Latitude and longitude must be supplied together.",
            },
        )
    actor_id, actor_type, actor_label = actor
    coordinates = (
        NetworkAssetCoordinates(
            latitude=payload.latitude,
            longitude=payload.longitude,
        )
        if payload.latitude is not None and payload.longitude is not None
        else None
    )
    command = SubmitNetworkAssetProposalCommand(
        context=_network_map_command_context(
            actor_id=actor_id,
            actor_type=actor_type,
            scope=network_map_asset_changes.PROPOSE_PERMISSION,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        ),
        actor_id=actor_id,
        actor_type=actor_type,
        actor_label=actor_label,
        asset_type=payload.asset_type,
        operation=payload.operation,
        asset_id=payload.asset_id,
        proposed=NetworkAssetDraft(
            name=payload.name,
            code=payload.code,
            coordinates=coordinates,
            notes=payload.notes,
            access_point_type=payload.access_point_type,
            placement=payload.placement,
            street=payload.street,
            city=payload.city,
            support_type=payload.support_type,
        ),
    )
    try:
        db_session_adapter.release_read_transaction(db)
        proposal = network_map_asset_changes.submit_proposal(db, command)
    except DomainError as error:
        return _proposal_error_response(error)
    return proposal.to_transport()


def _network_map_v2_review(
    *,
    proposal_id: UUID,
    decision: NetworkAssetReviewDecision,
    payload: NetworkAssetProposalReviewRequest,
    db: Session,
    auth: dict[str, object],
) -> dict[str, object] | JSONResponse:
    actor = _network_map_actor(auth)
    if actor is None:
        return JSONResponse(
            status_code=403,
            content={"error": "invalid_actor", "message": "A valid actor is required."},
        )
    actor_id, actor_type, actor_label = actor
    command = ReviewNetworkAssetProposalCommand(
        context=_network_map_command_context(
            actor_id=actor_id,
            actor_type=actor_type,
            scope=network_map_asset_changes.REVIEW_PERMISSION,
            reason=payload.review_notes,
            idempotency_key=payload.idempotency_key,
        ),
        actor_id=actor_id,
        actor_type=actor_type,
        actor_label=actor_label,
        proposal_id=proposal_id,
        decision=decision,
        expected_proposal_sha256=payload.expected_proposal_sha256,
        review_notes=payload.review_notes,
    )
    try:
        db_session_adapter.release_read_transaction(db)
        proposal = network_map_asset_changes.review_proposal(db, command)
    except DomainError as error:
        return _proposal_error_response(error)
    return proposal.to_transport()


@router.post(
    "/map-v2/proposals/{proposal_id}/approve",
    response_model=None,
    dependencies=[
        Depends(require_permission(network_map_asset_changes.REVIEW_PERMISSION))
    ],
)
def network_map_v2_approve_proposal(
    proposal_id: UUID,
    payload: NetworkAssetProposalReviewRequest,
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(network_map_asset_changes.REVIEW_PERMISSION)
    ),
) -> dict[str, object] | JSONResponse:
    """Approve a proposal through the canonical fibre asset owner."""
    return _network_map_v2_review(
        proposal_id=proposal_id,
        decision=NetworkAssetReviewDecision.approve,
        payload=payload,
        db=db,
        auth=auth,
    )


@router.post(
    "/map-v2/proposals/{proposal_id}/reject",
    response_model=None,
    dependencies=[
        Depends(require_permission(network_map_asset_changes.REVIEW_PERMISSION))
    ],
)
def network_map_v2_reject_proposal(
    proposal_id: UUID,
    payload: NetworkAssetProposalReviewRequest,
    db: Session = Depends(get_db),
    auth: dict[str, object] = Depends(
        require_permission(network_map_asset_changes.REVIEW_PERMISSION)
    ),
) -> dict[str, object] | JSONResponse:
    """Reject a proposal without changing its canonical target."""
    return _network_map_v2_review(
        proposal_id=proposal_id,
        decision=NetworkAssetReviewDecision.reject,
        payload=payload,
        db=db,
        auth=auth,
    )


@router.get(
    "/map/plant-data",
    dependencies=[Depends(require_permission("network:map:read"))],
)
def network_map_plant_data(db: Session = Depends(get_db)) -> dict[str, object]:
    """Read-only GeoJSON plant subset for the dispatch live map."""
    from app.services import network_map as network_map_service

    return network_map_service.build_network_map_plant_projection(db=db).to_transport()
