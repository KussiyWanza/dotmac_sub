"""Native infrastructure poller (Zabbix runtime cutover, Phase 1)."""

from __future__ import annotations

from types import SimpleNamespace

from app.celery_app import celery_app
from app.models.network_monitoring import DeviceStatus, NetworkDevice
from app.services import infrastructure_polling
from app.services import web_network_core_runtime as runtime
from app.services.topology.live_status import warm_topology_status

TASK_NAME = "app.tasks.infrastructure_polling.run_infrastructure_poll"


def _device(name, ip, **kw):
    kw.setdefault("is_active", True)
    kw.setdefault("ping_enabled", True)
    kw.setdefault("source", "zabbix_reconcile")
    return NetworkDevice(name=name, mgmt_ip=ip, **kw)


def test_task_registered_routed_and_exported():
    import app.tasks as tasks

    assert TASK_NAME in celery_app.tasks
    assert celery_app.conf.task_routes[TASK_NAME] == {"queue": "monitoring"}
    assert "monitoring" in {queue.name for queue in celery_app.conf.task_queues}
    assert "run_infrastructure_poll" in tasks.__all__
    assert hasattr(tasks, "run_infrastructure_poll")


def test_pollable_devices_selection(db_session):
    included_ping = _device("ping-target", "10.80.0.1")
    included_snmp = _device(
        "snmp-target", None, ping_enabled=False, snmp_enabled=True, hostname="snmp-h"
    )
    db_session.add_all(
        [
            included_ping,
            included_snmp,
            # inactive -> excluded
            _device("inactive", "10.80.0.2", is_active=False),
            # no enabled check -> excluded
            _device("no-checks", "10.80.0.3", ping_enabled=False, snmp_enabled=False),
            # no address at all -> excluded
            _device("no-address", None),
        ]
    )
    db_session.flush()

    names = {d.name for d in infrastructure_polling.pollable_devices(db_session)}
    assert names == {"ping-target", "snmp-target"}


def test_poll_infrastructure_delegates_to_stale_refresh(db_session, monkeypatch):
    db_session.add(_device("sweep-me", "10.80.1.1"))
    db_session.flush()
    captured = {}

    def _fake_refresh(db, devices, **kwargs):
        captured["devices"] = list(devices)
        captured["kwargs"] = kwargs
        return {"checked": len(devices), "ping": len(devices), "snmp": 0}

    monkeypatch.setattr(
        infrastructure_polling, "refresh_stale_devices_health", _fake_refresh
    )

    result = infrastructure_polling.poll_infrastructure(
        db_session, ping_interval_seconds=45, snmp_interval_seconds=200
    )

    assert [d.name for d in captured["devices"]] == ["sweep-me"]
    assert captured["kwargs"]["ping_interval_seconds"] == 45
    assert captured["kwargs"]["snmp_interval_seconds"] == 200
    assert captured["kwargs"]["include_snmp"] is True
    assert result == {
        "checked": 1,
        "ping": 1,
        "snmp": 0,
        "devices": 1,
        "ping_metric_lines": 0,
        "ping_metric_write_failed": 0,
        "interface_devices": 0,
        "interface_lines": 0,
        "interface_write_failed": 0,
    }


def test_refresh_stale_cap_takes_longest_unchecked(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    probed = []
    monkeypatch.setattr(
        runtime,
        "_refresh_device_health_worker",
        lambda device_id, do_ping, do_snmp: probed.append(device_id),
    )
    now = datetime.now(UTC)
    never = _device("cap-never", "10.80.7.1")
    oldest = _device("cap-oldest", "10.80.7.2", last_ping_at=now - timedelta(hours=3))
    newer = _device("cap-newer", "10.80.7.3", last_ping_at=now - timedelta(hours=1))
    db_session.add_all([never, oldest, newer])
    db_session.commit()

    totals = runtime.refresh_stale_devices_health(
        db_session,
        [newer, never, oldest],
        ping_interval_seconds=60,
        snmp_interval_seconds=300,
        max_devices=2,
    )

    assert totals["checked"] == 2
    assert set(probed) == {str(never.id), str(oldest.id)}


def test_unstampable_devices_never_count_as_stale(db_session, monkeypatch):
    # A ping-enabled device with no mgmt_ip can never be pinged (ping_device
    # early-returns without stamping), so it must not occupy capped batches.
    probed = []
    monkeypatch.setattr(
        runtime,
        "_refresh_device_health_worker",
        lambda device_id, do_ping, do_snmp: probed.append(device_id),
    )
    hostname_only = _device(
        "stale-hostname-only", None, hostname="ho-1", snmp_enabled=False
    )
    real = _device("stale-real", "10.80.9.1")
    db_session.add_all([hostname_only, real])
    db_session.commit()

    totals = runtime.refresh_stale_devices_health(
        db_session,
        [hostname_only, real],
        ping_interval_seconds=60,
        snmp_interval_seconds=300,
        max_devices=1,
    )

    assert totals["checked"] == 1
    assert probed == [str(real.id)]


def test_push_ping_metrics_emits_latency_and_loss(db_session, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from app.models.network_monitoring import (
        DeviceMetric,
        DeviceRole,
        MetricType,
        PopSite,
    )

    site = PopSite(name=f"site-{uuid4().hex[:6]}")
    db_session.add(site)
    db_session.flush()
    up = NetworkDevice(
        name="lat-up",
        mgmt_ip="10.82.0.1",
        is_active=True,
        ping_enabled=True,
        role=DeviceRole.core,
        pop_site_id=site.id,
        matched_device_type="nas",
    )
    down = NetworkDevice(
        name="lat-down", mgmt_ip="10.82.0.2", is_active=True, ping_enabled=True
    )
    db_session.add_all([up, down])
    db_session.flush()
    now = datetime.now(UTC)
    db_session.add_all(
        [
            DeviceMetric(
                device_id=up.id,
                metric_type=MetricType.custom,
                value=12,
                unit="ping_ms",
                recorded_at=now,
            ),
            DeviceMetric(
                device_id=down.id,
                metric_type=MetricType.custom,
                value=-1,
                unit="ping_timeout",
                recorded_at=now,
            ),
            # outside the sweep window -> excluded
            DeviceMetric(
                device_id=up.id,
                metric_type=MetricType.custom,
                value=99,
                unit="ping_ms",
                recorded_at=now - timedelta(minutes=30),
            ),
        ]
    )
    db_session.commit()

    written = {}

    class _Writer:
        def write_prometheus_lines(self, lines, **kwargs):
            written["lines"] = lines
            written["kwargs"] = kwargs
            return SimpleNamespace(success=True, written=len(lines))

    monkeypatch.setattr(infrastructure_polling, "_writer", lambda: _Writer())

    result = infrastructure_polling.push_ping_metrics(
        db_session, since=now - timedelta(seconds=60)
    )

    assert result == {"ping_metric_lines": 3, "ping_metric_write_failed": 0}
    latency = [ln for ln in written["lines"] if ln.startswith("device_ping_latency_ms")]
    loss = [ln for ln in written["lines"] if ln.startswith("device_ping_loss")]
    assert len(latency) == 1 and len(loss) == 2
    assert f'device_id="{up.id}"' in latency[0]
    assert 'device_role="core"' in latency[0]
    assert f'pop_site_id="{site.id}"' in latency[0]
    assert 'matched_device_type="nas"' in latency[0]
    assert " 12.0 " in latency[0]
    down_loss = next(ln for ln in loss if f'device_id="{down.id}"' in ln)
    assert " 1 " in down_loss
    up_loss = next(ln for ln in loss if f'device_id="{up.id}"' in ln)
    assert " 0 " in up_loss
    assert written["kwargs"]["operation"] == "ping_metrics"


def test_push_ping_metrics_no_rows_is_noop(db_session, monkeypatch):
    from datetime import UTC, datetime

    called = {"writer": False}
    monkeypatch.setattr(
        infrastructure_polling,
        "_writer",
        lambda: called.__setitem__("writer", True),
    )

    result = infrastructure_polling.push_ping_metrics(
        db_session, since=datetime.now(UTC)
    )

    assert result == {"ping_metric_lines": 0, "ping_metric_write_failed": 0}
    assert called["writer"] is False


def test_counter_targets_skip_ping_down_devices(db_session):
    from app.models.network_monitoring import DeviceInterface

    up = _device("ctr-up", "10.80.8.1", snmp_enabled=True, last_ping_ok=True)
    down = _device("ctr-down", "10.80.8.2", snmp_enabled=True, last_ping_ok=False)
    unknown = _device("ctr-unknown", "10.80.8.3", snmp_enabled=True)
    db_session.add_all([up, down, unknown])
    db_session.flush()
    for dev in (up, down, unknown):
        db_session.add(
            DeviceInterface(device_id=dev.id, name="sfp1", snmp_index=1, monitored=True)
        )
    db_session.commit()

    names = {
        device.name
        for device, _ in infrastructure_polling.monitored_interface_targets(db_session)
    }
    assert names == {"ctr-up", "ctr-unknown"}


def test_ping_transitions_drive_device_status(db_session, monkeypatch):
    device = _device("core-transition", "10.80.2.1")
    db_session.add(device)
    db_session.commit()

    # up
    monkeypatch.setattr(
        runtime.ping_service, "run_ping", lambda host, timeout_seconds=4: (True, 3.0)
    )
    runtime.ping_device(db_session, str(device.id))
    assert device.last_ping_ok is True
    assert device.ping_down_since is None
    assert device.status == DeviceStatus.online

    # down (default notification delay 0 -> offline immediately)
    monkeypatch.setattr(
        runtime.ping_service, "run_ping", lambda host, timeout_seconds=4: (False, None)
    )
    runtime.ping_device(db_session, str(device.id))
    assert device.last_ping_ok is False
    assert device.ping_down_since is not None
    assert device.status == DeviceStatus.offline

    # recovery
    monkeypatch.setattr(
        runtime.ping_service, "run_ping", lambda host, timeout_seconds=4: (True, 2.0)
    )
    runtime.ping_device(db_session, str(device.id))
    assert device.last_ping_ok is True
    assert device.ping_down_since is None
    assert device.status == DeviceStatus.online


def test_fetch_interface_octets_parses_snmpget_output(monkeypatch):
    from types import SimpleNamespace

    from app.services import snmp_probe

    device = SimpleNamespace(
        mgmt_ip="10.80.4.1",
        hostname=None,
        snmp_port=None,
        snmp_version="2c",
        snmp_community="enc",
    )
    monkeypatch.setattr(snmp_probe, "decrypt_credential", lambda value: "public")
    monkeypatch.setattr(snmp_probe.shutil, "which", lambda name: "/usr/bin/snmpget")

    captured = {}

    def _fake_run(args, **kwargs):
        captured["args"] = args
        return SimpleNamespace(
            returncode=0,
            stdout=(
                ".1.3.6.1.2.1.31.1.1.1.6.5 1500000\n"
                ".1.3.6.1.2.1.31.1.1.1.10.5 900000\n"
                ".1.3.6.1.2.1.31.1.1.1.6.7 No Such Instance currently exists\n"
                ".1.3.6.1.2.1.31.1.1.1.10.7 42\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(snmp_probe.subprocess, "run", _fake_run)

    readings = snmp_probe.fetch_interface_octets(device, [5, 7])

    assert "-v2c" in captured["args"]
    assert readings[5].in_octets == 1500000
    assert readings[5].out_octets == 900000
    assert readings[7].in_octets is None  # noSuchInstance -> absent
    assert readings[7].out_octets == 42


def test_fetch_interface_octets_unqueryable_device_returns_none(monkeypatch):
    from types import SimpleNamespace

    from app.services import snmp_probe

    monkeypatch.setattr(snmp_probe.shutil, "which", lambda name: "/usr/bin/snmpget")
    no_community = SimpleNamespace(
        mgmt_ip="10.80.4.2",
        hostname=None,
        snmp_port=None,
        snmp_version="2c",
        snmp_community=None,
    )
    assert snmp_probe.fetch_interface_octets(no_community, [1]) is None


def test_push_interface_counters_writes_prometheus_lines(db_session, monkeypatch):
    from app.models.network_monitoring import DeviceInterface
    from app.services.snmp_probe import InterfaceOctets

    device = _device("counter-router", "10.80.5.1", snmp_enabled=True)
    db_session.add(device)
    db_session.flush()
    iface = DeviceInterface(
        device_id=device.id, name="sfp-sfpplus1", snmp_index=5, monitored=True
    )
    excluded = DeviceInterface(
        device_id=device.id, name="ether2", snmp_index=6, monitored=False
    )
    db_session.add_all([iface, excluded])
    db_session.commit()

    monkeypatch.setattr(
        "app.services.snmp_probe.fetch_interface_octets",
        lambda dev, indexes, **kw: {5: InterfaceOctets(1500000, 900000)},
    )
    written = {}

    class _Writer:
        def write_prometheus_lines(self, lines, **kwargs):
            written["lines"] = lines
            written["kwargs"] = kwargs
            return SimpleNamespace(success=True, written=len(lines))

    monkeypatch.setattr(infrastructure_polling, "_writer", lambda: _Writer())

    result = infrastructure_polling.push_interface_counters(db_session)

    assert result == {
        "interface_devices": 1,
        "interface_lines": 2,
        "interface_write_failed": 0,
    }
    assert len(written["lines"]) == 2
    rx_line = next(line for line in written["lines"] if "in_octets" in line)
    assert f'device_id="{device.id}"' in rx_line
    assert f'interface_id="{iface.id}"' in rx_line
    assert 'snmp_index="5"' in rx_line
    assert " 1500000 " in rx_line


def test_push_interface_counters_no_targets_is_noop(db_session, monkeypatch):
    called = {"writer": False}
    monkeypatch.setattr(
        infrastructure_polling,
        "_writer",
        lambda: called.__setitem__("writer", True),
    )

    result = infrastructure_polling.push_interface_counters(db_session)

    assert result == {
        "interface_devices": 0,
        "interface_lines": 0,
        "interface_write_failed": 0,
    }
    assert called["writer"] is False


def test_push_interface_counters_surfaces_write_failure(db_session, monkeypatch):
    from app.models.network_monitoring import DeviceInterface
    from app.services.snmp_probe import InterfaceOctets

    device = _device("counter-router-fail", "10.80.5.2", snmp_enabled=True)
    db_session.add(device)
    db_session.flush()
    db_session.add(
        DeviceInterface(device_id=device.id, name="sfp1", snmp_index=5, monitored=True)
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.snmp_probe.fetch_interface_octets",
        lambda dev, indexes, **kw: {5: InterfaceOctets(100, 200)},
    )

    class _Writer:
        def write_prometheus_lines(self, lines, **kwargs):
            return SimpleNamespace(success=False, written=0, error="boom")

    monkeypatch.setattr(infrastructure_polling, "_writer", lambda: _Writer())

    result = infrastructure_polling.push_interface_counters(db_session)

    assert result["interface_lines"] == 2
    assert result["interface_write_failed"] == 2


def test_poll_results_feed_live_status_warmer(db_session, monkeypatch):
    # End-to-end seam check: a native ping failure surfaces as live_status
    # "down" (what outage auto-detect reads), and recovery flips it back "up".
    device = _device("edge-node", "10.80.3.1")
    db_session.add(device)
    db_session.commit()

    monkeypatch.setattr(
        runtime.ping_service, "run_ping", lambda host, timeout_seconds=4: (False, None)
    )
    runtime.ping_device(db_session, str(device.id))
    warm_topology_status(db_session)
    assert device.live_status == "down"

    monkeypatch.setattr(
        runtime.ping_service, "run_ping", lambda host, timeout_seconds=4: (True, 1.5)
    )
    runtime.ping_device(db_session, str(device.id))
    warm_topology_status(db_session)
    assert device.live_status == "up"
    assert device.live_status_at is not None
