"""Admin dashboard web routes."""

from fastapi import APIRouter, Depends, Form, Header, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.workforce_attendance import DashboardAttendanceLocation
from app.services import web_admin_attendance as web_admin_attendance_service
from app.services import web_admin_dashboard as web_admin_dashboard_service
from app.services import worker_control as worker_control_service
from app.services.audit_helpers import log_audit_event
from app.services.auth_dependencies import require_any_permission, require_permission
from app.services.workforce_attendance import AttendanceAction

router = APIRouter(tags=["web-admin-dashboard"])

_DASHBOARD_READ_DEPENDENCY = Depends(
    require_any_permission("billing:invoice:read", "monitoring:read", "customer:read")
)


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    # The dashboard is the default staff landing page. Allow any staff with a
    # granular read permission to see the overview.
    dependencies=[_DASHBOARD_READ_DEPENDENCY],
)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """Admin dashboard overview page."""
    return web_admin_dashboard_service.dashboard(request, db)


@router.get(
    "/dashboard/stats",
    response_class=HTMLResponse,
    dependencies=[_DASHBOARD_READ_DEPENDENCY],
)
def dashboard_stats_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for dashboard stats cards."""
    return web_admin_dashboard_service.dashboard_stats_partial(request, db)


@router.get(
    "/dashboard/activity",
    response_class=HTMLResponse,
    dependencies=[_DASHBOARD_READ_DEPENDENCY],
)
def dashboard_activity_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for recent activity feed."""
    return web_admin_dashboard_service.dashboard_activity_partial(request, db)


@router.get(
    "/dashboard/attendance",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("attendance:self:use"))],
)
def dashboard_attendance_partial(request: Request, db: Session = Depends(get_db)):
    """Load the current staff member's ERP-owned attendance independently."""
    return web_admin_attendance_service.load(request, db)


@router.post(
    "/dashboard/attendance/check-in",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("attendance:self:use"))],
)
def dashboard_attendance_check_in(
    request: Request,
    payload: DashboardAttendanceLocation,
    idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=200),
    db: Session = Depends(get_db),
):
    return web_admin_attendance_service.punch(
        request,
        db,
        action=AttendanceAction.CHECK_IN,
        payload=payload,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/dashboard/attendance/check-out",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("attendance:self:use"))],
)
def dashboard_attendance_check_out(
    request: Request,
    payload: DashboardAttendanceLocation,
    idempotency_key: str = Header(..., alias="Idempotency-Key", max_length=200),
    db: Session = Depends(get_db),
):
    return web_admin_attendance_service.punch(
        request,
        db,
        action=AttendanceAction.CHECK_OUT,
        payload=payload,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/dashboard/server-health",
    response_class=HTMLResponse,
    # Infrastructure internals are ops-facing: mirror the show_network flag
    # rather than the broad dashboard read dependency.
    dependencies=[
        Depends(
            require_any_permission(
                "network:device:read",
                "network:olt:read",
                "network:ont:read",
                "monitoring:read",
                "reports:network:read",
            )
        )
    ],
)
def dashboard_server_health_partial(request: Request, db: Session = Depends(get_db)):
    """HTMX partial for server health widget."""
    return web_admin_dashboard_service.dashboard_server_health_partial(request, db)


@router.post(
    "/dashboard/workers/restart",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("system:settings:write"))],
)
def dashboard_worker_restart(
    request: Request,
    target: str = Form(...),
    db: Session = Depends(get_db),
):
    """Restart a configured worker service target and refresh the health widget."""
    result = worker_control_service.restart_worker_target(target)
    log_audit_event(
        db=db,
        request=request,
        action="restart_worker",
        entity_type="celery_worker",
        entity_id=result.target,
        actor_id=getattr(request.state, "actor_id", None),
        metadata={
            "target": result.target,
            "ok": result.ok,
            "message": result.message,
            "returncode": result.returncode,
        },
        status_code=200 if result.ok else 400,
        is_success=result.ok,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
    notice = {
        "type": "success" if result.ok else "error",
        "message": result.message,
    }
    return web_admin_dashboard_service.dashboard_server_health_partial(
        request, db, worker_action_notice=notice
    )
