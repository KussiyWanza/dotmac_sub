"""Admin network fiber plant web routes."""

from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import fiber_cost_items as fiber_cost_items_service
from app.services import fiber_topology as fiber_topology_service
from app.services import web_network_fdh as web_network_fdh_service
from app.services import web_network_fiber as web_network_fiber_service
from app.services import (
    web_network_fiber_field_verification as fiber_field_verification_web_service,
)
from app.services import web_network_fiber_plant as web_network_fiber_plant_service
from app.services import (
    web_network_fiber_plant_actions as web_network_fiber_plant_actions_service,
)
from app.services import (
    web_network_fiber_plant_ledger as web_network_fiber_plant_ledger_service,
)
from app.services import (
    web_network_ont_identity_reviews as ont_identity_review_service,
)
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_permission
from app.services.network.as_built_plant_projection import (
    AsBuiltPlantProjectionError,
    activate_projected_segment,
)
from app.services.network.fiber_topology_connectivity_coverage import (
    reconcile_fiber_connectivity_coverage,
)
from app.services.network.fiber_topology_connectivity_review import (
    FiberTopologyConnectivityReviewError,
    inspect_connectivity_batch,
)
from app.services.network.fiber_topology_field_map import (
    project_fiber_field_verification_map,
)
from app.services.network.fiber_topology_identity_coverage import (
    reconcile_fiber_identity_coverage,
)
from app.services.network.ont_assignment_constraint_authorization import (
    inspect_ont_assignment_constraint_authorizations,
)
from app.services.network.ont_assignment_cutover_coverage import (
    reconcile_ont_assignment_cutover_coverage,
)
from app.services.network.ont_assignment_identity import (
    OntAssignmentIdentityError,
    approve_assignment_identity_repair,
    decline_assignment_identity_repair,
    execute_assignment_identity_repair,
)
from app.web.request_parsing import parse_form_data_sync, parse_json_body_sync

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/network", tags=["web-admin-network"])


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


# Transport-neutral refusals from the plant-projection owner, mapped here
# because status codes are the adapter's vocabulary, not the service's.
_ACTIVATION_STATUS = {"not_found": 404, "conflict": 409, "invalid": 400}


def _activation_actor(request: Request) -> str | None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request) or {}
    actor_id = str(current_user.get("principal_id") or current_user.get("id") or "")
    return actor_id or None


def _identity_actor(request: Request) -> str:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    principal_type = str(current_user.get("principal_type") or "user").strip()
    principal_id = str(
        current_user.get("principal_id") or current_user.get("id") or ""
    ).strip()
    if not principal_id:
        raise OntAssignmentIdentityError("authenticated actor identity is required")
    return f"{principal_type}:{principal_id}"


def _identity_audit(
    request: Request,
    db: Session,
    *,
    action: str,
    decision_id: object,
    metadata: dict[str, object] | None = None,
) -> None:
    from app.web.admin import get_current_user

    current_user = get_current_user(request)
    log_audit_event(
        db=db,
        request=request,
        action=action,
        entity_type="ont_assignment_identity_decision",
        entity_id=str(decision_id),
        actor_id=str(current_user.get("principal_id") or "") or None,
        metadata=metadata,
    )


def _identity_proposal_response(
    request: Request,
    db: Session,
    *,
    values: dict[str, str],
    preview=None,
    error: str | None = None,
    status_code: int = 200,
):
    context = _base_context(
        request, db, active_page="ont-identity-reviews", active_menu="fiber"
    )
    primary_id = values.get("primary_assignment_id", "")
    context.update(
        {
            "candidate": next(
                iter(
                    ont_identity_review_service.list_assignment_identity_candidates(
                        db, query=primary_id, limit=25
                    )
                ),
                None,
            )
            if primary_id
            else None,
            "error": error,
            "preview": preview,
            "values": values,
        }
    )
    return templates.TemplateResponse(
        "admin/network/fiber/ont_identity_proposal.html",
        context,
        status_code=status_code,
    )


@router.get(
    "/fiber-plant",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_plant_consolidated(
    request: Request,
    asset_type: str = Query(default="fdh", alias="type"),
    db: Session = Depends(get_db),
):
    """Consolidated fiber-plant ledger, projected from the fiber SOT owners."""
    context = _base_context(request, db, active_page="fiber-plant", active_menu="fiber")
    context.update(
        web_network_fiber_plant_ledger_service.fiber_plant_ledger_data(
            db, asset_type=asset_type
        )
    )
    return templates.TemplateResponse("admin/network/fiber-plant/index.html", context)


def _as_built_activation_response(
    request: Request,
    db: Session,
    *,
    error: str | None = None,
    error_as_built_id: str | None = None,
    status_code: int = 200,
):
    context = _base_context(
        request, db, active_page="fiber-as-built-activation", active_menu="fiber"
    )
    context.update(
        web_network_fiber_plant_service.as_built_activation_page_data(
            db, error=error, error_as_built_id=error_as_built_id
        )
    )
    return templates.TemplateResponse(
        "admin/network/fiber/as_built_activation.html",
        context,
        status_code=status_code,
    )


@router.get(
    "/fiber-as-built-activation",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_as_built_activation(request: Request, db: Session = Depends(get_db)):
    """Accepted vendor as-builts whose projected cable is still off the map."""
    return _as_built_activation_response(request, db)


# Activation is plant authority, not accounts-payable review: the operator is
# choosing which two terminations the cable landed on, so it is gated by the
# same network:fiber:write this router uses for every other plant mutation
# rather than the inventory/finance permissions the vendor-operations router
# carries.
@router.post(
    "/fiber-as-built-activation/{as_built_id}/activate",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def fiber_as_built_activation_activate(
    request: Request, as_built_id: str, db: Session = Depends(get_db)
):
    form = parse_form_data_sync(request)
    raw_fiber_count = str(form.get("fiber_count") or "").strip()
    if raw_fiber_count and not raw_fiber_count.isdigit():
        return _as_built_activation_response(
            request,
            db,
            error="invalid_fiber_count: Fiber count must be a whole number.",
            error_as_built_id=as_built_id,
            status_code=400,
        )
    try:
        activate_projected_segment(
            db,
            as_built_id=as_built_id,
            from_point_id=str(form.get("from_point_id") or "").strip(),
            to_point_id=str(form.get("to_point_id") or "").strip(),
            fiber_count=int(raw_fiber_count) if raw_fiber_count else None,
            actor_id=_activation_actor(request),
        )
    except AsBuiltPlantProjectionError as exc:
        return _as_built_activation_response(
            request,
            db,
            error=f"{exc.code}: {exc}",
            error_as_built_id=as_built_id,
            status_code=_ACTIVATION_STATUS.get(exc.kind, 400),
        )
    return RedirectResponse(
        url="/admin/network/fiber-as-built-activation", status_code=303
    )


@router.get(
    "/fiber-map",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_plant_map(request: Request, db: Session = Depends(get_db)):
    """Interactive fiber plant map."""
    page_data = web_network_fiber_service.get_fiber_plant_map_data(db)
    context = _base_context(request, db, active_page="fiber-map", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/map.html", context)


@router.get(
    "/fiber-connectivity-batches/{batch_id}",
    response_class=JSONResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_connectivity_batch_evidence(batch_id: str, db: Session = Depends(get_db)):
    """Read-only projection of an exact connectivity batch and its evidence."""

    try:
        return JSONResponse(inspect_connectivity_batch(db, batch_id))
    except FiberTopologyConnectivityReviewError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)


@router.get(
    "/fiber-connectivity-coverage",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_connectivity_coverage(request: Request, db: Session = Depends(get_db)):
    """Show exhaustive read-only staged-path connectivity coverage evidence."""

    coverage = reconcile_fiber_connectivity_coverage(db)
    context = _base_context(
        request,
        db,
        active_page="fiber-connectivity-coverage",
        active_menu="fiber",
    )
    context["coverage"] = coverage
    return templates.TemplateResponse(
        "admin/network/fiber/connectivity_coverage.html", context
    )


@router.get(
    "/fiber-identity-coverage",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_identity_coverage(request: Request, db: Session = Depends(get_db)):
    """Show exhaustive read-only staged point-identity coverage evidence."""

    coverage = reconcile_fiber_identity_coverage(db)
    context = _base_context(
        request,
        db,
        active_page="fiber-identity-coverage",
        active_menu="fiber",
    )
    context["coverage"] = coverage
    return templates.TemplateResponse(
        "admin/network/fiber/identity_coverage.html", context
    )


@router.get(
    "/fiber-field-verification",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_field_verification_worklist(
    request: Request,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=25),
    db: Session = Depends(get_db),
):
    """Show the complete read-only staged-source field-evidence worklist."""

    page_query = (
        fiber_field_verification_web_service.build_fiber_field_worklist_page_query(
            page=page,
            per_page=per_page,
        )
    )
    page_data = fiber_field_verification_web_service.get_fiber_field_worklist_page(
        db=db,
        query=page_query,
    )
    context = _base_context(
        request,
        db,
        active_page="fiber-field-verification",
        active_menu="fiber",
    )
    context.update(
        {
            "list_query": page_data.list_query,
            "page_meta": page_data.page_meta,
            "worklist": page_data.worklist,
            "worklist_rows": page_data.rows,
        }
    )
    return templates.TemplateResponse(
        "admin/network/fiber/field_verification_worklist.html", context
    )


@router.get(
    "/fiber-field-verification-map",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_field_verification_map(request: Request, db: Session = Depends(get_db)):
    """Show the complete worklist over exact staged source GeoJSON."""

    field_map = project_fiber_field_verification_map(db)
    context = _base_context(
        request,
        db,
        active_page="fiber-field-verification-map",
        active_menu="fiber",
    )
    context["field_map"] = field_map
    return templates.TemplateResponse(
        "admin/network/fiber/field_verification_map.html", context
    )


@router.get(
    "/fiber-trace",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_subscription_trace(
    request: Request,
    subscription_id: str | None = None,
    q: str | None = Query(default=None, max_length=160),
    db: Session = Depends(get_db),
):
    """Read-only validated fiber path and bounded fault-candidate view."""
    context = _base_context(request, db, active_page="fiber-trace", active_menu="fiber")
    context["query"] = (q or "").strip()
    context["search_results"] = fiber_topology_service.search_fiber_trace_subscriptions(
        db, context["query"]
    )
    context["selected_subscription_id"] = subscription_id
    context["localization"] = None
    context["candidate_asset_ids"] = set()
    context["trace_error"] = None
    if subscription_id:
        try:
            localization = fiber_topology_service.localize_fiber_fault(
                db, subscription_id
            )
            context["localization"] = localization
            context["candidate_asset_ids"] = {
                asset_id
                for candidate in localization.candidates
                for asset_id in candidate.asset_ids
            }
        except ValueError as exc:
            context["trace_error"] = str(exc)
    return templates.TemplateResponse("admin/network/fiber/trace.html", context)


@router.get(
    "/ont-assignment-constraint-authorizations",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def ont_assignment_constraint_authorizations(
    request: Request,
    target_environment: str | None = Query(default=None, max_length=255),
    db: Session = Depends(get_db),
):
    """Show current applicability of immutable cutover authorization evidence."""

    evidence = inspect_ont_assignment_constraint_authorizations(
        db, target_environment=target_environment
    )
    context = _base_context(
        request,
        db,
        active_page="ont-assignment-constraint-authorizations",
        active_menu="fiber",
    )
    context["authorization_evidence"] = evidence
    return templates.TemplateResponse(
        "admin/network/fiber/ont_assignment_constraint_authorizations.html",
        context,
    )


@router.get(
    "/ont-assignment-cutover-coverage",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def ont_assignment_cutover_coverage(
    request: Request,
    db: Session = Depends(get_db),
):
    """Show read-only current finding coverage and verification drift."""

    coverage = reconcile_ont_assignment_cutover_coverage(db)
    context = _base_context(
        request,
        db,
        active_page="ont-assignment-cutover-coverage",
        active_menu="fiber",
    )
    context["coverage"] = coverage
    return templates.TemplateResponse(
        "admin/network/fiber/ont_assignment_cutover_coverage.html", context
    )


@router.get(
    "/ont-identity-reviews",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def ont_identity_reviews(
    request: Request,
    status_filter: str | None = Query(default="active", alias="status"),
    q: str | None = Query(default=None, max_length=160),
    db: Session = Depends(get_db),
):
    """Show detected disagreements separately from reviewed repair decisions."""
    context = _base_context(
        request, db, active_page="ont-identity-reviews", active_menu="fiber"
    )
    context.update(
        ont_identity_review_service.decisions_page_data(
            db, status=status_filter, query=q
        )
    )
    return templates.TemplateResponse(
        "admin/network/fiber/ont_identity_reviews.html", context
    )


@router.get(
    "/ont-identity-reviews/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def ont_identity_review_new(
    request: Request,
    primary_assignment_id: str | None = None,
    db: Session = Depends(get_db),
):
    return _identity_proposal_response(
        request,
        db,
        values={
            "action": "canonicalize",
            "primary_assignment_id": str(primary_assignment_id or "").strip(),
            "target_subscription_id": "",
            "target_pon_port_id": "",
            "reason": "",
        },
    )


@router.post(
    "/ont-identity-reviews/preview",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def ont_identity_review_preview(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = {
        key: str(form.get(key) or "").strip()
        for key in (
            "action",
            "primary_assignment_id",
            "target_subscription_id",
            "target_pon_port_id",
            "reason",
        )
    }
    try:
        preview = ont_identity_review_service.preview_from_explicit_form(
            db,
            action=values["action"],
            primary_assignment_id=values["primary_assignment_id"],
            target_subscription_id=values["target_subscription_id"] or None,
            target_pon_port_id=values["target_pon_port_id"] or None,
        )
    except OntAssignmentIdentityError as exc:
        return _identity_proposal_response(
            request,
            db,
            values=values,
            error=str(exc),
            status_code=400,
        )
    return _identity_proposal_response(request, db, values=values, preview=preview)


@router.post(
    "/ont-identity-reviews/propose",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def ont_identity_review_propose(request: Request, db: Session = Depends(get_db)):
    form = parse_form_data_sync(request)
    values = {
        key: str(form.get(key) or "").strip()
        for key in (
            "action",
            "primary_assignment_id",
            "target_subscription_id",
            "target_pon_port_id",
            "reason",
        )
    }
    expected_input_sha256 = str(form.get("expected_input_sha256") or "").strip()
    try:
        decision = ont_identity_review_service.propose_from_explicit_preview(
            db,
            action=values["action"],
            primary_assignment_id=values["primary_assignment_id"],
            target_subscription_id=values["target_subscription_id"] or None,
            target_pon_port_id=values["target_pon_port_id"] or None,
            expected_input_sha256=expected_input_sha256,
            proposed_by=_identity_actor(request),
            reason=values["reason"],
        )
    except OntAssignmentIdentityError as exc:
        try:
            preview = ont_identity_review_service.preview_from_explicit_form(
                db,
                action=values["action"],
                primary_assignment_id=values["primary_assignment_id"],
                target_subscription_id=values["target_subscription_id"] or None,
                target_pon_port_id=values["target_pon_port_id"] or None,
            )
        except OntAssignmentIdentityError:
            preview = None
        return _identity_proposal_response(
            request,
            db,
            values=values,
            preview=preview,
            error=str(exc),
            status_code=400,
        )
    _identity_audit(
        request,
        db,
        action="propose",
        decision_id=decision.id,
        metadata={"input_sha256": decision.input_sha256},
    )
    return RedirectResponse(
        f"/admin/network/ont-identity-reviews/{decision.id}", status_code=303
    )


@router.get(
    "/ont-identity-reviews/{decision_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def ont_identity_review_detail(
    request: Request, decision_id: str, db: Session = Depends(get_db)
):
    try:
        page_data = ont_identity_review_service.decision_detail_page_data(
            db, decision_id
        )
    except OntAssignmentIdentityError as exc:
        return HTMLResponse(str(exc), status_code=404)
    context = _base_context(
        request, db, active_page="ont-identity-reviews", active_menu="fiber"
    )
    context.update(page_data)
    context["error"] = request.query_params.get("error")
    return templates.TemplateResponse(
        "admin/network/fiber/ont_identity_review_detail.html", context
    )


def _identity_transition_redirect(decision_id: str, error: str | None = None):
    target = f"/admin/network/ont-identity-reviews/{decision_id}"
    if error:
        target = f"{target}?error={quote(error, safe='')}"
    return RedirectResponse(target, status_code=303)


@router.post(
    "/ont-identity-reviews/{decision_id}/approve",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def ont_identity_review_approve(
    request: Request, decision_id: str, db: Session = Depends(get_db)
):
    notes = str(parse_form_data_sync(request).get("review_notes") or "").strip()
    try:
        decision = approve_assignment_identity_repair(
            db,
            decision_id,
            reviewed_by=_identity_actor(request),
            review_notes=notes,
        )
    except OntAssignmentIdentityError as exc:
        return _identity_transition_redirect(decision_id, str(exc))
    _identity_audit(
        request,
        db,
        action="approve",
        decision_id=decision.id,
        metadata={"review_notes": notes},
    )
    return _identity_transition_redirect(decision_id)


@router.post(
    "/ont-identity-reviews/{decision_id}/decline",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def ont_identity_review_decline(
    request: Request, decision_id: str, db: Session = Depends(get_db)
):
    notes = str(parse_form_data_sync(request).get("review_notes") or "").strip()
    try:
        decision = decline_assignment_identity_repair(
            db,
            decision_id,
            reviewed_by=_identity_actor(request),
            review_notes=notes,
        )
    except OntAssignmentIdentityError as exc:
        return _identity_transition_redirect(decision_id, str(exc))
    _identity_audit(
        request,
        db,
        action="decline",
        decision_id=decision.id,
        metadata={"review_notes": notes},
    )
    return _identity_transition_redirect(decision_id)


@router.post(
    "/ont-identity-reviews/{decision_id}/execute",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def ont_identity_review_execute(
    request: Request, decision_id: str, db: Session = Depends(get_db)
):
    try:
        decision = execute_assignment_identity_repair(
            db, decision_id, executed_by=_identity_actor(request)
        )
    except OntAssignmentIdentityError as exc:
        return _identity_transition_redirect(decision_id, str(exc))
    _identity_audit(
        request,
        db,
        action="execute",
        decision_id=decision.id,
        metadata={
            "outcome": (
                decision.result_payload.get("outcome")
                if decision.result_payload
                else decision.status
            ),
            "result_sha256": decision.result_sha256,
        },
    )
    return _identity_transition_redirect(decision_id)


@router.get(
    "/fiber-change-requests",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_change_requests(request: Request, db: Session = Depends(get_db)):
    """Review pending vendor fiber change requests."""
    page_data = web_network_fiber_plant_service.change_requests_page_data(
        db,
        bulk_status=request.query_params.get("bulk"),
        skipped=request.query_params.get("skipped"),
    )
    context = _base_context(
        request, db, active_page="fiber-change-requests", active_menu="fiber"
    )
    context.update(page_data)
    return templates.TemplateResponse(
        "admin/network/fiber/change_requests.html", context
    )


@router.get(
    "/fiber-change-requests/{request_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_change_request_detail(
    request: Request, request_id: str, db: Session = Depends(get_db)
):
    """Review a specific fiber change request."""
    page_data = web_network_fiber_plant_service.change_request_detail_page_data(
        db,
        request_id=request_id,
        error=request.query_params.get("error"),
    )
    context = _base_context(
        request, db, active_page="fiber-change-requests", active_menu="fiber"
    )
    context.update(page_data)
    return templates.TemplateResponse(
        "admin/network/fiber/change_request_detail.html", context
    )


@router.post(
    "/fiber-change-requests/{request_id}/approve",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def fiber_change_request_approve(
    request: Request, request_id: str, db: Session = Depends(get_db)
):
    redirect_url = (
        web_network_fiber_plant_actions_service.approve_change_request_from_form(
            request,
            db,
            request_id=request_id,
            form=parse_form_data_sync(request),
        )
    )
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post(
    "/fiber-change-requests/{request_id}/reject",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def fiber_change_request_reject(
    request: Request, request_id: str, db: Session = Depends(get_db)
):
    redirect_url = (
        web_network_fiber_plant_actions_service.reject_change_request_from_form(
            request,
            db,
            request_id=request_id,
            form=parse_form_data_sync(request),
        )
    )
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post(
    "/fiber-change-requests/bulk-approve",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def fiber_change_requests_bulk_approve(request: Request, db: Session = Depends(get_db)):
    redirect_url = (
        web_network_fiber_plant_actions_service.bulk_approve_change_requests_from_form(
            request, db, parse_form_data_sync(request)
        )
    )
    return RedirectResponse(
        url=redirect_url,
        status_code=303,
    )


@router.post(
    "/fiber-map/update-position",
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def update_asset_position(request: Request, db: Session = Depends(get_db)):
    """Update position of FDH cabinet or splice closure via drag-and-drop."""
    data: dict[str, object] = parse_json_body_sync(request)
    payload, status_code = web_network_fiber_plant_service.update_asset_position_data(
        db, data
    )
    return JSONResponse(payload, status_code=status_code)


@router.get(
    "/fiber-map/nearest-cabinet",
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def find_nearest_cabinet(
    request: Request, lat: float, lng: float, db: Session = Depends(get_db)
):
    """Find nearest FDH cabinet to given coordinates for installation planning."""
    payload, status_code = web_network_fiber_service.find_nearest_cabinet_data(
        db,
        lat=lat,
        lng=lng,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get(
    "/fiber-map/cost-estimate",
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_cost_estimate(
    request: Request,
    distance_m: Decimal = Query(ge=0, le=1_000_000, allow_inf_nan=False),
    db: Session = Depends(get_db),
):
    """Price one drop of `distance_m`.

    The auto-route response already carries its estimate, because it is already
    a server round trip. A MANUAL trace computes its distance in the browser and
    would otherwise have to price it there too — a second copy of the cost rule,
    in the layer that had it before this change and got it wrong. One request
    when a trace completes is cheaper than that.
    """

    estimate = fiber_cost_items_service.estimate_for_distance(db, distance_m)
    return JSONResponse(web_network_fiber_service.serialize_cost_estimate(estimate))


@router.get(
    "/fiber-map/plan-options",
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def plan_options(
    request: Request, lat: float, lng: float, db: Session = Depends(get_db)
):
    """List nearby cabinets for planning and manual routing."""
    payload, status_code = web_network_fiber_service.get_plan_options_data(
        db,
        lat=lat,
        lng=lng,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get(
    "/fiber-map/route", dependencies=[Depends(require_permission("network:fiber:read"))]
)
def plan_route(
    request: Request,
    lat: float,
    lng: float,
    cabinet_id: str,
    db: Session = Depends(get_db),
):
    """Calculate a fiber route between a point and a cabinet."""
    payload, status_code = web_network_fiber_service.get_plan_route_data(
        db,
        lat=lat,
        lng=lng,
        cabinet_id=cabinet_id,
    )
    return JSONResponse(payload, status_code=status_code)


@router.get(
    "/fiber-reports",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fiber_reports(
    request: Request, db: Session = Depends(get_db), map_limit: int | None = None
):
    """Fiber network deployment reports with asset statistics and customer map."""
    page_data = web_network_fiber_service.get_fiber_reports_data(db, map_limit)
    context = _base_context(
        request, db, active_page="fiber-reports", active_menu="fiber"
    )
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/reports.html", context)


@router.get(
    "/fdh-cabinets",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fdh_cabinets_list(request: Request, db: Session = Depends(get_db)):
    """List FDH cabinets."""
    page_data = web_network_fdh_service.list_page_data(db)
    context = _base_context(
        request, db, active_page="fdh-cabinets", active_menu="fiber"
    )
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/fdh-cabinets.html", context)


@router.get(
    "/fdh-cabinets/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fdh_cabinet_new(request: Request, db: Session = Depends(get_db)):
    form_context = web_network_fdh_service.build_form_context(
        db,
        cabinet=None,
        action_url="/admin/network/fdh-cabinets",
    )

    context = _base_context(
        request, db, active_page="fdh-cabinets", active_menu="fiber"
    )
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/network/fiber/fdh-cabinet-form.html", context
    )


@router.post(
    "/fdh-cabinets",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def fdh_cabinet_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_fiber_plant_actions_service.create_cabinet_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if not result.success:
        context = _base_context(
            request, db, active_page="fdh-cabinets", active_menu="fiber"
        )
        context.update(result.form_context or {})
        return templates.TemplateResponse(
            "admin/network/fiber/fdh-cabinet-form.html",
            context,
            status_code=result.status_code,
        )

    return RedirectResponse(
        result.redirect_url or "/admin/network/fdh-cabinets", status_code=303
    )


@router.get(
    "/fdh-cabinets/{cabinet_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fdh_cabinet_edit(request: Request, cabinet_id: str, db: Session = Depends(get_db)):
    cabinet = web_network_fdh_service.get_cabinet(db, cabinet_id)
    if not cabinet:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    form_context = web_network_fdh_service.build_form_context(
        db,
        cabinet=cabinet,
        action_url=f"/admin/network/fdh-cabinets/{cabinet.id}",
    )
    context = _base_context(
        request, db, active_page="fdh-cabinets", active_menu="fiber"
    )
    context.update(form_context)
    return templates.TemplateResponse(
        "admin/network/fiber/fdh-cabinet-form.html", context
    )


@router.post(
    "/fdh-cabinets/{cabinet_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def fdh_cabinet_update(
    request: Request, cabinet_id: str, db: Session = Depends(get_db)
):
    result = web_network_fiber_plant_actions_service.update_cabinet_from_form(
        request,
        db,
        cabinet_id=cabinet_id,
        form=parse_form_data_sync(request),
    )
    if result.not_found_message:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": result.not_found_message},
            status_code=404,
        )
    if not result.success:
        context = _base_context(
            request, db, active_page="fdh-cabinets", active_menu="fiber"
        )
        context.update(result.form_context or {})
        return templates.TemplateResponse(
            "admin/network/fiber/fdh-cabinet-form.html", context
        )

    return RedirectResponse(
        result.redirect_url or "/admin/network/fdh-cabinets", status_code=303
    )


@router.get(
    "/fdh-cabinets/{cabinet_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def fdh_cabinet_detail(
    request: Request, cabinet_id: str, db: Session = Depends(get_db)
):
    page_data = web_network_fdh_service.detail_page_data(db, cabinet_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "FDH Cabinet not found"},
            status_code=404,
        )

    context = _base_context(
        request, db, active_page="fdh-cabinets", active_menu="fiber"
    )
    context.update(page_data)
    context["activities"] = web_network_fiber_plant_actions_service.activity_for_entity(
        db, "fdh_cabinet", str(cabinet_id), limit=10
    )
    return templates.TemplateResponse(
        "admin/network/fiber/fdh-cabinet-detail.html", context
    )


@router.get(
    "/splitters",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def splitters_list(request: Request, db: Session = Depends(get_db)):
    page_data = web_network_fdh_service.list_splitters_page_data(db)
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(page_data)
    return templates.TemplateResponse("admin/network/fiber/splitters.html", context)


@router.get(
    "/splitters/new",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def splitter_new(
    request: Request, fdh_id: str | None = None, db: Session = Depends(get_db)
):
    form_context = web_network_fdh_service.build_splitter_form_context(
        db,
        splitter=None,
        action_url="/admin/network/splitters",
        selected_fdh_id=fdh_id,
    )
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)


@router.post(
    "/splitters",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def splitter_create(request: Request, db: Session = Depends(get_db)):
    result = web_network_fiber_plant_actions_service.create_splitter_from_form(
        request,
        db,
        parse_form_data_sync(request),
    )
    if not result.success:
        context = _base_context(
            request, db, active_page="splitters", active_menu="fiber"
        )
        context.update(result.form_context or {})
        return templates.TemplateResponse(
            "admin/network/fiber/splitter-form.html",
            context,
            status_code=result.status_code,
        )

    return RedirectResponse(
        result.redirect_url or "/admin/network/splitters", status_code=303
    )


@router.get(
    "/splitters/{splitter_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def splitter_edit(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    splitter = web_network_fdh_service.get_splitter(db, splitter_id)
    if not splitter:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    form_context = web_network_fdh_service.build_splitter_form_context(
        db,
        splitter=splitter,
        action_url=f"/admin/network/splitters/{splitter.id}",
        selected_fdh_id=str(splitter.fdh_id) if splitter.fdh_id else None,
    )
    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(form_context)
    return templates.TemplateResponse("admin/network/fiber/splitter-form.html", context)


@router.post(
    "/splitters/{splitter_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:write"))],
)
def splitter_update(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    result = web_network_fiber_plant_actions_service.update_splitter_from_form(
        request,
        db,
        splitter_id=splitter_id,
        form=parse_form_data_sync(request),
    )
    if result.not_found_message:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": result.not_found_message},
            status_code=404,
        )
    if not result.success:
        context = _base_context(
            request, db, active_page="splitters", active_menu="fiber"
        )
        context.update(result.form_context or {})
        return templates.TemplateResponse(
            "admin/network/fiber/splitter-form.html", context
        )

    return RedirectResponse(
        result.redirect_url or "/admin/network/splitters", status_code=303
    )


@router.get(
    "/splitters/{splitter_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("network:fiber:read"))],
)
def splitter_detail(request: Request, splitter_id: str, db: Session = Depends(get_db)):
    page_data = web_network_fdh_service.splitter_detail_page_data(db, splitter_id)
    if not page_data:
        return templates.TemplateResponse(
            "admin/errors/404.html",
            {"request": request, "message": "Splitter not found"},
            status_code=404,
        )

    context = _base_context(request, db, active_page="splitters", active_menu="fiber")
    context.update(page_data)
    context["activities"] = web_network_fiber_plant_actions_service.activity_for_entity(
        db, "splitter", str(splitter_id), limit=10
    )
    return templates.TemplateResponse(
        "admin/network/fiber/splitter-detail.html", context
    )
