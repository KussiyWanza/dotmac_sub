"""Tests for the unified device projection reconciler.

See app/services/device_projection_reconcile.py. The reconciler is the sole
canonical writer of the device_projections table; it delegates device
derivation to collect_devices and must materialise the result idempotently,
stamp freshness, and prune orphans. These tests drive it through a controlled
collect_devices so the contract is exercised without heavy device fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models.event_store import EventStore
from app.models.network_monitoring import DeviceProjection
from app.services import device_projection_reconcile as reconcile_mod
from app.services.device_projection_reconcile import (
    ReconcileDeviceProjectionsCommand,
    reconcile_device_projections,
)
from app.services.owner_commands import CommandContext


def _device(device_id, device_type, *, status="working", **overrides):
    device = {
        "id": device_id,
        "name": f"{device_type}-{device_id}",
        "type": device_type,
        "serial_number": f"SN{device_id}",
        "ip_address": "10.0.0.1",
        "vendor": "acme",
        "model": "x1",
        "status": status,
        "operational_reason": None,
        "last_seen": None,
        "subscriber": None,
    }
    device.update(overrides)
    return device


def _patch_collect(monkeypatch, devices):
    monkeypatch.setattr(reconcile_mod, "collect_devices", lambda db: list(devices))


def _command(*, now: datetime | None = None) -> ReconcileDeviceProjectionsCommand:
    return ReconcileDeviceProjectionsCommand(
        context=CommandContext.system(
            actor="pytest:device_projection",
            scope="network:test",
            reason="verify projection owner behavior",
        ),
        reconciled_at=now,
    )


def _rows(db):
    return {
        (r.device_type, r.source_id): r
        for r in db.execute(select(DeviceProjection)).scalars()
    }


def _naive(value):
    # SQLite drops tzinfo on read-back; normalise before comparing instants.
    return value.replace(tzinfo=None) if value is not None else None


def test_reconcile_inserts_one_row_per_derived_device(db_session, monkeypatch):
    _patch_collect(
        monkeypatch,
        [
            _device("1", "olt"),
            _device("2", "core"),
            _device("3", "ont"),
            _device("4", "cpe", status="not_working"),
        ],
    )

    result = reconcile_device_projections(db_session, _command())

    assert result.inserted == 4
    assert result.updated == 0
    assert result.pruned == 0
    rows = _rows(db_session)
    assert set(rows) == {("olt", "1"), ("core", "2"), ("ont", "3"), ("cpe", "4")}
    assert rows[("cpe", "4")].operational_status == "not_working"
    assert rows[("olt", "1")].vendor == "acme"


def test_reconcile_is_idempotent(db_session, monkeypatch):
    devices = [_device("1", "olt"), _device("2", "core")]
    _patch_collect(monkeypatch, devices)

    first = reconcile_device_projections(db_session, _command())
    assert first.inserted == 2

    second = reconcile_device_projections(db_session, _command())
    assert second.inserted == 0
    assert second.updated == 2
    assert second.pruned == 0
    assert len(_rows(db_session)) == 2


def test_reconcile_updates_changed_status_and_repair_evidence(db_session, monkeypatch):
    early = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    _patch_collect(monkeypatch, [_device("1", "olt", status="working")])
    first = reconcile_device_projections(db_session, _command(now=early))
    assert first.reconciled_at == early
    assert _naive(_rows(db_session)[("olt", "1")].refreshed_at) == _naive(early)
    db_session.commit()

    later = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
    _patch_collect(
        monkeypatch,
        [
            _device(
                "1",
                "olt",
                status="not_working",
                operational_reason="link_down",
            )
        ],
    )
    reconcile_device_projections(db_session, _command(now=later))

    row = _rows(db_session)[("olt", "1")]
    assert row.operational_status == "not_working"
    assert row.operational_reason == "link_down"
    assert _naive(row.refreshed_at) == _naive(later)


def test_reconcile_projects_archived_core_device_as_not_working(
    db_session, monkeypatch
):
    _patch_collect(
        monkeypatch,
        [
            _device(
                "1",
                "core",
                lifecycle_state="archived",
                status="working",
            )
        ],
    )

    reconcile_device_projections(db_session, _command())

    row = _rows(db_session)[("core", "1")]
    assert row.lifecycle_state == "archived"
    assert row.operational_status == "not_working"


def test_reconcile_prunes_orphaned_devices(db_session, monkeypatch):
    _patch_collect(monkeypatch, [_device("1", "olt"), _device("2", "core")])
    reconcile_device_projections(db_session, _command())

    # Device 2 disappears from the authoritative source.
    _patch_collect(monkeypatch, [_device("1", "olt")])
    result = reconcile_device_projections(db_session, _command())

    assert result.pruned == 1
    assert set(_rows(db_session)) == {("olt", "1")}


def test_same_source_id_across_types_are_distinct_rows(db_session, monkeypatch):
    # (device_type, source_id) is the natural key: an OLT and an ONT may share
    # a numeric id without colliding.
    _patch_collect(monkeypatch, [_device("1", "olt"), _device("1", "ont")])
    result = reconcile_device_projections(db_session, _command())

    assert result.inserted == 2
    assert set(_rows(db_session)) == {("olt", "1"), ("ont", "1")}


def test_reconcile_stages_versioned_event_with_command_evidence(
    db_session, monkeypatch
):
    command = _command()
    _patch_collect(monkeypatch, [_device("1", "olt")])

    result = reconcile_device_projections(db_session, command)

    record = db_session.scalar(
        select(EventStore).where(
            EventStore.event_type == "device_projection.reconciled"
        )
    )
    assert record is not None
    assert record.payload["schema_version"] == 1
    assert record.payload["command_id"] == str(command.context.command_id)
    assert record.payload["correlation_id"] == str(command.context.correlation_id)
    assert record.payload["aggregate_type"] == "device_projection"
    assert record.payload["aggregate_id"] == "network:global"
    assert record.payload["aggregate_version"] == str(command.context.command_id)
    assert record.payload["inserted"] == 1
    assert result.command_id == command.context.command_id


def test_reconcile_failure_rolls_back_projection_and_event(db_session, monkeypatch):
    def fail_collect(_db):
        raise RuntimeError("authoritative source unavailable")

    monkeypatch.setattr(reconcile_mod, "collect_devices", fail_collect)

    with pytest.raises(RuntimeError, match="authoritative source unavailable"):
        reconcile_device_projections(db_session, _command())

    assert not db_session.in_transaction()
    assert _rows(db_session) == {}
    assert (
        db_session.scalar(
            select(EventStore.id).where(
                EventStore.event_type == "device_projection.reconciled"
            )
        )
        is None
    )


def test_failure_after_event_staging_rolls_back_projection_and_outbox(
    db_session, monkeypatch
):
    _patch_collect(monkeypatch, [_device("1", "olt")])

    def fail_after_event(*_args, **_kwargs):
        raise RuntimeError("failure after event staging")

    monkeypatch.setattr(reconcile_mod.logger, "info", fail_after_event)

    with pytest.raises(RuntimeError, match="failure after event staging"):
        reconcile_device_projections(db_session, _command())

    assert not db_session.in_transaction()
    assert _rows(db_session) == {}
    assert (
        db_session.scalar(
            select(EventStore.id).where(
                EventStore.event_type == "device_projection.reconciled"
            )
        )
        is None
    )


def test_reconcile_rejects_naive_reconciliation_time_without_open_transaction(
    db_session, monkeypatch
):
    _patch_collect(monkeypatch, [])
    command = _command(now=datetime(2026, 7, 19, 8, 0))

    with pytest.raises(reconcile_mod.DeviceProjectionCommandError) as captured:
        reconcile_device_projections(db_session, command)

    assert captured.value.code == "network.device_projection.invalid_command"
    assert not db_session.in_transaction()


def test_class_facts_and_new_device_types_are_projected(db_session, monkeypatch):
    """NAS + routers project as first-class rows; class_facts round-trips."""
    _patch_collect(
        monkeypatch,
        [
            _device(
                "n1",
                "nas",
                class_facts={"health_status": "healthy", "site_name": "Abuja"},
            ),
            _device("r1", "router", class_facts={"routeros_version": "7.10"}),
            _device("o1", "ont", class_facts={"onu_rx_dbm": -21.2}),
            _device("c1", "core", class_facts=None),
        ],
    )
    reconcile_device_projections(db_session, _command())
    rows = _rows(db_session)
    assert {("nas", "n1"), ("router", "r1")} <= set(rows)
    assert rows[("nas", "n1")].class_facts["health_status"] == "healthy"
    assert rows[("nas", "n1")].class_facts["site_name"] == "Abuja"
    assert rows[("router", "r1")].class_facts["routeros_version"] == "7.10"
    assert rows[("ont", "o1")].class_facts["onu_rx_dbm"] == -21.2
    assert rows[("core", "c1")].class_facts is None
