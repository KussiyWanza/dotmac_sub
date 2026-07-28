"""Topology live-status warmer (native poll source, Zabbix cutover Phase 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models.network_monitoring import DeviceStatus, NetworkDevice
from app.services.topology.live_status import (
    STALE_POLL_AFTER_SECONDS,
    derive_live_status,
    live_status_observed_at,
    warm_topology_status,
)


def _now() -> datetime:
    return datetime.now(UTC)


_ip_counter = iter(range(1, 10_000))


def _node(name, **kw):
    kw.setdefault("source", "zabbix_reconcile")
    kw.setdefault("is_active", True)
    if "mgmt_ip" not in kw:
        n = next(_ip_counter)
        kw["mgmt_ip"] = f"10.77.{n // 250}.{n % 250 + 1}"
    return NetworkDevice(name=name, **kw)


def test_warm_sets_live_status_per_node(db_session):
    now = _now()
    db_session.add_all(
        [
            _node("up-node", ping_enabled=True, last_ping_ok=True, last_ping_at=now),
            _node("down-node", ping_enabled=True, last_ping_ok=False, last_ping_at=now),
            _node("unknown-node", ping_enabled=True),
            # source-agnostic: a non-reconciled (e.g. admin-created) device with
            # native poll data is warmed exactly like a reconciled node
            _node(
                "other-source",
                source="splynx",
                ping_enabled=True,
                last_ping_ok=True,
                last_ping_at=now,
            ),
            # unpollable (no mgmt_ip/hostname) -> untouched, keeps NULL so
            # surfaces with their own fallbacks (linked-router status) apply
            NetworkDevice(
                name="unpollable",
                source="zabbix_reconcile",
                is_active=True,
                ping_enabled=True,
                last_ping_ok=True,
                last_ping_at=now,
            ),
            # inactive -> untouched
            _node(
                "inactive",
                is_active=False,
                ping_enabled=True,
                last_ping_ok=True,
                last_ping_at=now,
            ),
        ]
    )
    db_session.flush()

    result = warm_topology_status(db_session)
    assert result["nodes"] == 4

    by_name = {n.name: n.live_status for n in db_session.query(NetworkDevice).all()}
    assert by_name["up-node"] == "up"
    assert by_name["down-node"] == "down"
    assert by_name["unknown-node"] == "unknown"
    assert by_name["other-source"] == "up"
    assert by_name["unpollable"] is None
    assert by_name["inactive"] is None

    warmed = (
        db_session.query(NetworkDevice)
        .filter(NetworkDevice.live_status.isnot(None))
        .all()
    )
    assert len(warmed) == 4
    assert all(n.live_status_at is not None for n in warmed)


def test_stale_ping_result_reads_unknown(db_session):
    # A poll result older than the staleness window proves nothing: the poller
    # stopped covering the device, so it degrades to unknown instead of
    # freezing on its last state.
    stale = _now() - timedelta(seconds=STALE_POLL_AFTER_SECONDS + 60)
    db_session.add(
        _node("stale-up", ping_enabled=True, last_ping_ok=True, last_ping_at=stale)
    )
    db_session.flush()

    warm_topology_status(db_session)

    n = db_session.query(NetworkDevice).filter_by(name="stale-up").one()
    assert n.live_status == "unknown"


def test_snmp_fills_in_for_ping_disabled_device(db_session):
    now = _now()
    db_session.add_all(
        [
            _node(
                "snmp-up",
                ping_enabled=False,
                snmp_enabled=True,
                last_snmp_ok=True,
                last_snmp_at=now,
            ),
            _node(
                "snmp-down",
                ping_enabled=False,
                snmp_enabled=True,
                last_snmp_ok=False,
                last_snmp_at=now,
            ),
        ]
    )
    db_session.flush()

    warm_topology_status(db_session)

    by_name = {n.name: n.live_status for n in db_session.query(NetworkDevice).all()}
    assert by_name["snmp-up"] == "up"
    assert by_name["snmp-down"] == "down"


def test_fresh_ping_wins_over_snmp(db_session):
    # Ping is the authoritative reachability signal (same precedence the old
    # Zabbix warmer gave icmpping over host availability).
    now = _now()
    db_session.add(
        _node(
            "ping-down-snmp-up",
            ping_enabled=True,
            last_ping_ok=False,
            last_ping_at=now,
            snmp_enabled=True,
            last_snmp_ok=True,
            last_snmp_at=now,
        )
    )
    db_session.flush()

    warm_topology_status(db_session)

    n = db_session.query(NetworkDevice).filter_by(name="ping-down-snmp-up").one()
    assert n.live_status == "down"


def test_stale_ping_falls_back_to_fresh_snmp(db_session):
    now = _now()
    stale = now - timedelta(seconds=STALE_POLL_AFTER_SECONDS + 60)
    db_session.add(
        _node(
            "stale-ping-fresh-snmp",
            ping_enabled=True,
            last_ping_ok=False,
            last_ping_at=stale,
            snmp_enabled=True,
            last_snmp_ok=True,
            last_snmp_at=now,
        )
    )
    db_session.flush()

    warm_topology_status(db_session)

    n = db_session.query(NetworkDevice).filter_by(name="stale-ping-fresh-snmp").one()
    assert n.live_status == "up"


def test_maintenance_device_is_unknown(db_session):
    # An operator put the device into maintenance: a deliberate shutdown must
    # not surface to customers as an outage, and a leftover "ok" must not read
    # as monitored-healthy (mirrors the old Zabbix maintenance handling).
    now = _now()
    db_session.add_all(
        [
            _node(
                "maint-down",
                status=DeviceStatus.maintenance,
                ping_enabled=True,
                last_ping_ok=False,
                last_ping_at=now,
            ),
            _node(
                "maint-up",
                status=DeviceStatus.maintenance,
                ping_enabled=True,
                last_ping_ok=True,
                last_ping_at=now,
            ),
        ]
    )
    db_session.flush()

    warm_topology_status(db_session)

    by_name = {n.name: n.live_status for n in db_session.query(NetworkDevice).all()}
    assert by_name["maint-down"] == "unknown"
    assert by_name["maint-up"] == "unknown"


def test_ping_disabled_result_not_trusted(db_session):
    # ping_enabled=False means nobody is refreshing last_ping_*; a leftover
    # value (however recent-looking) must not colour the node. The device is
    # still pollable via SNMP, which has produced no result yet -> unknown.
    db_session.add(
        _node(
            "ping-disabled",
            ping_enabled=False,
            snmp_enabled=True,
            last_ping_ok=True,
            last_ping_at=_now(),
        )
    )
    db_session.flush()

    warm_topology_status(db_session)

    n = db_session.query(NetworkDevice).filter_by(name="ping-disabled").one()
    assert n.live_status == "unknown"


def test_naive_poll_timestamp_treated_as_utc():
    # SQLite (and pre-tz rows) round-trip naive datetimes; freshness must not
    # crash or misread them.
    node = NetworkDevice(
        name="naive",
        source="zabbix_reconcile",
        is_active=True,
        ping_enabled=True,
        last_ping_ok=True,
        last_ping_at=datetime.now(UTC).replace(tzinfo=None),
    )
    assert derive_live_status(node) == "up"


def test_live_status_at_stamped_only_on_change(db_session):
    # live_status_at must mark when the node ENTERED its current state (the
    # dwell clock the customer-facing debounce relies on) — not every poll.
    db_session.add(
        _node("n5", ping_enabled=True, last_ping_ok=True, last_ping_at=_now())
    )
    db_session.flush()

    warm_topology_status(db_session)
    n = db_session.query(NetworkDevice).filter_by(name="n5").one()
    # Re-read from the DB so first_at is in the same representation the later
    # refreshed comparison uses — SQLite drops tzinfo on round-trip, so an
    # in-session aware value would never equal the refreshed naive one. We are
    # asserting the stamp does not MOVE, not its tz-awareness.
    db_session.refresh(n)
    first_at = n.live_status_at

    # same status next poll -> timestamp does NOT move
    warm_topology_status(db_session)
    db_session.refresh(n)
    assert n.live_status == "up"
    assert n.live_status_at == first_at

    # a real status change DOES move it
    n.last_ping_ok = False
    n.last_ping_at = _now()
    n.live_status_at = datetime(2020, 1, 1, tzinfo=UTC)
    db_session.flush()
    warm_topology_status(db_session)
    db_session.refresh(n)
    assert n.live_status == "down"
    assert n.live_status_at != datetime(2020, 1, 1, tzinfo=UTC)


def test_live_status_observed_at_uses_selected_native_collector_timestamp():
    now = _now()
    stale_ping = now - timedelta(seconds=STALE_POLL_AFTER_SECONDS + 60)
    fresh_snmp = now - timedelta(seconds=30)
    node = _node(
        "observation-time",
        ping_enabled=True,
        last_ping_ok=False,
        last_ping_at=stale_ping,
        snmp_enabled=True,
        last_snmp_ok=True,
        last_snmp_at=fresh_snmp,
        live_status="up",
        live_status_at=now - timedelta(days=30),
    )

    assert live_status_observed_at(node, now=now) == fresh_snmp
