"""Tests for secrets service."""

import httpx
import pytest
from fastapi import HTTPException

from app.services import secrets
from tests.mocks import FakeHTTPXResponse


def test_is_openbao_ref_valid():
    """Test detecting valid OpenBao references."""
    assert secrets.is_openbao_ref("openbao://secret/data/myapp#password") is True
    assert secrets.is_openbao_ref("openbao://kv/data/config#api_key") is True
    assert secrets.is_openbao_ref("vault://secret/data/creds#token") is True


def test_is_openbao_ref_invalid():
    """Test detecting invalid OpenBao references."""
    assert secrets.is_openbao_ref("plain-text-secret") is False
    assert secrets.is_openbao_ref("https://example.com") is False
    assert secrets.is_openbao_ref("") is False
    assert secrets.is_openbao_ref(None) is False


def test_resolve_secret_passthrough_plain_value():
    """Test that plain values are passed through unchanged."""
    result = secrets.resolve_secret("my-plain-secret")
    assert result == "my-plain-secret"


def test_resolve_secret_none_value():
    """Test that None values return None."""
    result = secrets.resolve_secret(None)
    assert result is None


def test_resolve_openbao_ref_kv_v2(monkeypatch):
    """Test resolving OpenBao KV v2 reference."""
    # Mock environment
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "2")

    # Mock httpx response
    mock_response = FakeHTTPXResponse(
        json_data={
            "data": {
                "data": {
                    "password": "secret-password-123",
                }
            }
        }
    )

    def mock_get(url, **kwargs):
        return mock_response

    # Patch httpx.get
    import httpx

    monkeypatch.setattr(httpx, "get", mock_get)

    result = secrets.resolve_openbao_ref("openbao://secret/data/myapp#password")
    assert result == "secret-password-123"


def test_resolve_openbao_ref_kv_v1(monkeypatch):
    """Test resolving OpenBao KV v1 reference."""
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "1")

    mock_response = FakeHTTPXResponse(
        json_data={
            "data": {
                "api_key": "key-abc-123",
            }
        }
    )

    def mock_get(url, **kwargs):
        return mock_response

    import httpx

    monkeypatch.setattr(httpx, "get", mock_get)

    result = secrets.resolve_openbao_ref("openbao://kv/myapp#api_key")
    assert result == "key-abc-123"


def test_resolve_openbao_ref_with_namespace(monkeypatch):
    """Test resolving OpenBao reference with namespace."""
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_NAMESPACE", "my-namespace")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "2")

    captured_headers = {}

    def mock_get(url, headers=None, **kwargs):
        captured_headers.update(headers or {})
        return FakeHTTPXResponse(json_data={"data": {"data": {"secret": "ns-value"}}})

    import httpx

    monkeypatch.setattr(httpx, "get", mock_get)

    result = secrets.resolve_openbao_ref("openbao://secret/data/app#secret")
    assert result == "ns-value"
    assert "X-Vault-Namespace" in captured_headers


def test_resolve_openbao_ref_missing_field_error(monkeypatch):
    """Test error when requested field is missing."""
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "2")

    mock_response = FakeHTTPXResponse(
        json_data={
            "data": {
                "data": {
                    "other_field": "some-value",
                }
            }
        }
    )

    def mock_get(url, **kwargs):
        return mock_response

    import httpx

    monkeypatch.setattr(httpx, "get", mock_get)

    with pytest.raises(HTTPException) as exc_info:
        secrets.resolve_openbao_ref("openbao://secret/data/myapp#missing_field")
    assert exc_info.value.status_code == 500


def test_resolve_openbao_ref_missing_secret_returns_404(monkeypatch):
    """Test missing OpenBao secret path is surfaced as not found."""
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "2")

    request = httpx.Request("GET", "https://vault.test.local:8200/v1/secret/data/myapp")
    response = httpx.Response(404, request=request)
    error = httpx.HTTPStatusError("not found", request=request, response=response)

    def mock_get(url, **kwargs):
        raise error

    monkeypatch.setattr(httpx, "get", mock_get)

    with pytest.raises(HTTPException) as exc_info:
        secrets.resolve_openbao_ref("openbao://secret/data/myapp#password")
    assert exc_info.value.status_code == 404


def test_get_secret_uses_default_for_missing_secret(monkeypatch, caplog):
    """Test missing OpenBao secret falls back without error log spam."""
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_KV_VERSION", "2")

    request = httpx.Request(
        "GET", "https://vault.test.local:8200/v1/secret/data/zabbix"
    )
    response = httpx.Response(404, request=request)
    error = httpx.HTTPStatusError("not found", request=request, response=response)

    def mock_get(url, **kwargs):
        raise error

    monkeypatch.setattr(httpx, "get", mock_get)

    with caplog.at_level("DEBUG"):
        result = secrets.get_secret("zabbix", "api_token", default="")

    assert result == ""
    assert "OpenBao secret missing at" in caplog.text
    assert "OpenBao request failed" not in caplog.text


def test_openbao_cache_expires_across_time_buckets(monkeypatch):
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("OPENBAO_CACHE_TTL_SECONDS", "10")
    clock = {"value": 1.0}
    monkeypatch.setattr(secrets.time, "monotonic", lambda: clock["value"])
    calls = {"count": 0}

    def mock_get(_url, **_kwargs):
        calls["count"] += 1
        return FakeHTTPXResponse(
            json_data={"data": {"data": {"value": f"v{calls['count']}"}}}
        )

    monkeypatch.setattr(httpx, "get", mock_get)

    assert secrets.resolve_openbao_ref("bao://secret/cache#value") == "v1"
    assert secrets.resolve_openbao_ref("bao://secret/cache#value") == "v1"
    clock["value"] = 11.0
    assert secrets.resolve_openbao_ref("bao://secret/cache#value") == "v2"
    assert calls["count"] == 2


def test_openbao_token_file_precedes_environment_token(monkeypatch, tmp_path):
    token_file = tmp_path / "openbao-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("OPENBAO_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("OPENBAO_TOKEN", "environment-token")

    assert secrets._read_openbao_token() == "file-token"


def test_list_secret_paths_expands_nested_openbao_metadata(monkeypatch):
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")

    responses = {
        "https://vault.test.local:8200/v1/secret/metadata/?list=true": {
            "data": {"keys": ["auth", "integrations/"]}
        },
        "https://vault.test.local:8200/v1/secret/metadata/integrations?list=true": {
            "data": {"keys": ["meta_social", "payments/"]}
        },
        "https://vault.test.local:8200/v1/secret/metadata/integrations/payments?list=true": {
            "data": {"keys": ["paystack"]}
        },
    }

    def mock_get(url, **_kwargs):
        return FakeHTTPXResponse(json_data=responses[url])

    monkeypatch.setattr(httpx, "get", mock_get)

    assert secrets.list_secret_paths() == (
        "auth",
        "integrations/meta_social",
        "integrations/payments/paystack",
    )


def test_list_secret_paths_keeps_visible_leaves_when_subtree_is_forbidden(
    monkeypatch,
):
    monkeypatch.setenv("OPENBAO_ADDR", "https://vault.test.local:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")

    def mock_get(url, **_kwargs):
        if url.endswith("/metadata/?list=true"):
            return FakeHTTPXResponse(
                json_data={"data": {"keys": ["auth", "restricted/"]}}
            )
        return FakeHTTPXResponse(status_code=403)

    monkeypatch.setattr(httpx, "get", mock_get)

    assert secrets.list_secret_paths() == ("auth",)
