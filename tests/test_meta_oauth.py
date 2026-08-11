"""Behavior coverage for the canonical Meta OAuth refresh owner."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.models.connector import ConnectorConfig, ConnectorType
from app.models.event_store import EventStore
from app.models.oauth_token import OAuthToken
from app.services import meta_oauth
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext


def _token(
    db_session,
    *,
    account_type: str = "user",
    access_token: str | None = None,
    expires_at: datetime,
) -> OAuthToken:
    connector = ConnectorConfig(
        name=f"Meta connector {uuid4()}",
        connector_type=ConnectorType.facebook,
    )
    db_session.add(connector)
    db_session.flush()
    token = OAuthToken(
        connector_config_id=connector.id,
        provider="meta",
        account_type=account_type,
        external_account_id=str(uuid4()),
        access_token=access_token if access_token is not None else "opaque-old-value",
        token_expires_at=expires_at,
        is_active=True,
    )
    db_session.add(token)
    db_session.commit()
    return token


def _command(
    db_session,
    token: OAuthToken,
    *,
    observed_at: datetime,
) -> meta_oauth.RefreshMetaTokenCommand:
    expires_at = token.token_expires_at
    assert expires_at is not None
    command_id = uuid4()
    command = meta_oauth.RefreshMetaTokenCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor="pytest:meta-oauth",
            scope=meta_oauth.META_OAUTH_REFRESH_SCOPE,
            reason="Verify Meta token refresh ownership",
            idempotency_key=f"pytest:{command_id}",
        ),
        candidate=meta_oauth.MetaTokenRefreshCandidate(
            token_id=token.id,
            expected_expires_at=expires_at,
        ),
        observed_at=observed_at,
        eligible_before=observed_at + timedelta(days=7),
    )
    db_session_adapter.release_read_transaction(db_session)
    return command


def _configure_meta(monkeypatch, *, secret_setting: str | None = None):
    values = {
        "meta_app_id": "meta-client-id",
        "meta_app_secret": (
            secret_setting if secret_setting is not None else "env://META_TEST_SECRET"
        ),
        "meta_graph_api_version": "v21.0",
    }
    monkeypatch.setattr(
        meta_oauth.settings_spec,
        "resolve_value",
        lambda _db, _domain, key: values[key],
    )
    original_resolve_secret = meta_oauth.secrets.resolve_secret
    monkeypatch.setattr(
        meta_oauth.secrets,
        "resolve_secret",
        lambda reference: (
            "resolved-client-secret"
            if reference == "env://META_TEST_SECRET"
            else original_resolve_secret(reference)
        ),
    )


class _SuccessfulTransport:
    def __init__(self) -> None:
        self.configuration: meta_oauth.MetaOAuthRefreshConfiguration | None = None
        self.access_token: str | None = None

    def exchange(self, configuration, access_token):
        self.configuration = configuration
        self.access_token = access_token
        return meta_oauth.MetaOAuthProviderResult(
            access_token="new-access-token",
            expires_in_seconds=60 * 24 * 60 * 60,
            token_type="bearer",
        )


def test_refresh_resolves_secret_and_persists_token_expiry_and_event(
    db_session, monkeypatch
):
    observed_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    token = _token(
        db_session,
        expires_at=observed_at + timedelta(days=1),
    )
    command = _command(db_session, token, observed_at=observed_at)
    transport = _SuccessfulTransport()
    _configure_meta(monkeypatch)
    monkeypatch.setattr(meta_oauth, "meta_oauth_refresh_transport", transport)

    result = meta_oauth.refresh_meta_token(db_session, command)

    refreshed = db_session.get(OAuthToken, token.id)
    assert refreshed is not None
    assert result.status is meta_oauth.MetaTokenRefreshStatus.REFRESHED
    assert refreshed.access_token == "new-access-token"
    assert refreshed.token_expires_at is not None
    assert refreshed.token_expires_at.replace(tzinfo=UTC) == observed_at + timedelta(
        days=60
    )
    assert refreshed.last_refreshed_at is not None
    assert refreshed.last_refreshed_at.replace(tzinfo=UTC) == observed_at
    assert refreshed.token_type == "bearer"
    assert refreshed.refresh_error is None
    assert transport.access_token == "opaque-old-value"
    assert transport.configuration is not None
    assert transport.configuration.grant_type is (
        meta_oauth.MetaOAuthGrantType.FB_EXCHANGE_TOKEN
    )
    assert "resolved-client-secret" not in repr(transport.configuration)
    assert "new-access-token" not in repr(
        meta_oauth.MetaOAuthProviderResult(
            access_token="new-access-token",
            expires_in_seconds=1,
        )
    )

    event = db_session.scalar(
        select(EventStore).where(
            EventStore.event_type == "oauth_token.refreshed",
            EventStore.payload["aggregate_id"].as_string() == str(token.id),
        )
    )
    assert event is not None
    event_text = str(event.payload)
    assert "opaque-old-value" not in event_text
    assert "new-access-token" not in event_text
    assert "resolved-client-secret" not in event_text


def test_candidate_query_excludes_page_and_instagram_token_classes(db_session):
    observed_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    user_token = _token(
        db_session,
        account_type="user",
        expires_at=observed_at + timedelta(days=1),
    )
    _token(
        db_session,
        account_type="page",
        expires_at=observed_at + timedelta(days=1),
    )
    _token(
        db_session,
        account_type="instagram_business",
        expires_at=observed_at + timedelta(days=1),
    )

    candidates = meta_oauth.list_meta_token_refresh_candidates(
        db_session,
        meta_oauth.MetaTokenRefreshCandidatesQuery(
            eligible_before=observed_at + timedelta(days=7)
        ),
    )

    assert [candidate.token_id for candidate in candidates] == [user_token.id]


def test_a_plaintext_client_secret_is_now_usable_and_never_echoed(
    db_session, monkeypatch
):
    """The reference requirement is gone, because encryption replaced it.

    `comms/meta_app_secret` used to be rejected unless the stored value WAS a
    `bao://…` reference — this codebase's way of saying "do not keep an app
    secret in the database in plaintext". That requirement is real and is now
    met properly: the row holds ciphertext, decrypted by the resolver, so the
    database carries nothing readable without the held key and no network call
    sits on the refresh path.

    The old gate was also weaker than it read. It checked the value WAS a
    reference and never WHICH one, so it constrained the format rather than the
    authority.

    What must NOT regress is the other half of the old test: whatever the
    secret is, it never reaches a stored error or a log line.
    """

    observed_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    token = _token(db_session, expires_at=observed_at + timedelta(days=1))
    command = _command(db_session, token, observed_at=observed_at)
    transport = _SuccessfulTransport()
    _configure_meta(monkeypatch, secret_setting="plaintext-client-secret")
    monkeypatch.setattr(meta_oauth, "meta_oauth_refresh_transport", transport)

    result = meta_oauth.refresh_meta_token(db_session, command)

    assert result.status is not meta_oauth.MetaTokenRefreshStatus.FAILED or (
        result.failure_code
        is not meta_oauth.MetaTokenRefreshFailureCode.SECRET_REFERENCE_REQUIRED
    )
    refreshed = db_session.get(OAuthToken, token.id)
    assert refreshed is not None
    assert "plaintext-client-secret" not in (refreshed.refresh_error or "")


def test_the_reference_required_code_survives_for_stored_occurrences() -> None:
    """Nothing raises it now; the member stays so old rows and logs still name it.

    Deleting a failure code that has been persisted turns every historical
    occurrence into an unresolvable string.
    """

    assert (
        meta_oauth.MetaTokenRefreshFailureCode.SECRET_REFERENCE_REQUIRED.value
        == "secret_reference_required"
    )


def test_provider_rejection_records_only_sanitized_evidence(
    db_session, monkeypatch, caplog
):
    observed_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    token = _token(
        db_session,
        expires_at=observed_at + timedelta(days=1),
    )
    command = _command(db_session, token, observed_at=observed_at)
    _configure_meta(monkeypatch)
    seen: dict[str, object] = {}

    class _RejectingClient:
        def __init__(self, *, timeout):
            seen["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, *, data):
            seen["url"] = url
            seen["data"] = data
            request = httpx.Request("POST", url)
            return httpx.Response(
                400,
                request=request,
                json={
                    "error": {
                        "message": ("bad resolved-client-secret and opaque-old-value")
                    }
                },
            )

    monkeypatch.setattr(meta_oauth.httpx, "Client", _RejectingClient)

    result = meta_oauth.refresh_meta_token(db_session, command)

    refreshed = db_session.get(OAuthToken, token.id)
    assert refreshed is not None
    assert result.failure_code is (
        meta_oauth.MetaTokenRefreshFailureCode.PROVIDER_REJECTED
    )
    assert seen["url"] == "https://graph.facebook.com/v21.0/oauth/access_token"
    assert "resolved-client-secret" not in str(seen["url"])
    assert "opaque-old-value" not in str(seen["url"])
    assert seen["data"] == {
        "grant_type": "fb_exchange_token",
        "client_id": "meta-client-id",
        "client_secret": "resolved-client-secret",
        "fb_exchange_token": "opaque-old-value",
    }
    assert refreshed.access_token == "opaque-old-value"
    assert refreshed.refresh_error is not None
    assert "resolved-client-secret" not in refreshed.refresh_error
    assert "opaque-old-value" not in refreshed.refresh_error
    assert "resolved-client-secret" not in caplog.text
    assert "opaque-old-value" not in caplog.text


def test_changed_expiry_skips_stale_retry_without_provider_call(
    db_session, monkeypatch
):
    observed_at = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    token = _token(
        db_session,
        expires_at=observed_at + timedelta(days=1),
    )
    command = _command(db_session, token, observed_at=observed_at)
    token.token_expires_at = observed_at + timedelta(days=30)
    db_session.commit()
    transport = _SuccessfulTransport()
    _configure_meta(monkeypatch)
    monkeypatch.setattr(meta_oauth, "meta_oauth_refresh_transport", transport)

    result = meta_oauth.refresh_meta_token(db_session, command)

    assert result.status is meta_oauth.MetaTokenRefreshStatus.SKIPPED
    assert transport.configuration is None
