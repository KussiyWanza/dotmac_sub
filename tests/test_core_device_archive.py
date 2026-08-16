"""Reviewed archive/restore lifecycle for core monitoring devices."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.network_monitoring import NetworkDevice
from app.services import core_device_archive
from app.services.db_session_adapter import db_session_adapter
from app.services.network.outage_impact import OutageImpact
from app.services.owner_commands import CommandContext


def _device(db, name: str, **values: object) -> NetworkDevice:
    device = NetworkDevice(name=name, is_active=True, **values)
    db.add(device)
    db.commit()
    return device


def _context(
    reason: str = "Hardware retired after reviewed replacement",
) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{uuid4()}",
        scope=core_device_archive.ARCHIVE_SCOPE,
        reason=reason,
        idempotency_key=f"pytest:{command_id}",
    )


@pytest.fixture(autouse=True)
def _no_affected_customers(monkeypatch):
    def empty_impact(_db, device_id):
        return OutageImpact(
            scope_type="node",
            scope_id=device_id,
            affected_count=0,
            payload={},
        )

    monkeypatch.setattr(core_device_archive, "resolve_node_impact", empty_impact)

    def empty_customers(*_args, **_kwargs):
        return {"subscriptions_by_node": {}, "count": 0}

    monkeypatch.setattr(
        "app.services.topology.affected.affected_customers",
        empty_customers,
    )


def test_archive_and_restore_are_evidenced_and_restore_as_inactive(db_session):
    device = _device(db_session, "retiring-core")
    device_id = device.id
    preview = core_device_archive.preview_core_device_archive(
        db_session,
        core_device_archive.PreviewCoreDeviceArchiveRequest(device_id=device_id),
    )
    assert preview.allowed
    db_session_adapter.release_read_transaction(db_session)

    archived = core_device_archive.archive_core_device(
        db_session,
        core_device_archive.ArchiveCoreDeviceCommand(
            context=_context(),
            device_id=device_id,
            expected_preview_fingerprint=preview.fingerprint,
        ),
    )
    assert archived.lifecycle_state == "archived"
    assert archived.replayed is False

    stored = db_session.get(NetworkDevice, device_id)
    assert stored is not None
    assert stored.is_active is False
    assert stored.archived_at is not None
    assert stored.archive_reason == "Hardware retired after reviewed replacement"
    assert db_session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "network.core_device_archived",
            AuditEvent.entity_id == str(device_id),
        )
    )
    assert db_session.scalar(
        select(EventStore).where(EventStore.event_type == "network_device.archived")
    )
    db_session_adapter.release_read_transaction(db_session)

    restored = core_device_archive.restore_core_device(
        db_session,
        core_device_archive.RestoreCoreDeviceCommand(
            context=_context("Restore archived device to inactive inventory"),
            device_id=device_id,
        ),
    )
    assert restored.lifecycle_state == "inactive"
    stored = db_session.get(NetworkDevice, device_id)
    assert stored is not None
    assert stored.is_active is False
    assert stored.archived_at is None
    assert stored.archived_by is None
    assert stored.archive_reason is None


def test_archive_preview_blocks_active_child_device(db_session):
    parent = _device(db_session, "core-parent")
    child = _device(db_session, "active-child", parent_device_id=parent.id)

    preview = core_device_archive.preview_core_device_archive(
        db_session,
        core_device_archive.PreviewCoreDeviceArchiveRequest(device_id=parent.id),
    )

    assert preview.allowed is False
    assert preview.active_child_ids == (child.id,)
    assert "1 active child device(s)" in preview.blockers


def test_archive_preview_blocks_affected_customers(db_session, monkeypatch):
    device = _device(db_session, "customer-serving-core")

    def customer_impact(_db, device_id):
        return OutageImpact(
            scope_type="node",
            scope_id=device_id,
            affected_count=2,
            payload={},
        )

    monkeypatch.setattr(core_device_archive, "resolve_node_impact", customer_impact)
    preview = core_device_archive.preview_core_device_archive(
        db_session,
        core_device_archive.PreviewCoreDeviceArchiveRequest(device_id=device.id),
    )

    assert preview.allowed is False
    assert preview.affected_customer_count == 2
    assert "2 affected active customer(s)" in preview.blockers


def test_archive_rejects_stale_impact_preview(db_session):
    device = _device(db_session, "changing-core")
    device_id = device.id
    preview = core_device_archive.preview_core_device_archive(
        db_session,
        core_device_archive.PreviewCoreDeviceArchiveRequest(device_id=device_id),
    )
    device.notes = "changed after preview"
    db_session.commit()

    with pytest.raises(core_device_archive.CoreDeviceArchiveError) as captured:
        core_device_archive.archive_core_device(
            db_session,
            core_device_archive.ArchiveCoreDeviceCommand(
                context=_context(),
                device_id=device_id,
                expected_preview_fingerprint=preview.fingerprint,
            ),
        )

    assert captured.value.code == "network.core_device_archive.stale_preview"
    assert db_session.get(NetworkDevice, device_id).archived_at is None


def test_external_sync_cannot_reactivate_archived_device(db_session):
    device = _device(db_session, "archived-core")
    device_id = device.id
    preview = core_device_archive.preview_core_device_archive(
        db_session,
        core_device_archive.PreviewCoreDeviceArchiveRequest(device_id=device_id),
    )
    db_session_adapter.release_read_transaction(db_session)
    core_device_archive.archive_core_device(
        db_session,
        core_device_archive.ArchiveCoreDeviceCommand(
            context=_context(),
            device_id=device_id,
            expected_preview_fingerprint=preview.fingerprint,
        ),
    )

    from app.services.network_monitoring import set_network_device_active

    set_network_device_active(
        db_session,
        device,
        True,
        reason="external_inventory_sync",
    )
    assert device.is_active is False
    assert device.archived_at is not None


@pytest.mark.parametrize("mutation", list(core_device_archive.CoreDeviceMutation))
def test_archived_device_rejects_every_legacy_mutation(db_session, mutation):
    device = _device(db_session, f"archived-{mutation.value}")
    device_id = device.id
    preview = core_device_archive.preview_core_device_archive(
        db_session,
        core_device_archive.PreviewCoreDeviceArchiveRequest(device_id=device_id),
    )
    db_session_adapter.release_read_transaction(db_session)
    core_device_archive.archive_core_device(
        db_session,
        core_device_archive.ArchiveCoreDeviceCommand(
            context=_context(),
            device_id=device_id,
            expected_preview_fingerprint=preview.fingerprint,
        ),
    )

    with pytest.raises(core_device_archive.CoreDeviceArchiveError) as captured:
        core_device_archive.require_core_device_mutable(
            db_session,
            core_device_archive.RequireCoreDeviceMutableRequest(
                device_id=device_id,
                mutation=mutation,
            ),
        )

    assert (
        captured.value.code == "network.core_device_archive.archived_device_read_only"
    )
    assert captured.value.details["mutation"] == mutation.value
