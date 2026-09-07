"""
Bandwidth API Router

Provides endpoints for bandwidth time series data, real-time streaming,
and usage statistics. Supports both admin and customer portal access.
"""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user, get_db
from app.db import finish_read_transaction
from app.services.bandwidth import (
    add_directions_to_series,
    bandwidth_samples,
)
from app.services.network.live_bandwidth_observations import (
    LiveBandwidthAccess,
    LiveBandwidthAccessDenied,
    LiveBandwidthConfigurationError,
    LiveBandwidthCoordinationUnavailable,
    LiveBandwidthNotFound,
    LiveBandwidthProbeBusy,
    LiveBandwidthProbeObservation,
    LiveBandwidthReadQuery,
    LiveBandwidthStreamQuery,
    authorize_live_bandwidth_read,
    live_bandwidth_events,
    probe_live_bandwidth,
)

router = APIRouter(prefix="/bandwidth", tags=["bandwidth"])


# Response schemas
class BandwidthSeriesPoint(BaseModel):
    timestamp: datetime
    rx_bps: float
    tx_bps: float
    # Subscriber-perspective rates (rx/tx above are NAS-perspective). Derived
    # via to_subscriber_directions(); clients must bind to these instead of
    # guessing the rx/tx convention. Without them the chart JS (which reads
    # download_bps/upload_bps exclusively) renders a flat-zero series.
    download_bps: float | None = None
    upload_bps: float | None = None


class BandwidthStats(BaseModel):
    current_rx_bps: float
    current_tx_bps: float
    peak_rx_bps: float
    peak_tx_bps: float
    total_rx_bytes: float
    total_tx_bytes: float
    sample_count: int
    # Subscriber-perspective rates (rx/tx above are NAS-perspective). The
    # service computes these via to_subscriber_directions(); clients should
    # bind to them instead of guessing the rx/tx convention.
    download_bps: float | None = None
    upload_bps: float | None = None
    peak_download_bps: float | None = None
    peak_upload_bps: float | None = None


class TopUserEntry(BaseModel):
    subscription_id: str
    total_bps: float
    account_name: str | None = None


class BandwidthSeriesResponse(BaseModel):
    data: list[BandwidthSeriesPoint]
    total: int
    source: str  # "postgres" or "victoriametrics"


# Admin endpoints
@router.get("/series/{subscription_id}", response_model=BandwidthSeriesResponse)
def get_bandwidth_series(
    subscription_id: UUID,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="auto", pattern="^(auto|1s|1m|5m|1h)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get bandwidth time series for a subscription.

    Automatically selects the appropriate data source based on time range:
    - Last 24 hours: PostgreSQL (raw samples)
    - 1-7 days: VictoriaMetrics (1-minute aggregates)
    - 8-30 days: VictoriaMetrics (5-minute aggregates)
    - 31+ days: VictoriaMetrics (1-hour aggregates)
    """
    bandwidth_samples.check_subscription_access(db, subscription_id, current_user)

    result = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_series,
        db,
        subscription_id,
        start_at,
        end_at,
        interval,
    )

    data = [
        BandwidthSeriesPoint(**point)
        for point in add_directions_to_series(result)["data"]
    ]
    return BandwidthSeriesResponse(
        data=data, total=result["total"], source=result["source"]
    )


@router.get("/stats/{subscription_id}", response_model=BandwidthStats)
def get_bandwidth_stats(
    subscription_id: UUID,
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get bandwidth statistics for a subscription.

    Returns current, peak, and total bandwidth for the specified period.
    """
    bandwidth_samples.check_subscription_access(db, subscription_id, current_user)

    stats = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_stats,
        db,
        subscription_id,
        period,
    )
    return BandwidthStats(**stats)


@router.get(
    "/mikrotik-live/{subscription_id}",
    # Preserve the legacy OpenAPI surface while returning a typed, minimized
    # service outcome. A separately reviewed API-contract change can expose the
    # schema later.
    response_model=None,
)
def get_mikrotik_live_bandwidth(
    subscription_id: UUID,
    db: Session = Depends(get_db),
    current_user: dict[str, object] = Depends(get_current_user),
) -> LiveBandwidthProbeObservation:
    """Run one rate-limited, PII-minimized direct RouterOS diagnostic."""
    query = LiveBandwidthReadQuery(
        subscription_id=subscription_id,
        access=LiveBandwidthAccess.from_principal(current_user),
    )
    try:
        return probe_live_bandwidth(db, query)
    except LiveBandwidthNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except LiveBandwidthAccessDenied as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc
    except LiveBandwidthConfigurationError as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc
    except LiveBandwidthProbeBusy as exc:
        raise HTTPException(status_code=429, detail=exc.code) from exc
    except LiveBandwidthCoordinationUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.code) from exc


@router.get("/live/{subscription_id}")
def get_live_bandwidth(
    subscription_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict[str, object] = Depends(get_current_user),
) -> EventSourceResponse:
    """
    Server-Sent Events stream for real-time bandwidth updates.

    Sends bandwidth updates approximately every second.
    """
    query = LiveBandwidthReadQuery(
        subscription_id=subscription_id,
        access=LiveBandwidthAccess.from_principal(current_user),
    )
    try:
        authorize_live_bandwidth_read(db, query)
    except LiveBandwidthNotFound as exc:
        raise HTTPException(status_code=404, detail=exc.code) from exc
    except LiveBandwidthAccessDenied as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc
    # Streaming responses outlive the route function. Release the request-scoped
    # session before the SSE loop starts so a live viewer does not hold a pooled
    # DB connection idle in transaction for the lifetime of the stream.
    finish_read_transaction(db)
    db.close()

    return EventSourceResponse(
        live_bandwidth_events(
            LiveBandwidthStreamQuery(subscription_id=subscription_id),
            is_disconnected=request.is_disconnected,
        ),
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/top-users", response_model=list[TopUserEntry])
def get_top_users(
    limit: int = Query(default=10, ge=1, le=100),
    duration: str = Query(default="1h", pattern="^(1h|24h|7d)$"),
    db: Session = Depends(get_db),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> list[TopUserEntry]:
    """
    Get top bandwidth consumers.

    Returns the top N subscriptions by bandwidth usage.
    Admin only.
    """
    roles = {str(role) for role in (current_user.get("roles") or [])}
    role_value = current_user.get("role")
    if isinstance(role_value, str):
        roles.add(role_value)
    if "admin" not in roles:
        raise HTTPException(status_code=403, detail="Admin access required")

    results = cast(
        list[dict[str, object]],
        anyio.from_thread.run(
            bandwidth_samples.get_top_users,
            db,
            limit,
            duration,
        ),
    )
    return [TopUserEntry.model_validate(r) for r in results]


# Customer portal endpoints (own data only)
@router.get("/my/series", response_model=BandwidthSeriesResponse)
def get_my_bandwidth_series(
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    interval: str = Query(default="auto", pattern="^(auto|1m|5m|1h)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get bandwidth time series for the current user's subscription.

    Customer portal endpoint - returns data for the user's own subscription only.
    """
    subscription = bandwidth_samples.get_user_active_subscription(db, current_user)

    result = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_series,
        db,
        subscription.id,
        start_at,
        end_at,
        interval,
    )
    data = [
        BandwidthSeriesPoint(**point)
        for point in add_directions_to_series(result)["data"]
    ]
    return BandwidthSeriesResponse(
        data=data, total=result["total"], source=result["source"]
    )


@router.get("/my/stats", response_model=BandwidthStats)
def get_my_bandwidth_stats(
    period: str = Query(default="24h", pattern="^(1h|24h|7d|30d)$"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Get bandwidth statistics for the current user's subscription.

    Customer portal endpoint.
    """
    subscription = bandwidth_samples.get_user_active_subscription(db, current_user)

    stats = anyio.from_thread.run(
        bandwidth_samples.get_bandwidth_stats,
        db,
        subscription.id,
        period,
    )
    return BandwidthStats(**stats)


@router.get("/my/live")
def get_my_live_bandwidth(
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Server-Sent Events stream for the current user's bandwidth.

    Customer portal endpoint.
    """
    subscription = bandwidth_samples.get_user_active_subscription(db, current_user)

    return get_live_bandwidth(
        subscription_id=subscription.id,
        request=request,
        db=db,
        current_user=current_user,
    )
