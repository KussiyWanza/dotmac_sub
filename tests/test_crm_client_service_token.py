"""DB-free CRM HTTP substrate authentication behavior."""

from unittest.mock import MagicMock, patch

import pytest

from app.services.crm_client import (
    _REACHABILITY_CIRCUIT,
    CRMClient,
    CRMClientError,
)


@pytest.fixture(autouse=True)
def _reset_circuit():
    _REACHABILITY_CIRCUIT.reset()
    yield
    _REACHABILITY_CIRCUIT.reset()


def _capturing_client(captured: dict):
    """Pooled-client stand-in that records the headers of the outbound request."""

    def factory(*_a, **_k):
        inner = MagicMock()

        def _request(*_ra, **rk):
            captured["headers"] = rk.get("headers", {})
            resp = MagicMock()
            resp.status_code = 200
            resp.text = "{}"
            resp.json.return_value = {}
            return resp

        inner.request.side_effect = _request
        return inner

    return factory


def test_service_token_sends_api_key():
    client = CRMClient("https://crm.example", service_token="svc-key-123")
    captured: dict = {}
    with patch("app.services.crm_client._pooled_client", _capturing_client(captured)):
        client._request("GET", "/api/v1/subscribers")

    assert captured["headers"]["X-API-Key"] == "svc-key-123"
    assert "Authorization" not in captured["headers"]


def test_no_service_token_fails_loudly():
    """The staff username/password->JWT fallback is retired (auth S1)."""
    client = CRMClient("https://crm.example", "user", "pass")
    with pytest.raises(CRMClientError, match="staff-credential login"):
        client._auth_headers()


def test_per_request_headers_still_override_but_api_key_stays():
    """Portal-scoped reads pass their own Authorization; the key rides alongside."""
    client = CRMClient("https://crm.example", service_token="svc-key-123")
    captured: dict = {}
    with patch("app.services.crm_client._pooled_client", _capturing_client(captured)):
        client._request(
            "GET", "/api/v1/portal/x", headers={"Authorization": "Bearer portal-tok"}
        )

    assert captured["headers"]["X-API-Key"] == "svc-key-123"
    assert captured["headers"]["Authorization"] == "Bearer portal-tok"


def test_auth_headers_requires_base_url():
    client = CRMClient("", "user", "pass", service_token="svc-key-123")
    with pytest.raises(CRMClientError):
        client._auth_headers()


def test_global_settings_factory_is_removed():
    from app.services import crm_client as mod

    assert not hasattr(mod, "get_crm_client")


def test_create_widget_session_uses_trusted_internal_endpoint():
    client = CRMClient("https://crm.example", service_token="svc-key-123")
    with patch.object(
        client,
        "_request",
        return_value={
            "session_id": "session-1",
            "visitor_token": "opaque",
        },
    ) as request:
        result = client.create_widget_session(
            config_id="config-1",
            email="customer@example.com",
            name="Customer",
            crm_subscriber_id="subscriber-1",
            metadata={"source": "dotmac_sub_portal"},
        )

    assert result["visitor_token"] == "opaque"
    request.assert_called_once_with(
        "POST",
        "/api/v1/widget/internal/session",
        json_data={
            "config_id": "config-1",
            "email": "customer@example.com",
            "name": "Customer",
            "crm_subscriber_id": "subscriber-1",
            "metadata": {"source": "dotmac_sub_portal"},
        },
    )
