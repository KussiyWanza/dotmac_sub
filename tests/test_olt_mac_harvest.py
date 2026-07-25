"""Tests for the Huawei OLT MAC-forwarding harvester (hop-1 foundation).

Covers the ``display mac-address port`` parser (against the exact proven
sample), the DB-bounded active-PON-port walk + upsert, idempotency, aged-out
pruning, per-OLT error isolation, ONT<->subscriber drift detection, and the
task's advisory-lock single-flight skip.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.network import (
    ForwardingObservation,
    OLTDevice,
    OntAssignment,
    OntUnit,
    OnuOnlineStatus,
    PonPort,
)
from app.models.subscriber import Subscriber
from app.services.topology import olt_mac_harvest
from app.services.topology.olt_mac_harvest import (
    harvest_olt_mac_table,
    harvest_olt_mac_tables,
    parse_mac_address_port,
)

# The EXACT proven ground-truth sample (Huawei MA5608T BOI OLT).
_SAMPLE = """
   SRV-P BUNDLE TYPE MAC            MAC TYPE F /S /P   VPI  VCI   VLAN ID
   INDEX INDEX
      58     -  gpon 9c74-1a3f-98c7 dynamic  0 /1 /7   5    1         203
     396     -  gpon 38eb-4710-46e4 dynamic  0 /1 /14  1    1         202
"""


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def test_parse_mac_address_port_extracts_mac_fsp_ontid_vlan():
    entries = parse_mac_address_port(_SAMPLE)
    assert len(entries) == 2

    first, second = entries
    # MAC normalized from dotted-quad to canonical uppercase colon form.
    assert first.mac == "9C:74:1A:3F:98:C7"
    assert first.fsp == "0/1/7"
    assert first.ont_id == 5  # the VPI column
    assert first.vlan == 203
    assert first.mac_type == "dynamic"

    assert second.mac == "38:EB:47:10:46:E4"
    assert second.fsp == "0/1/14"
    assert second.ont_id == 1
    assert second.vlan == 202


def test_parse_mac_address_port_skips_headers_and_more_pagination():
    noisy = _SAMPLE + "\n  ---- More ---- \nCommand:\n"
    assert len(parse_mac_address_port(noisy)) == 2


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #
def _huawei_olt(db, name="Huawei OLT", vendor="Huawei"):
    olt = OLTDevice(name=name, vendor=vendor, is_active=True)
    db.add(olt)
    db.flush()
    return olt


def _online_ont(db, olt, *, board, port, ont_id, serial):
    pon = PonPort(
        olt=olt, name=f"{board}/{port}", port_number=int(port), is_active=True
    )
    db.add(pon)
    db.flush()
    ont = OntUnit(
        serial_number=serial,
        olt_device=olt,
        pon_port=pon,
        board=board,
        port=port,
        external_id=str(ont_id),
        olt_status=OnuOnlineStatus.online,
        is_active=True,
    )
    db.add(ont)
    db.flush()
    return ont, pon


def _fake_runner(outputs):
    """Return a fake _run_readonly_command keyed on the F/S/P in the command."""

    def _run(olt, command):
        for fsp, out in outputs.items():
            if command.endswith(fsp):
                return True, "ok", out
        return True, "ok", ""

    return _run


_PORT_7 = (
    "   SRV-P BUNDLE TYPE MAC            MAC TYPE F /S /P   VPI  VCI   VLAN ID\n"
    "   INDEX INDEX\n"
    "      58     -  gpon 9c74-1a3f-98c7 dynamic  0 /1 /7   5    1         203\n"
)
_PORT_14 = (
    "   SRV-P BUNDLE TYPE MAC            MAC TYPE F /S /P   VPI  VCI   VLAN ID\n"
    "   INDEX INDEX\n"
    "     396     -  gpon 38eb-4710-46e4 dynamic  0 /1 /14  1    1         202\n"
)


# --------------------------------------------------------------------------- #
# Harvest upsert
# --------------------------------------------------------------------------- #
def test_harvest_upserts_and_maps_to_onts(db_session, monkeypatch):
    olt = _huawei_olt(db_session)
    ont7, pon7 = _online_ont(
        db_session, olt, board="0/1", port="7", ont_id=5, serial="HWTC00000007"
    )
    ont14, pon14 = _online_ont(
        db_session, olt, board="0/1", port="14", ont_id=1, serial="HWTC00000014"
    )
    db_session.commit()

    monkeypatch.setattr(
        olt_mac_harvest,
        "_run_readonly_command",
        _fake_runner({"0/1/7": _PORT_7, "0/1/14": _PORT_14}),
    )

    counters = harvest_olt_mac_tables(db_session)
    db_session.commit()

    assert counters["olts_polled"] == 1
    assert counters["ports_walked"] == 2
    assert counters["observations"] == 2
    assert counters["macs_seen"] == 2
    assert counters["olt_errors"] == 0

    rows = {
        r.mac: r for r in db_session.scalars(select_all(ForwardingObservation)).all()
    }
    assert set(rows) == {"9C:74:1A:3F:98:C7", "38:EB:47:10:46:E4"}
    r7 = rows["9C:74:1A:3F:98:C7"]
    assert r7.ont_unit_id == ont7.id
    assert r7.pon_port_id == pon7.id
    assert r7.ont_id_on_olt == 5
    assert r7.vlan == 203
    assert r7.source == "huawei_olt_mac"
    r14 = rows["38:EB:47:10:46:E4"]
    assert r14.ont_unit_id == ont14.id
    assert r14.pon_port_id == pon14.id


def test_harvest_ignores_non_huawei_and_offline_only(db_session, monkeypatch):
    _huawei_olt(db_session, name="Cisco OLT", vendor="Cisco")
    called = {"n": 0}

    def _run(olt, command):
        called["n"] += 1
        return True, "ok", _PORT_7

    monkeypatch.setattr(olt_mac_harvest, "_run_readonly_command", _run)
    counters = harvest_olt_mac_tables(db_session)
    assert counters["olts_polled"] == 0
    assert called["n"] == 0


def test_harvest_is_idempotent(db_session, monkeypatch):
    olt = _huawei_olt(db_session)
    _online_ont(db_session, olt, board="0/1", port="7", ont_id=5, serial="HWTC00000007")
    db_session.commit()
    monkeypatch.setattr(
        olt_mac_harvest, "_run_readonly_command", _fake_runner({"0/1/7": _PORT_7})
    )

    harvest_olt_mac_tables(db_session)
    db_session.commit()
    first = db_session.scalars(select_all(ForwardingObservation)).all()
    assert len(first) == 1
    original_id = first[0].id

    harvest_olt_mac_tables(db_session)
    db_session.commit()
    second = db_session.scalars(select_all(ForwardingObservation)).all()
    assert len(second) == 1  # upsert, not duplicate
    assert second[0].id == original_id


def test_harvest_prunes_aged_out(db_session, monkeypatch):
    olt = _huawei_olt(db_session)
    _online_ont(db_session, olt, board="0/1", port="7", ont_id=5, serial="HWTC00000007")
    stale = ForwardingObservation(
        olt_device_id=olt.id,
        ont_id_on_olt=99,
        mac="AA:BB:CC:DD:EE:FF",
        source="huawei_olt_mac",
        observed_at=datetime.now(UTC) - timedelta(hours=48),
    )
    db_session.add(stale)
    db_session.commit()

    monkeypatch.setattr(
        olt_mac_harvest, "_run_readonly_command", _fake_runner({"0/1/7": _PORT_7})
    )
    counters = harvest_olt_mac_tables(db_session)
    db_session.commit()

    assert counters["pruned"] == 1
    macs = {r.mac for r in db_session.scalars(select_all(ForwardingObservation)).all()}
    assert "AA:BB:CC:DD:EE:FF" not in macs
    assert "9C:74:1A:3F:98:C7" in macs


def test_single_olt_harvest_is_scoped(db_session, monkeypatch):
    selected = _huawei_olt(db_session, name="Selected OLT")
    _online_ont(
        db_session,
        selected,
        board="0/1",
        port="7",
        ont_id=5,
        serial="HWTCSELECT07",
    )
    other = _huawei_olt(db_session, name="Other OLT")
    _online_ont(
        db_session,
        other,
        board="0/1",
        port="7",
        ont_id=5,
        serial="HWTCOTHER007",
    )
    stale_at = datetime.now(UTC) - timedelta(hours=48)
    db_session.add_all(
        [
            ForwardingObservation(
                olt_device_id=selected.id,
                ont_id_on_olt=99,
                mac="AA:BB:CC:DD:EE:01",
                source="huawei_olt_mac",
                observed_at=stale_at,
            ),
            ForwardingObservation(
                olt_device_id=other.id,
                ont_id_on_olt=99,
                mac="AA:BB:CC:DD:EE:02",
                source="huawei_olt_mac",
                observed_at=stale_at,
            ),
        ]
    )
    db_session.commit()
    walked_olts = []

    def _run(olt, _command):
        walked_olts.append(olt.id)
        return True, "ok", _PORT_7

    monkeypatch.setattr(olt_mac_harvest, "_run_readonly_command", _run)

    counters = harvest_olt_mac_table(db_session, selected.id)
    db_session.commit()

    assert counters["olts_polled"] == 1
    assert walked_olts == [selected.id]
    macs = {
        row.mac for row in db_session.scalars(select_all(ForwardingObservation)).all()
    }
    assert "AA:BB:CC:DD:EE:01" not in macs
    assert "AA:BB:CC:DD:EE:02" in macs


def test_harvest_isolates_per_olt_failure(db_session, monkeypatch):
    good = _huawei_olt(db_session, name="Good OLT")
    _online_ont(
        db_session, good, board="0/1", port="7", ont_id=5, serial="HWTCGOOD0007"
    )
    bad = _huawei_olt(db_session, name="Bad OLT")
    _online_ont(db_session, bad, board="0/2", port="3", ont_id=2, serial="HWTCBAD00003")
    db_session.commit()

    def _run(olt, command):
        if olt.id == bad.id:
            raise RuntimeError("ssh blew up")
        return True, "ok", _PORT_7

    monkeypatch.setattr(olt_mac_harvest, "_run_readonly_command", _run)
    counters = harvest_olt_mac_tables(db_session)
    db_session.commit()

    assert counters["olt_errors"] == 1
    assert counters["olts_polled"] == 2
    # The good OLT's observation survived the bad OLT's savepoint rollback.
    macs = {r.mac for r in db_session.scalars(select_all(ForwardingObservation)).all()}
    assert "9C:74:1A:3F:98:C7" in macs


# --------------------------------------------------------------------------- #
# Drift detection (read-only toward assignments)
# --------------------------------------------------------------------------- #
def _subscriber(db, email):
    s = Subscriber(first_name="D", last_name="R", email=email)
    db.add(s)
    db.flush()
    return s


def _active_sub_with_mac(db, subscriber, offer, mac):
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        mac_address=mac,
        login=f"login-{subscriber.email.split('@')[0]}",
    )
    db.add(sub)
    db.flush()
    return sub


def test_harvest_flags_ont_subscriber_drift(db_session, catalog_offer, monkeypatch):
    olt = _huawei_olt(db_session)
    ont, _pon = _online_ont(
        db_session, olt, board="0/1", port="7", ont_id=5, serial="HWTC00000007"
    )
    owner_a = _subscriber(db_session, "owner_a@e.com")
    owner_b = _subscriber(db_session, "owner_b@e.com")
    # ONT is assigned to owner A, but the learned MAC belongs to owner B.
    assignment = OntAssignment(ont_unit=ont, subscriber_id=owner_a.id, active=True)
    db_session.add(assignment)
    _active_sub_with_mac(db_session, owner_b, catalog_offer, "9C:74:1A:3F:98:C7")
    db_session.commit()

    monkeypatch.setattr(
        olt_mac_harvest, "_run_readonly_command", _fake_runner({"0/1/7": _PORT_7})
    )
    counters = harvest_olt_mac_tables(db_session)
    db_session.commit()

    assert counters["drift_detected"] == 1
    assert counters["linkable_no_assignment"] == 0
    # Assignment is untouched — drift is detection-only.
    db_session.refresh(assignment)
    assert assignment.subscriber_id == owner_a.id
    assert assignment.active is True


def test_harvest_counts_linkable_no_assignment(db_session, catalog_offer, monkeypatch):
    olt = _huawei_olt(db_session)
    _online_ont(db_session, olt, board="0/1", port="7", ont_id=5, serial="HWTC00000007")
    owner = _subscriber(db_session, "owner@e.com")
    _active_sub_with_mac(db_session, owner, catalog_offer, "9c74-1a3f-98c7")
    db_session.commit()

    monkeypatch.setattr(
        olt_mac_harvest, "_run_readonly_command", _fake_runner({"0/1/7": _PORT_7})
    )
    counters = harvest_olt_mac_tables(db_session)
    db_session.commit()

    assert counters["linkable_no_assignment"] == 1
    assert counters["drift_detected"] == 0


def test_harvest_no_drift_when_mac_matches_assignment(
    db_session, catalog_offer, monkeypatch
):
    olt = _huawei_olt(db_session)
    ont, _pon = _online_ont(
        db_session, olt, board="0/1", port="7", ont_id=5, serial="HWTC00000007"
    )
    owner = _subscriber(db_session, "match@e.com")
    db_session.add(OntAssignment(ont_unit=ont, subscriber_id=owner.id, active=True))
    _active_sub_with_mac(db_session, owner, catalog_offer, "9C:74:1A:3F:98:C7")
    db_session.commit()

    monkeypatch.setattr(
        olt_mac_harvest, "_run_readonly_command", _fake_runner({"0/1/7": _PORT_7})
    )
    counters = harvest_olt_mac_tables(db_session)
    assert counters["drift_detected"] == 0
    assert counters["linkable_no_assignment"] == 0


# --------------------------------------------------------------------------- #
# Task dispatch and per-OLT single-flight
# --------------------------------------------------------------------------- #
def test_single_olt_task_skips_when_advisory_lock_held(monkeypatch):
    from app.tasks import olt_mac_harvest as task_mod

    @contextmanager
    def _lock_not_acquired(_key):
        yield False

    monkeypatch.setattr(task_mod, "postgres_session_advisory_lock", _lock_not_acquired)

    result = task_mod.run_single_olt_mac_harvest("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    assert result == {
        "olt_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "skipped": "already_running",
    }


def test_dispatcher_queues_each_olt_and_isolates_enqueue_failure(
    db_session, monkeypatch
):
    from app.tasks import olt_mac_harvest as task_mod

    first = _huawei_olt(db_session, name="First OLT")
    second = _huawei_olt(db_session, name="Second OLT")
    db_session.commit()

    @contextmanager
    def _read_session():
        yield db_session

    queued = []

    def _enqueue(_task, *, args, **_kwargs):
        queued.append(args[0])
        if args[0] == str(second.id):
            raise RuntimeError("broker rejected one message")
        return object()

    monkeypatch.setattr(task_mod.db_session_adapter, "read_session", _read_session)
    monkeypatch.setattr(task_mod, "enqueue_celery_task", _enqueue)

    result = task_mod.run_olt_mac_harvest()

    assert result == {"olts": 2, "queued": 1, "failed": 1}
    assert queued == sorted([str(first.id), str(second.id)])


def test_mac_harvest_tasks_registered_routed_and_exported():
    import app.tasks as tasks
    from app.celery_app import celery_app

    dispatcher = "app.tasks.olt_mac_harvest.run_olt_mac_harvest"
    child = "app.tasks.olt_mac_harvest.run_single_olt_mac_harvest"

    assert dispatcher in celery_app.tasks
    assert child in celery_app.tasks
    assert celery_app.conf.task_routes[dispatcher] == {"queue": "ingestion"}
    assert celery_app.conf.task_routes[child] == {"queue": "ingestion"}
    assert "run_single_olt_mac_harvest" in tasks.__all__


def select_all(model):
    from sqlalchemy import select

    return select(model)


def test_no_transaction_held_during_ssh_walk(db_session, monkeypatch):
    """The SSH phase must run with no DB transaction open (the held
    transaction sat idle-in-transaction for minutes and degraded the
    PostgreSQL health check in prod)."""
    from app.services.topology import olt_mac_harvest as mod

    olt = _huawei_olt(db_session)
    _online_ont(db_session, olt, board="0/1", port="0", ont_id=1, serial="HWTC1")
    db_session.commit()

    tx_states: list[bool] = []

    def _fake_run(olt_arg, command):
        tx_states.append(db_session.in_transaction())
        return True, "ok", ""

    monkeypatch.setattr(mod, "_run_readonly_command", _fake_run)
    mod.harvest_olt_mac_tables(db_session)
    assert tx_states, "SSH walk never ran"
    assert not any(tx_states), "a DB transaction was open during the SSH walk"
