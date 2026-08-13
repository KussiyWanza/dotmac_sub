"""A geocoding provider gets a credential, not a reference to one.

`google_api_key` and `mapbox_api_key` are declared `is_secret=True`, so every
write through `DomainSettings._write_secret_ref` puts the value in OpenBao and
stores a `bao://secret/settings/geocoding#<key>` REFERENCE in `value_text`.
`geocoding._setting_value` returned that column verbatim and the provider was
sent the literal string `bao://…` as its API key.

The same defect `radius_auth.authenticate` had, and the same reason it hid: the
provider answers 401, which reads as a bad key rather than as a settings bug.
"""

from __future__ import annotations

import pytest

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate
from app.services import domain_settings as domain_settings_service
from app.services import geocoding

REFERENCE = "bao://secret/settings/geocoding#google_api_key"
SECRET = "the-actual-google-key"


def _store(db_session, key: str, value: str) -> None:
    """Put a raw value in the row, bypassing the secret-ref write path.

    The service would rewrite this into a reference (or refuse without
    OpenBao); these tests are about what the READER does with whatever the
    column happens to hold.
    """

    domain_settings_service.DomainSettings(domain=SettingDomain.geocoding).create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.geocoding,
            key=key,
            value_type=SettingValueType.string,
            value_text=value,
            is_secret=False,
        ),
    )


@pytest.mark.parametrize("key", ["google_api_key", "mapbox_api_key"])
def test_a_stored_reference_is_resolved_not_forwarded(db_session, monkeypatch, key):
    import app.services.settings_spec as settings_spec

    monkeypatch.setattr(
        settings_spec,
        "resolve_value",
        lambda _db, _domain, _key: REFERENCE if _key == key else None,
    )
    monkeypatch.setattr(
        "app.services.secrets.resolve_secret",
        lambda value: SECRET if value == REFERENCE else value,
    )

    assert geocoding._secret_setting_value(db_session, key) == SECRET


@pytest.mark.parametrize("key", ["google_api_key", "mapbox_api_key"])
def test_a_plaintext_value_passes_through(db_session, key):
    """A key configured before OpenBao existed is stored in the clear, and must
    keep working — `resolve_secret` passes a non-reference through."""

    _store(db_session, key, SECRET)

    assert geocoding._secret_setting_value(db_session, key) == SECRET


def test_an_unresolvable_reference_reads_as_not_configured(db_session, monkeypatch):
    """Fails closed and quietly, like `ai/security.resolve_provider_api_key`.

    The caller raises "not configured", which is the truthful outcome — there
    is no usable credential — and neither the reference nor the store's own
    error reaches the response or the log.
    """

    import app.services.settings_spec as settings_spec

    monkeypatch.setattr(
        settings_spec, "resolve_value", lambda _db, _domain, _key: REFERENCE
    )

    def _unreachable(_value: str) -> str:
        raise RuntimeError("openbao unreachable: bao://secret/settings/geocoding")

    monkeypatch.setattr("app.services.secrets.resolve_secret", _unreachable)

    assert geocoding._secret_setting_value(db_session, "google_api_key") is None


def test_the_reference_is_never_logged(db_session, monkeypatch, caplog):
    import logging

    import app.services.settings_spec as settings_spec

    monkeypatch.setattr(
        settings_spec, "resolve_value", lambda _db, _domain, _key: REFERENCE
    )
    monkeypatch.setattr(
        "app.services.secrets.resolve_secret",
        lambda _value: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with caplog.at_level(logging.DEBUG, logger=geocoding.__name__):
        geocoding._secret_setting_value(db_session, "google_api_key")

    assert REFERENCE not in caplog.text
