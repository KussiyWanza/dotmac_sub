import pytest
from fastapi import HTTPException

from app.models.domain_settings import SettingDomain
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_api, settings_spec
from app.services.response import ListResponseMixin


class _ListResponseStub(ListResponseMixin):
    @staticmethod
    def list(_db, *args, **kwargs):
        return [{"id": "one"}, {"id": "two"}]


def test_list_response_mixin_requires_limit_offset(db_session):
    with pytest.raises(ValueError, match="limit and offset are required"):
        _ListResponseStub.list_response(db_session)


def test_list_response_mixin_with_args(db_session):
    response = _ListResponseStub.list_response(db_session, None, None, 2, 0)
    assert response["count"] == 2
    assert response["limit"] == 2
    assert response["offset"] == 0


def test_upsert_auth_setting_variants(db_session):
    ttl = settings_api.upsert_auth_setting(
        db_session,
        "jwt_access_ttl_minutes",
        DomainSettingUpdate(value_text="30"),
    )
    assert ttl.value_type.value == "integer"
    assert ttl.value_text == "30"

    secure = settings_api.upsert_auth_setting(
        db_session,
        "refresh_cookie_secure",
        DomainSettingUpdate(value_json=True),
    )
    assert secure.value_type.value == "boolean"
    assert secure.value_text == "true"
    assert secure.value_json is True

    # `jwt_secret` is no longer an auth SETTING — it is held from OpenBao at
    # boot and read from there, so the admin API must refuse it rather than
    # store a value nothing reads.
    with pytest.raises(HTTPException) as exc:
        settings_api.upsert_auth_setting(
            db_session,
            "jwt_secret",
            DomainSettingUpdate(value_text="super-secret"),
        )
    assert exc.value.status_code == 400

    samesite = settings_api.upsert_auth_setting(
        db_session,
        "refresh_cookie_samesite",
        DomainSettingUpdate(value_text="Lax"),
    )
    assert samesite.value_text == "lax"


def test_upsert_auth_setting_invalid_key(db_session):
    with pytest.raises(HTTPException) as excinfo:
        settings_api.upsert_auth_setting(
            db_session,
            "bad_key",
            DomainSettingUpdate(value_text="value"),
        )
    assert excinfo.value.status_code == 400


def test_upsert_audit_setting_list_and_bool(db_session):
    methods = settings_api.upsert_audit_setting(
        db_session,
        "methods",
        DomainSettingUpdate(value_text="POST, GET"),
    )
    assert methods.value_json == ["POST", "GET"]

    enabled = settings_api.upsert_audit_setting(
        db_session,
        "enabled",
        DomainSettingUpdate(value_json=False),
    )
    assert enabled.value_text == "false"


def test_upsert_imports_setting(db_session):
    imports_setting = settings_api.upsert_imports_setting(
        db_session,
        "max_rows",
        DomainSettingUpdate(value_text="500"),
    )
    assert imports_setting.value_type.value == "integer"
    assert imports_setting.value_text == "500"


def test_upsert_notification_setting_variants(db_session):
    delay = settings_api.upsert_notification_setting(
        db_session,
        "alert_notifications_default_delay_minutes",
        DomainSettingUpdate(value_text="15"),
    )
    assert delay.value_type.value == "integer"
    assert delay.value_text == "15"

    enabled = settings_api.upsert_notification_setting(
        db_session,
        "alert_notifications_enabled",
        DomainSettingUpdate(value_text="yes"),
    )
    assert enabled.value_type.value == "boolean"
    assert enabled.value_json is True

    channel = settings_api.upsert_notification_setting(
        db_session,
        "alert_notifications_default_channel",
        DomainSettingUpdate(value_text="email"),
    )
    assert channel.value_text == "email"


def test_upsert_scheduler_setting(db_session):
    beat = settings_api.upsert_scheduler_setting(
        db_session,
        "beat_refresh_seconds",
        DomainSettingUpdate(value_text="60"),
    )
    assert beat.value_type.value == "integer"
    assert beat.value_text == "60"

    tz = settings_api.upsert_scheduler_setting(
        db_session,
        "timezone",
        DomainSettingUpdate(value_text="UTC"),
    )
    assert tz.value_text == "UTC"


def test_upsert_geocoding_setting_variants(db_session):
    timeout = settings_api.upsert_geocoding_setting(
        db_session,
        "timeout_sec",
        DomainSettingUpdate(value_text="3"),
    )
    assert timeout.value_type.value == "integer"
    assert timeout.value_text == "3"

    enabled = settings_api.upsert_geocoding_setting(
        db_session,
        "enabled",
        DomainSettingUpdate(value_text="true"),
    )
    assert enabled.value_type.value == "boolean"
    assert enabled.value_json is True

    provider = settings_api.upsert_geocoding_setting(
        db_session,
        "provider",
        DomainSettingUpdate(value_text="nominatim"),
    )
    assert provider.value_text == "nominatim"


def test_upsert_radius_setting_variants(db_session):
    timeout = settings_api.upsert_radius_setting(
        db_session,
        "auth_timeout_sec",
        DomainSettingUpdate(value_text="5"),
    )
    assert timeout.value_type.value == "integer"
    assert timeout.value_text == "5"

    # `auth_shared_secret` is no longer a radius SETTING, so the admin API must
    # refuse it rather than store a value nothing reads. It is held from
    # OpenBao at boot and read there by `radius_auth.authenticate`; the spec was
    # retired for having no reader.
    with pytest.raises(HTTPException) as exc:
        settings_api.upsert_radius_setting(
            db_session,
            "auth_shared_secret",
            DomainSettingUpdate(value_text="radius-secret"),
        )
    assert exc.value.status_code == 400


def test_upsert_gis_setting_variants(db_session):
    interval = settings_api.upsert_gis_setting(
        db_session,
        "sync_interval_minutes",
        DomainSettingUpdate(value_text="5"),
    )
    assert interval.value_type.value == "integer"
    assert interval.value_text == "5"

    addresses = settings_api.upsert_gis_setting(
        db_session,
        "sync_addresses",
        DomainSettingUpdate(value_text="false"),
    )
    assert addresses.value_type.value == "boolean"
    assert addresses.value_json is False

    with pytest.raises(HTTPException):
        settings_api.upsert_gis_setting(
            db_session,
            "sync_enabled",
            DomainSettingUpdate(value_text="false"),
        )


def test_ai_polish_settings_are_integration_settings(db_session):
    for key in (
        "inbox_ai_polish_business_voice",
        "inbox_ai_polish_channel_guidance",
    ):
        spec = settings_spec.get_spec(SettingDomain.integration, key)
        assert spec is not None
        setting = settings_api.upsert_integration_setting(
            db_session,
            key,
            DomainSettingUpdate(value_text=f"configured {key}"),
        )
        assert setting.domain == SettingDomain.integration
        assert setting.value_text == f"configured {key}"

        with pytest.raises(HTTPException) as exc:
            settings_api.upsert_comms_setting(
                db_session,
                key,
                DomainSettingUpdate(value_text="wrong owner"),
            )
        assert exc.value.status_code == 400


def test_list_settings_response(db_session):
    settings_api.upsert_auth_setting(
        db_session,
        "jwt_access_ttl_minutes",
        DomainSettingUpdate(value_text="30"),
    )
    settings_api.upsert_auth_setting(
        db_session,
        "refresh_cookie_secure",
        DomainSettingUpdate(value_json=True),
    )
    response = settings_api.list_auth_settings_response(
        db_session, None, "key", "asc", 10, 0
    )
    assert response["count"] == 2
