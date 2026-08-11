"""A secret setting is stored as ciphertext and read back through the resolver.

The property that matters is end-to-end: what the write path stores must be
unreadable in the column, and what a reader gets must be the credential. Both
halves have failed independently in this codebase — the write path stored a
`bao://` reference, and three readers forwarded that reference to a provider as
if it were the secret — so neither half is asserted alone here.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from dotmac_kernel.settings_crypto import is_encrypted

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services.settings_spec import resolve_value

SECRET = "the-actual-smtp-password"


@pytest.fixture
def active_key(monkeypatch):
    """A real Fernet key through the kernel's own environment path.

    Not a stubbed `encrypt_value`: the test is worth little if it does not
    exercise the real encode/decode pair, and the kernel reads this variable
    fresh on every call precisely so a test can set one.
    """

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    yield


def _write(db_session, key: str, value: str):
    return domain_settings_service.DomainSettings(
        domain=SettingDomain.notification
    ).upsert_by_key(db_session, key, DomainSettingUpdate(value_text=value))


def test_a_written_secret_is_not_readable_in_the_column(db_session, active_key):
    row = _write(db_session, "smtp_password", SECRET)

    assert row.value_text != SECRET
    assert is_encrypted(row.value_text)
    assert SECRET not in row.value_text


def test_the_resolver_gives_the_reader_the_credential(db_session, active_key):
    _write(db_session, "smtp_password", SECRET)

    assert resolve_value(db_session, SettingDomain.notification, "smtp_password") == (
        SECRET
    )


def test_a_rewrite_is_stable_and_still_decrypts(db_session, active_key):
    """Encryption is not idempotent byte-for-byte — Fernet embeds a timestamp
    and IV — so what must hold is that the value keeps resolving, not that the
    ciphertext is unchanged."""

    _write(db_session, "smtp_password", SECRET)
    _write(db_session, "smtp_password", SECRET)

    assert resolve_value(db_session, SettingDomain.notification, "smtp_password") == (
        SECRET
    )


def test_writing_a_secret_without_a_key_is_refused_not_stored_in_the_clear(
    db_session, monkeypatch
):
    """Fail closed. The alternative is a credential sitting in a column behind
    a warning line nobody reads."""

    from fastapi import HTTPException

    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEYS", raising=False)
    monkeypatch.setattr(
        domain_settings_service.DomainSettings,
        "_allow_plain_secret_fallback",
        lambda _self, _db: False,
    )

    with pytest.raises(HTTPException) as exc:
        _write(db_session, "smtp_password", SECRET)
    assert exc.value.status_code == 500
    assert SECRET not in str(exc.value.detail)


def test_a_legacy_reference_is_left_alone_by_the_write_path(db_session, active_key):
    """A `bao://` value passes through unencrypted.

    Encrypting the reference TEXT would store the pointer and lose the secret.
    Converting one is the conversion script's job, and until it runs the row
    still resolves the way it always did.
    """

    reference = "bao://secret/settings/notification#smtp_password"
    row = _write(db_session, "smtp_password", reference)

    assert row.value_text == reference


def test_the_conversion_script_rewrites_a_reference_row(db_session, active_key):
    from scripts.one_off.encrypt_secret_settings import convert_secret_settings

    reference = "bao://secret/settings/notification#smtp_password"
    db_session.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key="smtp_password",
            value_type=SettingValueType.string,
            value_text=reference,
            is_secret=True,
        )
    )
    db_session.commit()

    import scripts.one_off.encrypt_secret_settings as script

    # The one network call the script is allowed, stubbed: this asserts the
    # rewrite, not OpenBao.
    script.resolve_secret = lambda value: SECRET if value == reference else value
    report = convert_secret_settings(db_session, apply=True)

    assert "notification.smtp_password" in report.converted
    assert report.ok
    assert resolve_value(db_session, SettingDomain.notification, "smtp_password") == (
        SECRET
    )


def test_the_conversion_script_reports_names_only(db_session, active_key):
    """An unresolvable reference is named, never quoted, and left unchanged."""

    import scripts.one_off.encrypt_secret_settings as script
    from scripts.one_off.encrypt_secret_settings import convert_secret_settings

    reference = "bao://secret/settings/notification#smtp_password"
    db_session.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key="smtp_password",
            value_type=SettingValueType.string,
            value_text=reference,
            is_secret=True,
        )
    )
    db_session.commit()

    def _unresolvable(_value: str) -> str:
        raise RuntimeError(f"openbao unreachable for {reference}")

    script.resolve_secret = _unresolvable
    report = convert_secret_settings(db_session, apply=True)

    assert report.unresolvable == ["notification.smtp_password"]
    assert not report.ok
    assert all(reference not in entry for entry in report.unresolvable)
    row = (
        db_session.query(DomainSetting)
        .filter(DomainSetting.key == "smtp_password")
        .one()
    )
    assert row.value_text == reference
