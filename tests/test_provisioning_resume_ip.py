"""Regression tests for IP re-provisioning on subscription resume.

Reactivation-on-payment emits ``subscription_resumed`` (not
``subscription_activated``). The ProvisioningHandler previously ignored it, so
the IPv4 assignment + ``subscriptions.ipv4_address`` were never restored and the
RADIUS refresh dropped Framed-IP-Address → BNG teardown ("paid -> went offline").
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.catalog import SubscriptionStatus
from app.services import provisioning as provisioning_service
from app.services.events.handlers.provisioning import ProvisioningHandler
from app.services.events.types import Event, EventType


def test_resume_event_triggers_ip_reprovision(db_session, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        provisioning_service,
        "ensure_ipv4_assignment_for_subscription",
        lambda db, subscription_id: calls.append(subscription_id),
    )
    event = Event(
        event_type=EventType.subscription_resumed,
        payload={"subscription_id": "sub-123"},
    )

    ProvisioningHandler().handle(db_session, event)

    assert calls == ["sub-123"]


def test_activated_event_still_reprovisions(db_session, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        provisioning_service,
        "ensure_ipv4_assignment_for_subscription",
        lambda db, subscription_id: calls.append(subscription_id),
    )
    # The activation path does more (radius/NAS); stub those so the test
    # isolates the IP step.
    monkeypatch.setattr(
        ProvisioningHandler, "_sync_radius_on_activation", lambda self, db, sid: None
    )
    monkeypatch.setattr(
        ProvisioningHandler, "_push_nas_provisioning", lambda self, db, sid: None
    )
    event = Event(
        event_type=EventType.subscription_activated,
        payload={"subscription_id": "sub-act"},
    )

    ProvisioningHandler().handle(db_session, event)

    assert calls == ["sub-act"]


# --- populate Framed-IP fallback guard ---


def _attrs(ipv4_on_sub, framed_ipv4):
    from app.services.radius_population import _radreply_attrs

    sub = SimpleNamespace(ipv4_address=ipv4_on_sub, status=SubscriptionStatus.active)
    offer = SimpleNamespace(name="O", speed_download_mbps=None, speed_upload_mbps=None)
    return _radreply_attrs(sub, offer, None, False, None, framed_ipv4=framed_ipv4)


def _framed_ip(attrs):
    return next((v for (a, _op, v) in attrs if a == "Framed-IP-Address"), None)


def test_framed_ip_fallback_used_when_subscription_ipv4_stale():
    # subscriptions.ipv4_address is cleared, but an active IPAssignment address is
    # passed as the fallback → Framed-IP must still be emitted (no de-IP).
    assert _framed_ip(_attrs(None, "10.0.0.5")) == "10.0.0.5"


def test_framed_ip_dropped_only_when_truly_no_ip():
    assert _framed_ip(_attrs(None, None)) is None


def test_zero_address_never_emitted_as_framed_ip():
    # "0.0.0.0" (the stale-reactivation artifact) must not become a bogus Framed-IP.
    assert _framed_ip(_attrs("0.0.0.0", None)) is None
    assert _framed_ip(_attrs(None, "0.0.0.0")) is None
