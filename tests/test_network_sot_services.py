from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.radius import RadiusClient
from app.models.radius_active_session import RadiusActiveSession
from app.models.usage import AccountingStatus, RadiusAccountingSession
from app.services.network.access_path import summarize_subscription_access_path
from app.services.network.identity import identity_for_subscription
from app.services.network.radius_sessions import (
    SubscriptionSessionBinding,
    SubscriptionSessionState,
    active_session_count_for_subscriber,
    latest_accounting_observation_at,
    latest_open_accounting_session_for_subscription,
    latest_open_accounting_sessions_by_subscription,
    live_framed_ips_by_subscription,
    live_nas_device_ids_for_subscription,
    recent_nas_history_by_subscription,
    resolve_subscriber_radius_sessions,
    subscription_session_snapshots,
)
from app.services.sot_registry.registry import (
    dependencies_for,
    service_names_for_domain,
)


def _nas_node(db_session, *, pop: PopSite | None = None):
    nas = NasDevice(name=f"NAS-{uuid.uuid4().hex[:6]}", management_ip="10.0.0.1")
    db_session.add(nas)
    db_session.flush()
    node = NetworkDevice(
        name="NAS Node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        pop_site_id=pop.id if pop is not None else None,
        is_active=True,
        ping_enabled=True,
        mgmt_ip="10.0.0.1",
        live_status="up",
    )
    db_session.add(node)
    db_session.flush()
    return nas, node


def test_network_sot_relationships_come_from_the_canonical_graph():
    names = service_names_for_domain("network")

    assert names[0] == "network.identity"
    assert {
        "network.access_path",
        "network.nas_lifecycle",
        "network.outage_lifecycle",
    } <= set(names)
    assert dependencies_for("network.access_path") == (
        "network.identity",
        "network.fiber_topology",
        "network.ont_assignment_commands",
        "network.ont_assignment_identity",
        "network.fiber_access_attachments",
        "network.fiber_physical_continuity",
        "network.forwarding_topology",
    )
    assert dependencies_for("network.nas_lifecycle") == (
        "network.identity",
        "network.access_path",
        "network.radius_sessions",
        "network.nas_inventory",
        "service_intent.subscription_nas_assignment",
        "access.radius_state",
        "runtime.db_sessions",
        "observability.recording",
    )
    assert dependencies_for("network.outage_lifecycle") == (
        "network.outage_impact",
        "events.dispatcher",
        "events.owner_outputs",
        "operations.sla_escalation",
        "support.ticket_lifecycle",
    )


def test_identity_and_access_path_resolve_subscription_nas_node(
    db_session, subscriber, catalog_offer
):
    pop = PopSite(name="POP 1", code="POP-SOT")
    db_session.add(pop)
    db_session.flush()
    nas, node = _nas_node(db_session, pop=pop)
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=nas.id,
    )
    db_session.add(subscription)
    db_session.flush()

    summary = summarize_subscription_access_path(db_session, subscription)
    identity = identity_for_subscription(db_session, subscription)

    assert summary.access_kind == "nas"
    assert summary.node_id == node.id
    assert summary.basestation_id == pop.id
    assert identity is not None
    assert identity.kind == "nas"
    assert identity.network_device == node
    assert identity.pop_site == pop


def test_radius_session_resolution_uses_freshest_session(
    db_session, subscriber, catalog_offer
):
    nas, _node = _nas_node(db_session)
    older = RadiusActiveSession(
        subscriber_id=subscriber.id,
        username="sot-user",
        acct_session_id="older-session",
        nas_device_id=nas.id,
        session_start=datetime.now(UTC) - timedelta(hours=2),
        last_update=datetime.now(UTC) - timedelta(minutes=10),
    )
    newer = RadiusActiveSession(
        subscriber_id=subscriber.id,
        username="sot-user",
        acct_session_id="newer-session",
        nas_device_id=nas.id,
        session_start=datetime.now(UTC) - timedelta(hours=1),
        last_update=datetime.now(UTC),
    )
    db_session.add_all([older, newer])
    db_session.flush()

    resolution = resolve_subscriber_radius_sessions(db_session, subscriber.id)

    assert resolution.is_online is True
    assert resolution.primary_session == newer
    assert resolution.primary_identity is not None
    assert resolution.primary_identity.kind == "nas"
    assert active_session_count_for_subscriber(db_session, subscriber.id) == 2


def test_live_nas_evidence_can_require_exact_subscription_binding(
    db_session, subscriber, catalog_offer
):
    first_nas, _ = _nas_node(db_session)
    second_nas = NasDevice(name="NAS-SECOND", management_ip="10.0.0.2")
    db_session.add(second_nas)
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(subscription)
    db_session.flush()
    db_session.add_all(
        [
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,
                username="bound",
                acct_session_id="bound-session",
                nas_device_id=first_nas.id,
                session_start=datetime.now(UTC),
            ),
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                subscription_id=None,
                username="unbound",
                acct_session_id="unbound-session",
                nas_device_id=second_nas.id,
                session_start=datetime.now(UTC),
            ),
        ]
    )
    db_session.flush()

    assert live_nas_device_ids_for_subscription(
        db_session,
        subscription.id,
        subscription.subscriber_id,
        allow_unbound=False,
    ) == (first_nas.id,)
    assert set(
        live_nas_device_ids_for_subscription(
            db_session,
            subscription.id,
            subscription.subscriber_id,
        )
    ) == {
        first_nas.id,
        second_nas.id,
    }


def test_subscription_session_snapshot_does_not_leak_unbound_session_to_siblings(
    db_session, subscriber, catalog_offer
):
    first = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    second = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add_all([first, second])
    db_session.flush()
    db_session.add(
        RadiusActiveSession(
            subscriber_id=subscriber.id,
            subscription_id=None,
            username="ambiguous-unbound",
            acct_session_id="ambiguous-unbound",
            session_start=datetime.now(UTC),
            last_update=datetime.now(UTC),
            framed_ip_address="10.0.0.50",
        )
    )
    db_session.flush()

    snapshots = subscription_session_snapshots(db_session, [first, second])

    assert snapshots[first.id].state == SubscriptionSessionState.offline
    assert snapshots[second.id].state == SubscriptionSessionState.offline
    assert snapshots[first.id].binding == SubscriptionSessionBinding.none
    assert snapshots[second.id].binding == SubscriptionSessionBinding.none


def test_subscription_session_snapshot_prefers_exact_bound_session(
    db_session, subscriber, catalog_offer
):
    first = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    second = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add_all([first, second])
    db_session.flush()
    db_session.add(
        RadiusActiveSession(
            subscriber_id=subscriber.id,
            subscription_id=first.id,
            username="first-bound",
            acct_session_id="first-bound",
            session_start=datetime.now(UTC),
            last_update=datetime.now(UTC),
            framed_ip_address="10.0.0.51",
        )
    )
    db_session.flush()

    snapshots = subscription_session_snapshots(db_session, [first, second])

    assert snapshots[first.id].state == SubscriptionSessionState.connected
    assert snapshots[first.id].binding == (
        SubscriptionSessionBinding.exact_subscription
    )
    assert snapshots[first.id].framed_ip_address == "10.0.0.51"
    assert snapshots[second.id].state == SubscriptionSessionState.offline


def test_open_accounting_session_helpers_use_newest_non_stop_session(
    db_session, subscriber, catalog_offer
):
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(subscription)
    db_session.flush()
    older = RadiusAccountingSession(
        subscription_id=subscription.id,
        session_id="older",
        status_type=AccountingStatus.interim,
        session_start=datetime.now(UTC) - timedelta(hours=2),
        last_update_at=datetime.now(UTC) - timedelta(minutes=30),
        framed_ip_address="10.0.0.10",
    )
    newer = RadiusAccountingSession(
        subscription_id=subscription.id,
        session_id="newer",
        status_type=AccountingStatus.interim,
        session_start=datetime.now(UTC) - timedelta(hours=1),
        last_update_at=datetime.now(UTC),
        framed_ip_address="10.0.0.11",
    )
    stopped = RadiusAccountingSession(
        subscription_id=subscription.id,
        session_id="stopped",
        status_type=AccountingStatus.stop,
        session_start=datetime.now(UTC) - timedelta(minutes=5),
        last_update_at=datetime.now(UTC) + timedelta(minutes=1),
        session_end=datetime.now(UTC),
        framed_ip_address="10.0.0.12",
    )
    db_session.add_all([older, newer, stopped])
    db_session.flush()

    assert (
        latest_open_accounting_session_for_subscription(db_session, subscription.id)
        == newer
    )
    assert latest_open_accounting_sessions_by_subscription(
        db_session, [subscription.id]
    ) == {subscription.id: newer}
    assert live_framed_ips_by_subscription(db_session, [subscription.id]) == {
        subscription.id: "10.0.0.11"
    }


def test_recent_nas_history_prefers_direct_identity_and_falls_back_to_client(
    db_session, subscriber, catalog_offer, radius_server
):
    direct_nas = NasDevice(name="Direct accounting NAS", nas_ip="10.60.0.1")
    fallback_nas = NasDevice(name="Client-linked NAS", nas_ip="10.60.0.2")
    subscription = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add_all([direct_nas, fallback_nas, subscription])
    db_session.flush()
    client = RadiusClient(
        server_id=radius_server.id,
        nas_device_id=fallback_nas.id,
        client_ip="10.60.0.2",
        shared_secret_hash="hash",
        is_active=True,
    )
    db_session.add(client)
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add_all(
        [
            RadiusAccountingSession(
                subscription_id=subscription.id,
                nas_device_id=direct_nas.id,
                radius_client_id=client.id,
                session_id="direct-wins",
                status_type=AccountingStatus.stop,
                session_start=now - timedelta(hours=2),
                session_end=now - timedelta(hours=1),
                last_update_at=now - timedelta(hours=1),
            ),
            RadiusAccountingSession(
                subscription_id=subscription.id,
                radius_client_id=client.id,
                session_id="client-fallback",
                status_type=AccountingStatus.stop,
                session_start=now - timedelta(minutes=40),
                session_end=now - timedelta(minutes=30),
                last_update_at=now - timedelta(minutes=30),
            ),
        ]
    )
    db_session.flush()

    history = recent_nas_history_by_subscription(
        db_session,
        [subscription.id],
        since=now - timedelta(days=1),
    )

    assert {target.nas_device_id for target in history[subscription.id].targets} == {
        direct_nas.id,
        fallback_nas.id,
    }
    assert all(target.session_count == 1 for target in history[subscription.id].targets)
    assert latest_accounting_observation_at(db_session) == now - timedelta(minutes=30)
