import json
import logging
import os

from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.channel_health_contracts import DEFAULT_CHANNEL_HEALTH_CONTRACTS
from app.services.domain_settings import (
    DomainSettings,
    audit_settings,
    auth_settings,
    billing_settings,
    catalog_settings,
    collections_settings,
    comms_settings,
    geocoding_settings,
    gis_settings,
    imports_settings,
    network_monitoring_settings,
    network_settings,
    notification_settings,
    provisioning_settings,
    radius_settings,
    scheduler_settings,
    subscriber_settings,
    tr069_settings,
    usage_settings,
)
from app.services.secrets import is_openbao_ref
from app.services.settings_spec import (
    DOMAIN_SETTINGS_SERVICE,
    SCHEDULER_BOOLEAN_SETTING_KEYS,
    SCHEDULER_ENV_BOOTSTRAP_SETTING_KEYS,
    coerce_value,
    get_spec,
)
from app.timezone import APP_TIMEZONE_NAME

logger = logging.getLogger(__name__)


def seed_scheduler_runtime_settings(db: Session) -> None:
    """Materialize bootstrap values for registered scheduler inputs.

    Runtime scheduling resolves the database row or registered default.
    Environment variables are consulted only here, and ``ensure_by_key``
    preserves an existing operator decision instead of overriding it on restart.
    """

    registered = SCHEDULER_BOOLEAN_SETTING_KEYS | SCHEDULER_ENV_BOOTSTRAP_SETTING_KEYS
    for domain, key in sorted(
        registered,
        key=lambda item: (item[0].value, item[1]),
    ):
        spec = get_spec(domain, key)
        if spec is None or spec.value_type not in {
            SettingValueType.boolean,
            SettingValueType.integer,
            SettingValueType.string,
        }:
            raise RuntimeError(f"Invalid scheduler setting: {domain.value}.{key}")
        raw = os.getenv(spec.env_var) if spec.env_var else None
        value, error = coerce_value(spec, raw if raw is not None else spec.default)
        if error or value is None:
            raise RuntimeError(
                f"Invalid scheduler bootstrap value: {domain.value}.{key}"
            )
        value_text = str(value)
        value_json: bool | None = None
        # `==`, not `is`: SettingValueType is an open `str` subclass and is
        # deliberately not interned (migration 512).
        if spec.value_type == SettingValueType.boolean:
            if not isinstance(value, bool):
                raise RuntimeError(
                    f"Invalid scheduler bootstrap boolean: {domain.value}.{key}"
                )
            value_text = "true" if value else "false"
            value_json = value
        service = DOMAIN_SETTINGS_SERVICE[domain]
        service.ensure_by_key(
            db,
            key=key,
            value_type=spec.value_type,
            value_text=value_text,
            value_json=value_json,
        )


def seed_auth_settings(db: Session) -> None:
    rotation_enabled = os.getenv("CREDENTIAL_ROTATION_ENABLED", "true")
    auth_settings.ensure_by_key(
        db,
        key="credential_rotation_enabled",
        value_type=SettingValueType.boolean,
        value_text=rotation_enabled,
        value_json=rotation_enabled.lower() in {"1", "true", "yes", "on"},
    )
    rotation_auto_apply = os.getenv("CREDENTIAL_ROTATION_AUTO_APPLY", "true")
    auth_settings.ensure_by_key(
        db,
        key="credential_rotation_auto_apply",
        value_type=SettingValueType.boolean,
        value_text=rotation_auto_apply,
        value_json=rotation_auto_apply.lower() in {"1", "true", "yes", "on"},
    )
    auth_settings.ensure_by_key(
        db,
        key="credential_rotation_interval_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CREDENTIAL_ROTATION_INTERVAL_DAYS", "90"),
    )
    auth_settings.ensure_by_key(
        db,
        key="credential_rotation_grace_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CREDENTIAL_ROTATION_GRACE_DAYS", "7"),
    )
    auth_settings.ensure_by_key(
        db,
        key="jwt_algorithm",
        value_type=SettingValueType.string,
        value_text=os.getenv("JWT_ALGORITHM", "HS256"),
    )
    auth_settings.ensure_by_key(
        db,
        key="jwt_access_ttl_minutes",
        value_type=SettingValueType.integer,
        value_text=os.getenv("JWT_ACCESS_TTL_MINUTES", "15"),
    )
    auth_settings.ensure_by_key(
        db,
        key="jwt_refresh_ttl_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("JWT_REFRESH_TTL_DAYS", "30"),
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_name",
        value_type=SettingValueType.string,
        value_text=os.getenv("REFRESH_COOKIE_NAME", "refresh_token"),
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_secure",
        value_type=SettingValueType.boolean,
        value_text=os.getenv("REFRESH_COOKIE_SECURE", "false"),
        value_json=os.getenv("REFRESH_COOKIE_SECURE", "false").lower()
        in {"1", "true", "yes", "on"},
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_samesite",
        value_type=SettingValueType.string,
        value_text=os.getenv("REFRESH_COOKIE_SAMESITE", "lax"),
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_domain",
        value_type=SettingValueType.string,
        value_text=os.getenv("REFRESH_COOKIE_DOMAIN", ""),
    )
    auth_settings.ensure_by_key(
        db,
        key="refresh_cookie_path",
        value_type=SettingValueType.string,
        value_text=os.getenv("REFRESH_COOKIE_PATH", "/auth"),
    )
    auth_settings.ensure_by_key(
        db,
        key="totp_issuer",
        value_type=SettingValueType.string,
        value_text=os.getenv("TOTP_ISSUER", "dotmac_sm"),
    )
    auth_settings.ensure_by_key(
        db,
        key="api_key_rate_window_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("API_KEY_RATE_WINDOW_SECONDS", "60"),
    )
    auth_settings.ensure_by_key(
        db,
        key="api_key_rate_max",
        value_type=SettingValueType.integer,
        value_text=os.getenv("API_KEY_RATE_MAX", "5"),
    )
    auth_settings.ensure_by_key(
        db,
        key="api_key_max_ttl_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("API_KEY_MAX_TTL_DAYS", "0"),
    )
    auth_settings.ensure_by_key(
        db,
        key="api_key_max_per_owner",
        value_type=SettingValueType.integer,
        value_text=os.getenv("API_KEY_MAX_PER_OWNER", "0"),
    )
    jwt_secret = os.getenv("JWT_SECRET")
    if jwt_secret and is_openbao_ref(jwt_secret):
        auth_settings.ensure_by_key(
            db,
            key="jwt_secret",
            value_type=SettingValueType.string,
            value_text=jwt_secret,
            is_secret=True,
        )
    totp_key = os.getenv("TOTP_ENCRYPTION_KEY")
    if totp_key and is_openbao_ref(totp_key):
        auth_settings.ensure_by_key(
            db,
            key="totp_encryption_key",
            value_type=SettingValueType.string,
            value_text=totp_key,
            is_secret=True,
        )


def seed_audit_settings(db: Session) -> None:
    audit_settings.ensure_by_key(
        db,
        key="enabled",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    audit_settings.ensure_by_key(
        db,
        key="methods",
        value_type=SettingValueType.json,
        value_json=["POST", "PUT", "PATCH", "DELETE"],
    )
    audit_settings.ensure_by_key(
        db,
        key="skip_paths",
        value_type=SettingValueType.json,
        value_json=["/static", "/web", "/health"],
    )
    audit_settings.ensure_by_key(
        db,
        key="read_trigger_header",
        value_type=SettingValueType.string,
        value_text="x-audit-read",
    )
    audit_settings.ensure_by_key(
        db,
        key="read_trigger_query",
        value_type=SettingValueType.string,
        value_text="audit",
    )


def seed_imports_settings(db: Session) -> None:
    imports_settings.ensure_by_key(
        db,
        key="max_file_bytes",
        value_type=SettingValueType.integer,
        value_text=str(5 * 1024 * 1024),
    )
    imports_settings.ensure_by_key(
        db,
        key="max_rows",
        value_type=SettingValueType.integer,
        value_text="5000",
    )
    imports_settings.ensure_by_key(
        db,
        key="import_history_log",
        value_type=SettingValueType.json,
        value_json=[],
    )
    imports_settings.ensure_by_key(
        db,
        key="import_rollback_window_hours",
        value_type=SettingValueType.integer,
        value_text=os.getenv("IMPORT_ROLLBACK_WINDOW_HOURS", "24"),
    )
    imports_settings.ensure_by_key(
        db,
        key="import_background_threshold_rows",
        value_type=SettingValueType.integer,
        value_text=os.getenv("IMPORT_BACKGROUND_THRESHOLD_ROWS", "1000"),
    )
    imports_settings.ensure_by_key(
        db,
        key="import_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )


def seed_gis_settings(db: Session) -> None:
    gis_settings.ensure_by_key(
        db,
        key="sync_interval_minutes",
        value_type=SettingValueType.integer,
        value_text="60",
    )
    gis_settings.ensure_by_key(
        db,
        key="sync_pop_sites",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    gis_settings.ensure_by_key(
        db,
        key="sync_addresses",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    gis_settings.ensure_by_key(
        db,
        key="sync_deactivate_missing",
        value_type=SettingValueType.boolean,
        value_text="false",
        value_json=False,
    )
    gis_settings.ensure_by_key(
        db,
        key="map_customer_limit",
        value_type=SettingValueType.integer,
        value_text="2000",
    )
    gis_settings.ensure_by_key(
        db,
        key="map_nearest_search_max_km",
        value_type=SettingValueType.integer,
        value_text="50",
    )
    gis_settings.ensure_by_key(
        db,
        key="map_snap_max_m",
        value_type=SettingValueType.integer,
        value_text="250",
    )
    gis_settings.ensure_by_key(
        db,
        key="map_allow_straightline_fallback",
        value_type=SettingValueType.boolean,
        value_text="false",
    )
    # Customer pin auto-approval — seeded conservatively: enabled but in SHADOW
    # (evaluate + record, never auto-approve) until ops reviews and turns shadow
    # off; tight 100 m radius; rate-limited to one auto-approval per 30 days.
    gis_settings.ensure_by_key(
        db,
        key="location_auto_approve_enabled",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    gis_settings.ensure_by_key(
        db,
        key="location_auto_approve_shadow",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    gis_settings.ensure_by_key(
        db,
        key="location_auto_approve_radius_m",
        value_type=SettingValueType.integer,
        value_text="100",
    )
    gis_settings.ensure_by_key(
        db,
        key="location_auto_require_coverage",
        value_type=SettingValueType.boolean,
        value_text="false",
        value_json=False,
    )
    gis_settings.ensure_by_key(
        db,
        key="location_auto_approve_window_days",
        value_type=SettingValueType.integer,
        value_text="30",
    )
    gis_settings.ensure_by_key(
        db,
        key="location_auto_approve_max_per_window",
        value_type=SettingValueType.integer,
        value_text="1",
    )
    gis_settings.ensure_by_key(
        db,
        key="location_geocode_retry_days",
        value_type=SettingValueType.integer,
        value_text="7",
    )


def seed_usage_settings(db: Session) -> None:
    enabled_raw = os.getenv("USAGE_RATING_ENABLED", "true")
    usage_settings.ensure_by_key(
        db,
        key="usage_rating_enabled",
        value_type=SettingValueType.boolean,
        value_text=enabled_raw,
        value_json=enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    usage_settings.ensure_by_key(
        db,
        key="usage_rating_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("USAGE_RATING_INTERVAL_SECONDS", "86400"),
    )
    usage_settings.ensure_by_key(
        db,
        key="usage_metering_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("USAGE_METERING_INTERVAL_SECONDS", "60"),
    )
    usage_settings.ensure_by_key(
        db,
        key="fup_evaluation_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("FUP_EVALUATION_INTERVAL_SECONDS", "900"),
    )
    usage_settings.ensure_by_key(
        db,
        key="radius_accounting_import_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_ACCOUNTING_IMPORT_INTERVAL_SECONDS", "60"),
    )
    usage_settings.ensure_by_key(
        db,
        key="usage_warning_thresholds",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_WARNING_THRESHOLDS", "0.8,0.9"),
    )
    usage_settings.ensure_by_key(
        db,
        key="fup_throttle_radius_profile_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_FUP_THROTTLE_RADIUS_PROFILE_ID", ""),
    )
    usage_settings.ensure_by_key(
        db,
        key="fup_action",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_FUP_ACTION", "throttle"),
    )


def seed_notification_settings(db: Session) -> None:
    # SMS channel controls. `env_var` is a BOOTSTRAP input: it is materialised
    # into a row here and never consulted again at runtime, which is what
    # replaced `sms._get_setting`'s env-above-row read. The credentials are not
    # seeded — they are held in app.config (ADR-0009).
    sms_enabled_raw = os.getenv("SMS_ENABLED", "false")
    notification_settings.ensure_by_key(
        db,
        key="sms_enabled",
        value_type=SettingValueType.boolean,
        value_text=sms_enabled_raw,
        value_json=sms_enabled_raw.lower() in {"1", "true", "yes", "on", "enabled"},
    )
    for key, env_key, fallback in (
        ("sms_provider", "SMS_PROVIDER", ""),
        ("sms_from_number", "SMS_FROM_NUMBER", ""),
        ("sms_username", "SMS_USERNAME", ""),
        ("sms_webhook_url", "SMS_WEBHOOK_URL", ""),
    ):
        notification_settings.ensure_by_key(
            db,
            key=key,
            value_type=SettingValueType.string,
            value_text=os.getenv(env_key, fallback),
        )
    for key, env_key, fallback in (
        ("sms_api_timeout_seconds", "SMS_API_TIMEOUT_SECONDS", "30"),
        ("sms_max_length", "SMS_MAX_LENGTH", "160"),
    ):
        notification_settings.ensure_by_key(
            db,
            key=key,
            value_type=SettingValueType.integer,
            value_text=os.getenv(env_key, fallback),
        )

    enabled_raw = os.getenv("ALERT_NOTIFICATIONS_ENABLED", "true")
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_enabled",
        value_type=SettingValueType.boolean,
        value_text=enabled_raw,
        value_json=enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_channel",
        value_type=SettingValueType.string,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_CHANNEL", "email"),
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_recipient",
        value_type=SettingValueType.string,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_RECIPIENT", ""),
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_template_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_TEMPLATE_ID", ""),
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_rotation_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_ROTATION_ID", ""),
    )
    notification_settings.ensure_by_key(
        db,
        key="alert_notifications_default_delay_minutes",
        value_type=SettingValueType.integer,
        value_text=os.getenv("ALERT_NOTIFICATIONS_DEFAULT_DELAY_MINUTES", "0"),
    )
    notification_settings.ensure_by_key(
        db,
        key="notification_queue_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("NOTIFICATION_QUEUE_INTERVAL_SECONDS", "60"),
    )
    escalation_delivery_enabled_raw = os.getenv(
        "OPERATIONAL_ESCALATION_DELIVERY_ENABLED", "true"
    )
    notification_settings.ensure_by_key(
        db,
        key="operational_escalation_delivery_enabled",
        value_type=SettingValueType.boolean,
        value_text=escalation_delivery_enabled_raw,
        value_json=escalation_delivery_enabled_raw.lower()
        in {"1", "true", "yes", "on"},
    )
    notification_settings.ensure_by_key(
        db,
        key="operational_escalation_delivery_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("OPERATIONAL_ESCALATION_DELIVERY_INTERVAL_SECONDS", "60"),
    )
    for key, env_name, default in [
        ("notification_max_retries", "NOTIFICATION_MAX_RETRIES", "3"),
        (
            "notification_sending_timeout_minutes",
            "NOTIFICATION_SENDING_TIMEOUT_MINUTES",
            "10",
        ),
        (
            "notification_retry_backoff_minutes",
            "NOTIFICATION_RETRY_BACKOFF_MINUTES",
            "1,5,15",
        ),
        (
            "notification_per_channel_rate_limit",
            "NOTIFICATION_PER_CHANNEL_RATE_LIMIT",
            "50",
        ),
        ("sms_api_timeout_seconds", "SMS_API_TIMEOUT_SECONDS", "30"),
        ("sms_max_length", "SMS_MAX_LENGTH", "160"),
        ("notification_quiet_hours_start", "NOTIFICATION_QUIET_HOURS_START", "22:00"),
        ("notification_quiet_hours_end", "NOTIFICATION_QUIET_HOURS_END", "07:00"),
        (
            "notification_dedupe_window_minutes",
            "NOTIFICATION_DEDUPE_WINDOW_MINUTES",
            "0",
        ),
    ]:
        notification_settings.ensure_by_key(
            db,
            key=key,
            value_type=SettingValueType.integer
            if default.isdigit()
            else SettingValueType.string,
            value_text=os.getenv(env_name, default),
        )


def _seed_missing_notification_templates(db: Session) -> int:
    """Insert any missing default notification templates without committing."""
    from app.models.notification import NotificationChannel, NotificationTemplate

    templates = [
        {
            "code": "subscriber_created",
            "name": "Subscriber Created",
            "channel": NotificationChannel.email,
            "subject": "Your customer account is ready",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your customer account has been created successfully. "
                "You can now manage your services and billing through the portal.\n\n"
                "Thank you for choosing us."
            ),
        },
        {
            "code": "subscriber_updated",
            "name": "Subscriber Updated",
            "channel": NotificationChannel.email,
            "subject": "Your account profile was updated",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your account profile was updated successfully.\n\n"
                "Updated fields: {updated_fields}\n\n"
                "If you did not make this change, please contact support immediately."
            ),
        },
        {
            "code": "subscription_created",
            "name": "Subscription Created",
            "channel": NotificationChannel.email,
            "subject": "Your new service subscription",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your subscription to {offer_name} has been created. "
                "A service order will be created for installation.\n\n"
                "If you have questions, contact our support team.\n\n"
                "Thank you for choosing us."
            ),
        },
        {
            "code": "subscription_created",
            "name": "Subscription Created SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} subscription has been created. "
                "We will contact you shortly with the next steps."
            ),
        },
        {
            "code": "subscription_activated",
            "name": "Subscription Activated",
            "channel": NotificationChannel.email,
            "subject": "Your service is now active",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription is now active. "
                "You can start using your service immediately.\n\n"
                "Your service credentials have been sent separately.\n\n"
                "Welcome aboard!"
            ),
        },
        {
            "code": "subscription_activated",
            "name": "Subscription Activated SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} service is now active. "
                "If you need help, contact support."
            ),
        },
        {
            "code": "subscription_suspended",
            "name": "Subscription Suspended",
            "channel": NotificationChannel.email,
            "subject": "Your service has been paused",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} service has been paused. This is usually due "
                "to an outstanding balance.\n\n"
                "To restore it, you can make a payment from your account "
                "({portal_url}/billing) or contact our support team — we're happy "
                "to help.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "subscription_suspended",
            "name": "Subscription Suspended SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} service has been paused. "
                "Pay at {portal_url}/billing or contact support to restore it."
            ),
        },
        {
            "code": "subscription_resumed",
            "name": "Subscription Resumed",
            "channel": NotificationChannel.email,
            "subject": "Your service has been resumed",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription has been resumed. "
                "Your service is now fully active and ready to use.\n\n"
                "Thank you for being a valued customer."
            ),
        },
        {
            "code": "subscription_resumed",
            "name": "Subscription Resumed SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} service has been resumed. "
                "You can now use your service as normal."
            ),
        },
        {
            "code": "suspension_warning",
            "name": "Suspension Warning",
            "channel": NotificationChannel.email,
            "subject": "A reminder about invoice #{invoice_number}",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Invoice #{invoice_number} for {amount} is currently unpaid. To "
                "avoid any interruption to your service, please arrange payment "
                "when you can.\n\n"
                "Pay anytime: {portal_url}/billing\n\n"
                "If you have already paid, please disregard this message. For any "
                "questions, please contact our support team."
            ),
        },
        {
            "code": "suspension_warning",
            "name": "Suspension Warning SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Reminder: invoice #{invoice_number} for {amount} is unpaid. "
                "Please pay to avoid any service interruption: {portal_url}/billing"
            ),
        },
        {
            "code": "subscription_canceled",
            "name": "Subscription Canceled",
            "channel": NotificationChannel.email,
            "subject": "Subscription canceled",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription has been canceled. "
                "If this was not expected, please contact our support team.\n\n"
                "We hope to serve you again in the future."
            ),
        },
        {
            "code": "subscription_expiring",
            "name": "Subscription Expiring",
            "channel": NotificationChannel.email,
            "subject": "Your subscription is expiring soon",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription will expire soon. "
                "Please renew to avoid service interruption.\n\n"
                "You can renew by making a payment through our customer portal "
                "or by contacting our support team.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "subscription_renewal_invoice_ready",
            "name": "Advance Renewal Invoice Ready",
            "channel": NotificationChannel.email,
            "subject": "Renewal invoice #{invoice_number} is ready",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your current service remains active until {renewal_period_start}. "
                "Invoice #{invoice_number} for {renewal_period_start} to "
                "{renewal_period_end} is ready. You can pay now to continue "
                "without interruption.\n\nView and pay: {invoice_url}"
            ),
        },
        {
            "code": "subscription_expiring",
            "name": "Subscription Expiring SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} subscription expires soon. "
                "Please renew to avoid interruption."
            ),
        },
        {
            "code": "subscription_expired",
            "name": "Subscription Expired",
            "channel": NotificationChannel.email,
            "subject": "Your subscription has expired",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your {offer_name} subscription has expired. "
                "Renew your service to restore access."
            ),
        },
        {
            "code": "subscription_expired",
            "name": "Subscription Expired SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your {offer_name} subscription has expired. "
                "Renew to restore access."
            ),
        },
        {
            "code": "subscription_upgraded",
            "name": "Subscription Upgraded",
            "channel": NotificationChannel.email,
            "subject": "Your plan has been upgraded",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your service has been upgraded from {old_offer_name} to {new_offer_name}."
            ),
        },
        {
            "code": "subscription_upgraded",
            "name": "Subscription Upgraded SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your plan has been upgraded to {new_offer_name}."
            ),
        },
        {
            "code": "subscription_downgraded",
            "name": "Subscription Downgraded",
            "channel": NotificationChannel.email,
            "subject": "Your plan has been updated",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your service has been changed from {old_offer_name} to {new_offer_name}."
            ),
        },
        {
            "code": "subscription_downgraded",
            "name": "Subscription Downgraded SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Hi {subscriber_name}, your plan has been updated to {new_offer_name}."
            ),
        },
        {
            "code": "invoice_created",
            "name": "Invoice Created",
            "channel": NotificationChannel.email,
            "subject": "New invoice #{invoice_number}",
            "body": (
                "Dear {subscriber_name},\n\n"
                "A new invoice #{invoice_number} for {amount} has been generated "
                "for your {offer_name} subscription.\n\n"
                "Due date: {due_date}\n\n"
                "You can view and pay this invoice through our customer portal.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "invoice_sent",
            "name": "Invoice Sent",
            "channel": NotificationChannel.email,
            "subject": "Invoice #{invoice_number} — payment due {due_date}",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Invoice #{invoice_number} for {amount} is due on {due_date}.\n\n"
                "Please make your payment before the due date to avoid "
                "service interruption.\n\n"
                "Pay online: {portal_url}/billing\n\n"
                "Thank you."
            ),
        },
        {
            "code": "invoice_sent",
            "name": "Invoice Sent SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Invoice #{invoice_number} for {amount} is due on {due_date}. "
                "Pay online: {portal_url}/billing"
            ),
        },
        {
            "code": "invoice_paid",
            "name": "Invoice Paid",
            "channel": NotificationChannel.email,
            "subject": "Invoice #{invoice_number} has been paid",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Invoice #{invoice_number} has been paid successfully. "
                "Thank you for your payment."
            ),
        },
        {
            "code": "invoice_overdue",
            "name": "Invoice Overdue",
            "channel": NotificationChannel.email,
            "subject": "Invoice #{invoice_number} is now due",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Our records show invoice #{invoice_number} for {amount} is now "
                "past its due date of {due_date}.\n\n"
                "If you have already paid, please disregard this message — thank "
                "you. Otherwise you can pay anytime from your account: "
                "{portal_url}/billing\n\n"
                "If you have any questions or would like to discuss payment "
                "options, please contact our support team."
            ),
        },
        {
            "code": "invoice_overdue",
            "name": "Invoice Overdue SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Invoice #{invoice_number} for {amount} is now past due. "
                "If you have already paid, please ignore this. "
                "Pay anytime: {portal_url}/billing"
            ),
        },
        {
            "code": "payment_received",
            "name": "Payment Received",
            "channel": NotificationChannel.email,
            "subject": "Payment receipt {receipt_number}",
            "body": (
                "Dear {subscriber_name},\n\n"
                "We have received your payment of {amount}. Thank you!\n\n"
                "Receipt: {receipt_number}\n"
                "View or download: {receipt_url}\n\n"
                "You can review how the payment was applied in your billing "
                "history.\n\n"
                "If you have questions about your billing, please contact support."
            ),
        },
        {
            "code": "payment_received",
            "name": "Payment Received SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "We received your payment of {amount}. Receipt {receipt_number}: "
                "{receipt_url}"
            ),
        },
        {
            "code": "prepaid_service_renewed",
            "name": "Prepaid Service Renewed",
            "channel": NotificationChannel.email,
            "subject": "Your {offer_name} service is renewed",
            "body": (
                "Dear {subscriber_name},\n\n"
                "We applied {amount} to your {offer_name} service. "
                "Your service is renewed through {renewed_through}.\n\n"
                "This renewal confirmation is separate from your payment receipt."
            ),
        },
        {
            "code": "payment_failed",
            "name": "Payment Failed",
            "channel": NotificationChannel.email,
            "subject": "Payment failed — please retry",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your recent payment attempt of {amount} was not successful. "
                "Please try again or use a different payment method.\n\n"
                "If you continue to experience issues, contact our support team.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "payment_refunded",
            "name": "Payment Refunded",
            "channel": NotificationChannel.email,
            "subject": "Payment refunded",
            "body": (
                "Dear {subscriber_name},\n\n"
                "A refund of {amount} has been processed on your account."
            ),
        },
        {
            "code": "payment_reversed",
            "name": "Payment Reversed",
            "channel": NotificationChannel.email,
            "subject": "Payment reversed",
            "body": (
                "Dear {subscriber_name},\n\n"
                "A settled payment of {amount} was reversed on your account. "
                "Please contact billing if you need more information."
            ),
        },
        {
            "code": "usage_warning",
            "name": "Usage Warning",
            "channel": NotificationChannel.email,
            "subject": "Data usage warning — {usage_percent}% used",
            "body": (
                "Dear {subscriber_name},\n\n"
                "You have used {usage_percent}% of your monthly data allowance "
                "on your {offer_name} plan.\n\n"
                "Consider upgrading your plan if you need more data.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "usage_exhausted",
            "name": "Usage Exhausted",
            "channel": NotificationChannel.email,
            "subject": "Data allowance exhausted",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your monthly data allowance on the {offer_name} plan has been exhausted. "
                "Your speed may be reduced until the next billing cycle.\n\n"
                "To restore full speed, consider upgrading your plan or purchasing a top-up.\n\n"
                "Thank you."
            ),
        },
        {
            "code": "provisioning_completed",
            "name": "Provisioning Completed",
            "channel": NotificationChannel.email,
            "subject": "Service installation complete",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Your service installation has been completed successfully. "
                "Your {offer_name} subscription is now ready to use.\n\n"
                "If you experience any issues, please contact our support team.\n\n"
                "Enjoy your service!"
            ),
        },
        {
            "code": "provisioning_completed",
            "name": "Provisioning Completed SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": (
                "Your {offer_name} installation is complete and the service is ready."
            ),
        },
        {
            "code": "provisioning_failed",
            "name": "Provisioning Failed",
            "channel": NotificationChannel.email,
            "subject": "Service installation issue",
            "body": (
                "Dear {subscriber_name},\n\n"
                "We encountered an issue while setting up your {offer_name} subscription. "
                "Our technical team has been notified and will follow up shortly.\n\n"
                "We apologize for the inconvenience."
            ),
        },
        {
            "code": "service_order_created",
            "name": "Service Order Created",
            "channel": NotificationChannel.email,
            "subject": "Your service order has been created",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Service order #{service_order_id} has been created for your account."
            ),
        },
        {
            "code": "service_order_created",
            "name": "Service Order Created SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": "Service order #{service_order_id} has been created for your account.",
        },
        {
            "code": "service_order_assigned",
            "name": "Service Order Assigned",
            "channel": NotificationChannel.email,
            "subject": "Your service order is in progress",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Service order #{service_order_id} is now being worked on by our team."
            ),
        },
        {
            "code": "service_order_assigned",
            "name": "Service Order Assigned SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": "Service order #{service_order_id} is now in progress.",
        },
        {
            "code": "service_order_completed",
            "name": "Service Order Completed",
            "channel": NotificationChannel.email,
            "subject": "Your service order is complete",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Service order #{service_order_id} has been completed successfully."
            ),
        },
        {
            "code": "service_order_completed",
            "name": "Service Order Completed SMS",
            "channel": NotificationChannel.sms,
            "subject": None,
            "body": "Service order #{service_order_id} has been completed successfully.",
        },
        {
            "code": "ont_offline",
            "name": "Connection Offline",
            "channel": NotificationChannel.email,
            "subject": "We've noticed an issue with your connection",
            "body": (
                "Dear {subscriber_name},\n\n"
                "We've detected that your service may currently be offline, and "
                "our team is looking into it.\n\n"
                "If your equipment has lost power, please check that it is "
                "switched on. If the issue continues, please contact our support "
                "team."
            ),
        },
        {
            "code": "ont_online",
            "name": "Connection Restored",
            "channel": NotificationChannel.email,
            "subject": "Your connection is back online",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Good news — your service is back online. Thank you for your "
                "patience.\n\n"
                "If you continue to experience any issues, please contact our "
                "support team."
            ),
        },
        {
            "code": "ont_signal_degraded",
            "name": "Connection Quality Check",
            "channel": NotificationChannel.email,
            "subject": "We're checking on your connection quality",
            "body": (
                "Dear {subscriber_name},\n\n"
                "Our monitoring suggests your connection quality may be reduced, "
                "and our team is looking into it. There is nothing you need to do "
                "right now.\n\n"
                "If you notice any problems, please contact our support team."
            ),
        },
        {
            "code": "ont_discovered",
            "name": "ONT Discovered (internal)",
            "channel": NotificationChannel.email,
            "subject": "New ONT discovered — {device_serial}",
            "body": (
                "A new ONT has been discovered on the network.\n\n"
                "Serial: {device_serial}\n"
                "OLT: {location}\n\n"
                "This device is awaiting assignment to a subscriber. (Internal "
                "notification — not customer-facing.)"
            ),
        },
    ]

    # Refer & Earn defaults are owned by the executable event-notification
    # policy. Seed from that canonical spec so editable database templates do
    # not maintain a second copy of the subject/body/channel decision.
    from app.services.events.handlers.notification import EVENT_NOTIFICATION_SPECS
    from app.services.events.types import EventType

    # Event specs no longer declare channels — channel selection is owned by
    # communications.channel_policy. Seed an editable template for each channel
    # the seeded billing-category policy can route this reward to, so a routed
    # channel is never missing its template. Operators add more via the UI.
    referral_reward_spec = EVENT_NOTIFICATION_SPECS[EventType.referral_reward_issued]
    # The seeded policy routes this reward to push; seed an email template too as
    # a usable spare if an operator adds email in the matrix.
    referral_template_channels = (
        NotificationChannel.push,
        NotificationChannel.email,
    )
    templates.extend(
        {
            "code": referral_reward_spec.template_code,
            "name": "Referral Reward Issued",
            "channel": channel,
            "subject": referral_reward_spec.subject,
            "body": referral_reward_spec.body,
        }
        for channel in referral_template_channels
    )

    created = 0
    for tmpl_data in templates:
        from sqlalchemy import select as sa_select

        existing = db.scalars(
            sa_select(NotificationTemplate).where(
                NotificationTemplate.code == tmpl_data["code"],
                NotificationTemplate.channel == tmpl_data["channel"],
            )
        ).first()
        if not existing:
            tmpl = NotificationTemplate(**tmpl_data)
            db.add(tmpl)
            created += 1
            logger.info("Seeded notification template: %s", tmpl_data["code"])

    db.flush()
    return created


def seed_notification_templates(db: Session) -> None:
    """Seed default notification templates for key ISP events.

    Uses upsert-by-code-and-channel: creates if missing, skips if already exists
    (admin may have customized the content).
    """
    _seed_missing_notification_templates(db)
    db.commit()


def seed_collections_settings(db: Session) -> None:
    collections_settings.ensure_by_key(
        db,
        key="dunning_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("DUNNING_INTERVAL_SECONDS", "86400"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_balance_sweep_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PREPAID_BALANCE_SWEEP_INTERVAL_SECONDS", "3600"),
    )
    collections_settings.ensure_by_key(
        db,
        key="suspension_notification_dedupe_hours",
        value_type=SettingValueType.integer,
        value_text=os.getenv("COLLECTIONS_SUSPENSION_NOTIFICATION_DEDUPE_HOURS", "24"),
    )
    collections_settings.ensure_by_key(
        db,
        key="enforcement_window_start",
        value_type=SettingValueType.string,
        value_text=os.getenv("ENFORCEMENT_WINDOW_START", ""),
    )
    collections_settings.ensure_by_key(
        db,
        key="enforcement_window_end",
        value_type=SettingValueType.string,
        value_text=os.getenv("ENFORCEMENT_WINDOW_END", ""),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_warning_subject",
        value_type=SettingValueType.string,
        value_text=os.getenv("PREPAID_WARNING_SUBJECT", "Low Balance Warning"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_warning_body",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "PREPAID_WARNING_BODY",
            "Your prepaid balance is below the minimum threshold ({threshold}). "
            "Current balance: {balance}. Please top up to avoid suspension.",
        ),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_deactivation_subject",
        value_type=SettingValueType.string,
        value_text=os.getenv("PREPAID_DEACTIVATION_SUBJECT", "Service Deactivated"),
    )
    collections_settings.ensure_by_key(
        db,
        key="prepaid_deactivation_body",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "PREPAID_DEACTIVATION_BODY",
            "Your prepaid balance has been exhausted and service has been deactivated. "
            "Please contact support to restore service.",
        ),
    )


def seed_geocoding_settings(db: Session) -> None:
    geocoding_settings.ensure_by_key(
        db,
        key="enabled",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )
    geocoding_settings.ensure_by_key(
        db,
        key="provider",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_PROVIDER", "nominatim"),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="base_url",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "GEOCODING_BASE_URL", "https://nominatim.openstreetmap.org"
        ),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="country_codes",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_COUNTRY_CODES", "ng"),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="user_agent",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_USER_AGENT", "dotmac_sm"),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="email",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_EMAIL", ""),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="timeout_sec",
        value_type=SettingValueType.integer,
        value_text=os.getenv("GEOCODING_TIMEOUT_SEC", "5"),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="min_interval_ms",
        value_type=SettingValueType.integer,
        value_text=os.getenv("GEOCODING_MIN_INTERVAL_MS", "1000"),
    )
    geocoding_settings.ensure_by_key(
        db,
        key="google_api_key",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_GOOGLE_API_KEY", ""),
        is_secret=True,
    )
    geocoding_settings.ensure_by_key(
        db,
        key="mapbox_api_key",
        value_type=SettingValueType.string,
        value_text=os.getenv("GEOCODING_MAPBOX_API_KEY", ""),
        is_secret=True,
    )
    geocoding_settings.ensure_by_key(
        db,
        key="batch_geocode_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )
    geocoding_settings.ensure_by_key(
        db,
        key="batch_geocode_log_rows",
        value_type=SettingValueType.json,
        value_json=[],
    )


def seed_scheduler_settings(db: Session) -> None:
    scheduler_settings.ensure_by_key(
        db,
        key="timezone",
        value_type=SettingValueType.string,
        value_text=os.getenv("CELERY_TIMEZONE", APP_TIMEZONE_NAME),
    )
    scheduler_settings.ensure_by_key(
        db,
        key="beat_max_loop_interval",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CELERY_BEAT_MAX_LOOP_INTERVAL", "5"),
    )
    scheduler_settings.ensure_by_key(
        db,
        key="beat_refresh_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CELERY_BEAT_REFRESH_SECONDS", "300"),
    )
    scheduler_settings.ensure_by_key(
        db,
        key="event_dispatch_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("EVENT_DISPATCH_INTERVAL_SECONDS", "60"),
    )
    scheduler_settings.ensure_by_key(
        db,
        key="event_dispatch_batch_size",
        value_type=SettingValueType.integer,
        value_text=os.getenv("EVENT_DISPATCH_BATCH_SIZE", "100"),
    )
    for key, env_name, default in [
        ("crm_ticket_pull_interval_minutes", "CRM_TICKET_PULL_INTERVAL_MINUTES", "5"),
        ("crm_cache_list_seconds", "CRM_CACHE_LIST_SECONDS", "60"),
        ("crm_cache_detail_seconds", "CRM_CACHE_DETAIL_SECONDS", "30"),
        ("crm_retry_max_attempts", "CRM_RETRY_MAX_ATTEMPTS", "2"),
        ("crm_retry_max_sleep_seconds", "CRM_RETRY_MAX_SLEEP_SECONDS", "8"),
        (
            "crm_reachability_circuit_seconds",
            "CRM_REACHABILITY_CIRCUIT_SECONDS",
            "30",
        ),
    ]:
        scheduler_settings.ensure_by_key(
            db,
            key=key,
            value_type=SettingValueType.integer,
            value_text=os.getenv(env_name, default),
        )


def seed_radius_settings(db: Session) -> None:
    radius_settings.ensure_by_key(
        db,
        key="auth_server_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_AUTH_SERVER_ID", ""),
    )
    radius_settings.ensure_by_key(
        db,
        key="auth_shared_secret",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_AUTH_SHARED_SECRET", ""),
        is_secret=True,
    )
    radius_settings.ensure_by_key(
        db,
        key="auth_dictionary_path",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_AUTH_DICTIONARY", "/etc/raddb/dictionary"),
    )
    radius_settings.ensure_by_key(
        db,
        key="auth_timeout_sec",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_AUTH_TIMEOUT_SEC", "3"),
    )
    radius_settings.ensure_by_key(
        db,
        key="coa_dictionary_path",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_COA_DICTIONARY", "/etc/raddb/dictionary"),
    )
    radius_settings.ensure_by_key(
        db,
        key="coa_timeout_sec",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_COA_TIMEOUT_SEC", "3"),
    )
    radius_settings.ensure_by_key(
        db,
        key="coa_retries",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_COA_RETRIES", "1"),
    )
    radius_settings.ensure_by_key(
        db,
        key="suspended_address_list",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_SUSPENDED_ADDRESS_LIST", "suspended"),
    )
    group_routing_raw = os.getenv("RADIUS_GROUP_ROUTING_ENABLED", "false")
    radius_settings.ensure_by_key(
        db,
        key="group_routing_enabled",
        value_type=SettingValueType.boolean,
        value_text=group_routing_raw,
        value_json=group_routing_raw.lower() in {"1", "true", "yes", "on"},
    )
    for key, env_name, default in (
        ("active_group_name", "RADIUS_ACTIVE_GROUP_NAME", "dotmac-active"),
        (
            "suspended_group_name",
            "RADIUS_SUSPENDED_GROUP_NAME",
            "dotmac-suspended",
        ),
        ("captive_group_name", "RADIUS_CAPTIVE_GROUP_NAME", "dotmac-captive"),
    ):
        radius_settings.ensure_by_key(
            db,
            key=key,
            value_type=SettingValueType.string,
            value_text=os.getenv(env_name, default),
        )
    radius_settings.ensure_by_key(
        db,
        key="access_group_priority",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_ACCESS_GROUP_PRIORITY", "0"),
    )
    for key, env_name, default in (
        (
            "enforcement_reconciler_max_kicks",
            "ENFORCEMENT_RECONCILER_MAX_KICKS",
            "25",
        ),
        (
            "enforcement_reconciler_unserviceable_grace_seconds",
            "ENFORCEMENT_RECONCILER_UNSERVICEABLE_GRACE_SECONDS",
            "1200",
        ),
        (
            "enforcement_reconciler_ghost_stale_seconds",
            "ENFORCEMENT_RECONCILER_GHOST_STALE_SECONDS",
            "7200",
        ),
    ):
        radius_settings.ensure_by_key(
            db,
            key=key,
            value_type=SettingValueType.integer,
            value_text=os.getenv(env_name, default),
        )
    refresh_raw = os.getenv("RADIUS_REFRESH_SESSIONS_ON_PROFILE_CHANGE", "true")
    radius_settings.ensure_by_key(
        db,
        key="refresh_sessions_on_profile_change",
        value_type=SettingValueType.boolean,
        value_text=refresh_raw,
        value_json=refresh_raw.lower() in {"1", "true", "yes", "on"},
    )
    # PPPoE auto-generation settings (disabled by default for dual-run)
    # pppoe_auto_generate_enabled removed — PPPoE generation is now mandatory
    radius_settings.ensure_by_key(
        db,
        key="pppoe_username_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("PPPOE_USERNAME_PREFIX", "1050"),
    )
    radius_settings.ensure_by_key(
        db,
        key="pppoe_username_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PPPOE_USERNAME_PADDING", "5"),
    )
    radius_settings.ensure_by_key(
        db,
        key="pppoe_username_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PPPOE_USERNAME_START", "1"),
    )
    radius_settings.ensure_by_key(
        db,
        key="pppoe_default_password_length",
        value_type=SettingValueType.integer,
        value_text=os.getenv("PPPOE_DEFAULT_PASSWORD_LENGTH", "12"),
    )
    # Captive portal redirect settings
    captive_enabled_raw = os.getenv("RADIUS_CAPTIVE_REDIRECT_ENABLED", "false")
    radius_settings.ensure_by_key(
        db,
        key="captive_redirect_enabled",
        value_type=SettingValueType.boolean,
        value_text=captive_enabled_raw,
        value_json=captive_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    radius_settings.ensure_by_key(
        db,
        key="captive_portal_ip",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_CAPTIVE_PORTAL_IP", ""),
    )
    radius_settings.ensure_by_key(
        db,
        key="captive_portal_url",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_CAPTIVE_PORTAL_URL", ""),
    )


def seed_billing_settings(db: Session) -> None:
    billing_settings.ensure_by_key(
        db,
        key="default_currency",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_CURRENCY", "NGN"),
    )
    billing_settings.ensure_by_key(
        db,
        key="prepaid_reconstruction_attestation_public_key_ref",
        value_type=SettingValueType.string,
        value_text=os.getenv("PREPAID_RECONSTRUCTION_ATTESTATION_PUBLIC_KEY_REF", ""),
        is_secret=True,
    )
    billing_settings.ensure_by_key(
        db,
        key="default_invoice_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_INVOICE_STATUS", "draft"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_tax_application",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_TAX_APPLICATION", "exclusive"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_payment_method_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_PAYMENT_METHOD_TYPE", "card"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_bank_account_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_BANK_ACCOUNT_TYPE", "checking"),
    )
    billing_settings.ensure_by_key(
        db,
        key="default_payment_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_DEFAULT_PAYMENT_STATUS", "pending"),
    )
    billing_settings.ensure_by_key(
        db,
        key="billing_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_INTERVAL_SECONDS", "86400"),
    )
    billing_settings.ensure_by_key(
        db,
        key="payment_due_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv(
            "BILLING_PAYMENT_DUE_DAYS",
            os.getenv("BILLING_INVOICE_DUE_DAYS", "14"),
        ),
    )
    billing_settings.ensure_by_key(
        db,
        key="autopay_max_consecutive_failures",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_AUTOPAY_MAX_CONSECUTIVE_FAILURES", "3"),
    )
    billing_settings.ensure_by_key(
        db,
        key="expiry_reminder_days",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_EXPIRY_REMINDER_DAYS", "7"),
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_reminder_days",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_INVOICE_REMINDER_DAYS", "7,1"),
    )
    billing_settings.ensure_by_key(
        db,
        key="minimum_balance",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_MINIMUM_BALANCE", "0"),
    )
    billing_settings.ensure_by_key(
        db,
        key="refund_policy",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_REFUND_POLICY", "none"),
    )
    billing_settings.ensure_by_key(
        db,
        key="upgrade_fee",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_UPGRADE_FEE", "0.00"),
    )
    billing_settings.ensure_by_key(
        db,
        key="downgrade_fee",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_DOWNGRADE_FEE", "0.00"),
    )
    billing_settings.ensure_by_key(
        db,
        key="fee_tax_rate",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_FEE_TAX_RATE", "0.00"),
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_timing",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_INVOICE_TIMING", "immediate"),
    )
    prepaid_rollover_raw = os.getenv("PLAN_CHANGE_PREPAID_ROLLOVER", "false")
    billing_settings.ensure_by_key(
        db,
        key="prepaid_rollover",
        value_type=SettingValueType.boolean,
        value_text=prepaid_rollover_raw,
        value_json=prepaid_rollover_raw.lower() in {"1", "true", "yes", "on"},
    )
    discount_transfer_raw = os.getenv("PLAN_CHANGE_DISCOUNT_TRANSFER", "false")
    billing_settings.ensure_by_key(
        db,
        key="discount_transfer",
        value_type=SettingValueType.boolean,
        value_text=discount_transfer_raw,
        value_json=discount_transfer_raw.lower() in {"1", "true", "yes", "on"},
    )
    billing_settings.ensure_by_key(
        db,
        key="minimum_invoice_amount",
        value_type=SettingValueType.string,
        value_text=os.getenv("PLAN_CHANGE_MINIMUM_INVOICE_AMOUNT", "0.00"),
    )
    billing_settings.ensure_by_key(
        db,
        key="arrangement_min_installments",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_ARRANGEMENT_MIN_INSTALLMENTS", "2"),
    )
    billing_settings.ensure_by_key(
        db,
        key="arrangement_max_installments",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_ARRANGEMENT_MAX_INSTALLMENTS", "24"),
    )
    billing_settings.ensure_by_key(
        db,
        key="arrangement_default_overdue_installments",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_ARRANGEMENT_DEFAULT_OVERDUE_INSTALLMENTS", "2"),
    )
    billing_settings.ensure_by_key(
        db,
        key="service_extension_max_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_SERVICE_EXTENSION_MAX_DAYS", "30"),
    )
    billing_settings.ensure_by_key(
        db,
        key="subscription_billing_treatment_max_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_SUBSCRIPTION_TREATMENT_MAX_DAYS", "366"),
    )
    billing_settings.ensure_by_key(
        db,
        key="topup_preset_amounts",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "BILLING_TOPUP_PRESET_AMOUNTS", "1000,2000,5000,10000,20000,50000"
        ),
    )
    for reconciliation_key in (
        "topup_reconciliation_stale_minutes",
        "topup_reconciliation_max_age_days",
        "topup_reconciliation_expiry_grace_hours",
        "topup_reconciliation_batch_size",
    ):
        reconciliation_spec = get_spec(SettingDomain.billing, reconciliation_key)
        if reconciliation_spec is None or reconciliation_spec.env_var is None:
            raise RuntimeError(
                f"Top-up reconciliation spec is missing: {reconciliation_key}"
            )
        billing_settings.ensure_by_key(
            db,
            key=reconciliation_spec.key,
            value_type=reconciliation_spec.value_type,
            value_text=os.getenv(
                reconciliation_spec.env_var,
                str(reconciliation_spec.default),
            ),
        )
    gateway_intent_ttl_spec = get_spec(
        SettingDomain.billing,
        "gateway_topup_intent_ttl_minutes",
    )
    if gateway_intent_ttl_spec is None or gateway_intent_ttl_spec.env_var is None:
        raise RuntimeError("Gateway top-up intent TTL spec is missing")
    billing_settings.ensure_by_key(
        db,
        key=gateway_intent_ttl_spec.key,
        value_type=gateway_intent_ttl_spec.value_type,
        value_text=os.getenv(
            gateway_intent_ttl_spec.env_var,
            str(gateway_intent_ttl_spec.default),
        ),
    )
    billing_settings.ensure_by_key(
        db,
        key="ar_aging_bucket_days",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_AR_AGING_BUCKET_DAYS", "30,60,90"),
    )
    billing_settings.ensure_by_key(
        db,
        key="billing_health_scan_min_ratio",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_HEALTH_SCAN_MIN_RATIO", "0.5"),
    )
    billing_settings.ensure_by_key(
        db,
        key="billing_health_payment_volume_min_ratio",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_HEALTH_PAYMENT_VOLUME_MIN_RATIO", "0.4"),
    )
    billing_settings.ensure_by_key(
        db,
        key="billing_health_payment_baseline_min_daily",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_HEALTH_PAYMENT_BASELINE_MIN_DAILY", "5.0"),
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_number_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_INVOICE_NUMBER_PREFIX", "INV-"),
    )
    billing_settings.ensure_by_key(
        db,
        key="invoice_number_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_INVOICE_NUMBER_PADDING", "6"),
    )

    billing_settings.ensure_by_key(
        db,
        key="invoice_number_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_INVOICE_NUMBER_START", "1"),
    )
    billing_settings.ensure_by_key(
        db,
        key="credit_note_number_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("BILLING_CREDIT_NOTE_NUMBER_PREFIX", "CR-"),
    )
    billing_settings.ensure_by_key(
        db,
        key="credit_note_number_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_CREDIT_NOTE_NUMBER_PADDING", "6"),
    )
    billing_settings.ensure_by_key(
        db,
        key="credit_note_number_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("BILLING_CREDIT_NOTE_NUMBER_START", "1"),
    )


def seed_catalog_settings(db: Session) -> None:
    catalog_settings.ensure_by_key(
        db,
        key="default_proration_policy",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_PRORATION_POLICY", "immediate"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_downgrade_policy",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_DOWNGRADE_POLICY", "next_cycle"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_suspension_action",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_SUSPENSION_ACTION", "suspend"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_refund_policy",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_REFUND_POLICY", "none"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_billing_cycle",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_BILLING_CYCLE", "monthly"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_contract_term",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_CONTRACT_TERM", "month_to_month"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_offer_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_OFFER_STATUS", "active"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_subscription_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_SUBSCRIPTION_STATUS", "pending"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_billing_mode",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_BILLING_MODE", "prepaid"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="billing_mode_help_text",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "CATALOG_BILLING_MODE_HELP_TEXT", "Overrides tariff default."
        ),
    )
    catalog_settings.ensure_by_key(
        db,
        key="billing_mode_prepaid_notice",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "CATALOG_BILLING_MODE_PREPAID_NOTICE", "Balance enforcement applies."
        ),
    )
    catalog_settings.ensure_by_key(
        db,
        key="billing_mode_postpaid_notice",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "CATALOG_BILLING_MODE_POSTPAID_NOTICE",
            "This subscription follows dunning steps.",
        ),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_price_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_PRICE_TYPE", "recurring"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_addon_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_ADDON_TYPE", "custom"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_addon_quantity",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CATALOG_DEFAULT_ADDON_QUANTITY", "1"),
    )
    catalog_settings.ensure_by_key(
        db,
        key="default_nas_vendor",
        value_type=SettingValueType.string,
        value_text=os.getenv("CATALOG_DEFAULT_NAS_VENDOR", "other"),
    )


def seed_subscriber_settings(db: Session) -> None:
    subscriber_settings.ensure_by_key(
        db,
        key="default_address_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("SUBSCRIBER_DEFAULT_ADDRESS_TYPE", "service"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="default_country_code",
        value_type=SettingValueType.string,
        value_text=os.getenv("DEFAULT_COUNTRY_CODE", "234"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="identity_sensitive_automation_min_confidence",
        value_type=SettingValueType.string,
        value_text=os.getenv("IDENTITY_SENSITIVE_AUTOMATION_MIN_CONFIDENCE", "MEDIUM"),
    )
    subscriber_number_enabled_raw = os.getenv("SUBSCRIBER_NUMBER_ENABLED", "true")
    subscriber_settings.ensure_by_key(
        db,
        key="subscriber_number_enabled",
        value_type=SettingValueType.boolean,
        value_text=subscriber_number_enabled_raw,
        value_json=subscriber_number_enabled_raw.lower() in {"1", "true", "yes", "on"},
    )
    subscriber_settings.ensure_by_key(
        db,
        key="subscriber_number_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("SUBSCRIBER_NUMBER_PREFIX", "SUB-"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="subscriber_number_padding",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SUBSCRIBER_NUMBER_PADDING", "6"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="subscriber_number_start",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SUBSCRIBER_NUMBER_START", "1"),
    )
    subscriber_settings.ensure_by_key(
        db,
        key="account_number_prefix",
        value_type=SettingValueType.string,
        value_text=os.getenv("SUBSCRIBER_ACCOUNT_NUMBER_PREFIX", "ACC-"),
    )


def seed_usage_policy_settings(db: Session) -> None:
    usage_settings.ensure_by_key(
        db,
        key="default_charge_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_DEFAULT_CHARGE_STATUS", "staged"),
    )
    usage_settings.ensure_by_key(
        db,
        key="default_rating_run_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("USAGE_DEFAULT_RATING_RUN_STATUS", "running"),
    )


def seed_collections_policy_settings(db: Session) -> None:
    collections_settings.ensure_by_key(
        db,
        key="default_dunning_case_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("COLLECTIONS_DEFAULT_DUNNING_CASE_STATUS", "open"),
    )
    for key, env_name, default in [
        (
            "billing_enforcement_require_active_gateway",
            "BILLING_ENFORCEMENT_REQUIRE_ACTIVE_GATEWAY",
            "false",
        ),
    ]:
        raw = os.getenv(env_name, default)
        collections_settings.ensure_by_key(
            db,
            key=key,
            value_type=SettingValueType.boolean,
            value_text=raw,
            value_json=raw.lower() in {"1", "true", "yes", "on"},
        )
    for key, env_name, default in [
        (
            "billing_enforcement_min_enforcing_day_offset",
            "BILLING_ENFORCEMENT_MIN_ENFORCING_DAY_OFFSET",
            "3",
        ),
        (
            "billing_enforcement_notification_max_oldest_queued_minutes",
            "BILLING_ENFORCEMENT_NOTIFICATION_MAX_OLDEST_QUEUED_MINUTES",
            "120",
        ),
        (
            "billing_enforcement_notification_max_failed",
            "BILLING_ENFORCEMENT_NOTIFICATION_MAX_FAILED",
            "100",
        ),
        (
            "billing_enforcement_notification_max_stuck_sending",
            "BILLING_ENFORCEMENT_NOTIFICATION_MAX_STUCK_SENDING",
            "25",
        ),
        (
            "billing_enforcement_notification_failed_window_hours",
            "BILLING_ENFORCEMENT_NOTIFICATION_FAILED_WINDOW_HOURS",
            "24",
        ),
        (
            "billing_enforcement_payment_health_window_hours",
            "BILLING_ENFORCEMENT_PAYMENT_HEALTH_WINDOW_HOURS",
            "24",
        ),
        (
            "billing_enforcement_payment_max_pending_minutes",
            "BILLING_ENFORCEMENT_PAYMENT_MAX_PENDING_MINUTES",
            "45",
        ),
        (
            "billing_enforcement_payment_max_dead_letters",
            "BILLING_ENFORCEMENT_PAYMENT_MAX_DEAD_LETTERS",
            "0",
        ),
        (
            "billing_enforcement_payment_max_stale_pending_topups",
            "BILLING_ENFORCEMENT_PAYMENT_MAX_STALE_PENDING_TOPUPS",
            "20",
        ),
        (
            "billing_enforcement_payment_min_recent_successes",
            "BILLING_ENFORCEMENT_PAYMENT_MIN_RECENT_SUCCESSES",
            "0",
        ),
    ]:
        collections_settings.ensure_by_key(
            db,
            key=key,
            value_type=SettingValueType.integer,
            value_text=os.getenv(env_name, default),
        )


def seed_auth_policy_settings(db: Session) -> None:
    auth_settings.ensure_by_key(
        db,
        key="default_auth_provider",
        value_type=SettingValueType.string,
        value_text=os.getenv("AUTH_DEFAULT_AUTH_PROVIDER", "local"),
    )
    auth_settings.ensure_by_key(
        db,
        key="default_session_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("AUTH_DEFAULT_SESSION_STATUS", "active"),
    )


def seed_provisioning_settings(db: Session) -> None:
    provisioning_settings.ensure_by_key(
        db,
        key="default_service_order_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_SERVICE_ORDER_STATUS", "draft"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="service_migration_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )
    provisioning_settings.ensure_by_key(
        db,
        key="bulk_activation_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )


def seed_provisioning_workflows(db: Session) -> None:
    """Seed default provisioning workflows for fiber subscriber onboarding.

    Creates a "Fiber PPPoE Full Provisioning" workflow with the standard
    5-step automated sequence. Skips if a workflow with this name already exists.
    """
    from app.models.provisioning import (
        ProvisioningStep,
        ProvisioningStepType,
        ProvisioningVendor,
        ProvisioningWorkflow,
    )

    workflow_name = "Fiber PPPoE Full Provisioning"
    from sqlalchemy import select as sa_select

    existing = db.scalars(
        sa_select(ProvisioningWorkflow).where(
            ProvisioningWorkflow.name == workflow_name
        )
    ).first()
    if existing:
        return

    workflow = ProvisioningWorkflow(
        name=workflow_name,
        vendor=ProvisioningVendor.huawei,
        description=(
            "End-to-end automated provisioning for Huawei FTTH with MikroTik NAS. "
            "Creates OLT service-port, NAS VLAN/IP/PPPoE, pushes WAN config and "
            "PPPoE credentials to ONT via TR-069, then verifies subscriber comes online."
        ),
        is_active=True,
    )
    db.add(workflow)
    db.flush()

    steps = [
        {
            "name": "Create OLT Service Port",
            "step_type": ProvisioningStepType.create_olt_service_port,
            "order_index": 10,
            "config": {
                "description": "Map ONT GEM port to service VLAN on OLT",
            },
        },
        {
            "name": "Ensure NAS VLAN",
            "step_type": ProvisioningStepType.ensure_nas_vlan,
            "order_index": 20,
            "config": {
                "parent_interface": "ether3",
                "pppoe_default_profile": "default",
                "description": "Create VLAN interface, IP, and PPPoE server on MikroTik NAS",
            },
        },
        {
            "name": "Confirm Subscriber Online",
            "step_type": ProvisioningStepType.confirm_up,
            "order_index": 30,
            "config": {
                "description": "Verify subscriber has connected and is online via RADIUS",
            },
        },
    ]

    for step_data in steps:
        step = ProvisioningStep(
            workflow_id=workflow.id,
            **step_data,
        )
        db.add(step)

    db.commit()
    logger.info(
        "Seeded provisioning workflow: %s (%d steps)", workflow_name, len(steps)
    )


def seed_projects_settings(db: Session) -> None:
    """Seed minimal projects settings required by services/tests."""
    projects_settings = DomainSettings(SettingDomain.projects)
    projects_settings.ensure_by_key(
        db,
        key="default_project_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROJECTS_DEFAULT_PROJECT_STATUS", "active"),
    )
    projects_settings.ensure_by_key(
        db,
        key="default_project_priority",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROJECTS_DEFAULT_PROJECT_PRIORITY", "normal"),
    )
    projects_settings.ensure_by_key(
        db,
        key="default_labor_hourly_rate",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROJECTS_DEFAULT_LABOR_HOURLY_RATE", "0.00"),
    )
    projects_settings.ensure_by_key(
        db,
        key="default_sales_project_type",
        value_type=SettingValueType.string,
        value_text=os.getenv(
            "PROJECTS_DEFAULT_SALES_PROJECT_TYPE", "fiber_optics_installation"
        ),
    )


def seed_inventory_settings(db: Session) -> None:
    """Seed provisioning defaults historically grouped with inventory."""
    provisioning_settings.ensure_by_key(
        db,
        key="default_appointment_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_APPOINTMENT_STATUS", "proposed"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="default_task_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_TASK_STATUS", "pending"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="default_vendor",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_VENDOR", "other"),
    )
    provisioning_settings.ensure_by_key(
        db,
        key="default_workflow_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("PROVISIONING_DEFAULT_WORKFLOW_ID", ""),
    )


def seed_tr069_settings(db: Session) -> None:
    tr069_settings.ensure_by_key(
        db,
        key="default_acs_server_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("TR069_DEFAULT_ACS_SERVER_ID", ""),
    )


def seed_network_policy_settings(db: Session) -> None:
    from app.services.domain_settings import network_settings

    network_settings.ensure_by_key(
        db,
        key="default_device_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_DEVICE_TYPE", "other"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_device_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_DEVICE_STATUS", "active"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_port_type",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_PORT_TYPE", "ethernet"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_port_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_PORT_STATUS", "down"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_ip_version",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_IP_VERSION", "ipv4"),
    )
    network_settings.ensure_by_key(
        db,
        key="default_fiber_strand_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_FIBER_STRAND_STATUS", "available"),
    )
    # Fiber installation planning cost rates
    network_settings.ensure_by_key(
        db,
        key="fiber_drop_cable_cost_per_meter",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_FIBER_DROP_CABLE_COST_PER_METER", "2.50"),
    )
    network_settings.ensure_by_key(
        db,
        key="fiber_labor_cost_per_meter",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_FIBER_LABOR_COST_PER_METER", "1.50"),
    )
    network_settings.ensure_by_key(
        db,
        key="fiber_ont_device_cost",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_FIBER_ONT_DEVICE_COST", "85.00"),
    )
    network_settings.ensure_by_key(
        db,
        key="fiber_installation_base_fee",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_FIBER_INSTALLATION_BASE_FEE", "50.00"),
    )


def seed_network_settings(db: Session) -> None:
    network_settings.ensure_by_key(
        db,
        key="default_mikrotik_address_list",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_DEFAULT_MIKROTIK_ADDRESS_LIST", ""),
    )
    network_settings.ensure_by_key(
        db,
        key="internet_service_vlans",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_INTERNET_SERVICE_VLANS", "203"),
    )
    network_settings.ensure_by_key(
        db,
        key="speedtest_sla_ratio",
        value_type=SettingValueType.string,
        value_text=os.getenv("NETWORK_SPEEDTEST_SLA_RATIO", "0.8"),
    )
    network_settings.ensure_by_key(
        db,
        key="vpn_control_jobs_log",
        value_type=SettingValueType.json,
        value_json=[],
    )


def seed_radius_policy_settings(db: Session) -> None:
    radius_settings.ensure_by_key(
        db,
        key="default_auth_port",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_DEFAULT_AUTH_PORT", "1812"),
    )


def seed_network_monitoring_settings(db: Session) -> None:
    network_monitoring_settings.ensure_by_key(
        db,
        key="channel_health_contracts",
        value_type=SettingValueType.json,
        value_json=json.loads(json.dumps(DEFAULT_CHANNEL_HEALTH_CONTRACTS)),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_disk_warn_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SERVER_HEALTH_DISK_WARN_PCT", "80"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_disk_crit_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SERVER_HEALTH_DISK_CRIT_PCT", "90"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_mem_warn_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SERVER_HEALTH_MEM_WARN_PCT", "80"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_mem_crit_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("SERVER_HEALTH_MEM_CRIT_PCT", "90"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_load_warn",
        value_type=SettingValueType.string,
        value_text=os.getenv("SERVER_HEALTH_LOAD_WARN", "1.0"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="server_health_load_crit",
        value_type=SettingValueType.string,
        value_text=os.getenv("SERVER_HEALTH_LOAD_CRIT", "1.5"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="network_health_warn_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("NETWORK_HEALTH_WARN_PCT", "90"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="network_health_crit_pct",
        value_type=SettingValueType.integer,
        value_text=os.getenv("NETWORK_HEALTH_CRIT_PCT", "70"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="celery_long_running_task_minutes",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CELERY_LONG_RUNNING_TASK_MINUTES", "30"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="celery_reserved_backlog_threshold",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CELERY_RESERVED_BACKLOG_THRESHOLD", "100"),
    )
    network_monitoring_settings.ensure_by_key(
        db,
        key="celery_queue_backlog_threshold",
        value_type=SettingValueType.integer,
        value_text=os.getenv("CELERY_QUEUE_BACKLOG_THRESHOLD", "500"),
    )
    radius_settings.ensure_by_key(
        db,
        key="default_acct_port",
        value_type=SettingValueType.integer,
        value_text=os.getenv("RADIUS_DEFAULT_ACCT_PORT", "1813"),
    )
    default_sync_users = os.getenv("RADIUS_DEFAULT_SYNC_USERS", "true")
    radius_settings.ensure_by_key(
        db,
        key="default_sync_users",
        value_type=SettingValueType.boolean,
        value_text=default_sync_users,
        value_json=default_sync_users.lower() in {"1", "true", "yes", "on"},
    )
    default_sync_clients = os.getenv("RADIUS_DEFAULT_SYNC_NAS_CLIENTS", "true")
    radius_settings.ensure_by_key(
        db,
        key="default_sync_nas_clients",
        value_type=SettingValueType.boolean,
        value_text=default_sync_clients,
        value_json=default_sync_clients.lower() in {"1", "true", "yes", "on"},
    )
    radius_settings.ensure_by_key(
        db,
        key="default_sync_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("RADIUS_DEFAULT_SYNC_STATUS", "running"),
    )


def seed_comms_settings(db: Session) -> None:
    campaign_processing_enabled = os.getenv("CAMPAIGN_PROCESSING_ENABLED", "false")
    comms_settings.ensure_by_key(
        db,
        key="campaign_processing_enabled",
        value_type=SettingValueType.boolean,
        value_text=campaign_processing_enabled,
        value_json=campaign_processing_enabled.strip().lower()
        in {"1", "true", "yes", "on"},
    )
    comms_settings.ensure_by_key(
        db,
        key="default_notification_status",
        value_type=SettingValueType.string,
        value_text=os.getenv("COMMS_DEFAULT_NOTIFICATION_STATUS", "pending"),
    )
    # Meta (Facebook/Instagram) Integration Settings
    comms_settings.ensure_by_key(
        db,
        key="meta_app_id",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_APP_ID", ""),
    )
    comms_settings.ensure_by_key(
        db,
        key="meta_app_secret",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_APP_SECRET", ""),
        is_secret=True,
    )
    comms_settings.ensure_by_key(
        db,
        key="meta_graph_api_version",
        value_type=SettingValueType.string,
        value_text=os.getenv("META_GRAPH_API_VERSION", "v19.0"),
    )


def seed_wireguard_settings(db: Session) -> None:
    """Seed WireGuard VPN settings from environment variables."""
    # Encryption key for storing WireGuard private keys at rest
    wg_key = os.getenv("WIREGUARD_KEY_ENCRYPTION_KEY")
    if wg_key and is_openbao_ref(wg_key):
        network_settings.ensure_by_key(
            db,
            key="wireguard_key_encryption_key",
            value_type=SettingValueType.string,
            value_text=wg_key,
            is_secret=True,
        )
    # Log retention
    network_settings.ensure_by_key(
        db,
        key="wireguard_log_retention_days",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_LOG_RETENTION_DAYS", "90"),
    )
    # Log cleanup cadence; enablement is canonical ``vpn.log_cleanup``.
    network_settings.ensure_by_key(
        db,
        key="wireguard_log_cleanup_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_LOG_CLEANUP_INTERVAL_SECONDS", "86400"),
    )
    # Token cleanup cadence; enablement is canonical ``vpn.token_cleanup``.
    network_settings.ensure_by_key(
        db,
        key="wireguard_token_cleanup_interval_seconds",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_TOKEN_CLEANUP_INTERVAL_SECONDS", "3600"),
    )
    # VPN Server Defaults
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_listen_port",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_DEFAULT_LISTEN_PORT", "51820"),
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_vpn_address",
        value_type=SettingValueType.string,
        value_text=os.getenv("WIREGUARD_DEFAULT_VPN_ADDRESS", "10.10.0.1/24"),
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_vpn_address_v6",
        value_type=SettingValueType.string,
        value_text=os.getenv("WIREGUARD_DEFAULT_VPN_ADDRESS_V6", ""),
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_mtu",
        value_type=SettingValueType.integer,
        value_text=os.getenv("WIREGUARD_DEFAULT_MTU", "1420"),
    )
    network_settings.ensure_by_key(
        db,
        key="wireguard_default_interface_name",
        value_type=SettingValueType.string,
        value_text=os.getenv("WIREGUARD_DEFAULT_INTERFACE_NAME", "wg0"),
    )
