from __future__ import annotations

import smtplib
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from app import team_inbox_smtp as smtp_runtime
from app.config import settings
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.team_inbox import InboxMessage, TeamInboxEmailRoute
from app.services import team_inbox_health, team_inbox_smtp_inbound

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _configure_smtp(
    monkeypatch,
    *,
    enabled: bool = True,
    recipients: str = "support@dotmac.io",
    probe_recipient: str = "",
    probe_interval_seconds: int = 900,
    probe_timeout_seconds: int = 120,
) -> None:
    configured = replace(
        settings,
        team_inbox_smtp_inbound_enabled=enabled,
        team_inbox_smtp_inbound_recipients=recipients,
        team_inbox_smtp_inbound_host="127.0.0.1",
        team_inbox_smtp_inbound_port=2525,
        team_inbox_smtp_fallback_service_team_id="",
        team_inbox_smtp_probe_recipient=probe_recipient,
        team_inbox_smtp_probe_interval_seconds=probe_interval_seconds,
        team_inbox_smtp_probe_timeout_seconds=probe_timeout_seconds,
        team_inbox_smtp_log_level="INFO",
    )
    monkeypatch.setattr(team_inbox_smtp_inbound, "settings", configured)
    monkeypatch.setattr(smtp_runtime, "settings", configured)


def test_runtime_refuses_implicit_enablement(monkeypatch):
    _configure_smtp(monkeypatch, enabled=False)

    assert (
        smtp_runtime.serve_forever(install_signal_handlers=False)
        == smtp_runtime.EXIT_CONFIGURATION_ERROR
    )


def test_runtime_requires_an_allowed_recipient(monkeypatch):
    _configure_smtp(monkeypatch, recipients="")

    assert (
        smtp_runtime.serve_forever(install_signal_handlers=False)
        == smtp_runtime.EXIT_CONFIGURATION_ERROR
    )


def test_runtime_supervises_one_owner_controller(monkeypatch):
    stopped: list[bool] = []
    shutdown = threading.Event()
    shutdown.set()
    _configure_smtp(monkeypatch)
    monkeypatch.setattr(
        team_inbox_smtp_inbound,
        "start_smtp_inbound_server",
        lambda: True,
    )
    monkeypatch.setattr(
        team_inbox_smtp_inbound,
        "stop_smtp_inbound_server",
        lambda: stopped.append(True),
    )

    assert (
        smtp_runtime.serve_forever(stop_event=shutdown, install_signal_handlers=False)
        == 0
    )
    assert stopped == [True]


def test_controller_liveness_uses_aiosmtpd_private_thread(monkeypatch):
    class _Thread:
        def is_alive(self) -> bool:
            return True

    class _Controller:
        _thread = _Thread()

    monkeypatch.setattr(team_inbox_smtp_inbound, "_SMTP_CONTROLLER", _Controller())

    assert team_inbox_smtp_inbound.smtp_inbound_server_running() is True


def test_readiness_uses_smtp_noop(monkeypatch):
    calls: list[tuple[str, int, float] | str] = []

    class _Client:
        def __init__(self, *, host: str, port: int, timeout: float):
            calls.append((host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def noop(self):
            calls.append("noop")
            return 250, b"OK"

    monkeypatch.setattr(smtplib, "SMTP", _Client)

    assert smtp_runtime.smtp_readiness(host="smtp", port=2526, timeout=3.0) is True
    assert calls == [("smtp", 2526, 3.0), "noop"]


def test_probe_header_survives_owner_ingestion(db_session):
    team = ServiceTeam(name="SMTP probe", team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    db_session.add(
        TeamInboxEmailRoute(
            service_team_id=team.id,
            email_address="probe@dotmac.io",
            is_active=True,
        )
    )
    db_session.commit()
    message, external_message_id = smtp_runtime.build_probe_message(
        sender="probe-sender@example.com",
        recipient="probe@dotmac.io",
    )

    result = team_inbox_smtp_inbound.handle_smtp_message(
        db_session,
        mail_from="probe-sender@example.com",
        rcpt_to=["probe@dotmac.io"],
        data=message.as_bytes(),
        allowed_recipients={"probe@dotmac.io"},
    )
    row = db_session.get(InboxMessage, result.message_id)

    assert result.kind == "received"
    assert row.external_message_id == external_message_id
    assert row.metadata_["smtp_probe"] == smtp_runtime.PROBE_HEADER_VALUE
    db_session.commit()

    delivered = team_inbox_health.verify_smtp_probe_delivery(
        db_session,
        external_message_id=external_message_id,
    )
    db_session.refresh(row)

    assert delivered is not None
    assert row.metadata_[team_inbox_health.SMTP_PROBE_VERIFIED_KEY] is True


def test_probe_submission_uses_canonical_email_owner(monkeypatch):
    message, external_message_id = smtp_runtime.build_probe_message(
        sender="unused@example.com",
        recipient="probe@dotmac.io",
    )
    closed: list[bool] = []
    calls: list[dict[str, Any]] = []

    class _Session:
        def close(self) -> None:
            closed.append(True)

    session = _Session()

    def _deliver(db, **kwargs) -> bool:
        assert db is session
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        "app.tasks.notifications.deliver_inbound_smtp_health_probe",
        _deliver,
    )

    assert (
        smtp_runtime.send_probe_message(
            message,
            session_factory=lambda: session,
        )
        is True
    )
    assert closed == [True]
    assert calls[0]["recipient"] == "probe@dotmac.io"
    assert calls[0]["message_id"] == external_message_id
    assert calls[0]["marker"] == smtp_runtime.PROBE_HEADER_VALUE


def test_e2e_probe_requires_recipient_allowlist(monkeypatch):
    _configure_smtp(monkeypatch)

    result = smtp_runtime.run_e2e_probe(
        recipient="probe@dotmac.io",
        timeout=1.0,
    )

    assert result == smtp_runtime.EXIT_CONFIGURATION_ERROR


def test_e2e_probe_submits_and_reports_committed_message(monkeypatch, capsys):
    submitted: list[str] = []
    _configure_smtp(monkeypatch, recipients="probe@dotmac.io")

    def _send(message: Any) -> bool:
        submitted.append(str(message["Message-ID"]))
        return True

    monkeypatch.setattr("app.team_inbox_smtp.send_probe_message", _send)
    monkeypatch.setattr(
        "app.team_inbox_smtp.wait_for_probe_delivery",
        lambda _message_id, *, timeout: {
            "message_id": "message-id",
            "conversation_id": "conversation-id",
            "external_message_id": "external-id",
        },
    )

    result = smtp_runtime.run_e2e_probe(
        recipient="probe@dotmac.io",
        timeout=2.0,
    )

    assert result == 0
    assert len(submitted) == 1
    assert submitted[0].startswith("<dotmac-smtp-probe-")
    assert '"status": "ok"' in capsys.readouterr().out


def test_runtime_runs_continuous_probe_when_recipient_is_configured(monkeypatch):
    calls: list[dict[str, Any]] = []

    class _StopAfterFirstIteration:
        calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    _configure_smtp(
        monkeypatch,
        recipients="probe@dotmac.io",
        probe_recipient="probe@dotmac.io",
        probe_interval_seconds=300,
    )
    monkeypatch.setattr(
        team_inbox_smtp_inbound,
        "start_smtp_inbound_server",
        lambda: True,
    )
    monkeypatch.setattr(
        team_inbox_smtp_inbound,
        "stop_smtp_inbound_server",
        lambda: None,
    )
    monkeypatch.setattr(
        team_inbox_smtp_inbound,
        "smtp_inbound_server_running",
        lambda: True,
    )
    monkeypatch.setattr(
        smtp_runtime,
        "run_e2e_probe",
        lambda **kwargs: calls.append(kwargs) or 0,
    )

    result = smtp_runtime.serve_forever(
        stop_event=_StopAfterFirstIteration(),  # type: ignore[arg-type]
        install_signal_handlers=False,
    )

    assert result == 0
    assert len(calls) == 1
    assert calls[0]["recipient"] == "probe@dotmac.io"
    assert calls[0]["timeout"] == 120.0
    assert calls[0]["emit_result"] is False


def test_deployment_contract_is_profile_gated_and_loopback_only():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    makefile = (PROJECT_ROOT / "Makefile").read_text(encoding="utf-8")
    main = (PROJECT_ROOT / "app/main.py").read_text(encoding="utf-8")

    smtp_service = compose.split("  team-inbox-smtp:", maxsplit=1)[1].split(
        "  vmagent:", maxsplit=1
    )[0]
    assert "- smtp-inbound" in smtp_service
    assert (
        "127.0.0.1:${TEAM_INBOX_SMTP_INBOUND_PUBLISH_PORT:-2525}:2525" in smtp_service
    )
    assert "- app.team_inbox_smtp\n    - serve" in smtp_service
    assert "- readiness" in smtp_service
    assert "prod-smtp-inbound-up:" in makefile
    assert "--no-scale team-inbox-smtp" not in makefile
    assert "mem_limit: 512m" in smtp_service
    assert "prod-smtp-inbound-probe:" in makefile
    assert "start_smtp_inbound_server" not in main
