"""Canonical support-ticket region projection."""

from __future__ import annotations

from sqlalchemy.orm import Session


def normalize_region_value(value: str | None) -> str:
    """Return the case-insensitive identity used by region reads and filters."""

    return str(value or "").strip().lower()


def list_canonical_region_options(
    _db: Session,
    *,
    configured_regions: tuple[str, ...],
) -> tuple[str, ...]:
    """Return only the configured region vocabulary.

    Ticket rows are observations of historical assignments, not configuration
    input. They must never expand the selectable region vocabulary.
    """

    return tuple(
        sorted(
            {
                normalized_region
                for normalized_region in (
                    normalize_region_value(region) for region in configured_regions
                )
                if normalized_region
            }
        )
    )


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
