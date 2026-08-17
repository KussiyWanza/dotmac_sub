"""Wiring tests for the NCC ①/pack routes, the weekly beat, and the on-demand
AI insight route. These exercise the route handlers directly (as functions
with a db_session), mirroring the repo's other report-route tests, and stub
the AI gateway the way test_ai_engine does.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from app.models.ai_insight import AIInsight
from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.models.ncc_reporting import NccWeeklyReportRun, NccWeeklyReportRunStatus
from app.models.support import Ticket
from app.services import control_registry, ncc_report_email
from app.services.ai import engine as ai_engine
from app.services.owner_commands import CommandContext
from app.web.admin import reports as reports_web


def _fake_current_user():
    return SimpleNamespace(id=None, username="admin", email="admin@example.test")


def _stub_admin(monkeypatch):
    import app.web.admin as admin_pkg

    monkeypatch.setattr(
        admin_pkg, "get_current_user", lambda request: _fake_current_user()
    )
    monkeypatch.setattr(admin_pkg, "get_sidebar_stats", lambda db: {})


def _request():
    # A TemplateResponse only needs request.scope for url_for; a minimal
    # Starlette-style stand-in is enough for these render smoke tests.
    return SimpleNamespace(
        scope={"type": "http"},
        query_params={},
        url=SimpleNamespace(path="/admin/reports"),
    )


# ── NCC complaints + pack ────────────────────────────────────────────────────


def test_ncc_complaints_export_streams_a_valid_xlsx(db_session):
    resp = reports_web.reports_ncc_complaints_export(db=db_session)
    assert resp.media_type.endswith("spreadsheetml.sheet")
    # The body is a zip (xlsx). Its magic bytes are PK\x03\x04.
    assert resp.body[:4] == b"PK\x03\x04"
    assert "attachment; filename=" in resp.headers["Content-Disposition"]


def test_ncc_complaints_page_renders_twenty_rows_and_pagination(
    db_session, monkeypatch
):
    _stub_admin(monkeypatch)
    monkeypatch.setattr(reports_web, "can", lambda request, permission: False)
    for index in range(21):
        db_session.add(
            Ticket(
                title=f"Rendered complaint {index:02d}",
                status="open",
                priority="normal",
                created_at=datetime(2026, 8, 1, 8, index, tzinfo=UTC),
            )
        )
    db_session.commit()

    response = reports_web.reports_ncc_complaints(
        _request(),
        date_from="2026-08-01",
        date_to="2026-08-31",
        page=1,
        per_page=20,
        db=db_session,
    )
    body = response.body.decode()

    assert "Rendered complaint 00" in body
    assert "Rendered complaint 19" in body
    assert "Rendered complaint 20" not in body
    assert "Showing 1 to 20 of 21 complaints" in body
    assert "Page 2" in body


def test_ncc_regulatory_pack_json_has_all_three_returns(db_session):
    import json

    resp = reports_web.reports_ncc_regulatory_pack(db=db_session)
    pack = json.loads(resp.body)
    assert set(pack) >= {"meta", "complaints", "subscribers", "financials", "staff"}
    # Every section reports its own availability; nothing fabricates.
    for key in ("complaints", "subscribers", "financials", "staff"):
        assert "available" in pack[key]


def test_ncc_pack_pdf_route_returns_a_document(db_session):
    resp = reports_web.reports_ncc_regulatory_pack_pdf(db=db_session)
    # weasyprint may be unavailable in CI; either a real PDF or the honest
    # HTML fallback is acceptable — never an empty/fake document.
    assert resp.media_type in (
        "application/pdf",
        "text/html; charset=utf-8",
    )
    assert resp.body


# ── the weekly beat ──────────────────────────────────────────────────────────


def test_ncc_report_email_beat_is_registered_and_default_off(db_session):
    from app.services import ncc_report_email

    # Default OFF: no setting row means disabled.
    assert ncc_report_email.is_enabled(db_session) is False
    configuration = ncc_report_email.get_configuration(db=db_session)
    assert configuration.send_day is ncc_report_email.NccWeekday.tuesday
    assert configuration.local_time.strftime("%H:%M") == "08:00"
    db_session.rollback()
    result = ncc_report_email.run_scheduled_ncc_report_email(db=db_session)
    assert result.decision is ncc_report_email.NccWeeklyRunDecision.disabled


def test_ncc_report_email_task_is_importable():
    from app.tasks import send_scheduled_ncc_report

    assert callable(send_scheduled_ncc_report)


def _weekly_configuration_command(*, enabled: bool = True):
    return ncc_report_email.UpdateNccWeeklyDeliveryConfigurationCommand(
        context=CommandContext.system(
            actor="pytest",
            scope="ncc.weekly_delivery_configuration",
            reason="test Tuesday report delivery",
        ),
        enabled=enabled,
        to_address="compliance@example.test",
        cc_addresses="copy@example.test",
        bcc_addresses="archive@example.test",
        sender_key="",
        subject="Tuesday NCC workbook",
        body_template=ncc_report_email.DEFAULT_BODY_TEMPLATE,
        local_time="08:00",
        timezone="Africa/Lagos",
        send_day="tuesday",
        lookback_days=7,
    )


def _run_command(observed_at: datetime):
    return ncc_report_email.RunNccWeeklyDeliveryCommand(
        context=CommandContext.system(
            actor="pytest",
            scope="ncc.weekly_report",
            reason="test scheduled NCC occurrence",
            idempotency_key=f"pytest-ncc:{observed_at.isoformat()}",
        ),
        observed_at=observed_at,
    )


def test_ncc_weekly_configuration_validates_tuesday_and_full_recipients():
    preview = ncc_report_email.preview_configuration(_weekly_configuration_command())

    assert preview.send_day is ncc_report_email.NccWeekday.tuesday
    assert preview.recipients.to == "compliance@example.test"
    assert preview.recipients.cc == ("copy@example.test",)
    assert preview.recipients.bcc == ("archive@example.test",)


def test_ncc_weekly_owner_only_queues_on_tuesday_after_local_time(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        ncc_report_email,
        "get_brand",
        lambda: {"app_url": "https://selfcare.dotmac.io"},
    )
    ncc_report_email.update_configuration(
        db=db_session, command=_weekly_configuration_command()
    )

    monday = ncc_report_email.run_due_delivery(
        db=db_session,
        command=_run_command(datetime(2026, 7, 20, 8, 0, tzinfo=UTC)),
    )
    before = ncc_report_email.run_due_delivery(
        db=db_session,
        command=_run_command(datetime(2026, 7, 21, 6, 59, tzinfo=UTC)),
    )
    first = ncc_report_email.run_due_delivery(
        db=db_session,
        command=_run_command(datetime(2026, 7, 21, 7, 0, tzinfo=UTC)),
    )
    second = ncc_report_email.run_due_delivery(
        db=db_session,
        command=_run_command(datetime(2026, 7, 21, 7, 5, tzinfo=UTC)),
    )

    assert monday.decision is ncc_report_email.NccWeeklyRunDecision.not_scheduled_day
    assert (
        before.decision is ncc_report_email.NccWeeklyRunDecision.before_scheduled_time
    )
    assert first.decision is ncc_report_email.NccWeeklyRunDecision.queued
    assert second.decision is ncc_report_email.NccWeeklyRunDecision.already_queued
    run = db_session.get(NccWeeklyReportRun, first.run_id)
    assert run is not None
    assert run.status is NccWeeklyReportRunStatus.queued
    assert run.artifact_content is not None
    assert run.artifact_content.startswith(b"PK\x03\x04")
    assert run.window_end.replace(tzinfo=UTC) == datetime(2026, 7, 21, 7, 0, tzinfo=UTC)
    assert run.notification is not None
    assert run.notification.metadata_["cc"] == ["copy@example.test"]
    assert run.notification.metadata_["bcc"] == ["archive@example.test"]


def test_ncc_weekly_failed_occurrence_is_durable_and_retried(db_session, monkeypatch):
    monkeypatch.setattr(
        ncc_report_email,
        "get_brand",
        lambda: {"app_url": "https://selfcare.dotmac.io"},
    )
    ncc_report_email.update_configuration(
        db=db_session, command=_weekly_configuration_command()
    )
    original_builder = ncc_report_email.ncc_workbook.build_workbook
    attempts = 0

    def flaky_builder(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated workbook failure")
        return original_builder(*args, **kwargs)

    monkeypatch.setattr(ncc_report_email.ncc_workbook, "build_workbook", flaky_builder)
    failed = ncc_report_email.run_due_delivery(
        db=db_session,
        command=_run_command(datetime(2026, 7, 21, 7, 0, tzinfo=UTC)),
    )
    retried = ncc_report_email.run_due_delivery(
        db=db_session,
        command=_run_command(datetime(2026, 7, 21, 7, 5, tzinfo=UTC)),
    )

    assert failed.decision is ncc_report_email.NccWeeklyRunDecision.failed
    assert failed.failure_code == (
        "communications.ncc_weekly_delivery.artifact_or_delivery_failed"
    )
    assert retried.decision is ncc_report_email.NccWeeklyRunDecision.queued
    assert retried.run_id == failed.run_id


# ── AI insight route ─────────────────────────────────────────────────────────


def _enable_generation(db):
    control = control_registry._CONTROLS["ai.generation"]
    db.add(
        DomainSetting(
            domain=SettingDomain.modules,
            key=control_registry.canonical_setting_key(control),
            value_type=SettingValueType.boolean,
            value_text="true",
            is_active=True,
        )
    )
    db.flush()


class _Gateway:
    def enabled(self, db):
        return True

    def generate_with_fallback(self, db, **kwargs):
        return (
            SimpleNamespace(
                content='{"title": "Breaches cluster", "summary": "s",'
                ' "risk_level": "high", "recommended_actions": ["look here"]}',
                provider="vllm",
                model="qwen2.5",
                tokens_in=100,
                tokens_out=50,
            ),
            {"endpoint": "primary"},
        )


def test_insight_route_generates_and_renders(db_session, monkeypatch):
    _stub_admin(monkeypatch)
    _enable_generation(db_session)
    with (
        patch.object(ai_engine, "_gateway", lambda: _Gateway()),
        patch.object(
            reports_web.ticket_sla_reports_service,
            "summary",
            lambda db, a, b: {
                "total_clocks": 40,
                "total_breaches": 12,
                "breach_rate": 0.3,
            },
        ),
    ):
        resp = reports_web.reports_generate_insight(
            _request(), "ticket_sla_advisor", db=db_session
        )
    assert resp.status_code == 200
    # Exactly one insight persisted, through the single writer.
    assert db_session.query(AIInsight).count() == 1


def test_insight_route_degrades_gracefully_when_disabled(db_session, monkeypatch):
    _stub_admin(monkeypatch)
    # ai.generation OFF (no row) → advise() raises AIEngineError → graceful msg.
    with patch.object(
        reports_web.ticket_sla_reports_service,
        "summary",
        lambda db, a, b: {"total_clocks": 0, "total_breaches": 0, "breach_rate": 0.0},
    ):
        resp = reports_web.reports_generate_insight(
            _request(), "ticket_sla_advisor", db=db_session
        )
    # Not a 500 — a rendered partial with the disabled message.
    assert resp.status_code == 200
    assert db_session.query(AIInsight).count() == 0


def test_insight_route_unknown_advisor_is_404(db_session, monkeypatch):
    _stub_admin(monkeypatch)
    resp = reports_web.reports_generate_insight(
        _request(), "no_such_advisor", db=db_session
    )
    assert resp.status_code == 404


def test_ai_engine_declared_and_out_of_writer_baseline():
    from pathlib import Path

    from app.services import sot_relationships as sr

    names = {s.name for d in sr.DOMAIN_SOT_RELATIONSHIPS for s in d.services}
    assert "ai.generation" in names
    baseline = Path("tests/architecture/sot_writer_baseline.txt").read_text()
    assert "app.services.ai.engine" not in baseline.split()
