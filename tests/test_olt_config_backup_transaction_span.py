"""The OLT config backup must not hold a session across fleet SSH.

Serial SSH across the fleet is unbounded latency this application does not
control. Holding the read transaction open across it exceeded PostgreSQL's
``idle_in_transaction_session_timeout``, and the run then lost every write
after the devices had already answered.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.network.olt_protocol_adapters import OltConnectionConfig
from app.tasks import olt_config_backup as task_module


def _target(name: str) -> task_module._BackupTarget:
    return task_module._BackupTarget(
        connection=OltConnectionConfig(
            id=uuid4(),
            name=name,
            hostname=None,
            mgmt_ip="10.0.0.1",
            vendor="huawei",
            model="MA5608T",
            firmware_version=None,
            software_version=None,
            ssh_username="admin",
            ssh_password="secret",
            ssh_port=22,
        ),
        serial="SN-1234",
    )


@pytest.fixture
def _no_persistence(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the order of SSH fetches and session acquisitions."""
    events: list[str] = []

    def fake_create_session() -> MagicMock:
        events.append("session")
        return MagicMock()

    monkeypatch.setattr(
        task_module.db_session_adapter, "create_session", fake_create_session
    )
    monkeypatch.setattr(
        task_module.backup_alerts, "queue_backup_failure_notification", MagicMock()
    )
    monkeypatch.setattr(task_module, "_cleanup_old_backups", lambda *a, **k: 0)
    return events


def test_every_ssh_fetch_happens_before_any_session_is_opened(
    monkeypatch: pytest.MonkeyPatch, _no_persistence: list[str], tmp_path
) -> None:
    events = _no_persistence
    monkeypatch.setattr(task_module, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(
        task_module, "_load_backup_targets", lambda: [_target("a"), _target("b")]
    )

    def fake_fetch(target: task_module._BackupTarget) -> str:
        events.append("fetch")
        return f"config for {target.name}\n"

    monkeypatch.setattr(task_module, "_fetch_running_config_via_ssh", fake_fetch)

    result = task_module.backup_all_olts()

    assert events.count("fetch") == 2
    assert "session" in events, "phase 3 must open a session to persist"
    # The whole point: no session is bound while the fleet SSH runs.
    assert events.index("session") > max(
        index for index, event in enumerate(events) if event == "fetch"
    )
    assert result["backed_up"] == 2


def test_ssh_phase_uses_detached_values_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fetch must read nothing an OltConnectionConfig cannot supply.

    Guards the failure mode behind the 2026-08-04 commissioning outage, where a
    detached stand-in lacked an attribute its call chain read.
    """
    target = _target("gpon-jabi-1")
    captured: dict[str, object] = {}

    class _Adapter:
        def fetch_running_config(self) -> MagicMock:
            result = MagicMock()
            result.success = True
            result.data = {"config_text": "interface gpon 0/1\n"}
            return result

    import app.services.network.olt_protocol_adapters as adapters

    def fake_from_config(config: OltConnectionConfig) -> _Adapter:
        captured["config"] = config
        return _Adapter()

    monkeypatch.setattr(adapters, "get_protocol_adapter_from_config", fake_from_config)

    text = task_module._fetch_running_config_via_ssh(target)

    assert captured["config"] is target.connection
    assert text is not None
    assert "# OLT Full Running Config: gpon-jabi-1" in text
    assert "# Serial: SN-1234" in text
    assert "interface gpon 0/1" in text
