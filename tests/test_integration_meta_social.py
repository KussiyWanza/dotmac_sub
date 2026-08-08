from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from app.models.integration_platform import IntegrationInstallation
from app.services.integrations import meta_social_capability
from app.services.integrations.connectors import meta_social_runtime
from app.services.integrations.meta_social_contracts import (
    MetaDirectMessageCommand,
    MetaSocialChannel,
)
from app.services.integrations.meta_social_installation import (
    META_SOCIAL_CONFIGURATION_SCOPE,
    ConfigureMetaSocialInstallationCommand,
    configure_meta_social_installation,
    get_meta_social_installation_projection,
)
from app.services.integrations.runtime import (
    OperationEnvelope,
    OperationStatus,
    OperationTrigger,
    ValidationResult,
)
from app.services.owner_commands import CommandContext
from app.services.web_integrations_meta_social import (
    MetaSocialConfigFormCommand,
    save_config,
)


def _context() -> CommandContext:
    return CommandContext.system(
        actor="test.meta",
        scope=META_SOCIAL_CONFIGURATION_SCOPE,
        reason="Test Meta social configuration",
        idempotency_key="test-meta-social-config",
    )


def _command(**overrides: str) -> ConfigureMetaSocialInstallationCommand:
    values = {
        "auth_mode": "individual",
        "app_id": "app-1",
        "facebook_page_id": "page-1",
        "instagram_account_id": "ig-1",
        "graph_version": "v21.0",
        "webhook_url": "https://sub.example.test/api/v1/webhooks/meta",
        "meta_oauth_access_token_ref": (
            "bao://secret/integrations/meta_social#meta_oauth_access_token"
        ),
        "facebook_page_access_token_ref": (
            "bao://secret/integrations/meta_social#facebook_page_access_token"
        ),
        "instagram_login_access_token_ref": (
            "bao://secret/integrations/meta_social#instagram_login_access_token"
        ),
        "webhook_signing_secret_ref": (
            "bao://secret/integrations/meta_social#webhook_signing_secret"
        ),
        "webhook_verify_token_ref": (
            "bao://secret/integrations/meta_social#webhook_verify_token"
        ),
        "environment": "test",
    }
    values.update(overrides)
    return ConfigureMetaSocialInstallationCommand(**values)


def _envelope(
    *, channel: MetaSocialChannel, preview: bool = False
) -> OperationEnvelope:
    return OperationEnvelope(
        operation_id=uuid4(),
        correlation_id="test:meta:1",
        installation_id=uuid4(),
        capability_binding_id=uuid4(),
        capability_id=meta_social_runtime.META_SOCIAL_SEND_CAPABILITY,
        connector_key="meta.social",
        connector_version="1.1.0",
        manifest_digest="a" * 64,
        config_revision_id=uuid4(),
        trigger=OperationTrigger.event,
        idempotency_key="test-meta-operation",
        deadline_at=datetime.now(UTC) + timedelta(seconds=30),
        payload={
            "action": "send_direct_message",
            "params": {
                "channel": channel.value,
                "provider_account_id": (
                    "page-1"
                    if channel is MetaSocialChannel.facebook_messenger
                    else "ig-1"
                ),
                "recipient_id": "recipient-1",
                "body": "Hello",
                "preview": preview,
            },
        },
    )


def _config() -> dict[str, object]:
    return {
        "provider": "meta_social",
        "auth_mode": "individual",
        "app_id": "app-1",
        "facebook_page_id": "page-1",
        "facebook_auth_mode": "page_access_token",
        "instagram_account_id": "ig-1",
        "instagram_auth_mode": "instagram_login",
        "graph_version": "v21.0",
        "timeout_seconds": 10,
    }


def _secrets() -> dict[str, str]:
    return {
        "facebook_page_access_token": "test-facebook-page-token",
        "instagram_login_access_token": "test-instagram-login-token",
        "meta_oauth_access_token": "test-meta-oauth-token",
        "webhook_signing_secret": "test-signing-secret",
        "webhook_verify_token": "test-verify-token",
    }


def test_configuration_owner_persists_refs_and_distinct_auth_modes(db_session):
    result = configure_meta_social_installation(
        db_session,
        _command(),
        context=_context(),
    )

    installation = db_session.get(IntegrationInstallation, result.installation_id)
    assert installation is not None
    revision = installation.current_config_revision
    assert revision is not None
    assert revision.config_json["auth_mode"] == "individual"
    assert revision.config_json["facebook_auth_mode"] == "page_access_token"
    assert revision.config_json["instagram_auth_mode"] == "instagram_login"
    assert revision.secret_refs == {
        "facebook_page_access_token": (
            "bao://secret/integrations/meta_social#facebook_page_access_token"
        ),
        "instagram_login_access_token": (
            "bao://secret/integrations/meta_social#instagram_login_access_token"
        ),
        "webhook_signing_secret": (
            "bao://secret/integrations/meta_social#webhook_signing_secret"
        ),
        "webhook_verify_token": (
            "bao://secret/integrations/meta_social#webhook_verify_token"
        ),
    }
    assert {binding.capability_id for binding in installation.capability_bindings} == {
        "messaging.send.v1",
        "messaging.receive.v1",
    }
    assert result.installation_state == "disabled"


def test_configuration_owner_rejects_plaintext_secret(db_session):
    with pytest.raises(ValueError, match="references only"):
        configure_meta_social_installation(
            db_session,
            _command(facebook_page_access_token_ref="plaintext-token"),
            context=_context(),
        )

    assert db_session.query(IntegrationInstallation).count() == 0


def test_configuration_owner_persists_shared_oauth_mode(db_session):
    result = configure_meta_social_installation(
        db_session,
        _command(auth_mode="oauth"),
        context=_context(),
    )

    installation = db_session.get(IntegrationInstallation, result.installation_id)
    assert installation is not None
    revision = installation.current_config_revision
    assert revision is not None
    assert revision.config_json["auth_mode"] == "oauth"
    assert revision.config_json["facebook_auth_mode"] == "meta_oauth"
    assert revision.config_json["instagram_auth_mode"] == "meta_oauth"
    assert revision.secret_refs["meta_oauth_access_token"] == (
        "bao://secret/integrations/meta_social#meta_oauth_access_token"
    )
    assert "facebook_page_access_token" not in revision.secret_refs
    assert "instagram_login_access_token" not in revision.secret_refs


def test_configuration_projection_never_returns_secret_references(db_session):
    configure_meta_social_installation(db_session, _command(), context=_context())

    projection = get_meta_social_installation_projection(db_session)

    assert projection.facebook_page_id == "page-1"
    assert projection.instagram_account_id == "ig-1"
    assert projection.facebook_token_bound is True
    assert projection.instagram_token_bound is True
    assert not hasattr(projection, "facebook_page_access_token_ref")


def test_update_preserves_existing_secret_refs_inside_owner_transaction(db_session):
    first = configure_meta_social_installation(
        db_session,
        _command(),
        context=_context(),
    )

    updated = save_config(
        db_session,
        MetaSocialConfigFormCommand(
            app_id="app-1",
            facebook_page_id="page-1",
            instagram_account_id="ig-1",
            graph_version="v22.0",
            webhook_url="https://sub.example.test/api/v1/webhooks/meta",
            auth_mode="individual",
            meta_oauth_access_token_ref="",
            facebook_page_access_token_ref="",
            instagram_login_access_token_ref="",
            webhook_signing_secret_ref="",
            webhook_verify_token_ref="",
        ),
        context=CommandContext.system(
            actor="test.meta",
            scope=META_SOCIAL_CONFIGURATION_SCOPE,
            reason="Update non-secret Meta settings",
            idempotency_key="test-meta-social-config-update",
        ),
    )

    installation = db_session.get(IntegrationInstallation, first.installation_id)
    assert installation is not None
    assert updated.config_revision_id != first.config_revision_id
    assert installation.current_config_revision is not None
    assert installation.current_config_revision.secret_refs == {
        "facebook_page_access_token": (
            "bao://secret/integrations/meta_social#facebook_page_access_token"
        ),
        "instagram_login_access_token": (
            "bao://secret/integrations/meta_social#instagram_login_access_token"
        ),
        "webhook_signing_secret": (
            "bao://secret/integrations/meta_social#webhook_signing_secret"
        ),
        "webhook_verify_token": (
            "bao://secret/integrations/meta_social#webhook_verify_token"
        ),
    }


@pytest.mark.parametrize(
    ("channel", "expected_host", "expected_token", "expected_payload"),
    [
        (
            MetaSocialChannel.facebook_messenger,
            "graph.facebook.com",
            "test-facebook-page-token",
            {
                "recipient": {"id": "recipient-1"},
                "message": {"text": "Hello"},
                "messaging_type": "RESPONSE",
            },
        ),
        (
            MetaSocialChannel.instagram_dm,
            "graph.instagram.com",
            "test-instagram-login-token",
            {
                "recipient": '{"id":"recipient-1"}',
                "message": '{"text":"Hello"}',
            },
        ),
    ],
)
def test_runtime_keeps_channel_hosts_and_credentials_separate(
    monkeypatch, channel, expected_host, expected_token, expected_payload
):
    calls: list[tuple[str, str, dict[str, object]]] = []

    def provider_post(url, *, json, headers, timeout):
        calls.append((url, headers["Authorization"], json))
        return httpx.Response(
            200,
            json={"message_id": f"mid-{channel.value}", "recipient_id": "r-1"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(meta_social_runtime.httpx, "post", provider_post)
    result = meta_social_runtime.MetaSocialRuntimeRunner().execute(
        _envelope(channel=channel),
        config=_config(),
        secret_material=_secrets(),
    )

    assert result.status is OperationStatus.succeeded
    assert len(calls) == 1
    assert expected_host in calls[0][0]
    assert calls[0][1] == f"Bearer {expected_token}"
    assert calls[0][2] == expected_payload
    assert result.external_receipt["provider_message_id"].startswith("mid-")


def test_runtime_shared_oauth_uses_one_token_for_both_channels(monkeypatch):
    calls: list[tuple[str, str]] = []

    def provider_post(url, *, json, headers, timeout):
        calls.append((url, headers["Authorization"]))
        return httpx.Response(
            200,
            json={"message_id": "mid-oauth", "recipient_id": "r-1"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(meta_social_runtime.httpx, "post", provider_post)
    config = {**_config(), "auth_mode": "oauth"}
    for channel in (
        MetaSocialChannel.facebook_messenger,
        MetaSocialChannel.instagram_dm,
    ):
        result = meta_social_runtime.MetaSocialRuntimeRunner().execute(
            _envelope(channel=channel),
            config=config,
            secret_material=_secrets(),
        )
        assert result.status is OperationStatus.succeeded

    assert calls == [
        (
            "https://graph.facebook.com/v21.0/page-1/messages",
            "Bearer test-meta-oauth-token",
        ),
        (
            "https://graph.facebook.com/v21.0/ig-1/messages",
            "Bearer test-meta-oauth-token",
        ),
    ]


def test_runtime_preview_has_no_provider_side_effect(monkeypatch):
    monkeypatch.setattr(
        meta_social_runtime.httpx,
        "post",
        lambda *args, **kwargs: pytest.fail("preview contacted Meta"),
    )

    result = meta_social_runtime.MetaSocialRuntimeRunner().execute(
        _envelope(channel=MetaSocialChannel.instagram_dm, preview=True),
        config=_config(),
        secret_material={},
    )

    assert result.status is OperationStatus.succeeded
    assert result.output["sent"] is False
    assert result.output["payload"]["message"] == '{"text":"Hello"}'


def test_runtime_rejected_response_is_not_recorded_as_sent(monkeypatch):
    def provider_post(url, *, json, headers, timeout):
        return httpx.Response(
            400,
            json={"error": {"message": "rejected"}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(meta_social_runtime.httpx, "post", provider_post)
    result = meta_social_runtime.MetaSocialRuntimeRunner().execute(
        _envelope(channel=MetaSocialChannel.facebook_messenger),
        config=_config(),
        secret_material=_secrets(),
    )

    assert result.status is OperationStatus.rejected
    assert result.output["sent"] is False
    assert result.error_code == "provider_rejected_message"


def test_runtime_connection_validation_probes_each_bound_account(monkeypatch):
    observed: list[str] = []

    def provider_get(url, *, params, headers, timeout):
        observed.append(url)
        account_id = url.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={"id": account_id},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(meta_social_runtime.httpx, "get", provider_get)
    result = meta_social_runtime.MetaSocialRuntimeRunner().validate(
        manifest=object(),  # type: ignore[arg-type]
        config=_config(),
        secret_material=_secrets(),
    )

    assert result == ValidationResult(valid=True)
    assert any("graph.facebook.com" in url for url in observed)
    assert any("graph.instagram.com" in url for url in observed)


def test_typed_facade_returns_sanitized_outcome(db_session, monkeypatch):
    configured = configure_meta_social_installation(
        db_session,
        _command(),
        context=_context(),
    )
    installation = db_session.get(IntegrationInstallation, configured.installation_id)
    assert installation is not None
    from app.services.integrations import installations

    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    db_session.commit()

    def provider_post(url, *, json, headers, timeout):
        return httpx.Response(
            200,
            json={"message_id": "mid-1", "recipient_id": "recipient-1"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(meta_social_runtime.httpx, "post", provider_post)
    outcome = meta_social_capability.send_direct_message(
        db_session,
        MetaDirectMessageCommand(
            channel=MetaSocialChannel.facebook_messenger,
            provider_account_id="page-1",
            recipient_id="recipient-1",
            body="Hello",
            correlation_id="notification:1",
        ),
        secret_resolver=lambda reference: {
            "bao://secret/integrations/meta_social#facebook_page_access_token": (
                "test-facebook-page-token"
            ),
            "bao://secret/integrations/meta_social#instagram_login_access_token": (
                "test-instagram-login-token"
            ),
            "bao://secret/integrations/meta_social#meta_oauth_access_token": (
                "test-meta-oauth-token"
            ),
            "bao://secret/integrations/meta_social#webhook_signing_secret": (
                "test-signing-secret"
            ),
            "bao://secret/integrations/meta_social#webhook_verify_token": (
                "test-verify-token"
            ),
        }.get(str(reference)),
    )

    assert outcome.accepted is True
    assert outcome.provider_message_id == "mid-1"
    assert "token" not in outcome.model_dump_json()
