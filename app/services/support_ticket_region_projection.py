"""Canonical support-ticket region projection."""

from __future__ import annotations

from sqlalchemy import literal, select, union_all
from sqlalchemy.orm import Session

from app.models.support import Ticket


def list_canonical_region_options(
    db: Session,
    *,
    configured_regions: tuple[str, ...],
) -> tuple[str, ...]:
    """Combine configured regions with current authoritative Ticket observations."""

    ticket_regions = select(Ticket.region.label("region")).where(
        Ticket.is_active.is_(True),
        Ticket.region.isnot(None),
        Ticket.region != "",
    )
    configured_region_queries = tuple(
        select(literal(region).label("region"))
        for region in configured_regions
        if region
    )
    region_sources = union_all(
        ticket_regions,
        *configured_region_queries,
    ).subquery()
    rows = db.execute(
        select(region_sources.c.region)
        .where(
            region_sources.c.region.isnot(None),
            region_sources.c.region != "",
        )
        .distinct()
        .order_by(region_sources.c.region.asc())
        .limit(200)
    ).all()
    return tuple(str(item[0]) for item in rows if item and item[0])


def canonical_region_option(
    db: Session,
    submitted: str | None,
    *,
    configured_regions: tuple[str, ...],
) -> str | None:
    """Resolve a submitted region only when it is a current canonical option."""

    candidate = str(submitted or "").strip()
    if not candidate:
        return None
    return next(
        (
            option
            for option in list_canonical_region_options(
                db,
                configured_regions=configured_regions,
            )
            if option == candidate
        ),
        None,
    )
