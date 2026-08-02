"""Topology-gaps report + match-rate (Phase 1, Task 8)."""

from __future__ import annotations

import uuid

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import OLTDevice, OntAssignment, OntUnit
from app.models.network_monitoring import NetworkDevice, PopSite
from app.models.subscriber import Subscriber
from app.services.topology.gaps import topology_gaps


def _sub(subscriber_id, offer_id):
    return Subscription(
        subscriber_id=subscriber_id,
        offer_id=offer_id,
        status=SubscriptionStatus.active,
    )


def test_topology_gaps_counts_and_match_rate(db_session, subscriber, catalog_offer):
    # --- Complete-path subscriber (fiber): ONT -> OLT -> node -> pop_site ---
    olt = OLTDevice(name="OLT-1", hostname="olt1", mgmt_ip="10.0.0.1")
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add_all([olt, pop])
    db_session.flush()
    db_session.add(
        NetworkDevice(
            name="olt1-node",
            source="zabbix_reconcile",
            matched_device_type="olt",
            matched_device_id=olt.id,
            pop_site_id=pop.id,
            zabbix_hostid="201",
        )
    )
    ont = OntUnit(serial_number="SN-1", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    complete_sub = _sub(subscriber.id, catalog_offer.id)

    # --- Gappy subscriber: no ONT, no NAS ---
    other = Subscriber(
        first_name="No", last_name="Path", email=f"nopath-{uuid.uuid4().hex}@ex.com"
    )
    db_session.add(other)
    db_session.flush()
    gappy_sub = _sub(other.id, catalog_offer.id)

    # --- An unmatched Zabbix node (reconciled, no provisioning match) ---
    ghost = NetworkDevice(
        name="ghost-host",
        source="zabbix_reconcile",
        matched_device_id=None,
        mgmt_ip="192.0.2.9",
        zabbix_hostid="999",
        is_active=True,
    )
    db_session.add_all([complete_sub, gappy_sub, ghost])
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.unmatched_node_count == 1
    assert gaps.unmatched_nodes[0].name == "ghost-host"
    assert gaps.active_subscriptions == 2
    assert gaps.resolved_complete == 1
    assert gaps.subscription_gap_count == 1
    assert gaps.subscription_gaps[0]["id"] == gappy_sub.id
    assert gaps.match_rate == 0.5


# --- Wireless arm: gaps stays in sync with resolve_customer_path's radio arm ---


def _wireless_setup(db_session, subscriber_id, uisp_status="active", pop=True):
    from app.models.network import CPEDevice

    pop_site = None
    if pop:
        pop_site = PopSite(name=f"BTS-{uuid.uuid4().hex[:6]}", zabbix_group_id="40")
        db_session.add(pop_site)
        db_session.flush()
    ap = NetworkDevice(
        name=f"ap-{uuid.uuid4().hex[:6]}",
        pop_site_id=pop_site.id if pop_site else None,
        uisp_device_id=f"uisp-{uuid.uuid4().hex[:6]}",
    )
    db_session.add(ap)
    db_session.flush()
    db_session.add(
        CPEDevice(
            subscriber_id=subscriber_id,
            parent_network_device_id=ap.id,
            last_uisp_status=uisp_status,
        )
    )
    db_session.flush()


def test_wireless_only_subscriber_is_not_a_gap(db_session, subscriber, catalog_offer):
    # Radio -> AP -> pop_site: complete path, no gap, counted in match-rate.
    _wireless_setup(db_session, subscriber.id)
    db_session.add(_sub(subscriber.id, catalog_offer.id))
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.active_subscriptions == 1
    assert gaps.subscription_gap_count == 0
    assert gaps.resolved_complete == 1
    assert gaps.match_rate == 1.0


def test_vanished_cpe_only_subscriber_is_still_a_gap(
    db_session, subscriber, catalog_offer
):
    # A vanished radio does not resolve a path (mirrors resolve_customer_path,
    # which skips it and, with no NAS, lands on GAP_NO_ONT).
    _wireless_setup(db_session, subscriber.id, uisp_status="vanished")
    db_session.add(_sub(subscriber.id, catalog_offer.id))
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.subscription_gap_count == 1
    assert gaps.subscription_gaps[0]["gap"] == "no_ont"


def test_wireless_subscriber_ap_without_basestation_is_gap_no_basestation(
    db_session, subscriber, catalog_offer
):
    _wireless_setup(db_session, subscriber.id, pop=False)
    db_session.add(_sub(subscriber.id, catalog_offer.id))
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.subscription_gap_count == 1
    assert gaps.subscription_gaps[0]["gap"] == "no_basestation"


# --- Live-session arm: batched classifier stays in sync with resolve_customer_path ---


def _nas_node(db, nas_id, pop_id, hostid):
    db.add(
        NetworkDevice(
            name=f"nas-node-{hostid}",
            source="zabbix_reconcile",
            matched_device_type="nas",
            matched_device_id=nas_id,
            pop_site_id=pop_id,
            zabbix_hostid=hostid,
        )
    )
    db.flush()


def _live_session(db, subscription, nas_device_id):
    from datetime import UTC, datetime

    from app.models.radius_active_session import RadiusActiveSession

    ras = RadiusActiveSession(
        subscriber_id=subscription.subscriber_id,
        subscription_id=subscription.id,
        nas_device_id=nas_device_id,
        username="u",
        acct_session_id=uuid.uuid4().hex,
        session_start=datetime.now(UTC),
    )
    db.add(ras)
    db.flush()
    return ras


def test_live_session_only_subscriber_is_not_a_gap(
    db_session, subscriber, catalog_offer
):
    # A sub with NO provisioning NAS, resolvable ONLY via a live session, must
    # count as resolved in the batched classifier (mirrors resolve_customer_path
    # now resolving it via the live NAS).
    from app.models.catalog import NasDevice
    from app.services.topology.gaps import classify_active_subscriptions

    nas = NasDevice(name="NAS-Live-Gaps", management_ip="10.0.11.1")
    pop = PopSite(name="Live Gaps Site", zabbix_group_id="50")
    db_session.add_all([nas, pop])
    db_session.flush()
    _nas_node(db_session, nas.id, pop.id, "501")
    sub = _sub(subscriber.id, catalog_offer.id)  # provisioning_nas is None
    db_session.add(sub)
    db_session.flush()
    _live_session(db_session, sub, nas.id)

    rows = {r["id"]: r for r in classify_active_subscriptions(db_session)}
    assert rows[sub.id]["gap"] is None
    assert rows[sub.id]["medium"] == "nas"


def test_no_session_nas_subscriber_unchanged(db_session, subscriber, catalog_offer):
    # Behavior for a NAS sub with no live session is unchanged: it still
    # resolves via the static provisioning NAS.
    from app.models.catalog import NasDevice
    from app.services.topology.gaps import classify_active_subscriptions

    nas = NasDevice(name="NAS-Static-Gaps", management_ip="10.0.11.2")
    pop = PopSite(name="Static Gaps Site", zabbix_group_id="51")
    db_session.add_all([nas, pop])
    db_session.flush()
    _nas_node(db_session, nas.id, pop.id, "502")
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=nas.id,
    )
    db_session.add(sub)
    db_session.flush()

    rows = {r["id"]: r for r in classify_active_subscriptions(db_session)}
    assert rows[sub.id]["gap"] is None
    assert rows[sub.id]["medium"] == "nas"


def test_live_session_unresolvable_falls_back_to_static_in_classifier(
    db_session, subscriber, catalog_offer
):
    # Live session on an unmatched NAS (no node) but a resolvable static
    # provisioning NAS: the batched classifier must fall back to static (gap
    # None), matching resolve_customer_path's fix, not mark a spurious gap.
    from app.models.catalog import NasDevice
    from app.services.topology.gaps import classify_active_subscriptions

    static_nas = NasDevice(name="NAS-Static-OK-G", management_ip="10.0.11.3")
    unmatched_nas = NasDevice(name="NAS-Unmatched-G", management_ip="10.0.11.4")
    pop = PopSite(name="Static OK Gaps Site", zabbix_group_id="52")
    db_session.add_all([static_nas, unmatched_nas, pop])
    db_session.flush()
    _nas_node(db_session, static_nas.id, pop.id, "503")  # only static has a node
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=static_nas.id,
    )
    db_session.add(sub)
    db_session.flush()
    _live_session(db_session, sub, unmatched_nas.id)

    rows = {r["id"]: r for r in classify_active_subscriptions(db_session)}
    assert rows[sub.id]["gap"] is None
    assert rows[sub.id]["medium"] == "nas"


def test_batched_classifier_excludes_sibling_subscription_session(
    db_session, subscriber, catalog_offer
):
    # The invariant that regressed in #848, guarded through the BATCHED path:
    # subscriber owns sub A (owns a live session on a matched NAS) + sub B (no
    # own session, no provisioning NAS). The batched classifier must NOT credit
    # B with A's live NAS — B has no access device and gaps as GAP_NO_ONT.
    from app.models.catalog import NasDevice
    from app.services.topology.gaps import classify_active_subscriptions

    a_live_nas = NasDevice(name="NAS-A-Live-G", management_ip="10.0.11.5")
    a_pop = PopSite(name="A Live Gaps Site", zabbix_group_id="53")
    db_session.add_all([a_live_nas, a_pop])
    db_session.flush()
    _nas_node(db_session, a_live_nas.id, a_pop.id, "504")
    sub_a = _sub(subscriber.id, catalog_offer.id)  # owns the session
    db_session.add(sub_a)
    db_session.flush()
    _live_session(db_session, sub_a, a_live_nas.id)
    sub_b = _sub(subscriber.id, catalog_offer.id)  # same subscriber, no session/NAS
    db_session.add(sub_b)
    db_session.flush()

    rows = {r["id"]: r for r in classify_active_subscriptions(db_session)}
    assert rows[sub_a.id]["gap"] is None  # A resolves via its own live NAS
    # B must NOT borrow A's session -> no resolvable access device at all.
    assert rows[sub_b.id]["gap"] == "no_ont"
    assert rows[sub_b.id]["medium"] == "unknown"


def test_batched_picker_prefers_own_bound_over_null_bound(
    db_session, subscriber, catalog_offer
):
    # Batched mirror of test_own_bound_session_beats_null_bound: own-binding is
    # the primary key ahead of freshness, so a fresher null-bound session must
    # not preempt the subscription's own. Exercises _live_nas_by_subscription
    # directly (the Python 0/1 ranking that mirrors the SQL CASE).
    from datetime import UTC, datetime

    from app.models.catalog import NasDevice
    from app.models.radius_active_session import RadiusActiveSession
    from app.services.topology.gaps import _live_nas_by_subscription

    own_nas = NasDevice(name="NAS-Own-B", management_ip="10.0.12.3")
    null_nas = NasDevice(name="NAS-Null-B", management_ip="10.0.12.4")
    db_session.add_all([own_nas, null_nas])
    db_session.flush()
    sub = _sub(subscriber.id, catalog_offer.id)
    db_session.add(sub)
    db_session.flush()
    db_session.add_all(
        [
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                subscription_id=sub.id,  # own binding, OLDER
                nas_device_id=own_nas.id,
                username="u",
                acct_session_id="own-b",
                session_start=datetime(2026, 6, 1, tzinfo=UTC),
                last_update=datetime(2026, 6, 1, tzinfo=UTC),
            ),
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                subscription_id=None,  # null binding, FRESHER
                nas_device_id=null_nas.id,
                username="u",
                acct_session_id="null-b",
                session_start=datetime(2026, 7, 1, tzinfo=UTC),
                last_update=datetime(2026, 7, 1, tzinfo=UTC),
            ),
        ]
    )
    db_session.flush()

    picked = _live_nas_by_subscription(db_session, [sub], {subscriber.id})
    assert picked[sub.id] == own_nas.id  # own beats fresher null-bound


def test_wireless_classifier_pins_radio_per_subscription(
    db_session, subscriber, catalog_offer
):
    # Lockstep with customer_path._active_wireless_cpe's exact-first tiers: a
    # multi-service subscriber with one radio per subscription must have each
    # subscription judged by ITS OWN radio — not by whichever radio the
    # subscriber-level ordering happens to rank first.
    from datetime import UTC, datetime

    from app.models.network import CPEDevice

    sub_complete = _sub(subscriber.id, catalog_offer.id)
    sub_partial = _sub(subscriber.id, catalog_offer.id)
    db_session.add_all([sub_complete, sub_partial])
    db_session.flush()

    pop_site = PopSite(name=f"BTS-{uuid.uuid4().hex[:6]}", zabbix_group_id="41")
    db_session.add(pop_site)
    db_session.flush()
    ap_with_pop = NetworkDevice(
        name=f"ap-pop-{uuid.uuid4().hex[:6]}", pop_site_id=pop_site.id
    )
    ap_without_pop = NetworkDevice(name=f"ap-nopop-{uuid.uuid4().hex[:6]}")
    db_session.add_all([ap_with_pop, ap_without_pop])
    db_session.flush()

    # The PARTIAL subscription's radio synced later: subscriber-level ordering
    # alone would judge BOTH subscriptions by it.
    db_session.add_all(
        [
            CPEDevice(
                subscriber_id=subscriber.id,
                subscription_id=sub_complete.id,
                parent_network_device_id=ap_with_pop.id,
                last_uisp_status="active",
                uisp_synced_at=datetime(2026, 6, 1, tzinfo=UTC),
            ),
            CPEDevice(
                subscriber_id=subscriber.id,
                subscription_id=sub_partial.id,
                parent_network_device_id=ap_without_pop.id,
                last_uisp_status="active",
                uisp_synced_at=datetime(2026, 7, 1, tzinfo=UTC),
            ),
        ]
    )
    db_session.flush()

    gaps = topology_gaps(db_session)

    assert gaps.active_subscriptions == 2
    # The subscription behind the pop-less AP is the only gap...
    assert gaps.subscription_gap_count == 1
    assert {str(row["id"]) for row in gaps.subscription_gaps} == {str(sub_partial.id)}
    # ...and the complete one resolved through ITS radio despite being older.
    assert gaps.resolved_complete == 1
