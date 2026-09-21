from datetime import datetime, time
from uuid import uuid4

import pytest

from app.services import ncc_report_email
from app.services.owner_commands import CommandContext


def _command(**overrides):
    values = {
        "enabled": True,
        "to_address": "compliance@example.test",
        "cc_addresses": "copy@example.test",
        "bcc_addresses": "archive@example.test",
        "sender_key": "regulatory",
        "subject": "Weekly NCC CSV",
        "body_template": ncc_report_email.DEFAULT_BODY_TEMPLATE,
        "local_time": "08:00",
        "timezone": "Africa/Lagos",
        "send_day": "tuesday",
        "lookback_days": 7,
    }
    values.update(overrides)
    return ncc_report_email.UpdateNccWeeklyDeliveryConfigurationCommand(
        context=CommandContext.system(
            actor="pytest",
            scope="ncc.weekly_delivery_configuration",
            reason="validate weekly delivery configuration",
        ),
        **values,
    )


def test_registered_default_is_tuesday():
    preview = ncc_report_email.preview_configuration(_command())

    assert preview.send_day is ncc_report_email.NccWeekday.tuesday
    assert preview.local_time.strftime("%H:%M") == "08:00"
    assert preview.timezone == "Africa/Lagos"


def test_enabled_configuration_requires_primary_recipient():
    with pytest.raises(ncc_report_email.NccWeeklyDeliveryError) as exc_info:
        ncc_report_email.preview_configuration(_command(to_address=""))

    assert exc_info.value.code.endswith(".invalid_configuration")


def test_configuration_rejects_unsupported_body_placeholder():
    with pytest.raises(ncc_report_email.NccWeeklyDeliveryError) as exc_info:
        ncc_report_email.preview_configuration(
            _command(body_template="Report for {recipient_secret}")
        )

    assert exc_info.value.details == {"field": "body_template"}


def test_local_observation_handles_naive_scheduler_timestamp():
    observed = ncc_report_email._local_observation(
        datetime(2026, 7, 21, 7, 0), "Africa/Lagos"
    )

    assert observed.tzinfo is not None
    assert observed.weekday() == ncc_report_email.NccWeekday.tuesday.python_weekday
    assert observed.hour == 8


def _configuration(*, body_template: str | None = None):
    return ncc_report_email.NccWeeklyDeliveryConfiguration(
        enabled=True,
        recipients=ncc_report_email.NccWeeklyRecipientSet(
            to="compliance@example.test", cc=(), bcc=()
        ),
        sender_key="regulatory",
        subject="Weekly NCC CSV",
        body_template=body_template or ncc_report_email.DEFAULT_BODY_TEMPLATE,
        local_time=time(8, 0),
        timezone="Africa/Lagos",
        send_day=ncc_report_email.NccWeekday.tuesday,
        lookback_days=7,
    )


def test_default_body_template_does_not_expose_internal_readiness_counter():
    assert "not_filable_count" not in ncc_report_email.DEFAULT_BODY_TEMPLATE
    assert "Rows not yet filable" not in ncc_report_email.DEFAULT_BODY_TEMPLATE


def test_configuration_rejects_not_filable_count_placeholder():
    with pytest.raises(ncc_report_email.NccWeeklyDeliveryError) as exc_info:
        ncc_report_email.preview_configuration(
            _command(body_template="Rows not yet filable: {not_filable_count}")
        )

    assert exc_info.value.details == {"field": "body_template"}


def test_render_body_strips_legacy_not_filable_line(monkeypatch):
    monkeypatch.setattr(
        ncc_report_email,
        "get_brand",
        lambda: {"app_url": "https://selfcare.example.test"},
    )
    legacy_template = (
        "Dear NCC Team,\n"
        "Rows included: {row_count}.\n"
        "Rows not yet filable: {not_filable_count}.\n"
        "Download: {download_url}"
    )

    body_text, body_html = ncc_report_email._render_body(
        _configuration(body_template=legacy_template),
        run_id=uuid4(),
        row_count=170,
        not_filable_count=148,
        report_date="2026-08-25",
    )

    assert "Rows included: 170." in body_text
    assert "not yet fil" not in body_text.lower()
    assert "Rows not yet filable: 148." not in body_text
    assert "not yet fil" not in body_html.lower()


def test_report_delivery_rejects_not_filable_rows():
    with pytest.raises(ncc_report_email.NccWeeklyDeliveryError) as exc_info:
        ncc_report_email._raise_if_report_not_filable(
            row_count=170, not_filable_count=148
        )

    assert exc_info.value.code.endswith(".report_not_filable")
    assert exc_info.value.details == {
        "row_count": 170,
        "not_filable_count": 148,
    }
