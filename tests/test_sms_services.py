"""Tests for SMS service."""

from unittest.mock import patch

import httpx

from app.services import sms as sms_service


def test_send_sms_twilio_auth_failure_logs(caplog):
    response = httpx.Response(
        401,
        request=httpx.Request(
            "POST", "https://api.twilio.com/2010-04-01/Accounts/acct/Messages.json"
        ),
        json={"message": "Authentication failed"},
    )

    with patch("httpx.post", return_value=response):
        with caplog.at_level("ERROR"):
            success, sid, error = sms_service._send_via_twilio(
                "acct",
                "secret",
                "+15550001111",
                "+15550002222",
                "Hello",
            )

    assert success is False
    assert sid is None
    assert "Authentication failed" in error
    assert "sms_auth_failed provider=twilio" in caplog.text


def test_send_via_webhook_non_json_2xx_reports_unverifiable_not_crash(caplog):
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://example.test/webhook"),
        content=b"not-json",
    )

    with patch("httpx.post", return_value=response):
        with caplog.at_level("WARNING"):
            success, external_id, error = sms_service._send_via_webhook(
                "https://example.test/webhook",
                None,
                "+15550002222",
                "Hello",
            )

    assert success is True
    assert external_id is None
    assert error == sms_service._UNVERIFIABLE_SENTINEL
    assert "sms_webhook_unverifiable" in caplog.text


def test_send_via_webhook_2xx_missing_message_id_reports_unverifiable(caplog):
    response = httpx.Response(
        202,
        request=httpx.Request("POST", "https://example.test/webhook"),
        json={"status": "queued"},
    )

    with patch("httpx.post", return_value=response):
        with caplog.at_level("WARNING"):
            success, external_id, error = sms_service._send_via_webhook(
                "https://example.test/webhook",
                None,
                "+15550002222",
                "Hello",
            )

    assert success is True
    assert external_id is None
    assert error == sms_service._UNVERIFIABLE_SENTINEL
    assert "sms_webhook_unverifiable" in caplog.text
