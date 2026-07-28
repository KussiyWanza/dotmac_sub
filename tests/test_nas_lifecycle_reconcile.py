from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.models.catalog import (
    NasDevice,
    NasDeviceStatus,
    Subscription,
    SubscriptionStatus,
)
from app.models.network_monitoring import NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.services import nas_lifecycle
from app.services.radius import RadiusNasLifecycleState


def _mock_radius_states(monkeypatch):
    def resolve(_db, devices):
        return {
            device.id: RadiusNasLifecycleState(
                client_ip=(device.nas_ip or device.management_ip or device.ip_address),
                internal_active_clients=1 if device.is_active else 0,
                external_present=bool(device.is_active),
            )
            for device in devices
        }

    monkeypatch.setattr(nas_lifecycle, "radius_nas_lifecycle_states", resolve)


def _subscription(db, subscriber, offer, nas, *, status=SubscriptionStatus.active):
    row = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        provisioning_nas_device_id=nas.id,
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def test_inactive_nas_with_unresolved_service_dependency_requires_review(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _mock_radius_states(monkeypatch)
    device = NasDevice(
        name="manual-review",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
        nas_ip="10.30.0.1",
        shared_secret="plain:secret",
    )
    db_session.add(device)
    db_session.flush()
    _subscription(db_session, subscriber, catalog_offer, device)
    db_session.commit()

    plan = nas_lifecycle.build_nas_lifecycle_plan(db_session)

    assert plan.blocked == 1
    assert plan.action_counts == {"manual_review": 1}
    assert plan.items[0].reason == "decommissioned_nas_has_service_dependencies"


def test_fresh_monitoring_can_reactivate_inactive_active_intent(
    db_session, monkeypatch
):
    _mock_radius_states(monkeypatch)
    node = NetworkDevice(
        name="monitored-router",
        hostname="monitored-router",
        mgmt_ip="10.30.0.2",
        is_active=True,
        live_status="up",
        live_status_at=datetime.now(UTC),
        ping_enabled=True,
        last_ping_ok=True,
        last_ping_at=datetime.now(UTC),
    )
    db_session.add(node)
    db_session.flush()
    device = NasDevice(
        name="reactivate-router",
        is_active=False,
        status=NasDeviceStatus.active,
        nas_ip="10.30.0.2",
        network_device_id=node.id,
        shared_secret="plain:secret",
    )
    db_session.add(device)
    db_session.commit()

    plan = nas_lifecycle.build_nas_lifecycle_plan(db_session)

    assert plan.blocked == 0
    assert plan.action_counts == {"reactivate": 1}
    assert plan.items[0].reason == "fresh_monitoring_proves_active"


def test_recent_transition_does_not_replace_stale_native_monitoring(
    db_session, monkeypatch
):
    _mock_radius_states(monkeypatch)
    now = datetime.now(UTC)
    node = NetworkDevice(
        name="stale-monitored-router",
        hostname="stale-monitored-router",
        mgmt_ip="10.30.0.22",
        is_active=True,
        live_status="up",
        live_status_at=now,
        ping_enabled=True,
        last_ping_ok=True,
        last_ping_at=(now - nas_lifecycle._MONITORING_FRESHNESS - timedelta(seconds=1)),
    )
    db_session.add(node)
    db_session.flush()
    device = NasDevice(
        name="do-not-reactivate-router",
        is_active=False,
        status=NasDeviceStatus.active,
        nas_ip="10.30.0.22",
        network_device_id=node.id,
        shared_secret="plain:secret",
    )
    db_session.add(device)
    db_session.commit()

    plan = nas_lifecycle.build_nas_lifecycle_plan(db_session)

    assert plan.action_counts != {"reactivate": 1}
    assert plan.items[0].monitoring_up is False
    assert plan.items[0].monitoring_state == "stale_or_unknown"


def test_exact_live_session_can_relink_and_decommission_source(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _mock_radius_states(monkeypatch)
    source = NasDevice(
        name="old-router",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
        nas_ip="10.30.0.3",
        shared_secret="plain:old-secret",
    )
    target = NasDevice(
        name="new-router",
        is_active=True,
        status=NasDeviceStatus.active,
        nas_ip="10.30.0.4",
        shared_secret="plain:new-secret",
    )
    db_session.add_all([source, target])
    db_session.flush()
    subscription = _subscription(db_session, subscriber, catalog_offer, source)
    db_session.add(
        RadiusActiveSession(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id,
            nas_device_id=target.id,
            username="relinked-user",
            acct_session_id="relinked-session",
            session_start=datetime.now(UTC),
        )
    )
    db_session.commit()

    plan = nas_lifecycle.build_nas_lifecycle_plan(db_session)

    source_item = next(
        item for item in plan.items if item.nas_device_id == str(source.id)
    )
    assert (
        source_item.action == nas_lifecycle.NasLifecycleAction.relink_and_decommission
    )
    assert len(source_item.relinks) == 1
    assert source_item.relinks[0].target_nas_device_id == str(target.id)


def test_unbound_session_is_not_strong_enough_to_relink(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _mock_radius_states(monkeypatch)
    source = NasDevice(
        name="unbound-source",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
        nas_ip="10.30.0.5",
        shared_secret="plain:old-secret",
    )
    target = NasDevice(
        name="unbound-target",
        is_active=True,
        status=NasDeviceStatus.active,
        nas_ip="10.30.0.6",
        shared_secret="plain:new-secret",
    )
    db_session.add_all([source, target])
    db_session.flush()
    _subscription(db_session, subscriber, catalog_offer, source)
    db_session.add(
        RadiusActiveSession(
            subscriber_id=subscriber.id,
            subscription_id=None,
            nas_device_id=target.id,
            username="unbound-user",
            acct_session_id="unbound-session",
            session_start=datetime.now(UTC),
        )
    )
    db_session.commit()

    plan = nas_lifecycle.build_nas_lifecycle_plan(db_session)

    source_item = next(
        item for item in plan.items if item.nas_device_id == str(source.id)
    )
    assert source_item.action == nas_lifecycle.NasLifecycleAction.manual_review


def test_inactive_unused_nas_is_decommission_candidate(db_session, monkeypatch):
    _mock_radius_states(monkeypatch)
    device = NasDevice(
        name="unused-router",
        is_active=False,
        status=NasDeviceStatus.active,
        nas_ip="10.30.0.7",
        shared_secret="plain:secret",
    )
    db_session.add(device)
    db_session.commit()

    plan = nas_lifecycle.build_nas_lifecycle_plan(db_session)

    assert plan.action_counts == {"decommission": 1}
    assert plan.items[0].reason == "inactive_nas_has_no_service_or_session_dependency"


def test_active_external_radius_identity_does_not_require_local_secret(
    db_session, monkeypatch, subscriber, catalog_offer
):
    device = NasDevice(
        name="external-authority-router",
        is_active=True,
        status=NasDeviceStatus.active,
        nas_ip="10.30.0.10",
        shared_secret=None,
    )
    db_session.add(device)
    db_session.flush()
    _subscription(db_session, subscriber, catalog_offer, device)
    db_session.commit()
    monkeypatch.setattr(
        nas_lifecycle,
        "radius_nas_lifecycle_states",
        lambda _db, devices: {
            row.id: RadiusNasLifecycleState(
                client_ip=row.nas_ip,
                internal_active_clients=1,
                external_present=True,
            )
            for row in devices
        },
    )

    plan = nas_lifecycle.build_nas_lifecycle_plan(db_session)

    assert plan.items == ()


def test_execute_requires_digest_and_commits_owned_transitions(db_session, monkeypatch):
    _mock_radius_states(monkeypatch)
    device = NasDevice(
        name="execute-router",
        is_active=False,
        status=NasDeviceStatus.active,
        nas_ip="10.30.0.8",
        shared_secret="plain:secret",
    )
    db_session.add(device)
    db_session.commit()
    monkeypatch.setattr(nas_lifecycle, "_publish_plan", lambda *a, **k: None)
    monkeypatch.setattr(nas_lifecycle, "stage_audit_event", lambda *a, **k: None)
    monkeypatch.setattr(
        nas_lifecycle,
        "apply_radius_nas_lifecycle",
        lambda *_args, **_kwargs: SimpleNamespace(
            internal_clients_changed=0,
            external_clients_changed=0,
        ),
    )

    wrong = nas_lifecycle.reconcile_nas_lifecycle(
        db_session,
        execute=True,
        confirm_plan_digest="wrong",
    )
    db_session.refresh(device)
    assert wrong.status == "confirmation_required"
    assert device.status == NasDeviceStatus.active

    plan = nas_lifecycle.build_nas_lifecycle_plan(db_session)
    result = nas_lifecycle.reconcile_nas_lifecycle(
        db_session,
        execute=True,
        confirm_plan_digest=plan.digest,
    )

    db_session.refresh(device)
    assert result.status == "completed"
    assert result.nas_transitions == 1
    assert device.is_active is False
    assert device.status == NasDeviceStatus.decommissioned


def test_plan_details_contain_network_not_customer_identity(
    db_session, monkeypatch, subscriber, catalog_offer
):
    _mock_radius_states(monkeypatch)
    device = NasDevice(
        name="detail-router",
        is_active=False,
        status=NasDeviceStatus.decommissioned,
        nas_ip="10.30.0.9",
        shared_secret="plain:secret",
    )
    db_session.add(device)
    db_session.flush()
    _subscription(db_session, subscriber, catalog_offer, device)
    db_session.commit()

    payload = nas_lifecycle.build_nas_lifecycle_plan(db_session).as_dict(
        include_details=True
    )
    serialized = str(payload)

    assert device.name in serialized
    assert str(device.id) in serialized
    assert str(subscriber.id) not in serialized
