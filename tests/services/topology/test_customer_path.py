"""resolve_customer_path: ONT -> access device -> basestation (Phase 1, Task 6)."""

from __future__ import annotations

from app.models.catalog import NasDevice
from app.models.network import (
    FdhCabinet,
    OLTDevice,
    OntAssignment,
    OntUnit,
    PonPort,
    PonPortSplitterLink,
    Splitter,
    SplitterPort,
    SplitterPortAssignment,
)
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services.topology.customer_path import (
    GAP_NO_NODE,
    GAP_NO_ONT,
    resolve_customer_path,
)


def _node(matched_type, device_id, pop_site_id, hostid):
    return NetworkDevice(
        name=f"{matched_type}-node-{hostid}",
        matched_device_type=matched_type,
        matched_device_id=device_id,
        pop_site_id=pop_site_id,
        zabbix_hostid=hostid,
    )


def test_fiber_happy_path(db_session, subscriber, subscription):
    olt = OLTDevice(name="OLT-1", hostname="olt1", mgmt_ip="10.0.0.1")
    pop = PopSite(name="Garki", zabbix_group_id="10")
    db_session.add_all([olt, pop])
    db_session.flush()
    db_session.add(_node("olt", olt.id, pop.id, "201"))
    ont = OntUnit(serial_number="SN-123", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)
    assert path.gap is None
    assert path.access_device_kind == "olt"
    assert path.access_device.id == olt.id
    assert path.ont.id == ont.id
    assert path.basestation.id == pop.id


def test_fiber_path_includes_physical_plant(db_session, subscriber, subscription):
    olt = OLTDevice(name="OLT-Plant", hostname="olt-plant", mgmt_ip="10.0.0.10")
    pop = PopSite(name="Gudu", zabbix_group_id="12")
    fdh = FdhCabinet(name="FDH Alpha", code="FDH-A")
    splitter = Splitter(name="SPL-A", fdh=fdh, splitter_ratio="1:8")
    db_session.add_all([olt, pop, fdh, splitter])
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/2")
    splitter_port = SplitterPort(splitter_id=splitter.id, port_number=8)
    db_session.add_all([pon, splitter_port])
    db_session.flush()
    db_session.add(_node("olt", olt.id, pop.id, "203"))
    ont = OntUnit(
        serial_number="SN-PLANT",
        olt_device_id=olt.id,
        pon_port_id=pon.id,
        splitter_port_id=splitter_port.id,
    )
    db_session.add(ont)
    db_session.flush()
    assignment = OntAssignment(
        ont_unit_id=ont.id,
        pon_port_id=pon.id,
        subscriber_id=subscriber.id,
        active=True,
    )
    db_session.add(assignment)
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.ont_assignment.id == assignment.id
    assert path.pon_port.id == pon.id
    assert path.splitter_port.id == splitter_port.id
    assert path.splitter.id == splitter.id
    assert path.fdh.id == fdh.id
    assert path.access_device.id == olt.id


def test_fiber_path_uses_assignment_and_pon_splitter_fallbacks(
    db_session, subscriber, subscription
):
    olt = OLTDevice(name="OLT-Fallback", hostname="olt-fallback", mgmt_ip="10.0.0.11")
    pop = PopSite(name="Jabi", zabbix_group_id="13")
    fdh = FdhCabinet(name="FDH Beta", code="FDH-B")
    splitter = Splitter(name="SPL-B", fdh=fdh, splitter_ratio="1:8")
    db_session.add_all([olt, pop, fdh, splitter])
    db_session.flush()
    pon = PonPort(olt_id=olt.id, name="0/1/3")
    splitter_port = SplitterPort(splitter_id=splitter.id, port_number=4)
    db_session.add_all([pon, splitter_port])
    db_session.flush()
    db_session.add_all(
        [
            _node("olt", olt.id, pop.id, "204"),
            PonPortSplitterLink(pon_port_id=pon.id, splitter_port_id=splitter_port.id),
        ]
    )
    ont = OntUnit(serial_number="SN-FALLBACK")
    db_session.add(ont)
    db_session.flush()
    db_session.add_all(
        [
            OntAssignment(
                ont_unit_id=ont.id,
                pon_port_id=pon.id,
                subscriber_id=subscriber.id,
                active=True,
            ),
            SplitterPortAssignment(
                splitter_port_id=splitter_port.id,
                subscriber_id=subscriber.id,
                active=True,
            ),
        ]
    )
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.pon_port.id == pon.id
    assert path.splitter_port.id == splitter_port.id
    assert path.splitter.id == splitter.id
    assert path.fdh.id == fdh.id
    assert path.access_device.id == olt.id


def test_non_fiber_happy_path(db_session, subscription):
    nas = NasDevice(name="NAS-1", management_ip="10.0.0.5")
    pop = PopSite(name="Lekki", zabbix_group_id="11")
    db_session.add_all([nas, pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, pop.id, "202"))
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)
    assert path.gap is None
    assert path.access_device_kind == "nas"
    assert path.access_device.id == nas.id
    assert path.ont is None
    assert path.basestation.id == pop.id


def test_gap_no_ont_when_no_provisioning(db_session, subscription):
    # No ONT assignment and no provisioning NAS -> provisioning incomplete.
    path = resolve_customer_path(db_session, subscription)
    assert path.gap == GAP_NO_ONT
    assert path.access_device is None
    assert path.basestation is None


def test_gap_no_node_when_device_unmatched(db_session, subscriber, subscription):
    # Fiber ONT + OLT exist, but no NetworkDevice is matched to the OLT.
    olt = OLTDevice(name="OLT-2", hostname="olt2", mgmt_ip="10.0.0.2")
    db_session.add(olt)
    db_session.flush()
    ont = OntUnit(serial_number="SN-456", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)
    assert path.gap == GAP_NO_NODE
    assert path.access_device.id == olt.id
    assert path.node is None
    assert path.basestation is None


# --- Wireless arm: radio -> AP -> basestation (read-side of the UISP sync) ---


def _cpe(db, subscriber_id, parent_id, uisp_status="active", synced_at=None, **kw):
    from app.models.network import CPEDevice

    cpe = CPEDevice(
        subscriber_id=subscriber_id,
        parent_network_device_id=parent_id,
        last_uisp_status=uisp_status,
        uisp_synced_at=synced_at,
        **kw,
    )
    db.add(cpe)
    db.flush()
    return cpe


def _ap_node(db, name, pop_site_id=None, **kw):
    node = NetworkDevice(
        name=name, pop_site_id=pop_site_id, uisp_device_id=f"uisp-{name}", **kw
    )
    db.add(node)
    db.flush()
    return node


def test_wireless_happy_path_radio_ap_basestation(db_session, subscriber, subscription):
    from datetime import UTC, datetime

    from app.models.network_monitoring import DeviceRole
    from tests.services.topology.forwarding_test_support import (
        declare_forwarding_edge,
    )

    pop = PopSite(name="Karu BTS", zabbix_group_id="20")
    db_session.add(pop)
    db_session.flush()
    ap = _ap_node(db_session, "AP-Karu-Sector1", pop_site_id=pop.id)
    core = NetworkDevice(name="Core-1", role=DeviceRole.core, is_active=True)
    db_session.add(core)
    db_session.flush()
    declare_forwarding_edge(
        db_session,
        ap,
        core,
        downstream_role="access",
        upstream_role="core",
    )
    cpe = _cpe(
        db_session,
        subscriber.id,
        ap.id,
        synced_at=datetime(2026, 7, 1, tzinfo=UTC),
        model="LiteBeam 5AC",
    )

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.access_device_kind == "ap"
    assert path.radio.id == cpe.id
    assert path.access_device.id == ap.id
    assert path.node.id == ap.id
    assert path.basestation.id == pop.id
    assert [hop.id for hop in path.upstream_chain] == [core.id]


def test_wireless_beats_nas_fallback(db_session, subscriber, subscription):
    # A wireless subscriber whose PPPoE terminates on a NAS at the BTS: the
    # radio arm must win (finer: it pins the customer to the actual AP).
    nas = NasDevice(name="NAS-BTS", management_ip="10.0.0.9")
    nas_pop = PopSite(name="NAS Site", zabbix_group_id="21")
    ap_pop = PopSite(name="AP Site", zabbix_group_id="22")
    db_session.add_all([nas, nas_pop, ap_pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, nas_pop.id, "301"))
    ap = _ap_node(db_session, "AP-Fine", pop_site_id=ap_pop.id)
    _cpe(db_session, subscriber.id, ap.id)
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "ap"
    assert path.node.id == ap.id
    assert path.basestation.id == ap_pop.id


def test_fiber_precedence_over_wireless(db_session, subscriber, subscription):
    # An active ONT assignment implies fiber; the radio must not preempt it.
    olt = OLTDevice(name="OLT-W", hostname="olt-w", mgmt_ip="10.0.0.3")
    pop = PopSite(name="Fiber Site", zabbix_group_id="23")
    db_session.add_all([olt, pop])
    db_session.flush()
    db_session.add(_node("olt", olt.id, pop.id, "302"))
    ont = OntUnit(serial_number="SN-W", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    ap = _ap_node(db_session, "AP-Ignored")
    _cpe(db_session, subscriber.id, ap.id)
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "olt"
    assert path.radio is None


def test_nas_fallback_when_cpe_has_no_parent(db_session, subscriber, subscription):
    nas = NasDevice(name="NAS-F", management_ip="10.0.0.7")
    pop = PopSite(name="Fallback Site", zabbix_group_id="24")
    db_session.add_all([nas, pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, pop.id, "303"))
    _cpe(db_session, subscriber.id, None)  # CPE exists but no AP edge
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "nas"
    assert path.radio is None
    assert path.basestation.id == pop.id


def test_nas_fallback_when_cpe_vanished(db_session, subscriber, subscription):
    nas = NasDevice(name="NAS-V", management_ip="10.0.0.8")
    pop = PopSite(name="Vanish Site", zabbix_group_id="25")
    db_session.add_all([nas, pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, pop.id, "304"))
    ap = _ap_node(db_session, "AP-Gone")
    _cpe(db_session, subscriber.id, ap.id, uisp_status="vanished")
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "nas"
    assert path.radio is None


# --- Live-session arm: prefer where the customer is connected RIGHT NOW ---


def _session(db, subscription, nas_device_id, subscriber_id=None):
    from datetime import UTC, datetime

    from app.models.radius_active_session import RadiusActiveSession

    ras = RadiusActiveSession(
        subscriber_id=(
            subscriber_id
            if subscriber_id is not None
            else getattr(subscription, "subscriber_id", None)
        ),
        subscription_id=getattr(subscription, "id", None),
        nas_device_id=nas_device_id,
        username="u",
        acct_session_id="live-1",
        session_start=datetime.now(UTC),
    )
    db.add(ras)
    db.flush()
    return ras


def test_live_session_beats_provisioning_nas(db_session, subscriber, subscription):
    # Customer online on a NAS other than the one provisioned (roaming/failover):
    # the trace must land on the LIVE NAS's basestation, not the static one.
    prov_nas = NasDevice(name="NAS-Prov", management_ip="10.0.9.1")
    live_nas = NasDevice(name="NAS-Live", management_ip="10.0.9.2")
    prov_pop = PopSite(name="Prov Site", zabbix_group_id="30")
    live_pop = PopSite(name="Live Site", zabbix_group_id="31")
    db_session.add_all([prov_nas, live_nas, prov_pop, live_pop])
    db_session.flush()
    db_session.add_all(
        [
            _node("nas", prov_nas.id, prov_pop.id, "401"),
            _node("nas", live_nas.id, live_pop.id, "402"),
        ]
    )
    subscription.provisioning_nas_device_id = prov_nas.id
    db_session.flush()
    _session(db_session, subscription, live_nas.id)

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.access_device_kind == "nas"
    assert path.access_device.id == live_nas.id
    assert path.basestation.id == live_pop.id
    assert path.live_session is True


def test_live_session_unresolvable_falls_back_to_static(
    db_session, subscriber, subscription
):
    # Customer online on a NAS not yet Zabbix-matched (NasDevice row exists but
    # no topology node), while the static provisioning NAS resolves completely.
    # The live session must NOT regress the sub to a gap — resolve via static,
    # and the live_session marker reflects that static (not live) was used.
    static_nas = NasDevice(name="NAS-Static-OK", management_ip="10.0.9.7")
    unmatched_nas = NasDevice(name="NAS-Unmatched", management_ip="10.0.9.8")
    pop = PopSite(name="Static OK Site", zabbix_group_id="35")
    db_session.add_all([static_nas, unmatched_nas, pop])
    db_session.flush()
    # Only the static NAS has a topology node -> basestation.
    db_session.add(_node("nas", static_nas.id, pop.id, "406"))
    subscription.provisioning_nas_device_id = static_nas.id
    db_session.flush()
    _session(db_session, subscription, unmatched_nas.id)  # live on the unmatched NAS

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.access_device_kind == "nas"
    assert path.access_device.id == static_nas.id
    assert path.basestation.id == pop.id
    assert path.live_session is False


def test_sibling_subscription_session_is_excluded(db_session, subscriber, subscription):
    # The subscriber owns sub A (the `subscription` fixture, which OWNS the
    # session) and sub B (no own session). Resolving B must NOT borrow A's
    # session (duplicate-login case) — it falls back to B's static NAS.
    from app.models.catalog import Subscription, SubscriptionStatus

    a_live_nas = NasDevice(name="NAS-A-Live", management_ip="10.0.9.9")
    b_static_nas = NasDevice(name="NAS-B-Static", management_ip="10.0.10.1")
    a_pop = PopSite(name="A Live Site", zabbix_group_id="36")
    b_pop = PopSite(name="B Static Site", zabbix_group_id="37")
    db_session.add_all([a_live_nas, b_static_nas, a_pop, b_pop])
    db_session.flush()
    db_session.add_all(
        [
            _node("nas", a_live_nas.id, a_pop.id, "407"),
            _node("nas", b_static_nas.id, b_pop.id, "408"),
        ]
    )
    # The session belongs to sub A on a_live_nas.
    _session(db_session, subscription, a_live_nas.id)
    # Sub B: same subscriber, own static provisioning NAS, no session of its own.
    sub_b = Subscription(
        subscriber_id=subscriber.id,
        offer_id=subscription.offer_id,
        status=SubscriptionStatus.active,
        provisioning_nas_device_id=b_static_nas.id,
    )
    db_session.add(sub_b)
    db_session.flush()

    path = resolve_customer_path(db_session, sub_b)

    assert path.access_device_kind == "nas"
    assert path.access_device.id == b_static_nas.id  # B's static NAS, not A's live
    assert path.basestation.id == b_pop.id
    assert path.live_session is False


def test_own_bound_session_beats_null_bound(db_session, subscriber, subscription):
    # Own-binding is the PRIMARY tie-break, ahead of freshness: even a FRESHER
    # null-bound session must not preempt this subscription's own session. Locks
    # the CASE order-by against the Postgres NULLS-FIRST-under-DESC quirk.
    from datetime import UTC, datetime

    from app.models.radius_active_session import RadiusActiveSession

    own_nas = NasDevice(name="NAS-Own", management_ip="10.0.12.1")
    null_nas = NasDevice(name="NAS-Null", management_ip="10.0.12.2")
    own_pop = PopSite(name="Own Site", zabbix_group_id="38")
    null_pop = PopSite(name="Null Site", zabbix_group_id="39")
    db_session.add_all([own_nas, null_nas, own_pop, null_pop])
    db_session.flush()
    db_session.add_all(
        [
            _node("nas", own_nas.id, own_pop.id, "409"),
            _node("nas", null_nas.id, null_pop.id, "410"),
        ]
    )
    db_session.add_all(
        [
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                subscription_id=subscription.id,  # own binding, but OLDER
                nas_device_id=own_nas.id,
                username="u",
                acct_session_id="own-1",
                session_start=datetime(2026, 6, 1, tzinfo=UTC),
                last_update=datetime(2026, 6, 1, tzinfo=UTC),
            ),
            RadiusActiveSession(
                subscriber_id=subscriber.id,
                subscription_id=None,  # null binding, but FRESHER
                nas_device_id=null_nas.id,
                username="u",
                acct_session_id="null-1",
                session_start=datetime(2026, 7, 1, tzinfo=UTC),
                last_update=datetime(2026, 7, 1, tzinfo=UTC),
            ),
        ]
    )
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "nas"
    assert path.access_device.id == own_nas.id  # own beats fresher null-bound
    assert path.basestation.id == own_pop.id
    assert path.live_session is True


def test_no_live_session_falls_back_to_provisioning_unchanged(db_session, subscription):
    # No live session: behavior is byte-for-byte the pre-change provisioning arm.
    nas = NasDevice(name="NAS-Static", management_ip="10.0.9.3")
    pop = PopSite(name="Static Site", zabbix_group_id="32")
    db_session.add_all([nas, pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, pop.id, "403"))
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.gap is None
    assert path.access_device_kind == "nas"
    assert path.access_device.id == nas.id
    assert path.basestation.id == pop.id
    assert path.live_session is False


def test_live_session_with_null_nas_falls_back_to_provisioning(
    db_session, subscription
):
    # A live session that never resolved to a nas_device_id must not mask the
    # static provisioning arm.
    nas = NasDevice(name="NAS-NullLive", management_ip="10.0.9.4")
    pop = PopSite(name="Null Live Site", zabbix_group_id="33")
    db_session.add_all([nas, pop])
    db_session.flush()
    db_session.add(_node("nas", nas.id, pop.id, "404"))
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()
    _session(db_session, subscription, None)

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "nas"
    assert path.access_device.id == nas.id
    assert path.live_session is False


def test_fiber_precedence_over_live_session(db_session, subscriber, subscription):
    # An active ONT assignment implies fiber; a live RADIUS session must not
    # preempt the OLT arm.
    olt = OLTDevice(name="OLT-LS", hostname="olt-ls", mgmt_ip="10.0.9.5")
    pop = PopSite(name="Fiber LS Site", zabbix_group_id="34")
    nas = NasDevice(name="NAS-LS", management_ip="10.0.9.6")
    db_session.add_all([olt, pop, nas])
    db_session.flush()
    db_session.add(_node("olt", olt.id, pop.id, "405"))
    ont = OntUnit(serial_number="SN-LS", olt_device_id=olt.id)
    db_session.add(ont)
    db_session.flush()
    db_session.add(
        OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber.id, active=True)
    )
    db_session.flush()
    _session(db_session, subscription, nas.id)

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "olt"
    assert path.access_device.id == olt.id
    assert path.live_session is False


def test_multiple_radios_pick_most_recently_synced(
    db_session, subscriber, subscription
):
    from datetime import UTC, datetime

    pop = PopSite(name="Multi Site", zabbix_group_id="26")
    db_session.add(pop)
    db_session.flush()
    ap_old = _ap_node(db_session, "AP-Old", pop_site_id=pop.id)
    ap_new = _ap_node(db_session, "AP-New", pop_site_id=pop.id)
    _cpe(
        db_session,
        subscriber.id,
        ap_old.id,
        synced_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    newer = _cpe(
        db_session,
        subscriber.id,
        ap_new.id,
        synced_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    path = resolve_customer_path(db_session, subscription)

    assert path.radio.id == newer.id
    assert path.node.id == ap_new.id


# --- Subscription-first radio resolution (exact beats subscriber-level) ----


def test_subscription_bound_radio_beats_newer_sibling_bound_radio(
    db_session, subscriber, subscription, catalog_offer
):
    from datetime import UTC, datetime

    from app.models.catalog import Subscription, SubscriptionStatus

    pop = PopSite(name="Exact Site", zabbix_group_id="30")
    db_session.add(pop)
    db_session.flush()
    ap_mine = _ap_node(db_session, "AP-Mine", pop_site_id=pop.id)
    ap_sibling = _ap_node(db_session, "AP-Sibling", pop_site_id=pop.id)

    sibling_sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=SubscriptionStatus.active,
    )
    db_session.add(sibling_sub)
    db_session.flush()

    # The sibling's radio synced LATER — recency must not beat exactness.
    mine = _cpe(
        db_session,
        subscriber.id,
        ap_mine.id,
        synced_at=datetime(2026, 6, 1, tzinfo=UTC),
        subscription_id=subscription.id,
    )
    _cpe(
        db_session,
        subscriber.id,
        ap_sibling.id,
        synced_at=datetime(2026, 7, 1, tzinfo=UTC),
        subscription_id=sibling_sub.id,
    )

    path = resolve_customer_path(db_session, subscription)

    assert path.radio.id == mine.id
    assert path.node.id == ap_mine.id
    assert path.radio_resolution == "subscription"


def test_legacy_subscriber_level_radio_still_resolves(
    db_session, subscriber, subscription
):
    # Rows written before subscription binding existed (subscription_id NULL)
    # keep resolving through the subscriber-level fallback tier.
    ap = _ap_node(db_session, "AP-Legacy")
    cpe = _cpe(db_session, subscriber.id, ap.id)

    path = resolve_customer_path(db_session, subscription)

    assert path.radio.id == cpe.id
    assert path.radio_resolution == "subscriber_legacy"


def test_unparented_radio_is_surfaced_for_ap_unresolved_display(
    db_session, subscriber, subscription
):
    from app.models.network import CPEDevice, DeviceType

    orphan = CPEDevice(
        subscriber_id=subscriber.id,
        device_type=DeviceType.wireless_radio,
        last_uisp_status="active",
    )
    db_session.add(orphan)
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.radio is None
    assert path.unparented_radio is not None
    assert path.unparented_radio.id == orphan.id
    assert path.gap is not None  # no access device resolved


def test_unparented_radio_survives_nas_fallback(db_session, subscriber, subscription):
    # The NAS arm builds its own CustomerPath; the unparented-radio marker
    # must survive onto the returned path so the card can still say
    # "AP unresolved" next to the coarse NAS endpoint.
    from app.models.network import CPEDevice, DeviceType

    nas = NasDevice(name="nas-fallback", ip_address="10.9.9.9")
    db_session.add(nas)
    db_session.flush()
    subscription.provisioning_nas_device_id = nas.id
    db_session.flush()
    orphan = CPEDevice(
        subscriber_id=subscriber.id,
        device_type=DeviceType.wireless_radio,
        last_uisp_status="active",
    )
    db_session.add(orphan)
    db_session.flush()

    path = resolve_customer_path(db_session, subscription)

    assert path.access_device_kind == "nas"
    assert path.unparented_radio is not None
    assert path.unparented_radio.id == orphan.id
