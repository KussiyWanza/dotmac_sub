"""Regression coverage for the direct live-bandwidth diagnostic boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.services.nas._mikrotik import MikrotikLiveBandwidthTarget
from app.services.network import identity as network_identity
from app.services.network import live_bandwidth_observations as owner


class _Session:
    def __init__(self, subscription: object) -> None:
        self.subscription = subscription
        self.committed = False
        self.expire_on_commit = True
        self.new: set[object] = set()
        self.dirty: set[object] = set()
        self.deleted: set[object] = set()

    def get(self, _model, _pk):
        return self.subscription

    def in_transaction(self) -> bool:
        return True

    def in_nested_transaction(self) -> bool:
        return False

    def commit(self) -> None:
        self.committed = True


def _admin_query(subscription_id: UUID) -> owner.LiveBandwidthReadQuery:
    return owner.LiveBandwidthReadQuery(
        subscription_id=subscription_id,
        access=owner.LiveBandwidthAccess(
            roles=frozenset({"admin"}),
            principal_type="system_user",
            owner_id=None,
        ),
    )


def _resolved_identity(
    subscriber_id: UUID,
    target: MikrotikLiveBandwidthTarget | None,
    *,
    configuration_error: str | None = None,
) -> network_identity.LiveBandwidthNetworkIdentity:
    return network_identity.LiveBandwidthNetworkIdentity(
        subscriber_id=subscriber_id,
        target=target,
        configuration_error=configuration_error,
    )


def test_network_identity_materializes_detached_probe_target(monkeypatch):
    subscription_id = uuid4()
    subscriber_id = uuid4()
    nas_device = SimpleNamespace(id=uuid4(), name="nas-1")
    subscription = SimpleNamespace(
        subscriber_id=subscriber_id,
        provisioning_nas_device=nas_device,
        login="pppoe1",
    )
    db = _Session(subscription)
    target = MikrotikLiveBandwidthTarget(
        device_id=nas_device.id,
        device_name=nas_device.name,
        host="192.0.2.1",
        port=8728,
        username="operator",
        password="secret",
        login=subscription.login,
    )
    monkeypatch.setattr(
        network_identity,
        "build_mikrotik_live_bandwidth_target",
        lambda *_args, **_kwargs: target,
    )

    resolved = network_identity.live_bandwidth_identity_for_subscription(
        db,
        subscription_id,
    )

    assert resolved == _resolved_identity(subscriber_id, target)


def test_direct_probe_releases_db_before_router_call(monkeypatch):
    subscription_id = uuid4()
    nas_id = uuid4()
    nas_device = SimpleNamespace(id=nas_id, name="nas-1")
    subscription = SimpleNamespace(
        subscriber_id=uuid4(),
        provisioning_nas_device=nas_device,
        login="pppoe1",
    )
    db = _Session(subscription)
    target = MikrotikLiveBandwidthTarget(
        device_id=nas_id,
        device_name="nas-1",
        host="192.0.2.1",
        port=8728,
        username="operator",
        password="secret",
        login="pppoe1",
    )
    assert "secret" not in repr(target)
    monkeypatch.setattr(
        owner,
        "live_bandwidth_identity_for_subscription",
        lambda *_args, **_kwargs: _resolved_identity(
            subscription.subscriber_id,
            target,
        ),
    )
    monkeypatch.setattr(owner, "_claim_direct_probe", lambda _id: (object(), "token"))
    monkeypatch.setattr(owner, "_release_direct_probe", lambda *_args: None)

    def transport(observed_target):
        assert db.committed is True
        assert observed_target is target
        return {
            "online": True,
            "nas_device_id": str(nas_id),
            "nas_device_name": "nas-1",
            "timestamp": datetime.now(UTC).isoformat(),
            "current_rx_bps": 10,
            "current_tx_bps": 20,
            "download_bps": 20,
            "upload_bps": 10,
            # Transport details must not cross the typed public outcome.
            "login": "pppoe1",
            "framed_ip_address": "192.0.2.20",
            "caller_id": "sensitive",
        }

    result = owner.probe_live_bandwidth(
        db,
        _admin_query(subscription_id),
        transport=transport,
    )

    assert result.online is True
    assert result.download_bps == 20
    assert "login" not in result.model_dump()
    assert "framed_ip_address" not in result.model_dump()
    assert "caller_id" not in result.model_dump()


def test_direct_probe_missing_nas_fails_with_domain_error(monkeypatch):
    subscription = SimpleNamespace(
        subscriber_id=uuid4(), provisioning_nas_device=None, login="x"
    )
    db = _Session(subscription)
    monkeypatch.setattr(
        owner,
        "live_bandwidth_identity_for_subscription",
        lambda *_args, **_kwargs: _resolved_identity(
            subscription.subscriber_id,
            None,
            configuration_error="missing_nas",
        ),
    )

    with pytest.raises(owner.LiveBandwidthConfigurationError):
        owner.probe_live_bandwidth(db, _admin_query(uuid4()))


def test_direct_probe_fails_closed_when_another_probe_holds_claim(monkeypatch):
    subscription_id = uuid4()
    nas_id = uuid4()
    subscription = SimpleNamespace(
        subscriber_id=uuid4(),
        provisioning_nas_device=SimpleNamespace(id=nas_id, name="nas-1"),
        login="pppoe1",
    )
    db = _Session(subscription)
    target = MikrotikLiveBandwidthTarget(
        device_id=nas_id,
        device_name="nas-1",
        host="192.0.2.1",
        port=8728,
        username="operator",
        password="secret",
        login="pppoe1",
    )
    monkeypatch.setattr(
        owner,
        "live_bandwidth_identity_for_subscription",
        lambda *_args, **_kwargs: _resolved_identity(
            subscription.subscriber_id,
            target,
        ),
    )
    monkeypatch.setattr(
        owner,
        "_claim_direct_probe",
        lambda _id: (_ for _ in ()).throw(owner.LiveBandwidthProbeBusy()),
    )

    called = False

    def transport(_target):
        nonlocal called
        called = True
        return {}

    with pytest.raises(owner.LiveBandwidthProbeBusy):
        owner.probe_live_bandwidth(
            db,
            _admin_query(subscription_id),
            transport=transport,
        )
    assert called is False
