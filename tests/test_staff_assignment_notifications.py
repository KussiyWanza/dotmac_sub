from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.notification import Notification, NotificationChannel
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.schemas.project import ProjectCreate
from app.schemas.support import TicketCreate
from app.services.staff_notifications import (
    queue_staff_assignment_notifications,
    resolve_assignment_users,
)
from tests.staff_identity_fixtures import add_bound_staff_user


def test_assignment_audience_combines_users_and_service_teams(db_session) -> None:
    direct, _direct_person = add_bound_staff_user(
        db_session, email=f"direct-{uuid4()}@example.com"
    )
    member, member_person = add_bound_staff_user(
        db_session, email=f"member-{uuid4()}@example.com"
    )
    team = ServiceTeam(name=f"Assignment {uuid4()}", is_active=True)
    db_session.add(team)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(
            team_id=team.id,
            person_id=member_person.id,
            is_active=True,
        )
    )
    db_session.flush()

    users = resolve_assignment_users(
        db_session,
        person_ids={str(direct.id)},
        service_team_ids={str(team.id)},
    )

    assert {user.id for user in users} == {direct.id, member.id}

    queue_staff_assignment_notifications(
        db_session,
        users=users,
        subject="Assigned",
        body="Please review",
    )
    rows = db_session.query(Notification).all()
    assert {(row.channel, row.recipient) for row in rows} == {
        (NotificationChannel.push, str(direct.id)),
        (NotificationChannel.email, direct.email),
        (NotificationChannel.push, str(member.id)),
        (NotificationChannel.email, member.email),
    }


@pytest.mark.parametrize(
    ("schema", "field"),
    [
        (ProjectCreate, "assistant_manager_person_id"),
        (TicketCreate, "site_coordinator_person_id"),
    ],
)
def test_new_records_reject_retired_site_coordinator(schema, field) -> None:
    with pytest.raises(ValidationError):
        schema(name="Project", title="Ticket", **{field: uuid4()})
