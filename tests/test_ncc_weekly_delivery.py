from datetime import UTC, datetime

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
        "subject": "Weekly NCC workbook",
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
