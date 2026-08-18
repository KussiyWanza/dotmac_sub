from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import uuid4

from app.models.audit import AuditEvent
from app.models.dispatch import TechnicianProfile
from app.models.field_location import FieldTechLocationPing, FieldTechPresence
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services.field.location_retention import (
    LOCATION_HISTORY_RETENTION_DAYS,
    PruneFieldLocationHistoryCommand,
    prune_field_location_history,
)
from app.services.owner_commands import CommandContext
from app.services.observability import StateObservation

task_module = import_module("app.tasks.field_location_retention")


def _technician(db_session) -> TechnicianProfile:
    user = SystemUser(
        first_name="Retention",
        last_name="Technician",
        display_name="Retention Technician",
        email=f"location-retention-{uuid4().hex}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        title="Field engineer",
    )
    db_session.add(profile)
    db_session.flush()
    return profile


def _ping(
    profile: TechnicianProfile,
    *,
    received_at: datetime,
) -> FieldTechLocationPing:
    return FieldTechLocationPing(
        technician_id=profile.id,
        person_id=profile.person_id,
        latitude=9.0765,
        longitude=7.3986,
        captured_at=received_at,
        received_at=received_at,
        source="mobile",
    )


def _command(as_of: datetime, *, batch_size: int = 10_000):
    return PruneFieldLocationHistoryCommand(
        context=CommandContext.system(
            actor="test:field_location_retention",
            scope="field:test",
            reason="verify detailed GPS retention",
        ),
        as_of=as_of,
        batch_size=batch_size,
    )


def test_prune_deletes_only_pings_older_than_30_days(db_session):
    as_of = datetime(2026, 8, 18, 12, tzinfo=UTC)
    profile = _technician(db_session)
    presence = FieldTechPresence(
        technician_id=profile.id,
        person_id=profile.person_id,
        status="on_shift",
        location_sharing_enabled=True,
        last_latitude=9.0765,
        last_longitude=7.3986,
        last_location_at=as_of,
    )
    old = _ping(
        profile,
        received_at=as_of - timedelta(days=LOCATION_HISTORY_RETENTION_DAYS, seconds=1),
    )
    retained = _ping(
        profile,
        received_at=as_of - timedelta(days=LOCATION_HISTORY_RETENTION_DAYS),
    )
    db_session.add_all([presence, old, retained])
    db_session.commit()
    old_id = old.id
    retained_id = retained.id

    outcome = prune_field_location_history(db_session, _command(as_of))

    assert outcome.deleted_count == 1
    assert db_session.get(FieldTechLocationPing, old_id) is None
    assert db_session.get(FieldTechLocationPing, retained_id) is not None
    assert db_session.get(FieldTechPresence, presence.id) is not None
    audit = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "field_location_history_pruned")
        .one()
    )
    assert audit.metadata_["deleted_count"] == 1
    assert "latitude" not in audit.metadata_
    assert "longitude" not in audit.metadata_


def test_prune_is_bounded_and_converges_on_replay(db_session):
    as_of = datetime(2026, 8, 18, 12, tzinfo=UTC)
    profile = _technician(db_session)
    db_session.add_all(
        [
            _ping(profile, received_at=as_of - timedelta(days=31, minutes=index))
            for index in range(3)
        ]
    )
    db_session.commit()

    first = prune_field_location_history(
        db_session, _command(as_of, batch_size=2)
    )
    second = prune_field_location_history(
        db_session, _command(as_of, batch_size=2)
    )
    third = prune_field_location_history(
        db_session, _command(as_of, batch_size=2)
    )

    assert first.deleted_count == 2
    assert first.batch_limit_reached is True
    assert second.deleted_count == 1
    assert third.deleted_count == 0
    assert db_session.query(FieldTechLocationPing).count() == 0


def test_retention_task_publishes_success_and_backlog_metrics(monkeypatch):
    fake_db = object()
    cutoff = datetime(2026, 7, 19, 12, tzinfo=UTC)
    seen: dict[str, object] = {}
    snapshots: list[tuple[str, tuple[StateObservation, ...], str]] = []
    task_runs: list[tuple[str, str, dict[str, object]]] = []

    @contextmanager
    def owner_session():
        yield fake_db

    def prune(db, command):
        seen["db"] = db
        seen["command"] = command
        return task_module.PruneFieldLocationHistoryOutcome(
            command_id=command.context.command_id,
            cutoff=cutoff,
            deleted_count=10_000,
            batch_limit_reached=True,
        )

    def publish(
        domain: str,
        observations: Iterable[StateObservation],
        *,
        status: str = "ok",
        now: datetime | None = None,
    ) -> bool:
        snapshots.append((domain, tuple(observations), status))
        return True

    def record(
        task_name: str,
        *,
        status: str,
        counters: dict[str, object] | None = None,
    ) -> None:
        task_runs.append((task_name, status, dict(counters or {})))

    monkeypatch.setattr(
        task_module.db_session_adapter,
        "owner_command_session",
        owner_session,
    )
    monkeypatch.setattr(task_module, "prune_field_location_history", prune)
    monkeypatch.setattr(task_module, "publish_state_snapshot", publish)
    monkeypatch.setattr(task_module, "record_task_run", record)

    task = task_module.prune_field_location_history_task
    task.push_request(id="field-location-retention-test")
    try:
        result = task.run()
    finally:
        task.pop_request()

    assert seen["db"] is fake_db
    assert result["deleted_count"] == 10_000
    assert result["batch_limit_reached"] is True
    assert snapshots[0][0] == "field_location_retention"
    assert snapshots[0][2] == "degraded"
    observations = {
        observation.signal: observation.value for observation in snapshots[0][1]
    }
    assert observations == {"deleted_rows": 10_000, "batch_limit_reached": 1}
    assert task_runs == [
        (
            "app.tasks.field_location_retention.prune_field_location_history",
            "success",
            {"deleted_count": 10_000, "batch_limit_reached": True},
        )
    ]
