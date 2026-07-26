"""Reading back the ratings customers were already giving.

``rate_technician`` has always written a rating per completed visit and the
portal has always collected one. Nothing read it. These tests cover the
resolver that turns those per-visit facts into a per-technician view, and in
particular the two distinctions that are easy to collapse and wrong to:

* unrated is not zero-rated;
* a rating outside the window is excluded, not silently averaged in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import technician_satisfaction


def _technician(db_session, name: str) -> TechnicianProfile:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=f"crm-{name.lower()}-{uuid4().hex[:6]}",
        title="Installer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _subscriber(db_session) -> Subscriber:
    subscriber = Subscriber(
        first_name="Rated",
        last_name="Customer",
        email=f"rated-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _rated_visit(
    db_session,
    subscriber: Subscriber,
    profile: TechnicianProfile,
    *,
    rating: int | None,
    rated_at: datetime | None = None,
    status: str = "completed",
) -> WorkOrder:
    metadata = {}
    if rating is not None:
        metadata["technician_rating"] = {
            "rating": rating,
            "comment": None,
            "rated_at": (rated_at or datetime.now(UTC)).isoformat(),
            "source": "customer_selfcare",
        }
    row = WorkOrder(
        crm_work_order_id=f"wo-rate-{uuid4().hex[:8]}",
        subscriber_id=subscriber.id,
        title="Rated visit",
        status=status,
        metadata_=metadata or None,
    )
    db_session.add(row)
    db_session.flush()
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=row.id,
            assigned_technician_id=profile.id,
            status=DispatchQueueStatus.assigned,
        )
    )
    db_session.flush()
    return row


def test_the_average_comes_from_the_visits(db_session):
    subscriber = _subscriber(db_session)
    profile = _technician(db_session, "Ada")
    for rating in (5, 4, 3):
        _rated_visit(db_session, subscriber, profile, rating=rating)
    db_session.commit()

    card = technician_satisfaction.scorecard(db_session, profile.id)

    assert card.rated_visits == 3
    assert card.average == 4.0
    assert card.distribution == {5: 1, 4: 1, 3: 1}


def test_an_unrated_technician_has_no_average_rather_than_zero(db_session):
    """Zero would render as the worst possible score for someone unjudged."""
    profile = _technician(db_session, "Bea")
    db_session.commit()

    card = technician_satisfaction.scorecard(db_session, profile.id)

    assert card.rated_visits == 0
    assert card.average is None


def test_a_visit_nobody_rated_is_not_counted(db_session):
    subscriber = _subscriber(db_session)
    profile = _technician(db_session, "Cal")
    _rated_visit(db_session, subscriber, profile, rating=5)
    _rated_visit(db_session, subscriber, profile, rating=None)
    db_session.commit()

    card = technician_satisfaction.scorecard(db_session, profile.id)

    assert card.rated_visits == 1
    assert card.average == 5.0


def test_ratings_outside_the_window_are_excluded(db_session):
    subscriber = _subscriber(db_session)
    profile = _technician(db_session, "Dee")
    _rated_visit(db_session, subscriber, profile, rating=5)
    _rated_visit(
        db_session,
        subscriber,
        profile,
        rating=1,
        rated_at=datetime.now(UTC) - timedelta(days=200),
    )
    db_session.commit()

    card = technician_satisfaction.scorecard(db_session, profile.id, window_days=90)

    assert card.rated_visits == 1
    assert card.average == 5.0


def test_the_whole_history_is_available_without_a_window(db_session):
    subscriber = _subscriber(db_session)
    profile = _technician(db_session, "Eve")
    _rated_visit(db_session, subscriber, profile, rating=5)
    _rated_visit(
        db_session,
        subscriber,
        profile,
        rating=1,
        rated_at=datetime.now(UTC) - timedelta(days=200),
    )
    db_session.commit()

    card = technician_satisfaction.scorecard(db_session, profile.id, window_days=None)

    assert card.rated_visits == 2
    assert card.average == 3.0


def test_scorecards_rank_best_first_and_put_the_unrated_last(db_session):
    subscriber = _subscriber(db_session)
    weak = _technician(db_session, "Fay")
    strong = _technician(db_session, "Gus")
    unrated = _technician(db_session, "Hal")
    _rated_visit(db_session, subscriber, weak, rating=2)
    _rated_visit(db_session, subscriber, strong, rating=5)
    db_session.commit()

    cards = technician_satisfaction.scorecards(
        db_session, technician_ids=[weak.id, strong.id, unrated.id]
    )

    assert [card.technician_id for card in cards] == [
        str(strong.id),
        str(weak.id),
        str(unrated.id),
    ]


def test_the_scorecard_names_the_technician(db_session):
    subscriber = _subscriber(db_session)
    profile = _technician(db_session, "Ivy")
    _rated_visit(db_session, subscriber, profile, rating=4)
    db_session.commit()

    card = technician_satisfaction.scorecard(db_session, profile.id)

    assert card.technician_name == "Ivy Tech"


def test_an_incomplete_visit_is_not_rated_yet(db_session):
    """A rating can only describe a visit that happened."""
    subscriber = _subscriber(db_session)
    profile = _technician(db_session, "Jon")
    _rated_visit(db_session, subscriber, profile, rating=5, status="in_progress")
    db_session.commit()

    card = technician_satisfaction.scorecard(db_session, profile.id)

    assert card.rated_visits == 0
