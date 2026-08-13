"""Canonical support-ticket region projection."""

from __future__ import annotations

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from app.models.support import Ticket


def normalize_region_value(value: str | None) -> str:
    """Return the case-insensitive identity used by region reads and filters."""

    return str(value or "").strip().lower()


def list_canonical_region_options(
    db: Session,
    *,
    configured_regions: tuple[str, ...],
) -> tuple[str, ...]:
    """Combine configured regions with current authoritative Ticket observations."""

    normalized_ticket_region = func.lower(func.trim(Ticket.region))
    ticket_regions = select(normalized_ticket_region.label("region")).where(
        Ticket.is_active.is_(True),
        Ticket.region.isnot(None),
        func.trim(Ticket.region) != "",
    )
    configured_region_queries = tuple(
        select(literal(normalized).label("region"))
        for region in configured_regions
        if (normalized := normalize_region_value(region))
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

    candidate = normalize_region_value(submitted)
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
