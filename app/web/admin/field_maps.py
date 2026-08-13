"""Admin dispatch field-map routes — live map + movement playback.

UI-only ports of the CRM ``operations/field-live-map`` and
``operations/field-movement-playback`` pages onto sub's native Phase-2 field
tracking data (``field_tech_presence`` / ``field_work_order_movements``). The
JSON feeds live under the same ``/dispatch`` prefix and share the
``operations:dispatch:read`` guard, matching the dispatch work-orders page.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.field import (
    FieldLiveMapFeed,
    FieldLiveMapFeedQuery,
    FieldLiveMapSearchQuery,
    FieldLiveMapSearchResponse,
    FieldMovementPlaybackFeed,
    FieldMovementPlaybackQuery,
)
from app.services import field_maps as field_maps_service
from app.services.auth_dependencies import can, require_permission

templates = Jinja2Templates(directory="templates")
router = APIRouter(prefix="/dispatch", tags=["web-admin-dispatch-maps"])


def _ctx(request: Request, db: Session, active_page: str) -> dict:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "active_page": active_page,
        "active_menu": "operations",
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
    }


@router.get(
    "/live-map",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch:read"))],
)
def field_live_map(request: Request, db: Session = Depends(get_db)):
    context = _ctx(request, db, "field-live-map")
    context["can_read_network_map"] = can(request, "network:map:read")
    return templates.TemplateResponse("admin/dispatch/live_map.html", context)


@router.get(
    "/live-map/feed",
    response_model=FieldLiveMapFeed,
    dependencies=[Depends(require_permission("operations:dispatch:read"))],
)
def field_live_map_feed(
    stale_after_seconds: int = Query(120, ge=15, le=3600),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
) -> FieldLiveMapFeed:
    return field_maps_service.list_technician_positions(
        db=db,
        query=FieldLiveMapFeedQuery(
            stale_after_seconds=stale_after_seconds,
            limit=limit,
        ),
    )


@router.get(
    "/live-map/search",
    response_model=FieldLiveMapSearchResponse,
    dependencies=[Depends(require_permission("operations:dispatch:read"))],
)
def field_live_map_search(
    q: str = Query(min_length=1, max_length=160),
    limit: int = Query(20, ge=1, le=50),
    db: Session = Depends(get_db),
) -> FieldLiveMapSearchResponse:
    return field_maps_service.search_live_map(
        db=db,
        search=FieldLiveMapSearchQuery(query=q, limit=limit),
    )


@router.get(
    "/movement-playback",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("operations:dispatch:read"))],
)
def field_movement_playback(
    request: Request,
    work_order: str | None = Query(default=None, min_length=1, max_length=64),
    technician_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    context = _ctx(request, db, "field-movement-playback")
    context.update(
        {
            "work_orders": field_maps_service.list_movement_work_orders(db=db),
            "selected_work_order": work_order,
            "selected_technician_id": (
                str(technician_id) if technician_id is not None else None
            ),
        }
    )
    return templates.TemplateResponse("admin/dispatch/movement_playback.html", context)


@router.get(
    "/movement-playback/feed",
    response_model=FieldMovementPlaybackFeed,
    dependencies=[Depends(require_permission("operations:dispatch:read"))],
)
def field_movement_playback_feed(
    work_order: str | None = Query(default=None, min_length=1, max_length=64),
    technician_id: UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(1000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> FieldMovementPlaybackFeed:
    return field_maps_service.list_movement_points(
        db=db,
        filters=FieldMovementPlaybackQuery(
            work_order_public_id=work_order,
            technician_id=technician_id,
            since=since,
            until=until,
            limit=limit,
        ),
    )
