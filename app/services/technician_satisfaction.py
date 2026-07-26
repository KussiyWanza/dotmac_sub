"""Per-technician satisfaction, derived from the ratings customers already give.

The rating itself is not new: ``customer_work_order_selfcare.rate_technician``
has always written one per completed visit, and the portal has always collected
it. Nothing ever read it back. A rating that is captured and never looked at is
not accountability — it is just storage — so this resolver turns the per-visit
facts into a per-technician view.

It is a **resolver**: it derives, it does not decide or write. The work order
keeps the canonical rating (``WorkOrder.technician_rating``, projected from
typed metadata) and this module never writes one, never adjusts one, and holds
no state of its own. Recompute it whenever you like.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services.field.jobs import _technician_name

#: A visit only carries a rating once it is finished.
_RATED_STATUS = "completed"

#: Default reporting window. Long enough to be stable, short enough that a
#: technician is not judged forever on their first month.
DEFAULT_WINDOW_DAYS = 90


@dataclass(frozen=True)
class TechnicianScorecard:
    """One technician's satisfaction over a window.

    ``average`` is None rather than 0.0 when there are no ratings: "not yet
    rated" and "rated badly" must never render the same.
    """

    technician_id: str
    technician_name: str | None
    rated_visits: int
    average: float | None
    distribution: dict[int, int] = field(default_factory=dict)
    latest_rated_at: datetime | None = None


def _window_start(days: int | None) -> datetime | None:
    if days is None:
        return None
    return datetime.now(UTC) - timedelta(days=max(1, days))


def _rated_at(row: WorkOrder) -> datetime | None:
    metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
    feedback = metadata.get("technician_rating")
    raw = feedback.get("rated_at") if isinstance(feedback, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rated_work_orders(
    db: Session, *, technician_ids: list[uuid.UUID] | None
) -> list[tuple[uuid.UUID, WorkOrder]]:
    """Completed visits paired with the technician who was assigned to them.

    The rating lives in work-order JSON metadata, so the rating filter is
    applied in Python rather than SQL — portable across the SQLite tests and
    Postgres, and cheap because only completed visits are considered.
    """
    query = (
        select(WorkOrderAssignmentQueue.assigned_technician_id, WorkOrder)
        .join(WorkOrder, WorkOrder.id == WorkOrderAssignmentQueue.work_order_mirror_id)
        .where(
            WorkOrderAssignmentQueue.status == DispatchQueueStatus.assigned,
            WorkOrderAssignmentQueue.assigned_technician_id.is_not(None),
            WorkOrder.status == _RATED_STATUS,
        )
    )
    if technician_ids:
        query = query.where(
            WorkOrderAssignmentQueue.assigned_technician_id.in_(technician_ids)
        )
    return [(row[0], row[1]) for row in db.execute(query).all()]


def scorecards(
    db: Session,
    *,
    technician_ids: list[uuid.UUID] | None = None,
    window_days: int | None = DEFAULT_WINDOW_DAYS,
) -> list[TechnicianScorecard]:
    """Satisfaction per technician, best-rated first.

    Technicians with no ratings in the window are included with
    ``rated_visits=0``: an absent scorecard is indistinguishable from a
    technician nobody has rated, and that difference matters to a manager.
    """
    since = _window_start(window_days)
    buckets: dict[uuid.UUID, list[tuple[int, datetime | None]]] = {}
    for technician_id, row in _rated_work_orders(db, technician_ids=technician_ids):
        rating = row.technician_rating
        if rating is None:
            continue
        rated_at = _rated_at(row)
        if since is not None and rated_at is not None and rated_at < since:
            continue
        buckets.setdefault(technician_id, []).append((rating, rated_at))

    wanted = technician_ids or list(buckets)
    profiles = {
        profile.id: profile
        for profile in db.scalars(
            select(TechnicianProfile).where(TechnicianProfile.id.in_(wanted))
        ).all()
    }
    users = {
        user.id: user
        for user in db.scalars(
            select(SystemUser).where(
                SystemUser.id.in_(
                    [p.system_user_id for p in profiles.values() if p.system_user_id]
                )
            )
        ).all()
    }

    results: list[TechnicianScorecard] = []
    for technician_id in wanted:
        ratings = buckets.get(technician_id, [])
        profile = profiles.get(technician_id)
        distribution: dict[int, int] = {}
        for rating, _ in ratings:
            distribution[rating] = distribution.get(rating, 0) + 1
        timestamps = [moment for _, moment in ratings if moment is not None]
        results.append(
            TechnicianScorecard(
                technician_id=str(technician_id),
                technician_name=(
                    _technician_name(
                        profile,
                        users.get(profile.system_user_id)
                        if profile.system_user_id
                        else None,
                    )
                    if profile is not None
                    else None
                ),
                rated_visits=len(ratings),
                average=(
                    round(sum(rating for rating, _ in ratings) / len(ratings), 2)
                    if ratings
                    else None
                ),
                distribution=distribution,
                latest_rated_at=max(timestamps) if timestamps else None,
            )
        )

    # Unrated technicians sort last: they are not the worst, they are unknown.
    results.sort(key=lambda card: (card.average is None, -(card.average or 0)))
    return results


def scorecard(
    db: Session,
    technician_id: uuid.UUID,
    *,
    window_days: int | None = DEFAULT_WINDOW_DAYS,
) -> TechnicianScorecard:
    return scorecards(db, technician_ids=[technician_id], window_days=window_days)[0]
