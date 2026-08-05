"""Tests for settings seed services."""

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import NotificationChannel, NotificationTemplate
from app.models.subscription_engine import SettingValueType
from app.services import settings_seed
from app.services.domain_settings import network_settings


class TestSeedSchedulerRuntimeSettings:
    def test_materializes_environment_only_at_bootstrap(self, db_session, monkeypatch):
        monkeypatch.setenv("NCC_REPORT_EMAIL_ENABLED", "true")

        settings_seed.seed_scheduler_runtime_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.notification,
                DomainSetting.key == "ncc_report_email_enabled",
            )
            .one()
        )
        assert setting.value_json is True

    def test_preserves_existing_database_decision(self, db_session, monkeypatch):
        db_session.add(
            DomainSetting(
                domain=SettingDomain.notification,
                key="ncc_report_email_enabled",
                value_type=SettingValueType.boolean,
                value_text="false",
                value_json=False,
                is_active=True,
            )
        )
        db_session.commit()
        monkeypatch.setenv("NCC_REPORT_EMAIL_ENABLED", "true")

        settings_seed.seed_scheduler_runtime_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.notification,
                DomainSetting.key == "ncc_report_email_enabled",
            )
            .one()
        )
        assert setting.value_json is False

    def test_materializes_registered_integer_environment_value(
        self, db_session, monkeypatch
    ):
        monkeypatch.setenv("COMPENSATION_RETRY_INTERVAL_SECONDS", "120")

        settings_seed.seed_scheduler_runtime_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.provisioning,
                DomainSetting.key == "compensation_retry_interval_seconds",
            )
            .one()
        )
        assert setting.value_text == "120"


# =============================================================================
# Auth Settings Tests
# =============================================================================


class TestSeedAuthSettings:
    """Tests for seed_auth_settings function."""

    def test_seeds_jwt_algorithm(self, db_session, monkeypatch):
        """Test JWT algorithm setting is seeded."""
        monkeypatch.setenv("JWT_ALGORITHM", "RS256")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)

        settings_seed.seed_auth_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.auth,
                DomainSetting.key == "jwt_algorithm",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "RS256"

    def test_seeds_jwt_ttl(self, db_session, monkeypatch):
        """Test JWT TTL settings are seeded."""
        monkeypatch.setenv("JWT_ACCESS_TTL_MINUTES", "30")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)

        settings_seed.seed_auth_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.auth,
                DomainSetting.key == "jwt_access_ttl_minutes",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "30"

    def test_seeds_refresh_cookie_settings(self, db_session, monkeypatch):
        """Test refresh cookie settings are seeded."""
        monkeypatch.setenv("REFRESH_COOKIE_NAME", "custom_refresh")
        monkeypatch.delenv("JWT_SECRET", raising=False)
        monkeypatch.delenv("TOTP_ENCRYPTION_KEY", raising=False)

        settings_seed.seed_auth_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.auth,
                DomainSetting.key == "refresh_cookie_name",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "custom_refresh"


# =============================================================================
# Audit Settings Tests
# =============================================================================


class TestSeedAuditSettings:
    """Tests for seed_audit_settings function."""

    def test_seeds_audit_enabled(self, db_session):
        """Test audit enabled setting is seeded."""
        settings_seed.seed_audit_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.audit,
                DomainSetting.key == "enabled",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_json is True

    def test_seeds_audit_methods(self, db_session):
        """Test audit methods setting is seeded."""
        settings_seed.seed_audit_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.audit,
                DomainSetting.key == "methods",
            )
            .first()
        )
        assert setting is not None
        assert "POST" in setting.value_json


# =============================================================================
# Imports Settings Tests
# =============================================================================


class TestSeedImportsSettings:
    """Tests for seed_imports_settings function."""

    def test_seeds_max_file_bytes(self, db_session):
        """Test max file bytes setting is seeded."""
        settings_seed.seed_imports_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.imports,
                DomainSetting.key == "max_file_bytes",
            )
            .first()
        )
        assert setting is not None
        assert int(setting.value_text) == 5 * 1024 * 1024

    def test_seeds_import_rollback_window_hours(self, db_session):
        """Test import rollback window setting is seeded."""
        settings_seed.seed_imports_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.imports,
                DomainSetting.key == "import_rollback_window_hours",
            )
            .first()
        )
        assert setting is not None
        assert int(setting.value_text) == 24

    def test_seeds_import_background_threshold_rows(self, db_session):
        """Test import background threshold setting is seeded."""
        settings_seed.seed_imports_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.imports,
                DomainSetting.key == "import_background_threshold_rows",
            )
            .first()
        )
        assert setting is not None
        assert int(setting.value_text) == 1000

    def test_seeds_import_jobs_log(self, db_session):
        """Test import jobs log setting is seeded."""
        settings_seed.seed_imports_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.imports,
                DomainSetting.key == "import_jobs_log",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_json == []


# =============================================================================
# GIS Settings Tests
# =============================================================================


class TestSeedGisSettings:
    """Tests for seed_gis_settings function."""

    def test_does_not_recreate_retired_sync_enabled_alias(self, db_session):
        settings_seed.seed_gis_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.gis,
                DomainSetting.key == "sync_enabled",
            )
            .first()
        )
        assert setting is None

    def test_seeds_sync_interval(self, db_session):
        """Test GIS sync interval setting is seeded."""
        settings_seed.seed_gis_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.gis,
                DomainSetting.key == "sync_interval_minutes",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "60"


# =============================================================================
# Usage Settings Tests
# =============================================================================


class TestSeedUsageSettings:
    """Tests for seed_usage_settings function."""

    def test_seeds_usage_rating_enabled(self, db_session, monkeypatch):
        """Test usage rating enabled setting is seeded."""
        monkeypatch.setenv("USAGE_RATING_ENABLED", "true")

        settings_seed.seed_usage_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.usage,
                DomainSetting.key == "usage_rating_enabled",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_json is True


# =============================================================================
# Notification Settings Tests
# =============================================================================


class TestSeedNotificationSettings:
    """Tests for seed_notification_settings function."""

    def test_seeds_alert_notifications_enabled(self, db_session, monkeypatch):
        """Test alert notifications enabled setting is seeded."""
        monkeypatch.setenv("ALERT_NOTIFICATIONS_ENABLED", "true")
        # Set all required env vars to avoid validation errors
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_CHANNEL", "email")
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_RECIPIENT", "admin@example.com")
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_TEMPLATE_ID", "tmpl-1")
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_ROTATION_ID", "rot-1")
        monkeypatch.setenv("ALERT_NOTIFICATIONS_DEFAULT_DELAY_MINUTES", "5")

        settings_seed.seed_notification_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.notification,
                DomainSetting.key == "alert_notifications_enabled",
            )
            .first()
        )
        assert setting is not None


class TestSeedNotificationTemplates:
    def test_seed_missing_notification_templates_does_not_commit(
        self, db_session, monkeypatch
    ):
        commit_called = False

        def _unexpected_commit():
            nonlocal commit_called
            commit_called = True
            raise AssertionError("commit should not be called")

        monkeypatch.setattr(db_session, "commit", _unexpected_commit)

        created = settings_seed._seed_missing_notification_templates(db_session)

        assert created > 0
        assert commit_called is False

    def test_seeds_suspension_warning_template(self, db_session):
        settings_seed.seed_notification_templates(db_session)

        template = (
            db_session.query(NotificationTemplate)
            .filter(NotificationTemplate.code == "suspension_warning")
            .first()
        )

        assert template is not None
        assert template.channel == NotificationChannel.email

    def test_seeds_sms_notification_templates(self, db_session):
        settings_seed.seed_notification_templates(db_session)

        codes = {
            (row.code, row.channel)
            for row in db_session.query(NotificationTemplate)
            .filter(NotificationTemplate.channel == NotificationChannel.sms)
            .all()
        }

        assert ("invoice_overdue", NotificationChannel.sms) in codes
        assert ("suspension_warning", NotificationChannel.sms) in codes
        assert ("subscription_suspended", NotificationChannel.sms) in codes

    def test_seeds_prepaid_service_renewed_email(self, db_session):
        settings_seed.seed_notification_templates(db_session)

        template = (
            db_session.query(NotificationTemplate)
            .filter(
                NotificationTemplate.code == "prepaid_service_renewed",
                NotificationTemplate.channel == NotificationChannel.email,
            )
            .one()
        )

        assert "{renewed_through}" in template.body
        assert template.is_active is True


# =============================================================================
# Collections Settings Tests
# =============================================================================


class TestSeedCollectionsSettings:
    """Tests for seed_collections_settings function."""

    def test_does_not_recreate_retired_dunning_alias(self, db_session, monkeypatch):
        monkeypatch.setenv("DUNNING_ENABLED", "false")

        settings_seed.seed_collections_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.collections,
                DomainSetting.key == "dunning_enabled",
            )
            .first()
        )
        assert setting is None

    def test_does_not_seed_retired_prepaid_skip_holidays(self, db_session, monkeypatch):
        monkeypatch.setenv("PREPAID_SKIP_HOLIDAYS", '["2026-01-01"]')

        settings_seed.seed_collections_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.collections,
                DomainSetting.key == "prepaid_skip_holidays",
            )
            .first()
        )
        assert setting is None

    def test_seeds_enforcement_window_policy(self, db_session, monkeypatch):
        monkeypatch.setenv("ENFORCEMENT_WINDOW_START", "09:00")
        monkeypatch.setenv("ENFORCEMENT_WINDOW_END", "17:00")
        monkeypatch.setenv("ENFORCEMENT_SKIP_WEEKENDS", "true")
        monkeypatch.setenv("ENFORCEMENT_SKIP_HOLIDAYS", '["2026-01-01"]')

        settings_seed.seed_collections_settings(db_session)

        rows = {
            row.key: row
            for row in db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.collections,
                DomainSetting.key.in_(
                    {
                        "enforcement_window_mode",
                        "enforcement_window_start",
                        "enforcement_window_end",
                        "enforcement_skip_weekends",
                        "enforcement_skip_holidays",
                    }
                ),
            )
            .all()
        }
        assert rows["enforcement_window_start"].value_text == "09:00"
        assert rows["enforcement_window_end"].value_text == "17:00"
        assert "enforcement_window_mode" not in rows
        assert "enforcement_skip_weekends" not in rows
        assert "enforcement_skip_holidays" not in rows

    def test_seeds_prepaid_balance_sweep_interval(self, db_session, monkeypatch):
        monkeypatch.setenv("PREPAID_BALANCE_SWEEP_INTERVAL_SECONDS", "1800")

        settings_seed.seed_collections_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.collections,
                DomainSetting.key == "prepaid_balance_sweep_interval_seconds",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "1800"

    def test_seeds_suspension_notification_dedupe_hours(self, db_session):
        settings_seed.seed_collections_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.collections,
                DomainSetting.key == "suspension_notification_dedupe_hours",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "24"


class TestSeedBillingNotificationSettings:
    def test_seeds_phase7_notification_keys(self, db_session):
        settings_seed.seed_billing_settings(db_session)

        settings = {
            row.key: row.value_text
            for row in db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.billing,
                DomainSetting.key.in_(
                    [
                        "expiry_reminder_days",
                        "invoice_reminder_days",
                        "autopay_max_consecutive_failures",
                        "minimum_balance",
                        "arrangement_min_installments",
                        "arrangement_max_installments",
                        "arrangement_default_overdue_installments",
                        "service_extension_max_days",
                        "subscription_billing_treatment_max_days",
                        "topup_preset_amounts",
                        "topup_reconciliation_stale_minutes",
                        "topup_reconciliation_max_age_days",
                        "topup_reconciliation_expiry_grace_hours",
                        "topup_reconciliation_batch_size",
                        "gateway_topup_intent_ttl_minutes",
                        "ar_aging_bucket_days",
                        "billing_health_scan_min_ratio",
                        "billing_health_payment_volume_min_ratio",
                        "billing_health_payment_baseline_min_daily",
                    ]
                ),
            )
            .all()
        }

        assert settings["expiry_reminder_days"] == "7"
        assert settings["invoice_reminder_days"] == "7,1"
        assert settings["autopay_max_consecutive_failures"] == "3"
        assert settings["minimum_balance"] == "0"
        assert settings["arrangement_min_installments"] == "2"
        assert settings["arrangement_max_installments"] == "24"
        assert settings["arrangement_default_overdue_installments"] == "2"
        assert settings["service_extension_max_days"] == "30"
        assert settings["subscription_billing_treatment_max_days"] == "366"
        assert settings["topup_preset_amounts"] == ("1000,2000,5000,10000,20000,50000")
        assert settings["topup_reconciliation_stale_minutes"] == "15"
        assert settings["topup_reconciliation_max_age_days"] == "7"
        assert settings["topup_reconciliation_expiry_grace_hours"] == "24"
        assert settings["topup_reconciliation_batch_size"] == "50"
        assert settings["gateway_topup_intent_ttl_minutes"] == "30"
        assert settings["ar_aging_bucket_days"] == "30,60,90"
        assert settings["billing_health_scan_min_ratio"] == "0.5"
        assert settings["billing_health_payment_volume_min_ratio"] == "0.4"
        assert settings["billing_health_payment_baseline_min_daily"] == "5.0"


# =============================================================================
# Geocoding Settings Tests
# =============================================================================


class TestSeedGeocodingSettings:
    """Tests for seed_geocoding_settings function."""

    def test_seeds_geocoding_enabled(self, db_session, monkeypatch):
        """Test geocoding enabled setting is seeded."""
        # Set all required env vars to avoid None value_text
        monkeypatch.setenv("GEOCODING_PROVIDER", "nominatim")
        monkeypatch.setenv("GEOCODING_BASE_URL", "https://nominatim.openstreetmap.org")
        monkeypatch.setenv("GEOCODING_USER_AGENT", "dotmac_sm")
        monkeypatch.setenv("GEOCODING_EMAIL", "test@example.com")
        monkeypatch.setenv("GEOCODING_TIMEOUT_SEC", "5")
        monkeypatch.setenv("GEOCODING_MIN_INTERVAL_MS", "1250")
        monkeypatch.setenv("GEOCODING_GOOGLE_API_KEY", "google-secret")
        monkeypatch.setenv("GEOCODING_MAPBOX_API_KEY", "mapbox-secret")

        settings_seed.seed_geocoding_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.geocoding,
                DomainSetting.key == "enabled",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_json is True

        interval = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.geocoding,
                DomainSetting.key == "min_interval_ms",
            )
            .first()
        )
        assert interval is not None
        assert interval.value_text == "1250"

        google_key = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.geocoding,
                DomainSetting.key == "google_api_key",
            )
            .first()
        )
        assert google_key is not None
        assert google_key.is_secret is True

    def test_seeds_geocoding_provider(self, db_session, monkeypatch):
        """Test geocoding provider setting is seeded."""
        monkeypatch.setenv("GEOCODING_PROVIDER", "google")
        monkeypatch.setenv("GEOCODING_BASE_URL", "https://maps.googleapis.com")
        monkeypatch.setenv("GEOCODING_USER_AGENT", "dotmac_sm")
        monkeypatch.setenv("GEOCODING_EMAIL", "test@example.com")
        monkeypatch.setenv("GEOCODING_TIMEOUT_SEC", "5")
        monkeypatch.setenv("GEOCODING_MIN_INTERVAL_MS", "0")
        monkeypatch.setenv("GEOCODING_GOOGLE_API_KEY", "google-secret")
        monkeypatch.setenv("GEOCODING_MAPBOX_API_KEY", "mapbox-secret")

        settings_seed.seed_geocoding_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.geocoding,
                DomainSetting.key == "provider",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "google"


# =============================================================================
# Scheduler Settings Tests
# =============================================================================


class TestSeedSchedulerSettings:
    """Tests for seed_scheduler_settings function."""

    def test_does_not_persist_deployment_broker_url(self, db_session, monkeypatch):
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://custom:6379/0")
        monkeypatch.delenv("REDIS_URL", raising=False)

        settings_seed.seed_scheduler_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.scheduler,
                DomainSetting.key == "broker_url",
            )
            .first()
        )
        assert setting is None

    def test_seeds_timezone(self, db_session, monkeypatch):
        """Test timezone setting is seeded."""
        monkeypatch.setenv("CELERY_TIMEZONE", "America/New_York")

        settings_seed.seed_scheduler_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.scheduler,
                DomainSetting.key == "timezone",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "America/New_York"

    def test_seeds_event_outbox_cadence_without_dispatch_toggle(
        self, db_session, monkeypatch
    ):
        monkeypatch.setenv("EVENT_DISPATCH_ENABLED", "true")
        monkeypatch.setenv("EVENT_DISPATCH_INTERVAL_SECONDS", "30")
        monkeypatch.setenv("EVENT_DISPATCH_BATCH_SIZE", "250")

        settings_seed.seed_scheduler_settings(db_session)

        rows = {
            row.key: row
            for row in db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.scheduler,
                DomainSetting.key.in_(
                    {
                        "event_dispatch_enabled",
                        "event_dispatch_interval_seconds",
                        "event_dispatch_batch_size",
                    }
                ),
            )
            .all()
        }
        assert "event_dispatch_enabled" not in rows
        assert rows["event_dispatch_interval_seconds"].value_text == "30"
        assert rows["event_dispatch_batch_size"].value_text == "250"


# =============================================================================
# Radius Settings Tests
# =============================================================================


class TestSeedRadiusSettings:
    """Tests for seed_radius_settings function."""

    def test_seeds_radius_auth_settings(self, db_session, monkeypatch):
        """Test RADIUS auth settings are seeded."""
        monkeypatch.setenv("RADIUS_AUTH_SERVER_ID", "server-123")
        monkeypatch.setenv("RADIUS_AUTH_SHARED_SECRET", "secret123")
        monkeypatch.setenv("RADIUS_AUTH_DICTIONARY", "/etc/raddb/dictionary")
        monkeypatch.setenv("RADIUS_AUTH_TIMEOUT_SEC", "3")

        settings_seed.seed_radius_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.radius,
                DomainSetting.key == "auth_server_id",
            )
            .first()
        )
        assert setting is not None


# =============================================================================
# Billing Settings Tests
# =============================================================================


class TestSeedBillingSettings:
    """Tests for seed_billing_settings function."""

    def test_seeds_default_currency(self, db_session, monkeypatch):
        """Test default currency setting is seeded."""
        monkeypatch.setenv("BILLING_DEFAULT_CURRENCY", "EUR")

        settings_seed.seed_billing_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.billing,
                DomainSetting.key == "default_currency",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "EUR"

    def test_seeds_prepaid_reconstruction_trust_anchor_as_openbao_ref(
        self, db_session, monkeypatch
    ):
        reference = (
            "bao://secret/billing/prepaid-reconstruction-attestation#public_key_pem"
        )
        monkeypatch.setenv(
            "PREPAID_RECONSTRUCTION_ATTESTATION_PUBLIC_KEY_REF",
            reference,
        )

        settings_seed.seed_billing_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.billing,
                DomainSetting.key
                == "prepaid_reconstruction_attestation_public_key_ref",
            )
            .one()
        )
        assert setting.is_secret is True
        assert setting.value_text == reference

    def test_seeds_invoice_number_settings(self, db_session, monkeypatch):
        """Test invoice number settings are seeded."""
        monkeypatch.setenv("BILLING_INVOICE_NUMBER_PREFIX", "INVOICE-")

        settings_seed.seed_billing_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.billing,
                DomainSetting.key == "invoice_number_prefix",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "INVOICE-"

    def test_does_not_seed_retired_access_controls(self, db_session):
        settings_seed.seed_billing_settings(db_session)

        retired = {
            "auto_suspend_on_overdue",
            "suspension_grace_hours",
            "dunning_escalation_days",
            "blocking_period_days",
            "deactivation_period_days",
        }
        seeded = {
            row.key
            for row in db_session.query(DomainSetting)
            .filter(DomainSetting.domain == SettingDomain.billing)
            .all()
        }
        assert retired.isdisjoint(seeded)

    def test_seeds_plan_change_runtime_settings(self, db_session, monkeypatch):
        """Test runtime plan-change settings are seeded in billing domain."""
        monkeypatch.setenv("PLAN_CHANGE_REFUND_POLICY", "prorated")
        monkeypatch.setenv("PLAN_CHANGE_INVOICE_TIMING", "next_invoice")

        settings_seed.seed_billing_settings(db_session)

        refund = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.billing,
                DomainSetting.key == "refund_policy",
            )
            .first()
        )
        invoice_timing = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.billing,
                DomainSetting.key == "invoice_timing",
            )
            .first()
        )
        assert refund is not None
        assert refund.value_text == "prorated"
        assert invoice_timing is not None
        assert invoice_timing.value_text == "next_invoice"


# =============================================================================
# Catalog Settings Tests
# =============================================================================


class TestSeedCatalogSettings:
    """Tests for seed_catalog_settings function."""

    def test_seeds_default_proration_policy(self, db_session, monkeypatch):
        """Test default proration policy is seeded."""
        monkeypatch.setenv("CATALOG_DEFAULT_PRORATION_POLICY", "none")

        settings_seed.seed_catalog_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.catalog,
                DomainSetting.key == "default_proration_policy",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "none"


# =============================================================================
# Subscriber Settings Tests
# =============================================================================


# =============================================================================
# Usage Policy Settings Tests
# =============================================================================


class TestSeedUsagePolicySettings:
    """Tests for seed_usage_policy_settings function."""

    def test_seeds_default_charge_status(self, db_session, monkeypatch):
        """Test default charge status is seeded."""
        monkeypatch.setenv("USAGE_DEFAULT_CHARGE_STATUS", "pending")

        settings_seed.seed_usage_policy_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.usage,
                DomainSetting.key == "default_charge_status",
            )
            .first()
        )
        assert setting is not None


# =============================================================================
# Collections Policy Settings Tests
# =============================================================================


class TestSeedCollectionsPolicySettings:
    """Tests for seed_collections_policy_settings function."""

    def test_seeds_default_dunning_case_status(self, db_session, monkeypatch):
        """Test default dunning case status is seeded."""
        monkeypatch.setenv("COLLECTIONS_DEFAULT_DUNNING_CASE_STATUS", "active")

        settings_seed.seed_collections_policy_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.collections,
                DomainSetting.key == "default_dunning_case_status",
            )
            .first()
        )
        assert setting is not None


# =============================================================================
# Auth Policy Settings Tests
# =============================================================================


class TestSeedAuthPolicySettings:
    """Tests for seed_auth_policy_settings function."""

    def test_seeds_default_auth_provider(self, db_session, monkeypatch):
        """Test default auth provider is seeded."""
        monkeypatch.setenv("AUTH_DEFAULT_AUTH_PROVIDER", "ldap")

        settings_seed.seed_auth_policy_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.auth,
                DomainSetting.key == "default_auth_provider",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "ldap"


# =============================================================================
# Provisioning Settings Tests
# =============================================================================


class TestSeedProvisioningSettings:
    """Tests for seed_provisioning_settings function."""

    def test_seeds_default_service_order_status(self, db_session, monkeypatch):
        """Test default service order status is seeded."""
        monkeypatch.setenv("PROVISIONING_DEFAULT_SERVICE_ORDER_STATUS", "pending")

        settings_seed.seed_provisioning_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.provisioning,
                DomainSetting.key == "default_service_order_status",
            )
            .first()
        )
        assert setting is not None


# =============================================================================
# Projects Settings Tests
# =============================================================================


class TestSeedProjectsSettings:
    """Tests for seed_projects_settings function."""

    def test_seeds_default_project_status(self, db_session, monkeypatch):
        """Test default project status is seeded."""
        monkeypatch.setenv("PROJECTS_DEFAULT_PROJECT_STATUS", "active")

        settings_seed.seed_projects_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.projects,
                DomainSetting.key == "default_project_status",
            )
            .first()
        )
        assert setting is not None


# =============================================================================
# Network Policy Settings Tests
# =============================================================================


class TestSeedNetworkPolicySettings:
    """Tests for seed_network_policy_settings function."""

    def test_seeds_default_device_type(self, db_session, monkeypatch):
        """Test default device type is seeded."""
        monkeypatch.setenv("NETWORK_DEFAULT_DEVICE_TYPE", "router")

        settings_seed.seed_network_policy_settings(db_session)

        from app.models.domain_settings import SettingDomain

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.network,
                DomainSetting.key == "default_device_type",
            )
            .first()
        )
        assert setting is not None

    def test_does_not_recreate_retired_api_session_kick_alias(
        self, db_session, monkeypatch
    ):
        monkeypatch.delenv("NETWORK_MIKROTIK_API_SESSION_KICK_ENABLED", raising=False)

        settings_seed.seed_network_policy_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.network,
                DomainSetting.key == "mikrotik_api_session_kick_enabled",
            )
            .first()
        )
        assert setting is None

    def test_does_not_mutate_retired_api_session_kick_alias(
        self, db_session, monkeypatch
    ):
        monkeypatch.delenv("NETWORK_MIKROTIK_API_SESSION_KICK_ENABLED", raising=False)
        network_settings.ensure_by_key(
            db_session,
            key="mikrotik_api_session_kick_enabled",
            value_type=SettingValueType.boolean,
            value_text="false",
            value_json=False,
        )

        settings_seed.seed_network_policy_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(DomainSetting.domain == SettingDomain.network)
            .filter(DomainSetting.key == "mikrotik_api_session_kick_enabled")
            .first()
        )
        assert setting is not None
        assert setting.value_text == "false"
        assert setting.value_json is False


# =============================================================================
# Radius Policy Settings Tests
# =============================================================================


class TestSeedRadiusPolicySettings:
    """Tests for seed_radius_policy_settings function."""

    def test_seeds_default_auth_port(self, db_session, monkeypatch):
        """Test default auth port is seeded."""
        monkeypatch.setenv("RADIUS_DEFAULT_AUTH_PORT", "1645")

        settings_seed.seed_radius_policy_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.radius,
                DomainSetting.key == "default_auth_port",
            )
            .first()
        )
        assert setting is not None
        assert setting.value_text == "1645"


# =============================================================================
# Inventory Settings Tests
# =============================================================================


# =============================================================================
# Comms Settings Tests
# =============================================================================


class TestSeedCommsSettings:
    """Tests for seed_comms_settings function."""

    def test_seeds_default_notification_status(self, db_session, monkeypatch):
        """Test default notification status is seeded."""
        monkeypatch.setenv("COMMS_DEFAULT_NOTIFICATION_STATUS", "sent")

        settings_seed.seed_comms_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.comms,
                DomainSetting.key == "default_notification_status",
            )
            .first()
        )
        assert setting is not None

    def test_seeds_periodic_campaign_admission(self, db_session, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_PROCESSING_ENABLED", "true")

        settings_seed.seed_comms_settings(db_session)

        setting = (
            db_session.query(DomainSetting)
            .filter(
                DomainSetting.domain == SettingDomain.comms,
                DomainSetting.key == "campaign_processing_enabled",
            )
            .one()
        )
        assert setting.value_json is True
