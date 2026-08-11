import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate, DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import settings_api as settings_api_service


def test_domain_setting_domain_mismatch(db_session):
    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    created = settings.create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="sync_interval_minutes",
            value_type=SettingValueType.integer,
            value_text="60",
        ),
    )
    with pytest.raises(HTTPException) as exc:
        settings.update(
            db_session,
            str(created.id),
            DomainSettingUpdate(domain=SettingDomain.auth),
        )
    assert exc.value.status_code == 400


def test_settings_api_gis_upsert_and_validation(db_session):
    updated = settings_api_service.upsert_gis_setting(
        db_session,
        "sync_addresses",
        DomainSettingUpdate(value_text="false"),
    )
    assert updated.value_type == SettingValueType.boolean
    assert updated.value_json is False
    fetched = settings_api_service.get_gis_setting(db_session, "sync_addresses")
    assert fetched.id == updated.id

    with pytest.raises(HTTPException):
        settings_api_service.upsert_gis_setting(
            db_session,
            "sync_enabled",
            DomainSettingUpdate(value_text="false"),
        )


def test_settings_api_invalid_key(db_session):
    with pytest.raises(HTTPException) as exc:
        settings_api_service.get_gis_setting(db_session, "bad_key")
    assert exc.value.status_code == 400


def test_ensure_by_key_returns_concurrent_insert(db_session, monkeypatch):
    settings = domain_settings_service.DomainSettings(domain=SettingDomain.gis)
    original_create = settings.create
    original_rollback = db_session.rollback
    raced_payload = None

    def racing_create(db, payload):
        nonlocal raced_payload
        raced_payload = payload
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    def rollback_with_raced_insert():
        original_rollback()
        assert raced_payload is not None
        original_create(db_session, raced_payload)

    monkeypatch.setattr(settings, "create", racing_create)
    monkeypatch.setattr(db_session, "rollback", rollback_with_raced_insert)

    setting = settings.ensure_by_key(
        db_session,
        "sync_interval_seconds",
        SettingValueType.integer,
        value_text="60",
    )

    assert setting.key == "sync_interval_seconds"
    assert setting.value_text == "60"


def test_secret_update_preserves_existing_classification(db_session, monkeypatch):
    """A secret stays secret across an update, and is stored unreadable.

    This used to assert the value became a `bao://` REFERENCE and that OpenBao
    received a write. Both were the old storage — and the reference is exactly
    what three call sites then forwarded to a provider as if it were the
    secret. What the test is FOR, that an update must not silently declassify a
    secret, holds either way and is asserted against the new storage.
    """

    from cryptography.fernet import Fernet
    from dotmac_kernel.settings_crypto import is_encrypted

    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
    settings = domain_settings_service.DomainSettings(domain=SettingDomain.auth)
    monkeypatch.setattr(settings, "_allow_plain_secret_fallback", lambda _db: False)

    created = settings.create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.auth,
            key="jwt_secret",
            value_type=SettingValueType.string,
            value_text="first-secret",
            is_secret=True,
        ),
    )
    updated = settings.update(
        db_session,
        str(created.id),
        DomainSettingUpdate(value_text="second-secret"),
    )

    assert updated.is_secret is True
    assert is_encrypted(updated.value_text)
    assert "second-secret" not in updated.value_text
