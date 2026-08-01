"""Canonical support-ticket region projection."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.support import Ticket


def list_canonical_region_options(
    db: Session,
    *,
    configured_regions: tuple[str, ...],
) -> tuple[str, ...]:
    """Combine configured regions with current authoritative Ticket observations."""

    rows = (
        db.query(Ticket.region)
        .filter(
            Ticket.is_active.is_(True),
            Ticket.region.isnot(None),
            Ticket.region != "",
        )
        .distinct()
        .order_by(Ticket.region.asc())
        .limit(200)
        .all()
    )
    discovered = tuple(str(item[0]) for item in rows if item and item[0])
    return tuple(sorted(set(discovered + configured_regions)))


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
