"""Last-mile diagnoser P2 — per-customer outage verdict.

Design: docs/designs/OUTAGE_CLASSIFIER.md §5 (ladder + verdicts), §7.3
(customer-power vs plant fault), §4 (fiber-vs-wireless observability asymmetry).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    CPEDevice,
    DeviceStatus,
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOfflineReason,
    OnuOnlineStatus,
)
from app.models.network_monitoring import DeviceRole, NetworkDevice
from app.models.radius_active_session import RadiusActiveSession
from app.models.radius_error import RadiusAuthError, RadiusAuthErrorType
from app.models.subscriber import Subscriber
from app.services.topology.last_mile import (
    AUTH,
    CONFIG,
    HEALTHY,
    MEDIUM_FIBER,
    MEDIUM_WIRELESS,
    POWER,
    ROUTER_OFFLINE,
    SIGNAL_DEGRADED,
    UNKNOWN,
    _plant_is_up,
    diagnose_last_mile,
    diagnose_many,
)

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


# --- fixture builders -----------------------------------------------------


def _sub(db, offer_id):
    s = Subscriber(first_name="A", last_name="B", email=f"{uuid.uuid4().hex}@ex.com")
    db.add(s)
    db.flush()
    sub = Subscription(
        subscriber_id=s.id, offer_id=offer_id, status=SubscriptionStatus.active
    )
    db.add(sub)
    db.flush()
    return sub


def _ont(
    db,
    subscriber_id,
    *,
    olt_device_id=None,
    status=OnuOnlineStatus.online,
    rx=-18.0,
    acs_age=timedelta(minutes=1),
    offline_reason=None,
):
    ont = OntUnit(
        serial_number=uuid.uuid4().hex[:12],
        olt_status=status,
        olt_status_seen_at=NOW,
        onu_rx_signal_dbm=rx,
        olt_device_id=olt_device_id,
        offline_reason=offline_reason,
        acs_last_inform_at=(NOW - acs_age) if acs_age is not None else None,
    )
    db.add(ont)
    db.flush()
    db.add(OntAssignment(ont_unit_id=ont.id, subscriber_id=subscriber_id, active=True))
    db.flush()
    return ont


def _radio(
    db,
    subscriber_id,
    node,
    *,
    last_uisp_status="active",
    rf_signal_dbm=None,
    rf_signal_observed_at=None,
):
    cpe = CPEDevice(
        subscriber_id=subscriber_id,
        parent_network_device_id=node.id,
        status=DeviceStatus.active,
        last_uisp_status=last_uisp_status,
        uisp_synced_at=NOW,
        rf_signal_dbm=rf_signal_dbm,
        rf_signal_source="uisp_ap_station" if rf_signal_dbm is not None else None,
        rf_signal_observed_at=rf_signal_observed_at
        or (NOW - timedelta(minutes=5) if rf_signal_dbm is not None else None),
    )
    db.add(cpe)
    db.flush()
    return cpe


def _olt_with_node(db, *, live_status="up"):
    olt = OLTDevice(name=f"olt-{uuid.uuid4().hex[:6]}")
    db.add(olt)
    db.flush()
    node = NetworkDevice(
        name=f"olt-node-{uuid.uuid4().hex[:6]}",
        matched_device_type="olt",
        matched_device_id=olt.id,
        role=DeviceRole.edge,
        is_active=True,
        live_status=live_status,
    )
    db.add(node)
    db.flush()
    return olt, node


def _ap_node(db, *, live_status="up"):
    node = NetworkDevice(
        name=f"ap-{uuid.uuid4().hex[:6]}",
        role=DeviceRole.edge,
        is_active=True,
        live_status=live_status,
    )
    db.add(node)
    db.flush()
    return node


def _auth_error(db, sub, *, age=timedelta(minutes=2)):
    db.add(
        RadiusAuthError(
            subscriber_id=sub.subscriber_id,
            subscription_id=sub.id,
            username="u",
            error_type=RadiusAuthErrorType.reject,
            occurred_at=NOW - age,
        )
    )
    db.flush()


def _live_session(db, sub):
    db.add(
        RadiusActiveSession(
            subscriber_id=sub.subscriber_id,
            subscription_id=sub.id,
            username="u",
            acct_session_id=uuid.uuid4().hex,
            session_start=NOW,
            last_update=NOW,
        )
    )
    db.flush()


# --- session up beats everything (design §0) ------------------------------


def test_healthy_when_session_live(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    _live_session(db_session, sub)
    # even with a broken ONT, a live session wins.
    _ont(db_session, sub.subscriber_id, status=OnuOnlineStatus.offline)
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == HEALTHY


# --- fiber ladder verdicts (design §5) ------------------------------------


def test_fiber_signal_degraded_bad_rx(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    _ont(db_session, sub.subscriber_id, rx=-29.0)  # below -27 floor
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == SIGNAL_DEGRADED
    assert out["medium"] == MEDIUM_FIBER
    assert out["signal_dbm"] == -29.0


def test_fiber_router_offline_acs_stale(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    _ont(db_session, sub.subscriber_id, rx=-18.0, acs_age=timedelta(hours=3))
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == ROUTER_OFFLINE


def test_fiber_auth_when_informing_and_recent_reject(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    _ont(db_session, sub.subscriber_id, rx=-18.0, acs_age=timedelta(minutes=1))
    _auth_error(db_session, sub)
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == AUTH


def test_fiber_config_when_informing_no_attempt(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    _ont(db_session, sub.subscriber_id, rx=-18.0, acs_age=timedelta(minutes=1))
    out = diagnose_last_mile(db_session, sub, now=NOW)  # no auth error, no session
    assert out["verdict"] == CONFIG


# --- §7.3: ONT absent — customer power vs plant fault ----------------------


def test_fiber_power_when_ont_absent_and_plant_up(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    olt, node = _olt_with_node(db_session, live_status="up")
    _ont(
        db_session,
        sub.subscriber_id,
        olt_device_id=olt.id,
        status=OnuOnlineStatus.offline,
        offline_reason=OnuOfflineReason.power_fail,
        acs_age=None,
    )
    out = diagnose_last_mile(db_session, sub, now=NOW, plant_cache={node.id: True})
    assert out["verdict"] == POWER
    assert out["evidence"]["offline_reason"] == "power_fail"
    assert "truck" in out["agent_action"] or "power" in out["agent_action"]


def test_fiber_absent_but_plant_down_is_upstream_not_customer(
    db_session, catalog_offer
):
    sub = _sub(db_session, catalog_offer.id)
    olt, node = _olt_with_node(db_session, live_status="down")
    _ont(
        db_session,
        sub.subscriber_id,
        olt_device_id=olt.id,
        status=OnuOnlineStatus.offline,
        acs_age=None,
    )
    # Plant is down -> P1 owns it; do NOT blame the customer's power.
    out = diagnose_last_mile(db_session, sub, now=NOW, plant_cache={node.id: False})
    assert out["verdict"] == UNKNOWN


def test_fiber_los_note_on_absent(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    olt, node = _olt_with_node(db_session, live_status="up")
    _ont(
        db_session,
        sub.subscriber_id,
        olt_device_id=olt.id,
        status=OnuOnlineStatus.offline,
        offline_reason=OnuOfflineReason.los,
        acs_age=None,
    )
    out = diagnose_last_mile(db_session, sub, now=NOW, plant_cache={node.id: True})
    assert out["verdict"] == POWER
    assert "drop" in out["evidence"]["note"]


# --- wireless ladder (design §4 gap: presence-only, no RF) -----------------


def test_wireless_power_when_disconnected_and_plant_up(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    node = _ap_node(db_session, live_status="up")
    _radio(db_session, sub.subscriber_id, node, last_uisp_status="disconnected")
    out = diagnose_last_mile(db_session, sub, now=NOW, plant_cache={node.id: True})
    assert out["verdict"] == POWER
    assert out["medium"] == MEDIUM_WIRELESS
    assert out["signal_dbm"] is None  # no RF value exists (design §4)


def test_wireless_auth_when_unauthorized(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    node = _ap_node(db_session)
    _radio(db_session, sub.subscriber_id, node, last_uisp_status="unauthorized")
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == AUTH
    assert out["medium"] == MEDIUM_WIRELESS


def test_wireless_router_offline_when_associated_no_reject(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    node = _ap_node(db_session)
    _radio(db_session, sub.subscriber_id, node, last_uisp_status="active")
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == ROUTER_OFFLINE
    assert out["signal_dbm"] is None
    assert out["evidence"]["rf_signal_freshness"] == "unavailable"


def test_wireless_signal_degraded_fresh_rf_below_floor(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    node = _ap_node(db_session)
    _radio(
        db_session,
        sub.subscriber_id,
        node,
        last_uisp_status="active",
        rf_signal_dbm=-82.0,
    )
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == SIGNAL_DEGRADED
    assert out["medium"] == MEDIUM_WIRELESS
    assert out["signal_dbm"] == -82.0
    assert out["evidence"]["rf_signal"] == -82.0
    assert out["evidence"]["rf_signal_freshness"] == "fresh"


def test_wireless_fresh_good_rf_does_not_degrade(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)
    node = _ap_node(db_session)
    _radio(
        db_session,
        sub.subscriber_id,
        node,
        last_uisp_status="active",
        rf_signal_dbm=-58.0,
    )
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == ROUTER_OFFLINE  # associated, RF fine, not dialing
    assert out["signal_dbm"] == -58.0


def test_wireless_stale_rf_never_gates_a_verdict(db_session, catalog_offer):
    # A below-floor value that has EXPIRED must not fabricate signal_degraded;
    # the ladder falls through to the presence/auth rungs.
    sub = _sub(db_session, catalog_offer.id)
    node = _ap_node(db_session)
    _radio(
        db_session,
        sub.subscriber_id,
        node,
        last_uisp_status="active",
        rf_signal_dbm=-82.0,
        rf_signal_observed_at=NOW - timedelta(hours=3),
    )
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == ROUTER_OFFLINE
    assert out["signal_dbm"] is None  # stale value is not presented as current
    assert out["evidence"]["rf_signal"] is None
    assert out["evidence"]["rf_signal_freshness"] == "stale"


# --- unknown when no CPE telemetry below session --------------------------


def test_unknown_when_no_ont_or_radio(db_session, catalog_offer):
    sub = _sub(db_session, catalog_offer.id)  # NAS-only, no ONT/radio
    out = diagnose_last_mile(db_session, sub, now=NOW)
    assert out["verdict"] == UNKNOWN
    assert out["evidence"]["rung"] == "linkage"


# --- _plant_is_up integrates P1 (real proof-of-life) ----------------------


def test_plant_is_up_true_with_online_customer(db_session, catalog_offer):
    # A node with a live session behind it is up by proof-of-life (P1 §0).
    from app.models.catalog import NasDevice

    nas = NasDevice(name="NAS-P", management_ip="10.0.9.9")
    db_session.add(nas)
    db_session.flush()
    node = NetworkDevice(
        name="nas-node",
        matched_device_type="nas",
        matched_device_id=nas.id,
        role=DeviceRole.edge,
        is_active=True,
        live_status="down",
    )
    db_session.add(node)
    db_session.flush()
    sub = _sub(db_session, catalog_offer.id)
    sub.provisioning_nas_device_id = nas.id
    db_session.flush()
    _live_session(db_session, sub)
    # live_status is 'down' but a customer is online -> monitoring_fault, and a
    # node serving someone is emphatically up. (classify: online>0 -> not outage)
    assert _plant_is_up(db_session, node, None) is not None


def test_plant_is_up_none_when_no_node():
    assert _plant_is_up(None, None, None) is None


# --- batch helper ----------------------------------------------------------


def test_diagnose_many_keys_by_subscription(db_session, catalog_offer):
    s1 = _sub(db_session, catalog_offer.id)
    _ont(db_session, s1.subscriber_id, rx=-29.0)
    s2 = _sub(db_session, catalog_offer.id)
    _ont(db_session, s2.subscriber_id, rx=-18.0, acs_age=timedelta(hours=5))
    out = diagnose_many(db_session, [s1.id, s2.id], now=NOW)
    assert out[s1.id]["verdict"] == SIGNAL_DEGRADED
    assert out[s2.id]["verdict"] == ROUTER_OFFLINE


def test_diagnose_many_empty():
    assert diagnose_many(None, []) == {}
