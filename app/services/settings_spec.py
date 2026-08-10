import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from dotmac_kernel.settings_models import SettingDomain as KernelSettingDomain
from dotmac_kernel.settings_resolver import resolve_many as kernel_resolve_many
from dotmac_kernel.settings_resolver import resolve_value as kernel_resolve_value

from app.models.domain_settings import SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import domain_settings as settings_service
from app.services.brand_theme import (
    DEFAULT_HEX,
    DEFAULT_SECONDARY_HEX,
    DEFAULT_SEMANTIC_COLORS,
)
from app.services.channel_health_contracts import (
    DEFAULT_CHANNEL_HEALTH_CONTRACTS,
)
from app.services.operator_tenant import operator_tenant_id
from app.services.response import ListResponseMixin
from app.services.settings_specs.integration import build_integration_specs
from app.services.settings_specs.provisioning import build_provisioning_specs
from app.timezone import APP_TIMEZONE_NAME

if TYPE_CHECKING:
    from app.models.domain_settings import DomainSetting


logger = logging.getLogger(__name__)


class SettingStringNormalization(str, Enum):
    """Canonical normalization applied to a declared string setting."""

    NONE = "none"
    LOWERCASE = "lowercase"


def _coerce_int_value(value: object) -> int | None:
    # Domain settings values are stored as text/json and flow through `object` types here.
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class SettingSpec(ListResponseMixin):
    """Schema for a database-authoritative operational setting.

    ``env_var`` names an optional bootstrap or migration input consumed by the
    settings seed/sync paths. Runtime resolvers never consult it as an override.
    """

    domain: SettingDomain
    key: str
    env_var: str | None
    value_type: SettingValueType
    default: object | None
    label: str | None = None
    required: bool = False
    allowed: set[str] | None = None
    min_value: int | None = None
    max_value: int | None = None
    is_secret: bool = False
    string_normalization: SettingStringNormalization = SettingStringNormalization.NONE


SCHEDULER_BOOLEAN_SETTING_KEYS = frozenset(
    {
        (SettingDomain.auth, "credential_rotation_enabled"),
        (SettingDomain.audit, "cross_app_drift_detection_enabled"),
        (SettingDomain.billing, "cutover_balance_audit_enabled"),
        (SettingDomain.billing, "funded_inactive_exposure_audit_enabled"),
        (SettingDomain.billing, "stale_overdue_lock_detect_enabled"),
        (SettingDomain.catalog, "nas_backup_retention_enabled"),
        (SettingDomain.comms, "oauth_token_refresh_enabled"),
        (SettingDomain.network_monitoring, "infra_availability_prune_enabled"),
        (SettingDomain.network_monitoring, "infra_availability_snapshot_enabled"),
        (SettingDomain.notification, "ncc_report_email_enabled"),
        (SettingDomain.notification, "operational_escalation_delivery_enabled"),
        (SettingDomain.radius, "connectivity_shadow_audit_enabled"),
        (SettingDomain.radius, "ip_consistency_audit_enabled"),
        (SettingDomain.radius, "radius_sync_enabled"),
        (SettingDomain.radius, "suspension_audit_enabled"),
        (SettingDomain.scheduler, "event_old_cleanup_enabled"),
        (SettingDomain.scheduler, "stale_infrastructure_check_enabled"),
        (SettingDomain.usage, "bandwidth_processing_enabled"),
        (SettingDomain.usage, "usage_rating_enabled"),
    }
)

SCHEDULER_ENV_BOOTSTRAP_SETTING_KEYS = frozenset(
    {
        (SettingDomain.billing, "autopay_interval_seconds"),
        (SettingDomain.billing, "billing_interval_seconds"),
        (SettingDomain.billing, "cutover_balance_audit_interval_seconds"),
        (SettingDomain.billing, "funded_inactive_exposure_audit_interval_seconds"),
        (SettingDomain.billing, "overdue_check_interval_seconds"),
        (SettingDomain.billing, "topup_reconciliation_interval_seconds"),
        (SettingDomain.catalog, "scheduled_plan_change_interval_seconds"),
        (SettingDomain.catalog, "scheduled_status_change_interval_seconds"),
        (SettingDomain.catalog, "subscription_expiration_interval_seconds"),
        (SettingDomain.catalog, "vacation_hold_resume_interval_seconds"),
        (SettingDomain.collections, "arrangement_check_interval_seconds"),
        (SettingDomain.collections, "billing_notifications_interval_seconds"),
        (SettingDomain.collections, "dunning_interval_seconds"),
        (SettingDomain.comms, "campaign_processing_interval_seconds"),
        (SettingDomain.gis, "sync_interval_minutes"),
        (
            SettingDomain.integration,
            "dotmac_erp_expense_status_refresh_interval_seconds",
        ),
        (
            SettingDomain.integration,
            "dotmac_erp_material_status_refresh_interval_seconds",
        ),
        (SettingDomain.integration, "dotmac_erp_outbox_interval_seconds"),
        (
            SettingDomain.integration,
            "dotmac_erp_purchase_invoice_status_refresh_interval_seconds",
        ),
        (SettingDomain.network, "olt_profile_sync_interval_seconds"),
        (SettingDomain.network_monitoring, "channel_health_interval_seconds"),
        (
            SettingDomain.network_monitoring,
            "device_projection_reconcile_interval_seconds",
        ),
        (
            SettingDomain.network_monitoring,
            "infra_availability_prune_interval_seconds",
        ),
        (
            SettingDomain.network_monitoring,
            "infra_availability_snapshot_interval_seconds",
        ),
        (
            SettingDomain.network_monitoring,
            "monitoring_coverage_interval_seconds",
        ),
        (SettingDomain.notification, "notification_queue_interval_seconds"),
        (
            SettingDomain.notification,
            "operational_escalation_delivery_interval_seconds",
        ),
        (SettingDomain.provisioning, "compensation_retry_interval_seconds"),
        (SettingDomain.radius, "connectivity_shadow_audit_interval_seconds"),
        (SettingDomain.radius, "ip_consistency_audit_interval_seconds"),
        (SettingDomain.radius, "suspension_audit_interval_seconds"),
        (SettingDomain.scheduler, "acs_task_soft_time_limit_seconds"),
        (SettingDomain.scheduler, "acs_task_time_limit_seconds"),
        (SettingDomain.scheduler, "beat_max_loop_interval"),
        (SettingDomain.scheduler, "beat_refresh_seconds"),
        (SettingDomain.scheduler, "crm_ticket_pull_interval_minutes"),
        (SettingDomain.scheduler, "long_task_soft_time_limit_seconds"),
        (SettingDomain.scheduler, "long_task_time_limit_seconds"),
        (SettingDomain.scheduler, "result_expires_seconds"),
        (SettingDomain.scheduler, "task_soft_time_limit_seconds"),
        (SettingDomain.scheduler, "task_time_limit_seconds"),
        (SettingDomain.scheduler, "timezone"),
        (SettingDomain.scheduler, "worker_prefetch_multiplier"),
        (SettingDomain.usage, "device_login_sync_interval_seconds"),
        (SettingDomain.usage, "enforcement_reconciler_interval_seconds"),
        (SettingDomain.usage, "fup_evaluation_interval_seconds"),
        (SettingDomain.usage, "radius_accounting_import_interval_seconds"),
        (
            SettingDomain.usage,
            "radius_active_session_reconcile_interval_seconds",
        ),
        (SettingDomain.usage, "radius_session_reap_interval_seconds"),
        (SettingDomain.usage, "usage_metering_interval_seconds"),
        (SettingDomain.usage, "usage_rating_interval_seconds"),
    }
)


#: 6 concatenated GSM-7 segments x 153 characters — the practical ceiling of a
#: multipart SMS, and the one bound on these settings that is PHYSICAL rather
#: than policy. It is hardcoded on purpose: it is not deployment-specific, and a
#: knob for it would exist only to let someone configure an impossibility.
MAX_CONCATENATED_SMS_CHARS = 918

SETTINGS_SPECS: list[SettingSpec] = [
    SettingSpec(
        domain=SettingDomain.auth,
        key="credential_rotation_enabled",
        env_var="CREDENTIAL_ROTATION_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Scheduled credential key rotation",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="credential_rotation_auto_apply",
        env_var="CREDENTIAL_ROTATION_AUTO_APPLY",
        value_type=SettingValueType.boolean,
        default=True,
        label="Automatically apply due credential rotations",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="credential_rotation_interval_days",
        env_var="CREDENTIAL_ROTATION_INTERVAL_DAYS",
        value_type=SettingValueType.integer,
        default=90,
        min_value=7,
        max_value=365,
        label="Credential rotation interval (days)",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="credential_rotation_grace_days",
        env_var="CREDENTIAL_ROTATION_GRACE_DAYS",
        value_type=SettingValueType.integer,
        default=7,
        min_value=1,
        max_value=30,
        label="Previous credential key grace period (days)",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_algorithm",
        label=None,
        env_var="JWT_ALGORITHM",
        value_type=SettingValueType.string,
        default="HS256",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_access_ttl_minutes",
        env_var="JWT_ACCESS_TTL_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="jwt_refresh_ttl_days",
        env_var="JWT_REFRESH_TTL_DAYS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_name",
        env_var="REFRESH_COOKIE_NAME",
        value_type=SettingValueType.string,
        default="refresh_token",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_secure",
        env_var="REFRESH_COOKIE_SECURE",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_samesite",
        env_var="REFRESH_COOKIE_SAMESITE",
        value_type=SettingValueType.string,
        default="lax",
        allowed={"lax", "strict", "none"},
        string_normalization=SettingStringNormalization.LOWERCASE,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_domain",
        env_var="REFRESH_COOKIE_DOMAIN",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="refresh_cookie_path",
        env_var="REFRESH_COOKIE_PATH",
        value_type=SettingValueType.string,
        default="/auth",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="totp_issuer",
        env_var="TOTP_ISSUER",
        value_type=SettingValueType.string,
        default="dotmac_sm",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="force_2fa",
        env_var="FORCE_2FA",
        value_type=SettingValueType.boolean,
        default=False,
        label="Force 2FA for All Admins",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="selfcare_domain",
        env_var="SELFCARE_DOMAIN",
        value_type=SettingValueType.string,
        default="",
        label="Selfcare Domain",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="selfcare_redirect_root",
        env_var="SELFCARE_REDIRECT_ROOT",
        value_type=SettingValueType.string,
        default="/portal/",
        allowed={"/portal/", "/portal/auth/login"},
        label="Selfcare Root Redirect",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="admin_domain",
        env_var="ADMIN_DOMAIN",
        value_type=SettingValueType.string,
        default="",
        label="Admin Domain",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="reseller_domain",
        env_var="RESELLER_DOMAIN",
        value_type=SettingValueType.string,
        default="",
        label="Reseller Domain",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="api_key_rate_window_seconds",
        env_var="API_KEY_RATE_WINDOW_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="api_key_rate_max",
        env_var="API_KEY_RATE_MAX",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="api_key_max_ttl_days",
        env_var="API_KEY_MAX_TTL_DAYS",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        label="API Key Max Lifetime (days)",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="api_key_max_per_owner",
        env_var="API_KEY_MAX_PER_OWNER",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        label="API Keys Max Per Owner",
    ),
    SettingSpec(
        domain=SettingDomain.imports,
        key="max_file_bytes",
        env_var="IMPORT_MAX_FILE_BYTES",
        value_type=SettingValueType.integer,
        default=5 * 1024 * 1024,
        min_value=1024,
    ),
    SettingSpec(
        domain=SettingDomain.imports,
        key="max_rows",
        env_var="IMPORT_MAX_ROWS",
        value_type=SettingValueType.integer,
        default=5000,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.imports,
        key="import_history_log",
        env_var=None,
        value_type=SettingValueType.list,
        default=[],
    ),
    SettingSpec(
        domain=SettingDomain.imports,
        key="import_rollback_window_hours",
        env_var="IMPORT_ROLLBACK_WINDOW_HOURS",
        value_type=SettingValueType.integer,
        default=24,
        min_value=1,
        max_value=168,
    ),
    SettingSpec(
        domain=SettingDomain.imports,
        key="import_background_threshold_rows",
        env_var="IMPORT_BACKGROUND_THRESHOLD_ROWS",
        value_type=SettingValueType.integer,
        default=1000,
        min_value=100,
        max_value=100000,
    ),
    SettingSpec(
        domain=SettingDomain.imports,
        key="import_jobs_log",
        env_var=None,
        value_type=SettingValueType.list,
        default=[],
    ),
    # ── Provisioning behaviour (OLT/ONT) ────────────────────────────────────
    #
    # Registered here rather than in `app/services/network/provisioning_settings.py`,
    # which held them as a frozen `ProvisioningDefaults` dataclass beside its own
    # `DomainSetting` query and its own cache — a parallel settings subsystem with
    # a second source of defaults, reading without a tenant filter and caching
    # under an unscoped key.
    #
    # Defaults are copied EXACTLY from that dataclass, and no `min_value` or
    # `max_value` is added. The module enforced no bounds, so inventing one here
    # would silently degrade any deployment already storing a value outside it —
    # the failure mode this repository has already paid for once. Bounds are a
    # separate, evidenced change.
    #
    # `env_var` is None throughout: these were never environment-seeded, and
    # giving them one would make the seed write rows that do not exist today.
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="tr069_bootstrap_timeout_sec",
        env_var=None,
        value_type=SettingValueType.integer,
        default=120,
        label="TR-069 bootstrap timeout (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="tr069_bootstrap_poll_interval_sec",
        env_var=None,
        value_type=SettingValueType.integer,
        default=10,
        label="TR-069 bootstrap poll interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="tr069_task_ready_timeout_sec",
        env_var=None,
        value_type=SettingValueType.integer,
        default=45,
        label="TR-069 task-ready timeout (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="tr069_task_ready_poll_interval_sec",
        env_var=None,
        value_type=SettingValueType.integer,
        default=5,
        label="TR-069 task-ready poll interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="pppoe_push_max_attempts",
        env_var=None,
        value_type=SettingValueType.integer,
        default=3,
        label="PPPoE push attempts before giving up",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="pppoe_push_retry_delay_sec",
        env_var=None,
        value_type=SettingValueType.integer,
        default=10,
        label="PPPoE push retry delay (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="stale_runtime_hours",
        env_var=None,
        value_type=SettingValueType.integer,
        default=24,
        label="Hours after which provisioning runtime state is stale",
    ),
    # `olt_write_mode_enabled` is NOT declared here. It already exists in
    # `app/services/settings_specs/provisioning.py`, with an `env_var` this one
    # would have dropped. I missed it because that package builds specs through
    # a `setting_spec` CALLABLE rather than a literal `SettingSpec(...)`, so the
    # sweep that told me "none of these sixteen is registered" was reading one
    # file instead of the spec system. Fifteen here, one there.
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="pppoe_provisioning_method",
        env_var=None,
        value_type=SettingValueType.string,
        default="auto",
        # `get_pppoe_provisioning_method` already normalises and falls back to
        # "auto" for anything else, so declaring the set changes no outcome —
        # it moves the knowledge to where the admin screen can render it.
        allowed={"auto", "omci", "tr069"},
        label="PPPoE provisioning method (auto | omci | tr069)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="verification_interval_sec",
        env_var=None,
        value_type=SettingValueType.integer,
        default=300,
        label="Async provisioning verification interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="verification_staleness_minutes",
        env_var=None,
        value_type=SettingValueType.integer,
        default=15,
        label="Minutes before a verification result is stale",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="drift_handling_mode",
        env_var=None,
        value_type=SettingValueType.string,
        default="alert_only",
        allowed={"alert_only", "auto_repair"},
        label="Provisioning drift handling (alert_only | auto_repair)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="circuit_breaker_failure_threshold",
        env_var=None,
        value_type=SettingValueType.integer,
        default=3,
        label="Provisioning circuit-breaker failure threshold",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="circuit_breaker_backoff_sec",
        env_var=None,
        value_type=SettingValueType.integer,
        default=30,
        label="Provisioning circuit-breaker backoff (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="service_port_pool_min_index",
        env_var=None,
        value_type=SettingValueType.integer,
        default=0,
        label="Service-port pool minimum index",
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="service_port_pool_max_index",
        env_var=None,
        value_type=SettingValueType.integer,
        default=65535,
        label="Service-port pool maximum index",
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_enabled",
        env_var="GIS_SYNC_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_interval_minutes",
        env_var="GIS_SYNC_INTERVAL_MINUTES",
        value_type=SettingValueType.integer,
        default=60,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_pop_sites",
        env_var="GIS_SYNC_POP_SITES",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_addresses",
        env_var="GIS_SYNC_ADDRESSES",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="sync_deactivate_missing",
        env_var="GIS_SYNC_DEACTIVATE_MISSING",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    # --- Customer pin auto-approval + address geocode-on-save ---
    SettingSpec(
        domain=SettingDomain.gis,
        key="location_auto_approve_enabled",
        label="Auto-approve customer pin corrections",
        env_var="GIS_LOCATION_AUTO_APPROVE_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="location_auto_approve_shadow",
        label="Shadow mode (evaluate but never auto-approve)",
        env_var="GIS_LOCATION_AUTO_APPROVE_SHADOW",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="location_auto_approve_radius_m",
        label="Max auto-approve move distance (metres)",
        env_var="GIS_LOCATION_AUTO_APPROVE_RADIUS_M",
        value_type=SettingValueType.integer,
        default=100,
        min_value=10,
        max_value=5000,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="location_auto_require_coverage",
        label="Require pin inside a coverage area to auto-approve",
        env_var="GIS_LOCATION_AUTO_REQUIRE_COVERAGE",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="location_auto_approve_window_days",
        label="Auto-approve rate-limit window (days)",
        env_var="GIS_LOCATION_AUTO_APPROVE_WINDOW_DAYS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=0,
        max_value=365,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="location_auto_approve_max_per_window",
        label="Max auto-approvals per window",
        env_var="GIS_LOCATION_AUTO_APPROVE_MAX_PER_WINDOW",
        value_type=SettingValueType.integer,
        default=1,
        min_value=0,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="location_geocode_retry_days",
        label="Geocode-on-save retry back-off (days)",
        env_var="GIS_LOCATION_GEOCODE_RETRY_DAYS",
        value_type=SettingValueType.integer,
        default=7,
        min_value=0,
        max_value=365,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="map_customer_limit",
        env_var="GIS_MAP_CUSTOMER_LIMIT",
        value_type=SettingValueType.integer,
        default=2000,
        min_value=100,
        max_value=50000,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="map_nearest_search_max_km",
        env_var="GIS_MAP_NEAREST_SEARCH_MAX_KM",
        value_type=SettingValueType.integer,
        default=50,
        min_value=1,
        max_value=1000,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="map_snap_max_m",
        env_var="GIS_MAP_SNAP_MAX_M",
        value_type=SettingValueType.integer,
        default=250,
        min_value=10,
        max_value=5000,
    ),
    SettingSpec(
        domain=SettingDomain.gis,
        key="map_allow_straightline_fallback",
        env_var="GIS_MAP_ALLOW_STRAIGHTLINE_FALLBACK",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_max_queue_age_hours",
        env_var="NOTIFICATION_MAX_QUEUE_AGE_HOURS",
        value_type=SettingValueType.integer,
        default=72,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_enabled",
        env_var="ALERT_NOTIFICATIONS_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    # The SMS channel's operational controls. They were read through
    # `sms._get_setting`, which consulted the environment ABOVE the stored row
    # and carried a per-call-site default — so `sms_enabled` defaulted to `true`
    # in `notification_adapter` and `false` in the three other readers, and
    # `is_available()` advertised a channel the send path refused. The
    # credentials (`sms_api_key`, `sms_api_secret`) are NOT here: a credential is
    # not a setting, and they are held in `app.config` (ADR-0009).
    SettingSpec(
        domain=SettingDomain.notification,
        key="sms_enabled",
        env_var="SMS_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="SMS channel enabled",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="sms_provider",
        env_var="SMS_PROVIDER",
        value_type=SettingValueType.string,
        default="",
        # "" is the deliberate unconfigured state: `send_sms` refuses and names
        # the three real providers rather than defaulting to `webhook`, which
        # with no webhook URL is a guaranteed failure dressed as a live channel.
        allowed={"", "twilio", "africastalking", "webhook"},
        label="SMS provider",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="sms_from_number",
        env_var="SMS_FROM_NUMBER",
        value_type=SettingValueType.string,
        default="",
        label="SMS sender number",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="sms_username",
        env_var="SMS_USERNAME",
        value_type=SettingValueType.string,
        default="",
        # Africa's Talking account identifier. Paired with the API key but not
        # itself secret, so it stays a setting under the split.
        label="SMS provider account username",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="sms_webhook_url",
        env_var="SMS_WEBHOOK_URL",
        value_type=SettingValueType.string,
        default="",
        label="SMS webhook URL",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="sms_api_timeout_seconds",
        env_var="SMS_API_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
        # No ceiling. An earlier version carried `max_value=60`, recovered from
        # a duplicate declaration and justified as "above a minute it stops
        # being a timeout" — which is a rationale, not a physical limit. A slow
        # gateway on a bad link can legitimately want ninety seconds, so the
        # number was an operator's judgement wearing validation's clothes.
        #
        # An out-of-range stored value does not clamp; it DEGRADES to the
        # default. So a policy ceiling here would silently reset a deliberately
        # tuned timeout, in the SMS path, with no signal. The duplicate's
        # `min_value=5` is not adopted for the same reason.
        label="SMS provider request timeout (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="sms_max_length",
        env_var="SMS_MAX_LENGTH",
        value_type=SettingValueType.integer,
        default=160,
        # `0` is load-bearing, not a lower bound nobody thought about:
        # `sms.py` reads `if max_length > 0 and len(body) > max_length`, so 0
        # is how truncation is DISABLED. The duplicate declaration this spec
        # absorbed had `min_value=1`, which would have made the documented
        # disable value unstorable — the concrete harm in letting a dead
        # second declaration look authoritative.
        min_value=0,
        # A named constant, not a bare literal: the number is a property of the
        # transport, and written as `918` it reads like somebody's preference.
        max_value=MAX_CONCATENATED_SMS_CHARS,
        label="SMS body truncation length (0 disables truncation)",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_channel",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_CHANNEL",
        value_type=SettingValueType.string,
        default="email",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_recipient",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_RECIPIENT",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_template_id",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_TEMPLATE_ID",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_rotation_id",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_ROTATION_ID",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="alert_notifications_default_delay_minutes",
        env_var="ALERT_NOTIFICATIONS_DEFAULT_DELAY_MINUTES",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_host",
        env_var="SMTP_HOST",
        value_type=SettingValueType.string,
        default="localhost",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_port",
        env_var="SMTP_PORT",
        value_type=SettingValueType.integer,
        default=587,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_username",
        env_var="SMTP_USERNAME",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_password",
        env_var="SMTP_PASSWORD",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_from_email",
        env_var="SMTP_FROM_EMAIL",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_from_name",
        env_var="SMTP_FROM_NAME",
        value_type=SettingValueType.string,
        default="DotMac SM",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_use_tls",
        env_var="SMTP_USE_TLS",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_use_ssl",
        env_var="SMTP_USE_SSL",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_queue_interval_seconds",
        env_var="NOTIFICATION_QUEUE_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_max_retries",
        env_var="NOTIFICATION_MAX_RETRIES",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
        max_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_sending_timeout_minutes",
        env_var="NOTIFICATION_SENDING_TIMEOUT_MINUTES",
        value_type=SettingValueType.integer,
        default=10,
        min_value=2,
        max_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_retry_backoff_minutes",
        env_var="NOTIFICATION_RETRY_BACKOFF_MINUTES",
        value_type=SettingValueType.string,
        default="1,5,15",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_per_channel_rate_limit",
        env_var="NOTIFICATION_PER_CHANNEL_RATE_LIMIT",
        value_type=SettingValueType.integer,
        default=50,
        min_value=1,
        max_value=1000,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_quiet_hours_start",
        env_var="NOTIFICATION_QUIET_HOURS_START",
        value_type=SettingValueType.string,
        default="22:00",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_quiet_hours_end",
        env_var="NOTIFICATION_QUIET_HOURS_END",
        value_type=SettingValueType.string,
        default="07:00",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_dedupe_window_minutes",
        env_var="NOTIFICATION_DEDUPE_WINDOW_MINUTES",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="notification_channel_policy",
        env_var=None,
        value_type=SettingValueType.json,
        # Channel selection is owned here (communications.channel_policy), not in
        # the event specs. This seeded default ships sensible multi-channel
        # routing per category so a fresh/unconfigured deploy is not email-only;
        # an operator edit in the admin matrix writes a DB row that overrides it.
        # SMS is intentionally absent — it is retired and disabled; a future SMS
        # plugin adds it back here per category. email is the always-available
        # base; whatsapp is the approved, delivering channel.
        # EVENT-level seed only — deliberately no global default and no category
        # defaults. Both of those tiers outrank a caller's own default_channels,
        # and several services pass deliberate per-call channel sets under
        # common categories: FUP enforcement (category="fup", per-kind push),
        # customer_experience_communications and the support resolution
        # confirmation (category="service", whatsapp+push). A category seed
        # silently hijacked those and dropped push.
        #
        # Event entries are keyed by template_code, so they can only affect the
        # specific event-spec notifications they name and can never intercept
        # another caller. These reproduce the exact channels those five
        # push-native specs declared before channels moved out of the specs, so
        # behaviour is unchanged. Every other spec event resolves to the email
        # system fallback until an operator widens it in the admin matrix —
        # which is where adding WhatsApp broadly belongs, as a deliberate
        # decision rather than a blanket default. SMS appears nowhere: it is
        # retired and disabled.
        default={
            "events": {
                "referral_reward_issued": ["push"],
                "usage_warning": ["push", "email"],
                "usage_exhausted": ["push", "email"],
                "service_extended": ["push", "email"],
                "addon_expiring": ["push", "email"],
            },
        },
        label="Notification channel policy: {default: [], categories: {}, events: {}}",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_rating_enabled",
        env_var="USAGE_RATING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_rating_interval_seconds",
        env_var="USAGE_RATING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_metering_interval_seconds",
        env_var="USAGE_METERING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="fup_evaluation_interval_seconds",
        env_var="FUP_EVALUATION_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="radius_accounting_import_enabled",
        env_var="RADIUS_ACCOUNTING_IMPORT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="radius_accounting_import_interval_seconds",
        env_var="RADIUS_ACCOUNTING_IMPORT_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="radius_accounting_source_stale_seconds",
        env_var="RADIUS_ACCOUNTING_SOURCE_STALE_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="radius_session_reap_enabled",
        env_var="RADIUS_SESSION_REAP_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="radius_session_reap_interval_seconds",
        env_var="RADIUS_SESSION_REAP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="radius_session_reap_stale_seconds",
        env_var="RADIUS_SESSION_REAP_STALE_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="radius_active_session_reconcile_interval_seconds",
        env_var="RADIUS_ACTIVE_SESSION_RECONCILE_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=120,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="dunning_interval_seconds",
        env_var="DUNNING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_balance_sweep_interval_seconds",
        env_var="PREPAID_BALANCE_SWEEP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
        max_value=3600,
    ),
    # Wall-clock budget for one sweep run (repair pass + account loop). It
    # must stay under the Celery soft time limit so the run always ends by
    # publishing its snapshot; accounts that do not fit are deferred to the
    # next run and counted as budget_deferred.
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_balance_sweep_budget_seconds",
        env_var="PREPAID_BALANCE_SWEEP_BUDGET_SECONDS",
        value_type=SettingValueType.integer,
        default=720,
        min_value=60,
        label="Prepaid sweep run budget (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_notif_send_hour",
        env_var="BILLING_NOTIF_SEND_HOUR",
        value_type=SettingValueType.integer,
        default=8,
        min_value=0,
        max_value=23,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_notifications_interval_seconds",
        env_var="BILLING_NOTIFICATIONS_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="default_prepaid_policy_set_id",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
        label="Default Prepaid Collections Policy Set",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="default_postpaid_policy_set_id",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
        label="Default Postpaid Collections Policy Set",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_min_enforcing_day_offset",
        env_var="BILLING_ENFORCEMENT_MIN_ENFORCING_DAY_OFFSET",
        value_type=SettingValueType.integer,
        default=3,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="suspension_notification_dedupe_hours",
        env_var="COLLECTIONS_SUSPENSION_NOTIFICATION_DEDUPE_HOURS",
        value_type=SettingValueType.integer,
        default=24,
        min_value=1,
        max_value=168,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_notification_max_oldest_queued_minutes",
        env_var="BILLING_ENFORCEMENT_NOTIFICATION_MAX_OLDEST_QUEUED_MINUTES",
        value_type=SettingValueType.integer,
        default=120,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_notification_max_failed",
        env_var="BILLING_ENFORCEMENT_NOTIFICATION_MAX_FAILED",
        value_type=SettingValueType.integer,
        default=100,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_notification_max_stuck_sending",
        env_var="BILLING_ENFORCEMENT_NOTIFICATION_MAX_STUCK_SENDING",
        value_type=SettingValueType.integer,
        default=25,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_notification_failed_window_hours",
        env_var="BILLING_ENFORCEMENT_NOTIFICATION_FAILED_WINDOW_HOURS",
        value_type=SettingValueType.integer,
        default=24,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_payment_health_window_hours",
        env_var="BILLING_ENFORCEMENT_PAYMENT_HEALTH_WINDOW_HOURS",
        value_type=SettingValueType.integer,
        default=24,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_payment_max_pending_minutes",
        env_var="BILLING_ENFORCEMENT_PAYMENT_MAX_PENDING_MINUTES",
        value_type=SettingValueType.integer,
        default=45,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_payment_max_dead_letters",
        env_var="BILLING_ENFORCEMENT_PAYMENT_MAX_DEAD_LETTERS",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_payment_max_stale_pending_topups",
        env_var="BILLING_ENFORCEMENT_PAYMENT_MAX_STALE_PENDING_TOPUPS",
        value_type=SettingValueType.integer,
        default=20,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_payment_min_recent_successes",
        env_var="BILLING_ENFORCEMENT_PAYMENT_MIN_RECENT_SUCCESSES",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="billing_enforcement_require_active_gateway",
        env_var="BILLING_ENFORCEMENT_REQUIRE_ACTIVE_GATEWAY",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    # Enforcement (suspend/block) time-of-day window, applied every day.
    SettingSpec(
        domain=SettingDomain.collections,
        key="enforcement_window_start",
        env_var="ENFORCEMENT_WINDOW_START",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="enforcement_window_end",
        env_var="ENFORCEMENT_WINDOW_END",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_warning_subject",
        env_var="PREPAID_WARNING_SUBJECT",
        value_type=SettingValueType.string,
        default="Low Balance Warning",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_warning_body",
        env_var="PREPAID_WARNING_BODY",
        value_type=SettingValueType.string,
        default="Your prepaid balance is below the minimum threshold ({threshold}). "
        "Current balance: {balance}. Please top up to avoid suspension.",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_deactivation_subject",
        env_var="PREPAID_DEACTIVATION_SUBJECT",
        value_type=SettingValueType.string,
        default="Service Deactivated",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="prepaid_deactivation_body",
        env_var="PREPAID_DEACTIVATION_BODY",
        value_type=SettingValueType.string,
        default="Your prepaid balance has been exhausted and service has been deactivated. "
        "Please contact support to restore service.",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="plan_families",
        env_var="CATALOG_PLAN_FAMILIES",
        value_type=SettingValueType.string,
        default="unlimited,dedicated,home_flex",
        label="Plan Families (comma-separated; changes stay within a family)",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="new_offer_visible_by_default",
        env_var="CATALOG_NEW_OFFER_VISIBLE_BY_DEFAULT",
        value_type=SettingValueType.boolean,
        default=False,
        label=(
            "Pre-tick customer-portal visibility and service availability on "
            "the new-offer form (off = a new offer must be published "
            "deliberately, not by forgetting to untick a box)"
        ),
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="reseller_default_catalog_open",
        env_var="RESELLER_DEFAULT_CATALOG_OPEN",
        value_type=SettingValueType.boolean,
        default=True,
        label=(
            "Resellers see all unrestricted offers by default (on = today's "
            "open catalog; off = a reseller sees only offers explicitly "
            "assigned to it, unless the reseller's own override says otherwise)"
        ),
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_billing_mode",
        env_var="CATALOG_DEFAULT_BILLING_MODE",
        value_type=SettingValueType.string,
        default="prepaid",
        allowed={"prepaid", "postpaid"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="billing_mode_help_text",
        env_var="CATALOG_BILLING_MODE_HELP_TEXT",
        value_type=SettingValueType.string,
        default="Overrides tariff default.",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="billing_mode_prepaid_notice",
        env_var="CATALOG_BILLING_MODE_PREPAID_NOTICE",
        value_type=SettingValueType.string,
        default="Balance enforcement applies.",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="billing_mode_postpaid_notice",
        env_var="CATALOG_BILLING_MODE_POSTPAID_NOTICE",
        value_type=SettingValueType.string,
        default="This subscription follows dunning steps.",
    ),
    # Customer self-service
    SettingSpec(
        domain=SettingDomain.catalog,
        key="max_suspend_days",
        env_var="CATALOG_MAX_SUSPEND_DAYS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
        max_value=90,
        label="Maximum Vacation Hold Duration (days)",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="max_suspend_holds_per_year",
        env_var="CATALOG_MAX_SUSPEND_HOLDS_PER_YEAR",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        max_value=12,
        label="Max Vacation Holds Per Year (0 = unlimited)",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="suspend_cooldown_days",
        env_var="CATALOG_SUSPEND_COOLDOWN_DAYS",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        max_value=90,
        label="Cooldown Between Vacation Holds (days, 0 = no cooldown)",
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="enabled",
        env_var="GEOCODING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="provider",
        env_var="GEOCODING_PROVIDER",
        value_type=SettingValueType.string,
        default="nominatim",
        allowed={"nominatim", "google", "mapbox"},
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="base_url",
        env_var="GEOCODING_BASE_URL",
        value_type=SettingValueType.string,
        default="https://nominatim.openstreetmap.org",
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="country_codes",
        env_var="GEOCODING_COUNTRY_CODES",
        value_type=SettingValueType.string,
        default="ng",
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="user_agent",
        env_var="GEOCODING_USER_AGENT",
        value_type=SettingValueType.string,
        default="dotmac_sm",
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="email",
        env_var="GEOCODING_EMAIL",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="timeout_sec",
        env_var="GEOCODING_TIMEOUT_SEC",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="min_interval_ms",
        env_var="GEOCODING_MIN_INTERVAL_MS",
        value_type=SettingValueType.integer,
        default=1000,
        min_value=0,
        label="Minimum interval between external geocoding requests (ms)",
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="google_api_key",
        env_var="GEOCODING_GOOGLE_API_KEY",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.geocoding,
        key="mapbox_api_key",
        env_var="GEOCODING_MAPBOX_API_KEY",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="timezone",
        env_var="CELERY_TIMEZONE",
        value_type=SettingValueType.string,
        default=APP_TIMEZONE_NAME,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="beat_max_loop_interval",
        env_var="CELERY_BEAT_MAX_LOOP_INTERVAL",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="beat_refresh_seconds",
        env_var="CELERY_BEAT_REFRESH_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=5,
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="event_dispatch_interval_seconds",
        env_var="EVENT_DISPATCH_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=10,
        max_value=300,
        label="Durable event outbox dispatch interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.workflow,
        key="cx_acceptance_attention_hours",
        env_var="CX_ACCEPTANCE_ATTENTION_HOURS",
        value_type=SettingValueType.integer,
        default=72,
        min_value=1,
        max_value=720,
        label="CX handoff acceptance attention deadline (hours)",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="durable_timer_dispatch_interval_seconds",
        env_var="DURABLE_TIMER_DISPATCH_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=30,
        max_value=600,
        label="Durable timer due-trigger dispatch interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="event_dispatch_batch_size",
        env_var="EVENT_DISPATCH_BATCH_SIZE",
        value_type=SettingValueType.integer,
        default=100,
        min_value=1,
        max_value=1000,
        label="Durable event outbox dispatch batch size",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="crm_ticket_pull_enabled",
        env_var="CRM_TICKET_PULL_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="CRM Ticket Pull Enabled",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="crm_ticket_pull_interval_minutes",
        env_var="CRM_TICKET_PULL_INTERVAL_MINUTES",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
        label="CRM Ticket Pull Interval (minutes)",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="crm_cache_list_seconds",
        env_var="CRM_CACHE_LIST_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=0,
        max_value=3600,
        label="CRM List Cache TTL (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="crm_cache_detail_seconds",
        env_var="CRM_CACHE_DETAIL_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=0,
        max_value=3600,
        label="CRM Detail Cache TTL (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="crm_retry_max_attempts",
        env_var="CRM_RETRY_MAX_ATTEMPTS",
        value_type=SettingValueType.integer,
        default=2,
        min_value=0,
        max_value=10,
        label="CRM Retry Max Attempts",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="crm_retry_max_sleep_seconds",
        env_var="CRM_RETRY_MAX_SLEEP_SECONDS",
        value_type=SettingValueType.integer,
        default=8,
        min_value=0,
        max_value=60,
        label="CRM Retry Max Sleep (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="crm_reachability_circuit_seconds",
        env_var="CRM_REACHABILITY_CIRCUIT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
        max_value=300,
        label="CRM Reachability Circuit Cooldown (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="auth_server_id",
        env_var="RADIUS_AUTH_SERVER_ID",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="generated_secret_length",
        env_var="RADIUS_GENERATED_SECRET_LENGTH",
        value_type=SettingValueType.integer,
        default=32,
        label="Auto-generated RADIUS shared-secret length",
        min_value=16,
        max_value=64,
    ),
    # `radius/auth_shared_secret` was RETIRED here: it stopped being a setting.
    #
    # Per the secret classification of 2026-08-09 it is one of the five Dotmac
    # issues and rotates, and it is now held from OpenBao at boot
    # (`app/services/kernel_secret_source.py`) and read by
    # `radius_auth.authenticate` from there. The row never held the secret
    # anyway — being `is_secret=True`, every write stored a `bao://` REFERENCE,
    # which that call site passed to the RADIUS client verbatim.
    #
    # Retired rather than left declared, because a spec with no reader is a
    # control an operator can set that changes nothing —
    # `tests/architecture/test_no_orphan_settings.py` names that "dead control"
    # and refuses it. `RADIUS_AUTH_SHARED_SECRET` remains the bootstrap input
    # for the OpenBao entry.
    SettingSpec(
        domain=SettingDomain.radius,
        key="auth_dictionary_path",
        env_var="RADIUS_AUTH_DICTIONARY",
        value_type=SettingValueType.string,
        default="/etc/raddb/dictionary",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="auth_timeout_sec",
        env_var="RADIUS_AUTH_TIMEOUT_SEC",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="coa_enabled",
        env_var="RADIUS_COA_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable RADIUS CoA Disconnects",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="coa_dictionary_path",
        env_var="RADIUS_COA_DICTIONARY",
        value_type=SettingValueType.string,
        default="/etc/raddb/dictionary",
        label="CoA Dictionary Path",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="coa_timeout_sec",
        env_var="RADIUS_COA_TIMEOUT_SEC",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
        label="CoA Timeout (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="coa_retries",
        env_var="RADIUS_COA_RETRIES",
        value_type=SettingValueType.integer,
        default=1,
        min_value=0,
        label="CoA Retries",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="suspension_audit_enabled",
        env_var="RADIUS_SUSPENSION_AUDIT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Suspension Enforcement Audit",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="suspension_audit_interval_seconds",
        env_var="RADIUS_SUSPENSION_AUDIT_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=21600,
        min_value=900,
        label="Suspension Audit Interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="suspended_address_list",
        env_var="RADIUS_SUSPENDED_ADDRESS_LIST",
        value_type=SettingValueType.string,
        default="suspended",
        label="Suspended Subscriber Address List",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="group_routing_enabled",
        env_var="RADIUS_GROUP_ROUTING_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Project Subscriber Access-State Groups",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="simultaneous_use_enforcement_enabled",
        env_var="RADIUS_SIMULTANEOUS_USE_ENFORCEMENT_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Enforce Per-Login RADIUS Session Limit",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="active_group_name",
        env_var="RADIUS_ACTIVE_GROUP_NAME",
        value_type=SettingValueType.string,
        default="dotmac-active",
        label="Active Subscriber RADIUS Group",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="suspended_group_name",
        env_var="RADIUS_SUSPENDED_GROUP_NAME",
        value_type=SettingValueType.string,
        default="dotmac-suspended",
        label="Suspended Subscriber RADIUS Group",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="captive_group_name",
        env_var="RADIUS_CAPTIVE_GROUP_NAME",
        value_type=SettingValueType.string,
        default="dotmac-captive",
        label="Captive Subscriber RADIUS Group",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="access_group_priority",
        env_var="RADIUS_ACCESS_GROUP_PRIORITY",
        value_type=SettingValueType.integer,
        default=0,
        label="Subscriber Access-State Group Priority",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="enforcement_reconciler_max_kicks",
        env_var="ENFORCEMENT_RECONCILER_MAX_KICKS",
        value_type=SettingValueType.integer,
        default=25,
        min_value=0,
        label="Enforcement Reconciler Maximum Disconnects Per Run",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="enforcement_reconciler_unserviceable_grace_seconds",
        env_var="ENFORCEMENT_RECONCILER_UNSERVICEABLE_GRACE_SECONDS",
        value_type=SettingValueType.integer,
        default=1200,
        min_value=60,
        label="Unserviceable Session Grace (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="enforcement_reconciler_ghost_stale_seconds",
        env_var="ENFORCEMENT_RECONCILER_GHOST_STALE_SECONDS",
        value_type=SettingValueType.integer,
        default=7200,
        min_value=300,
        label="Ghost Session Stale Threshold (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="refresh_sessions_on_profile_change",
        env_var="RADIUS_REFRESH_SESSIONS_ON_PROFILE_CHANGE",
        value_type=SettingValueType.boolean,
        default=True,
        label="Refresh Sessions on Profile Change",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="captive_redirect_enabled",
        env_var="RADIUS_CAPTIVE_REDIRECT_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Enable Captive Redirect for Eligible Residential Opt-ins",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="captive_portal_ip",
        env_var="RADIUS_CAPTIVE_PORTAL_IP",
        value_type=SettingValueType.string,
        default=None,
        label="Captive Portal IP/CIDR",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="captive_portal_url",
        env_var="RADIUS_CAPTIVE_PORTAL_URL",
        value_type=SettingValueType.string,
        default=None,
        label="Captive Portal URL",
    ),
    # PPPoE auto-generation settings (credentials are always generated on activation)
    SettingSpec(
        domain=SettingDomain.radius,
        key="pppoe_username_prefix",
        env_var="PPPOE_USERNAME_PREFIX",
        value_type=SettingValueType.string,
        default="1050",
        label="PPPoE Username Prefix",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="pppoe_username_padding",
        env_var="PPPOE_USERNAME_PADDING",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
        max_value=10,
        label="PPPoE Username Zero-Pad Width",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="pppoe_username_start",
        env_var="PPPOE_USERNAME_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
        label="PPPoE Username Sequence Start Value",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="pppoe_default_password_length",
        env_var="PPPOE_DEFAULT_PASSWORD_LENGTH",
        value_type=SettingValueType.integer,
        default=12,
        min_value=8,
        max_value=64,
        label="PPPoE Auto-Generated Password Length",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_currency",
        env_var="BILLING_DEFAULT_CURRENCY",
        value_type=SettingValueType.string,
        default="NGN",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_invoice_status",
        env_var="BILLING_DEFAULT_INVOICE_STATUS",
        value_type=SettingValueType.string,
        default="draft",
        allowed={"draft", "issued", "partially_paid", "paid", "void", "overdue"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_tax_application",
        env_var="BILLING_DEFAULT_TAX_APPLICATION",
        value_type=SettingValueType.string,
        default="exclusive",
        allowed={"exclusive", "inclusive", "exempt"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_tax_rate_id",
        env_var="BILLING_DEFAULT_TAX_RATE_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Default VAT rate (TaxRate id) when no address/subscriber rate set",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="withholding_tax_rate_percent",
        env_var="BILLING_WITHHOLDING_TAX_RATE_PERCENT",
        value_type=SettingValueType.string,
        default="5.00",
        label="Withholding tax rate percent for eligible bank-transfer invoice payments",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_payment_method_type",
        env_var="BILLING_DEFAULT_PAYMENT_METHOD_TYPE",
        value_type=SettingValueType.string,
        default="card",
        allowed={"card", "bank_account", "cash", "check", "transfer", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_bank_account_type",
        env_var="BILLING_DEFAULT_BANK_ACCOUNT_TYPE",
        value_type=SettingValueType.string,
        default="checking",
        allowed={"checking", "savings", "business", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="default_payment_status",
        env_var="BILLING_DEFAULT_PAYMENT_STATUS",
        value_type=SettingValueType.string,
        default="pending",
        allowed={"pending", "succeeded", "failed", "refunded", "canceled"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="billing_interval_seconds",
        env_var="BILLING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="payment_due_days",
        env_var="BILLING_PAYMENT_DUE_DAYS",
        value_type=SettingValueType.integer,
        default=14,
        min_value=0,
        label="Payment Due Days",
    ),
    # Autopay: only charge invoices that are actually due (due_at <= now, or
    # overdue with no due date). Disable to revert to charge-at-issuance.
    SettingSpec(
        domain=SettingDomain.billing,
        key="autopay_charge_only_due",
        env_var="BILLING_AUTOPAY_CHARGE_ONLY_DUE",
        value_type=SettingValueType.boolean,
        default=True,
        label="Autopay Charges Only Due Invoices",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="autopay_max_consecutive_failures",
        env_var="BILLING_AUTOPAY_MAX_CONSECUTIVE_FAILURES",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
        label="Autopay Max Consecutive Failures",
    ),
    # Overdue detection (independent of billing cycle)
    SettingSpec(
        domain=SettingDomain.billing,
        key="overdue_check_interval_seconds",
        env_var="BILLING_OVERDUE_CHECK_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
        label="Overdue Check Interval (seconds)",
    ),
    # Prepaid top-up limits
    SettingSpec(
        domain=SettingDomain.billing,
        key="topup_min_amount",
        env_var="BILLING_TOPUP_MIN_AMOUNT",
        value_type=SettingValueType.integer,
        default=1000,
        min_value=100,
        label="Minimum Top-Up Amount",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="topup_max_amount",
        env_var="BILLING_TOPUP_MAX_AMOUNT",
        value_type=SettingValueType.integer,
        default=500000,
        min_value=1000,
        label="Maximum Top-Up Amount",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="gateway_topup_intent_ttl_minutes",
        env_var="BILLING_GATEWAY_TOPUP_INTENT_TTL_MINUTES",
        value_type=SettingValueType.integer,
        default=30,
        min_value=5,
        max_value=1440,
        label="Gateway Top-Up Intent Lifetime (minutes)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="topup_preset_amounts",
        env_var="BILLING_TOPUP_PRESET_AMOUNTS",
        value_type=SettingValueType.string,
        default="1000,2000,5000,10000,20000,50000",
        label="Top-Up Preset Amounts",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="expiry_reminder_days",
        env_var="BILLING_EXPIRY_REMINDER_DAYS",
        value_type=SettingValueType.integer,
        default=7,
        label="Subscription Expiry Reminder (days before)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="renewal_invoice_notice_enabled",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Enable Advance Renewal Invoice Notifications",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="renewal_invoice_notice_days",
        env_var=None,
        value_type=SettingValueType.integer,
        default=None,
        min_value=0,
        max_value=90,
        label="Advance Renewal Invoice Notice (days before)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_reminder_days",
        env_var="BILLING_INVOICE_REMINDER_DAYS",
        value_type=SettingValueType.string,
        default="7,1",
        label="Invoice Payment Reminder (days before due)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="minimum_balance",
        env_var="BILLING_MINIMUM_BALANCE",
        value_type=SettingValueType.string,
        default="0",
        label="Minimum Balance",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="refund_policy",
        env_var="PLAN_CHANGE_REFUND_POLICY",
        value_type=SettingValueType.string,
        default="none",
        allowed={"none", "prorated", "full_within_days"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="upgrade_fee",
        env_var="PLAN_CHANGE_UPGRADE_FEE",
        value_type=SettingValueType.string,
        default="0.00",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="downgrade_fee",
        env_var="PLAN_CHANGE_DOWNGRADE_FEE",
        value_type=SettingValueType.string,
        default="0.00",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="fee_tax_rate",
        env_var="PLAN_CHANGE_FEE_TAX_RATE",
        value_type=SettingValueType.string,
        default="0.00",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_timing",
        env_var="PLAN_CHANGE_INVOICE_TIMING",
        value_type=SettingValueType.string,
        default="immediate",
        allowed={"immediate", "next_invoice"},
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="prepaid_rollover",
        env_var="PLAN_CHANGE_PREPAID_ROLLOVER",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="discount_transfer",
        env_var="PLAN_CHANGE_DISCOUNT_TRANSFER",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="minimum_invoice_amount",
        env_var="PLAN_CHANGE_MINIMUM_INVOICE_AMOUNT",
        value_type=SettingValueType.string,
        default="0.00",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="arrangement_min_installments",
        env_var="BILLING_ARRANGEMENT_MIN_INSTALLMENTS",
        value_type=SettingValueType.integer,
        default=2,
        min_value=2,
        max_value=60,
        label="Payment Arrangement Minimum Installments",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="arrangement_max_installments",
        env_var="BILLING_ARRANGEMENT_MAX_INSTALLMENTS",
        value_type=SettingValueType.integer,
        default=24,
        min_value=2,
        max_value=60,
        label="Payment Arrangement Maximum Installments",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="arrangement_default_overdue_installments",
        env_var="BILLING_ARRANGEMENT_DEFAULT_OVERDUE_INSTALLMENTS",
        value_type=SettingValueType.integer,
        default=2,
        min_value=1,
        max_value=5,
        label="Payment Arrangement Default Overdue Installments",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="service_extension_max_days",
        env_var="BILLING_SERVICE_EXTENSION_MAX_DAYS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
        max_value=365,
        label="Service Extension Maximum Days",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="subscription_billing_treatment_max_days",
        env_var="BILLING_SUBSCRIPTION_TREATMENT_MAX_DAYS",
        value_type=SettingValueType.integer,
        default=366,
        min_value=1,
        max_value=366,
        label="Subscription Billing Treatment Maximum Approval Days",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="topup_reconciliation_stale_minutes",
        env_var="BILLING_TOPUP_RECONCILIATION_STALE_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
        max_value=1440,
        label="Top-up Reconciliation Stale Minutes",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="topup_reconciliation_max_age_days",
        env_var="BILLING_TOPUP_RECONCILIATION_MAX_AGE_DAYS",
        value_type=SettingValueType.integer,
        default=7,
        min_value=1,
        max_value=30,
        label="Top-up Reconciliation Max Age Days",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="topup_reconciliation_expiry_grace_hours",
        env_var="BILLING_TOPUP_RECONCILIATION_EXPIRY_GRACE_HOURS",
        value_type=SettingValueType.integer,
        default=24,
        min_value=0,
        max_value=168,
        label="Top-up Reconciliation Expiry Grace Hours",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="topup_reconciliation_batch_size",
        env_var="BILLING_TOPUP_RECONCILIATION_BATCH_SIZE",
        value_type=SettingValueType.integer,
        default=50,
        min_value=1,
        max_value=500,
        label="Top-up Reconciliation Batch Size",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="ar_aging_bucket_days",
        env_var="BILLING_AR_AGING_BUCKET_DAYS",
        value_type=SettingValueType.string,
        default="30,60,90",
        label="AR Aging Bucket Days",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="billing_health_scan_min_ratio",
        env_var="BILLING_HEALTH_SCAN_MIN_RATIO",
        value_type=SettingValueType.string,
        default="0.5",
        label="Billing Health Scan Minimum Ratio",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="billing_health_payment_volume_min_ratio",
        env_var="BILLING_HEALTH_PAYMENT_VOLUME_MIN_RATIO",
        value_type=SettingValueType.string,
        default="0.4",
        label="Billing Health Payment Volume Minimum Ratio",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="billing_health_payment_baseline_min_daily",
        env_var="BILLING_HEALTH_PAYMENT_BASELINE_MIN_DAILY",
        value_type=SettingValueType.string,
        default="5.0",
        label="Billing Health Payment Baseline Minimum Daily",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="bill_ip_addon_requires_active_route",
        env_var="BILLING_IP_ADDON_REQUIRES_ACTIVE_ROUTE",
        value_type=SettingValueType.boolean,
        default=False,
        label=(
            "Bill public-IP add-ons only for active routed blocks "
            "(cap quantity to live routes; audit route data before enabling)"
        ),
    ),
    # ── Prepaid customer defaults ──
    SettingSpec(
        domain=SettingDomain.billing,
        key="prepaid_default_billing_day",
        env_var="BILLING_PREPAID_DEFAULT_BILLING_DAY",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        max_value=28,
        label="Prepaid Default Billing Day (0 = day of activation)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="prepaid_default_payment_due_days",
        env_var="BILLING_PREPAID_DEFAULT_PAYMENT_DUE_DAYS",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        label="Prepaid Default Payment Due Days",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="prepaid_default_grace_period_days",
        env_var="BILLING_PREPAID_DEFAULT_GRACE_PERIOD_DAYS",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        label="Prepaid Default Grace Period (days)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="prepaid_default_min_balance",
        env_var="BILLING_PREPAID_DEFAULT_MIN_BALANCE",
        value_type=SettingValueType.string,
        default="0.00",
        label="Prepaid Default Minimum Balance",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="prepaid_enforcement_currency",
        env_var="BILLING_PREPAID_ENFORCEMENT_CURRENCY",
        value_type=SettingValueType.string,
        default="NGN",
        label="Prepaid Enforcement Currency",
    ),
    # ── Postpaid customer defaults ──
    SettingSpec(
        domain=SettingDomain.billing,
        key="postpaid_default_billing_day",
        env_var="BILLING_POSTPAID_DEFAULT_BILLING_DAY",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
        max_value=28,
        label="Postpaid Default Billing Day",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="postpaid_default_payment_due_days",
        env_var="BILLING_POSTPAID_DEFAULT_PAYMENT_DUE_DAYS",
        value_type=SettingValueType.integer,
        default=14,
        min_value=0,
        label="Postpaid Default Payment Due Days",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="postpaid_default_grace_period_days",
        env_var="BILLING_POSTPAID_DEFAULT_GRACE_PERIOD_DAYS",
        value_type=SettingValueType.integer,
        default=7,
        min_value=0,
        label="Postpaid Default Grace Period (days)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="postpaid_default_min_balance",
        env_var="BILLING_POSTPAID_DEFAULT_MIN_BALANCE",
        value_type=SettingValueType.string,
        default="0.00",
        label="Postpaid Default Minimum Balance",
    ),
    # --- Direct bank transfer (customer-visible payment instructions) ---
    # Consumed through app.services.topup_intents.direct_transfer_configuration;
    # customer, reseller, and API adapters only serialize that owner projection. The
    # ``_accounts`` value holds a JSON *string* in value_text (not value_json),
    # so it is typed ``string`` deliberately — a ``json`` type would move it to
    # value_json and the readers would see an empty transfer config.
    SettingSpec(
        domain=SettingDomain.billing,
        key="direct_bank_transfer_instructions",
        env_var=None,
        value_type=SettingValueType.string,
        default="",
        label="Direct Bank Transfer Instructions",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="direct_bank_transfer_accounts",
        env_var=None,
        value_type=SettingValueType.string,
        default="",
        label="Direct Bank Transfer Accounts (JSON)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="direct_bank_transfer_intent_ttl_days",
        env_var="BILLING_DIRECT_BANK_TRANSFER_INTENT_TTL_DAYS",
        value_type=SettingValueType.integer,
        default=7,
        min_value=1,
        max_value=30,
        label="Direct Bank Transfer Intent Lifetime (days)",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_number_prefix",
        env_var="BILLING_INVOICE_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="INV-",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_number_padding",
        env_var="BILLING_INVOICE_NUMBER_PADDING",
        value_type=SettingValueType.integer,
        default=6,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="invoice_number_start",
        env_var="BILLING_INVOICE_NUMBER_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="credit_note_number_prefix",
        env_var="BILLING_CREDIT_NOTE_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="CR-",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="credit_note_number_padding",
        env_var="BILLING_CREDIT_NOTE_NUMBER_PADDING",
        value_type=SettingValueType.integer,
        default=6,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="credit_note_number_start",
        env_var="BILLING_CREDIT_NOTE_NUMBER_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_proration_policy",
        env_var="CATALOG_DEFAULT_PRORATION_POLICY",
        value_type=SettingValueType.string,
        default="immediate",
        allowed={"immediate", "next_cycle", "none"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_downgrade_policy",
        env_var="CATALOG_DEFAULT_DOWNGRADE_POLICY",
        value_type=SettingValueType.string,
        default="next_cycle",
        allowed={"immediate", "next_cycle", "none"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_suspension_action",
        env_var="CATALOG_DEFAULT_SUSPENSION_ACTION",
        value_type=SettingValueType.string,
        default="suspend",
        allowed={"none", "throttle", "suspend", "reject"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_refund_policy",
        env_var="CATALOG_DEFAULT_REFUND_POLICY",
        value_type=SettingValueType.string,
        default="none",
        allowed={"none", "prorated", "full_within_days"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="subscription_expiration_interval_seconds",
        env_var="SUBSCRIPTION_EXPIRATION_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,  # Daily
        min_value=3600,  # Minimum 1 hour
        label="Subscription Expiration Check Interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="expiry_reminder_interval_seconds",
        env_var="EXPIRY_REMINDER_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,  # Daily
        min_value=3600,  # Minimum 1 hour
        label="Subscription Expiry Reminder Interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_billing_cycle",
        env_var="CATALOG_DEFAULT_BILLING_CYCLE",
        value_type=SettingValueType.string,
        default="monthly",
        allowed={"daily", "weekly", "monthly", "quarterly", "annual"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_contract_term",
        env_var="CATALOG_DEFAULT_CONTRACT_TERM",
        value_type=SettingValueType.string,
        default="month_to_month",
        allowed={"month_to_month", "twelve_month", "twentyfour_month"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_offer_status",
        env_var="CATALOG_DEFAULT_OFFER_STATUS",
        value_type=SettingValueType.string,
        default="active",
        allowed={"active", "inactive", "archived"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_subscription_status",
        env_var="CATALOG_DEFAULT_SUBSCRIPTION_STATUS",
        value_type=SettingValueType.string,
        default="pending",
        allowed={"pending", "active", "suspended", "canceled", "expired"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_price_type",
        env_var="CATALOG_DEFAULT_PRICE_TYPE",
        value_type=SettingValueType.string,
        default="recurring",
        allowed={"recurring", "one_time", "usage"},
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_addon_type",
        env_var="CATALOG_DEFAULT_ADDON_TYPE",
        value_type=SettingValueType.string,
        default="custom",
        allowed={
            "static_ip",
            "router_rental",
            "install_fee",
            "premium_support",
            "extra_ip",
            "managed_wifi",
            "custom",
        },
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_addon_quantity",
        env_var="CATALOG_DEFAULT_ADDON_QUANTITY",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="default_nas_vendor",
        env_var="CATALOG_DEFAULT_NAS_VENDOR",
        value_type=SettingValueType.string,
        default="other",
        allowed={"mikrotik", "cisco", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="default_address_type",
        env_var="SUBSCRIBER_DEFAULT_ADDRESS_TYPE",
        value_type=SettingValueType.string,
        default="service",
        allowed={"service", "billing", "mailing"},
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="default_country_code",
        env_var="DEFAULT_COUNTRY_CODE",
        value_type=SettingValueType.string,
        default="234",
        label="Default Country Code",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="identity_sensitive_automation_min_confidence",
        env_var="IDENTITY_SENSITIVE_AUTOMATION_MIN_CONFIDENCE",
        value_type=SettingValueType.string,
        default="MEDIUM",
        allowed={"HIGH", "MEDIUM"},
        label="Sensitive Automation Minimum Identity Confidence",
    ),
    # customer.service_level PR-3 preparation.  The selector is deliberately
    # one-valued until an accepted discrepancy review authorizes the separate
    # cutover change.  A database row containing any other value resolves back
    # to this safe default.
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="sla_admin_display_authority",
        env_var=None,
        value_type=SettingValueType.string,
        default="legacy_availability",
        allowed={"legacy_availability"},
        label="Admin SLA Display Authority (pre-cutover)",
    ),
    # Referral program (— migrated from the CRM subscriber
    # domain; there is no program table, these five keys ARE the program).
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="referral_program_enabled",
        env_var="REFERRAL_PROGRAM_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Referral Program Enabled",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="referral_signup_context_expiry_minutes",
        env_var="REFERRAL_SIGNUP_CONTEXT_EXPIRY_MINUTES",
        value_type=SettingValueType.integer,
        default=1440,
        min_value=5,
        max_value=10080,
        label="Referral signup context expiry (minutes)",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="referral_reward_amount",
        env_var="REFERRAL_REWARD_AMOUNT",
        value_type=SettingValueType.string,
        default="0",
        label="Referral Reward Amount",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="referral_reward_currency",
        env_var="REFERRAL_REWARD_CURRENCY",
        value_type=SettingValueType.string,
        default="NGN",
        label="Referral Reward Currency",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="referral_share_base_url",
        env_var="PORTAL_REFERRAL_SHARE_BASE",
        value_type=SettingValueType.string,
        default="https://app.dotmac.io",
        label="Referral Share Base URL",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="referral_qualify_window_days",
        env_var="REFERRAL_QUALIFY_WINDOW_DAYS",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
        label="Referral Qualification Window (days)",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="referral_auto_approve_reward",
        env_var="REFERRAL_AUTO_APPROVE_REWARD",
        value_type=SettingValueType.boolean,
        default=False,
        label="Auto-Approve Referral Rewards",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="subscriber_number_enabled",
        env_var="SUBSCRIBER_NUMBER_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="subscriber_number_prefix",
        env_var="SUBSCRIBER_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="SUB-",
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="subscriber_number_padding",
        env_var="SUBSCRIBER_NUMBER_PADDING",
        value_type=SettingValueType.integer,
        default=6,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="subscriber_number_start",
        env_var="SUBSCRIBER_NUMBER_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="account_number_prefix",
        env_var="SUBSCRIBER_ACCOUNT_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="ACC-",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="default_charge_status",
        env_var="USAGE_DEFAULT_CHARGE_STATUS",
        value_type=SettingValueType.string,
        default="staged",
        allowed={"staged", "posted", "needs_review", "skipped"},
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="default_rating_run_status",
        env_var="USAGE_DEFAULT_RATING_RUN_STATUS",
        value_type=SettingValueType.string,
        default="running",
        allowed={"running", "success", "failed"},
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_warning_enabled",
        env_var="USAGE_WARNING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable Usage Warning Events",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="usage_warning_thresholds",
        env_var="USAGE_WARNING_THRESHOLDS",
        value_type=SettingValueType.string,
        default="0.8,0.9",
        label="Usage Warning Thresholds",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="fup_throttle_radius_profile_id",
        env_var="USAGE_FUP_THROTTLE_RADIUS_PROFILE_ID",
        value_type=SettingValueType.string,
        default=None,
        label="FUP Throttle RADIUS Profile ID",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="fup_submonthly_rules_enabled",
        env_var="USAGE_FUP_SUBMONTHLY_RULES_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label=(
            "Allow daily/weekly FUP rules (sub-monthly usage is samples-derived, "
            "not billing-grade — enable only after validating the metrics source)"
        ),
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="fup_action",
        env_var="USAGE_FUP_ACTION",
        value_type=SettingValueType.string,
        default="throttle",
        allowed={"throttle", "suspend", "block", "none"},
        label="FUP Exhaustion Action",
    ),
    # Router REST connection tunables (previously hardcoded module constants in
    # router_management.connection). Read by _rest_tunables() so operators can
    # tune WAN/high-latency plant without a code change.
    SettingSpec(
        domain=SettingDomain.network,
        key="router_rest_connect_timeout_seconds",
        env_var="NETWORK_ROUTER_REST_CONNECT_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=10,
        label="Router REST connect timeout (s)",
        min_value=1,
        max_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="router_rest_read_timeout_seconds",
        env_var="NETWORK_ROUTER_REST_READ_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        label="Router REST read timeout (s)",
        min_value=5,
        max_value=180,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="router_rest_max_retries",
        env_var="NETWORK_ROUTER_REST_MAX_RETRIES",
        value_type=SettingValueType.integer,
        default=3,
        label="Router REST max retries",
        min_value=1,
        max_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="router_rest_retry_backoff_base",
        env_var="NETWORK_ROUTER_REST_RETRY_BACKOFF_BASE",
        value_type=SettingValueType.string,
        default="2.0",
        label="Router REST retry backoff base (wait = base^attempt)",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="olt_mac_harvest_age_out_hours",
        env_var="NETWORK_OLT_MAC_HARVEST_AGE_OUT_HOURS",
        value_type=SettingValueType.integer,
        default=6,
        label="OLT MAC-forwarding observation age-out (hours)",
        min_value=1,
        max_value=168,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="coa_negative_cache_ttl_minutes",
        env_var="NETWORK_COA_NEGATIVE_CACHE_TTL_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        label="CoA negative-cache TTL (min) — skip CoA on a NAS that timed out",
        min_value=1,
        max_value=120,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="mikrotik_session_kill_enabled",
        env_var="NETWORK_MIKROTIK_SESSION_KILL_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable MikroTik Session Kill",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="mikrotik_api_session_kick_enabled",
        env_var="NETWORK_MIKROTIK_API_SESSION_KICK_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable MikroTik API Session Kick",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="address_list_block_enabled",
        env_var="NETWORK_ADDRESS_LIST_BLOCK_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable Address List Blocking",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_mikrotik_address_list",
        env_var="NETWORK_DEFAULT_MIKROTIK_ADDRESS_LIST",
        value_type=SettingValueType.string,
        default=None,
        label="Default MikroTik Address List",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="customer_ont_reboot_cooldown_seconds",
        env_var="NETWORK_CUSTOMER_ONT_REBOOT_COOLDOWN_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=0,
        label="Customer ONT Reboot Cooldown (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="default_dunning_case_status",
        env_var="COLLECTIONS_DEFAULT_DUNNING_CASE_STATUS",
        value_type=SettingValueType.string,
        default="open",
        allowed={"open", "paused", "resolved", "closed"},
    ),
    SettingSpec(
        domain=SettingDomain.collections,
        key="throttle_radius_profile_id",
        env_var="COLLECTIONS_THROTTLE_RADIUS_PROFILE_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Throttle RADIUS Profile ID",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="default_auth_provider",
        env_var="AUTH_DEFAULT_AUTH_PROVIDER",
        value_type=SettingValueType.string,
        default="local",
        allowed={"local", "sso", "radius"},
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="default_session_status",
        env_var="AUTH_DEFAULT_SESSION_STATUS",
        value_type=SettingValueType.string,
        default="active",
        allowed={"active", "revoked", "expired"},
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_service_order_status",
        env_var="PROVISIONING_DEFAULT_SERVICE_ORDER_STATUS",
        value_type=SettingValueType.string,
        default="draft",
        allowed={
            "draft",
            "submitted",
            "scheduled",
            "provisioning",
            "active",
            "canceled",
            "failed",
        },
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_appointment_status",
        env_var="PROVISIONING_DEFAULT_APPOINTMENT_STATUS",
        value_type=SettingValueType.string,
        default="proposed",
        allowed={"proposed", "confirmed", "completed", "no_show", "canceled"},
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_task_status",
        env_var="PROVISIONING_DEFAULT_TASK_STATUS",
        value_type=SettingValueType.string,
        default="pending",
        allowed={"pending", "in_progress", "blocked", "completed", "failed"},
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_vendor",
        env_var="PROVISIONING_DEFAULT_VENDOR",
        value_type=SettingValueType.string,
        default="other",
        allowed={"mikrotik", "huawei", "zte", "nokia", "genieacs", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.provisioning,
        key="default_workflow_id",
        env_var="PROVISIONING_DEFAULT_WORKFLOW_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Default Provisioning Workflow ID",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_device_type",
        env_var="NETWORK_DEFAULT_DEVICE_TYPE",
        value_type=SettingValueType.string,
        default="other",
        allowed={"ont", "router", "modem", "cpe", "access_point", "bridge", "other"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_device_status",
        env_var="NETWORK_DEFAULT_DEVICE_STATUS",
        value_type=SettingValueType.string,
        default="active",
        allowed={"active", "inactive", "retired"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_port_type",
        env_var="NETWORK_DEFAULT_PORT_TYPE",
        value_type=SettingValueType.string,
        default="ethernet",
        allowed={"pon", "ethernet", "wifi", "mgmt"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_port_status",
        env_var="NETWORK_DEFAULT_PORT_STATUS",
        value_type=SettingValueType.string,
        default="down",
        allowed={"up", "down", "disabled"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_ip_version",
        env_var="NETWORK_DEFAULT_IP_VERSION",
        value_type=SettingValueType.string,
        default="ipv4",
        allowed={"ipv4", "ipv6"},
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="default_fiber_strand_status",
        env_var="NETWORK_DEFAULT_FIBER_STRAND_STATUS",
        value_type=SettingValueType.string,
        default="available",
        allowed={
            "available",
            "in_use",
            "reserved",
            "damaged",
            "retired",
        },
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="internet_service_vlans",
        env_var="NETWORK_INTERNET_SERVICE_VLANS",
        value_type=SettingValueType.string,
        default="203",
        label="Internet Service VLANs",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="speedtest_sla_ratio",
        env_var="NETWORK_SPEEDTEST_SLA_RATIO",
        value_type=SettingValueType.string,
        default="0.8",
        label="Speedtest SLA Ratio",
    ),
    # Fiber installation planning cost rates
    SettingSpec(
        domain=SettingDomain.network,
        key="fiber_drop_cable_cost_per_meter",
        env_var="NETWORK_FIBER_DROP_CABLE_COST_PER_METER",
        value_type=SettingValueType.string,
        default="2.50",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="fiber_labor_cost_per_meter",
        env_var="NETWORK_FIBER_LABOR_COST_PER_METER",
        value_type=SettingValueType.string,
        default="1.50",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="fiber_ont_device_cost",
        env_var="NETWORK_FIBER_ONT_DEVICE_COST",
        value_type=SettingValueType.string,
        default="85.00",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="fiber_installation_base_fee",
        env_var="NETWORK_FIBER_INSTALLATION_BASE_FEE",
        value_type=SettingValueType.string,
        default="50.00",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="olt_profile_sync_worker_enabled",
        env_var="OLT_PROFILE_SYNC_WORKER_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="OLT Profile Sync Worker Enabled",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="olt_profile_sync_interval_seconds",
        env_var="OLT_PROFILE_SYNC_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=60,
        label="OLT Profile Sync Interval (seconds)",
    ),
    # Automated customer outage notification (ADR 0004). Database-authoritative
    # so an operator can arm, disarm or re-tighten automation from the admin UI
    # during an incident. An env-var-only flag would need a deploy to flip,
    # which is the wrong control for something that contacts customers.
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_auto_notify_enabled",
        label="Automated Outage Notification",
        env_var="OUTAGE_AUTO_NOTIFY_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_auto_notify_dry_run",
        label="Automated Outage Notification: Dry Run",
        env_var="OUTAGE_AUTO_NOTIFY_DRY_RUN",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_auto_notify_settle_minutes",
        label="Automated Outage Notification: Settling Window (minutes)",
        env_var="OUTAGE_AUTO_NOTIFY_SETTLE_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_auto_notify_min_affected",
        label="Automated Outage Notification: Minimum Affected Customers",
        env_var="OUTAGE_AUTO_NOTIFY_MIN_AFFECTED",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_auto_notify_max_incidents_per_run",
        label="Automated Outage Notification: Max Incidents Per Run",
        env_var="OUTAGE_AUTO_NOTIFY_MAX_INCIDENTS_PER_RUN",
        value_type=SettingValueType.integer,
        default=10,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_auto_notify_interval_seconds",
        label="Automated Outage Notification: Run Interval (seconds)",
        env_var="OUTAGE_AUTO_NOTIFY_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=120,
    ),
    # Customer outage communications (network.outage_communications,
    # OUTAGE_SLA_SPINE §3). Arming this makes the spine-driven owner the
    # canonical customer outage sender and disarms the legacy
    # outage_notifications paths above — two senders must never be live at
    # once. Same database-authoritative rationale as ADR 0004: anything that
    # contacts customers must be re-tightenable from the admin UI mid-incident.
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_customer_comms_enabled",
        label="Customer Outage Communications",
        env_var="OUTAGE_CUSTOMER_COMMS_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_customer_comms_dry_run",
        label="Customer Outage Communications: Dry Run",
        env_var="OUTAGE_CUSTOMER_COMMS_DRY_RUN",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_customer_comms_settle_minutes",
        label="Customer Outage Communications: Settling Window (minutes)",
        env_var="OUTAGE_CUSTOMER_COMMS_SETTLE_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_customer_comms_min_affected",
        label="Customer Outage Communications: Minimum Affected Customers",
        env_var="OUTAGE_CUSTOMER_COMMS_MIN_AFFECTED",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_customer_comms_update_interval_hours",
        label="Customer Outage Communications: Update Interval (hours)",
        env_var="OUTAGE_CUSTOMER_COMMS_UPDATE_INTERVAL_HOURS",
        value_type=SettingValueType.integer,
        default=6,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_customer_comms_max_recipients_per_run",
        label="Customer Outage Communications: Max Recipients Per Run",
        env_var="OUTAGE_CUSTOMER_COMMS_MAX_RECIPIENTS_PER_RUN",
        value_type=SettingValueType.integer,
        default=500,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_customer_comms_customer_cooldown_hours",
        label="Customer Outage Communications: Per-Customer Cooldown (hours)",
        env_var="OUTAGE_CUSTOMER_COMMS_CUSTOMER_COOLDOWN_HOURS",
        value_type=SettingValueType.integer,
        default=2,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_disk_warn_pct",
        label="Server Health Disk Warning (%)",
        env_var="SERVER_HEALTH_DISK_WARN_PCT",
        value_type=SettingValueType.integer,
        default=80,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_disk_crit_pct",
        label="Server Health Disk Critical (%)",
        env_var="SERVER_HEALTH_DISK_CRIT_PCT",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_mem_warn_pct",
        label="Server Health Memory Warning (%)",
        env_var="SERVER_HEALTH_MEM_WARN_PCT",
        value_type=SettingValueType.integer,
        default=80,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_mem_crit_pct",
        label="Server Health Memory Critical (%)",
        env_var="SERVER_HEALTH_MEM_CRIT_PCT",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_load_warn",
        label="Server Health Load Warning (per core)",
        env_var="SERVER_HEALTH_LOAD_WARN",
        value_type=SettingValueType.string,
        default="1.0",
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="server_health_load_crit",
        label="Server Health Load Critical (per core)",
        env_var="SERVER_HEALTH_LOAD_CRIT",
        value_type=SettingValueType.string,
        default="1.5",
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="network_health_warn_pct",
        label="Network Health Warning Threshold (%)",
        env_var="NETWORK_HEALTH_WARN_PCT",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="network_health_crit_pct",
        label="Network Health Critical Threshold (%)",
        env_var="NETWORK_HEALTH_CRIT_PCT",
        value_type=SettingValueType.integer,
        default=70,
        min_value=1,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="infrastructure_poll_interval_seconds",
        label="Native Infrastructure Poll Beat Interval (seconds)",
        env_var="INFRASTRUCTURE_POLL_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=30,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="infrastructure_ping_interval_seconds",
        label="Native Poll Ping Staleness Window (seconds)",
        env_var="INFRASTRUCTURE_PING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="infrastructure_snmp_interval_seconds",
        label="Native Poll SNMP Staleness Window (seconds)",
        env_var="INFRASTRUCTURE_SNMP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=30,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="infrastructure_poll_skip_streak_threshold",
        label="Poll Watchdog: Skipped-Run Streak Before Alert",
        env_var="INFRASTRUCTURE_POLL_SKIP_STREAK_THRESHOLD",
        value_type=SettingValueType.integer,
        default=5,
        min_value=2,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="infrastructure_poll_stale_ping_minutes",
        label="Poll Watchdog: Stale Ping Data Alert (minutes)",
        env_var="INFRASTRUCTURE_POLL_STALE_PING_MINUTES",
        value_type=SettingValueType.integer,
        default=10,
        min_value=3,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="radius_health_interval_seconds",
        label="RADIUS Health Check Interval (seconds)",
        env_var="RADIUS_HEALTH_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=120,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="radius_stale_session_seconds",
        label="RADIUS Stale Open Session Threshold (seconds)",
        env_var="RADIUS_STALE_SESSION_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=120,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="radius_acct_freshness_alert_seconds",
        label="RADIUS Accounting Freshness Alert (seconds)",
        env_var="RADIUS_ACCT_FRESHNESS_ALERT_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=180,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="autodetect_incident_impact_ttl_hours",
        label="Auto-detect Incident Customer-Impact TTL (hours)",
        env_var="AUTODETECT_INCIDENT_IMPACT_TTL_HOURS",
        value_type=SettingValueType.integer,
        default=6,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="customer_impact_metrics_interval_seconds",
        label="Customer Impact Metrics Interval (seconds)",
        env_var="CUSTOMER_IMPACT_METRICS_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=120,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_autodetect_min_affected",
        label="Outage Wireless-Cluster Min Down Radios",
        env_var="OUTAGE_AUTODETECT_MIN_AFFECTED",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_autodetect_min_fraction_pct",
        label="Outage Wireless-Cluster Min Down Fraction (%)",
        env_var="OUTAGE_AUTODETECT_MIN_FRACTION_PCT",
        value_type=SettingValueType.integer,
        default=40,
        min_value=1,
        max_value=100,
    ),
    # --- Detected-outage incident reconcile (design ) ------------------
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_reconcile_interval_seconds",
        label="Detected-Outage Reconcile Interval (seconds)",
        env_var="OUTAGE_RECONCILE_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=180,
        min_value=120,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_seconds_large",
        label="Outage Confirm Window — Large Impact (seconds)",
        env_var="OUTAGE_CONFIRM_SECONDS_LARGE",
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_seconds_med",
        label="Outage Confirm Window — Medium Impact (seconds)",
        env_var="OUTAGE_CONFIRM_SECONDS_MED",
        value_type=SettingValueType.integer,
        default=360,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_seconds_small",
        label="Outage Confirm Window — Small Impact (seconds)",
        env_var="OUTAGE_CONFIRM_SECONDS_SMALL",
        value_type=SettingValueType.integer,
        default=600,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_threshold_med",
        label="Outage Confirm — Medium-Impact Threshold (affected)",
        env_var="OUTAGE_CONFIRM_THRESHOLD_MED",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_confirm_threshold_large",
        label="Outage Confirm — Large-Impact Threshold (affected)",
        env_var="OUTAGE_CONFIRM_THRESHOLD_LARGE",
        value_type=SettingValueType.integer,
        default=20,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="outage_resolve_seconds",
        label="Outage Resolve Window — Sustained Recovery (seconds)",
        env_var="OUTAGE_RESOLVE_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="celery_long_running_task_minutes",
        label="Celery Long-running Task Threshold (minutes)",
        env_var="CELERY_LONG_RUNNING_TASK_MINUTES",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
        max_value=1440,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="celery_reserved_backlog_threshold",
        label="Celery Reserved Backlog Threshold",
        env_var="CELERY_RESERVED_BACKLOG_THRESHOLD",
        value_type=SettingValueType.integer,
        default=100,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="celery_queue_backlog_threshold",
        label="Celery Queue Backlog Threshold",
        env_var="CELERY_QUEUE_BACKLOG_THRESHOLD",
        value_type=SettingValueType.integer,
        default=500,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="dashboard_attention_ont_offline_threshold",
        label="Dashboard Attention: ONT Not Working Threshold",
        env_var="DASHBOARD_ATTENTION_ONT_OFFLINE_THRESHOLD",
        value_type=SettingValueType.integer,
        default=5,
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="dashboard_bandwidth_window_seconds",
        label="Dashboard Bandwidth Window (seconds)",
        env_var="DASHBOARD_BANDWIDTH_WINDOW_SECONDS",
        value_type=SettingValueType.integer,
        default=600,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="ont_signal_warning_dbm",
        label="ONT Signal Warning Threshold (dBm)",
        env_var="ONT_SIGNAL_WARNING_DBM",
        value_type=SettingValueType.integer,
        default=-25,
        min_value=-40,
        max_value=-5,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="ont_signal_critical_dbm",
        label="ONT Signal Critical Threshold (dBm)",
        env_var="ONT_SIGNAL_CRITICAL_DBM",
        value_type=SettingValueType.integer,
        default=-28,
        min_value=-40,
        max_value=-5,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="ont_signal_alert_cooldown_minutes",
        label="ONT Signal Alert Cooldown (minutes)",
        env_var="ONT_SIGNAL_ALERT_COOLDOWN_MINUTES",
        value_type=SettingValueType.integer,
        default=30,
        min_value=5,
        max_value=1440,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="topology_metrics_interval_seconds",
        label="Topology Metrics Export Interval (seconds)",
        env_var="TOPOLOGY_METRICS_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=300,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="forwarding_control_observation_interval_seconds",
        label="Forwarding Observation Collection Interval (seconds)",
        env_var="FORWARDING_CONTROL_OBSERVATION_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=60,
        max_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="forwarding_control_observation_ttl_seconds",
        label="Forwarding Observation TTL (seconds)",
        env_var="FORWARDING_CONTROL_OBSERVATION_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=300,
        max_value=7200,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="network_operation_dispatch_interval_seconds",
        label="Network Operation Dispatch Interval (seconds)",
        env_var="NETWORK_OPERATION_DISPATCH_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=10,
        min_value=5,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="network_operation_metrics_interval_seconds",
        label="Network Operation Metrics Interval (seconds)",
        env_var="NETWORK_OPERATION_METRICS_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_auth_port",
        env_var="RADIUS_DEFAULT_AUTH_PORT",
        value_type=SettingValueType.integer,
        default=1812,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_acct_port",
        env_var="RADIUS_DEFAULT_ACCT_PORT",
        value_type=SettingValueType.integer,
        default=1813,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_sync_users",
        env_var="RADIUS_DEFAULT_SYNC_USERS",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_sync_nas_clients",
        env_var="RADIUS_DEFAULT_SYNC_NAS_CLIENTS",
        value_type=SettingValueType.boolean,
        default=True,
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="default_sync_status",
        env_var="RADIUS_DEFAULT_SYNC_STATUS",
        value_type=SettingValueType.string,
        default="running",
        allowed={"running", "success", "failed"},
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="default_notification_status",
        env_var="COMMS_DEFAULT_NOTIFICATION_STATUS",
        value_type=SettingValueType.string,
        default="pending",
        allowed={"pending", "sent", "failed"},
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="chat_session_authority",
        env_var="CHAT_SESSION_AUTHORITY",
        value_type=SettingValueType.string,
        default="selfcare",
        allowed={"selfcare", "crm"},
        label="Live chat authority",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="inbox_reply_reminder_delay_minutes",
        env_var="INBOX_REPLY_REMINDER_DELAY_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
        label="Team Inbox first reply reminder delay (minutes)",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="inbox_reply_reminder_repeat_minutes",
        env_var="INBOX_REPLY_REMINDER_REPEAT_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
        label="Team Inbox reply reminder repeat interval (minutes)",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="campaign_processing_enabled",
        env_var="CAMPAIGN_PROCESSING_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Scheduled campaign admission",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="campaign_processing_interval_seconds",
        env_var="CAMPAIGN_PROCESSING_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=30,
        label="Campaign processing interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="sidebar_logo_url",
        env_var="SIDEBAR_LOGO_URL",
        value_type=SettingValueType.string,
        default="",
        label="Sidebar Logo URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="sidebar_logo_dark_url",
        env_var="SIDEBAR_LOGO_DARK_URL",
        value_type=SettingValueType.string,
        default="",
        label="Sidebar Dark Mode Logo URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="favicon_url",
        env_var="FAVICON_URL",
        value_type=SettingValueType.string,
        default="",
        label="Favicon URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_primary_color",
        env_var="BRAND_PRIMARY_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default=DEFAULT_HEX,
        label="Brand Primary Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_secondary_color",
        env_var="BRAND_SECONDARY_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default=DEFAULT_SECONDARY_HEX,
        label="Brand Secondary Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_semantic_positive_color",
        env_var="BRAND_SEMANTIC_POSITIVE_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default=DEFAULT_SEMANTIC_COLORS["positive"],
        label="Positive Status Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_semantic_info_color",
        env_var="BRAND_SEMANTIC_INFO_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default=DEFAULT_SEMANTIC_COLORS["info"],
        label="Informational Status Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_semantic_warning_color",
        env_var="BRAND_SEMANTIC_WARNING_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default=DEFAULT_SEMANTIC_COLORS["warning"],
        label="Warning Status Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_semantic_negative_color",
        env_var="BRAND_SEMANTIC_NEGATIVE_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default=DEFAULT_SEMANTIC_COLORS["negative"],
        label="Negative Status Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="brand_semantic_neutral_color",
        env_var="BRAND_SEMANTIC_NEUTRAL_COLOR_OVERRIDE",
        value_type=SettingValueType.string,
        default=DEFAULT_SEMANTIC_COLORS["neutral"],
        label="Neutral Status Colour",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="login_hero_customer_url",
        env_var="LOGIN_HERO_CUSTOMER_URL",
        value_type=SettingValueType.string,
        default="",
        label="Customer Login Image URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="login_hero_reseller_url",
        env_var="LOGIN_HERO_RESELLER_URL",
        value_type=SettingValueType.string,
        default="",
        label="Reseller Login Image URL",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="login_hero_admin_url",
        env_var="LOGIN_HERO_ADMIN_URL",
        value_type=SettingValueType.string,
        default="",
        label="Admin Login Image URL",
    ),
    # Meta (Facebook/Instagram) Integration Settings
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_app_id",
        env_var="META_APP_ID",
        value_type=SettingValueType.string,
        default=None,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_app_secret",
        env_var="META_APP_SECRET",
        value_type=SettingValueType.string,
        default=None,
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="meta_graph_api_version",
        env_var="META_GRAPH_API_VERSION",
        value_type=SettingValueType.string,
        default="v21.0",
        label="Meta Graph API Version",
    ),
    # ============== Auth Domain: Customer/Reseller/Vendor Portal Auth ==============
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_session_ttl_seconds",
        env_var="CUSTOMER_SESSION_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_remember_ttl_seconds",
        env_var="CUSTOMER_REMEMBER_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=86400,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_session_absolute_ttl_seconds",
        env_var="CUSTOMER_SESSION_ABSOLUTE_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_login_max_attempts",
        env_var="CUSTOMER_LOGIN_MAX_ATTEMPTS",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="customer_lockout_minutes",
        env_var="CUSTOMER_LOCKOUT_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="admin_mfa_required",
        env_var="ADMIN_MFA_REQUIRED",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="admin_login_max_attempts",
        env_var="ADMIN_LOGIN_MAX_ATTEMPTS",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="admin_lockout_minutes",
        env_var="ADMIN_LOCKOUT_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="mfa_max_failed_attempts",
        env_var="MFA_MAX_FAILED_ATTEMPTS",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="mfa_lockout_minutes",
        env_var="MFA_LOCKOUT_MINUTES",
        value_type=SettingValueType.integer,
        default=15,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="password_min_length",
        env_var="PASSWORD_MIN_LENGTH",
        value_type=SettingValueType.integer,
        default=8,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="reseller_session_ttl_seconds",
        env_var="RESELLER_SESSION_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="reseller_remember_ttl_seconds",
        env_var="RESELLER_REMEMBER_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=86400,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="reseller_session_absolute_ttl_seconds",
        env_var="RESELLER_SESSION_ABSOLUTE_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=2592000,
        min_value=3600,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="password_reset_expiry_minutes",
        env_var="PASSWORD_RESET_EXPIRY_MINUTES",
        value_type=SettingValueType.integer,
        default=1440,
        min_value=5,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="user_invite_expiry_minutes",
        env_var="USER_INVITE_EXPIRY_MINUTES",
        value_type=SettingValueType.integer,
        default=1440,
        min_value=5,
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="credential_enrollment_request_limit",
        env_var="CREDENTIAL_ENROLLMENT_REQUEST_LIMIT",
        value_type=SettingValueType.integer,
        default=3,
        min_value=1,
        max_value=10,
        label="Credential enrollment request limit",
    ),
    SettingSpec(
        domain=SettingDomain.auth,
        key="credential_enrollment_request_window_seconds",
        env_var="CREDENTIAL_ENROLLMENT_REQUEST_WINDOW_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=60,
        max_value=86400,
        label="Credential enrollment request window (seconds)",
    ),
    # ============== Comms Domain: External API Timeouts ==============
    SettingSpec(
        domain=SettingDomain.comms,
        key="nextcloud_talk_timeout_seconds",
        env_var="NEXTCLOUD_TALK_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    # ============== Notification Domain: Email Settings ==============
    SettingSpec(
        domain=SettingDomain.notification,
        key="smtp_test_timeout_seconds",
        env_var="SMTP_TEST_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=10,
        min_value=1,
    ),
    # ============== Bandwidth Domain: Bandwidth Processing ==============
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="stream_interval_seconds",
        env_var="BANDWIDTH_STREAM_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=5,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="aggregate_interval_seconds",
        env_var="BANDWIDTH_AGGREGATE_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=10,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="cleanup_interval_seconds",
        env_var="BANDWIDTH_CLEANUP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="trim_interval_seconds",
        env_var="BANDWIDTH_TRIM_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=600,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="hot_retention_hours",
        env_var="BANDWIDTH_HOT_RETENTION_HOURS",
        value_type=SettingValueType.integer,
        default=24,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="batch_size",
        env_var="BANDWIDTH_BATCH_SIZE",
        value_type=SettingValueType.integer,
        default=1000,
        min_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="redis_stream_max_length",
        env_var="BANDWIDTH_REDIS_STREAM_MAX_LENGTH",
        value_type=SettingValueType.integer,
        default=100000,
        min_value=1000,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="redis_read_timeout_ms",
        env_var="BANDWIDTH_REDIS_READ_TIMEOUT_MS",
        value_type=SettingValueType.integer,
        default=1000,
        min_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.bandwidth,
        key="victoriametrics_timeout_seconds",
        env_var="BANDWIDTH_VICTORIAMETRICS_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
    ),
    # ============== Network Domain: VPN/WireGuard ==============
    SettingSpec(
        domain=SettingDomain.network,
        key="vpn_cache_default_ttl_seconds",
        env_var="VPN_CACHE_DEFAULT_TTL_SECONDS",
        value_type=SettingValueType.integer,
        default=900,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_log_retention_days",
        env_var="WIREGUARD_LOG_RETENTION_DAYS",
        value_type=SettingValueType.integer,
        default=90,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_log_cleanup_enabled",
        env_var="WIREGUARD_LOG_CLEANUP_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="WireGuard Log Cleanup Enabled",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_log_cleanup_interval_seconds",
        env_var="WIREGUARD_LOG_CLEANUP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=86400,
        min_value=3600,
        label="WireGuard Log Cleanup Interval (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_token_cleanup_enabled",
        env_var="WIREGUARD_TOKEN_CLEANUP_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="WireGuard Token Cleanup Enabled",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_token_cleanup_interval_seconds",
        env_var="WIREGUARD_TOKEN_CLEANUP_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
        label="WireGuard Token Cleanup Interval (seconds)",
    ),
    # -------------- VPN Server Defaults --------------
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_listen_port",
        env_var="WIREGUARD_DEFAULT_LISTEN_PORT",
        value_type=SettingValueType.integer,
        default=51820,
        min_value=1,
        max_value=65535,
        label="VPN Default Listen Port",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_vpn_address",
        env_var="WIREGUARD_DEFAULT_VPN_ADDRESS",
        value_type=SettingValueType.string,
        default="10.10.0.1/24",
        label="VPN Default Address (CIDR)",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_vpn_address_v6",
        env_var="WIREGUARD_DEFAULT_VPN_ADDRESS_V6",
        value_type=SettingValueType.string,
        default=None,
        label="VPN Default IPv6 Address (CIDR)",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_mtu",
        env_var="WIREGUARD_DEFAULT_MTU",
        value_type=SettingValueType.integer,
        default=1420,
        min_value=1280,
        max_value=9000,
        label="VPN Default MTU",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="wireguard_default_interface_name",
        env_var="WIREGUARD_DEFAULT_INTERFACE_NAME",
        value_type=SettingValueType.string,
        default="wg0",
        label="VPN Default Interface Name",
    ),
    # ============== Provisioning Domain ==============
    *build_provisioning_specs(SettingSpec),
    # ============== Integration Domain (DotMac ERP edge) ==============
    *build_integration_specs(SettingSpec),
    SettingSpec(
        domain=SettingDomain.snmp,
        key="interface_walk_interval_seconds",
        env_var="SNMP_INTERFACE_WALK_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=300,
        min_value=30,
    ),
    SettingSpec(
        domain=SettingDomain.snmp,
        key="interface_discovery_interval_seconds",
        env_var="SNMP_INTERFACE_DISCOVERY_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=60,
    ),
    SettingSpec(
        domain=SettingDomain.tr069,
        key="default_acs_server_id",
        env_var="TR069_DEFAULT_ACS_SERVER_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Default TR-069 ACS Server ID",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="tr069_pending_resolution_timeout_seconds",
        env_var="TR069_PENDING_RESOLUTION_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=3600,
        min_value=300,
        max_value=86400,
        label="TR-069 Command Confirmation Window (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.network,
        key="tr069_command_reconciliation_interval_seconds",
        env_var="TR069_COMMAND_RECONCILIATION_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        min_value=30,
        max_value=3600,
        label="TR-069 Command Reconciliation Interval (seconds)",
    ),
    # ── Network Monitoring ─────────────────────────────────────────────
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="device_metrics_retention_days",
        env_var="DEVICE_METRICS_RETENTION_DAYS",
        value_type=SettingValueType.integer,
        default=90,
        label="Device Metrics Retention (days)",
        min_value=7,
        max_value=3650,
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="alert_evaluation_interval_seconds",
        env_var="ALERT_EVALUATION_INTERVAL_SECONDS",
        value_type=SettingValueType.integer,
        default=60,
        label="Alert Evaluation Interval (seconds)",
        min_value=10,
        max_value=600,
    ),
    # ── Projects lifecycle defaults ──
    SettingSpec(
        domain=SettingDomain.projects,
        key="project_number_enabled",
        env_var="PROJECTS_PROJECT_NUMBER_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable automatic project numbering",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="project_number_prefix",
        env_var="PROJECTS_PROJECT_NUMBER_PREFIX",
        value_type=SettingValueType.string,
        default="PROJ-",
        label="Project number prefix",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="project_number_padding",
        env_var="PROJECTS_PROJECT_NUMBER_PADDING",
        value_type=SettingValueType.integer,
        default=4,
        min_value=0,
        max_value=12,
        label="Project number padding",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="project_number_start",
        env_var="PROJECTS_PROJECT_NUMBER_START",
        value_type=SettingValueType.integer,
        default=1,
        min_value=1,
        label="Project number starting value",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="default_project_status",
        env_var="PROJECTS_DEFAULT_PROJECT_STATUS",
        value_type=SettingValueType.string,
        default="active",
        label="Default project status",
        allowed={"open", "planned", "active", "on_hold"},
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="default_project_priority",
        env_var="PROJECTS_DEFAULT_PROJECT_PRIORITY",
        value_type=SettingValueType.string,
        default="normal",
        label="Default project priority",
        allowed={"lower", "low", "medium", "normal", "high", "urgent"},
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="default_labor_hourly_rate",
        env_var="PROJECTS_DEFAULT_LABOR_HOURLY_RATE",
        value_type=SettingValueType.string,
        default="0.00",
        label="Default field labor hourly cost",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="default_sales_project_type",
        env_var="PROJECTS_DEFAULT_SALES_PROJECT_TYPE",
        value_type=SettingValueType.string,
        default="fiber_optics_installation",
        label="Default implementation type for sales orders",
        allowed={
            "cable_rerun",
            "fiber_optics_relocation",
            "air_fiber_relocation",
            "fiber_optics_installation",
            "air_fiber_installation",
            "cross_connect",
        },
    ),
    # ── Self-serve installation quotes ──
    # The nine CRM ``selfserve_quote_*`` keys migrated as-is, except the
    # price-book SKU keys which are re-keyed to sub catalog offers
    # (``*_offer_id`` — inventory remains externally owned). Placeholder defaults carried
    # from the CRM — tune per market. Estimate = base_fee + max(0,
    # distance_to_nearest_FAP - free_radius) * fee_per_km; deposit =
    # estimate * deposit_percent. Feasibility radius bounds "covered".
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_enabled",
        env_var="SELFSERVE_QUOTE_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Enable self-serve installation quotes",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_base_fee",
        env_var="SELFSERVE_QUOTE_BASE_FEE",
        value_type=SettingValueType.string,
        default="50000.00",
        label="Base installation fee (NGN) — fallback when no base offer is set",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_bundle_offer_id",
        env_var="SELFSERVE_QUOTE_BUNDLE_OFFER_ID",
        value_type=SettingValueType.string,
        default=None,
        label=(
            "Bundle install catalog offer id (flat price, overrides the "
            "derived estimate)"
        ),
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_base_offer_id",
        env_var="SELFSERVE_QUOTE_BASE_OFFER_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Base install catalog offer id — its price is the base fee",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_distance_offer_id",
        env_var="SELFSERVE_QUOTE_DISTANCE_OFFER_ID",
        value_type=SettingValueType.string,
        default=None,
        label="Per-km drop catalog offer id — its price is the per-km surcharge",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_free_radius_meters",
        env_var="SELFSERVE_QUOTE_FREE_RADIUS_METERS",
        value_type=SettingValueType.integer,
        default=300,
        label="Free drop radius from nearest fiber access point (m)",
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_fee_per_km",
        env_var="SELFSERVE_QUOTE_FEE_PER_KM",
        value_type=SettingValueType.string,
        default="25000.00",
        label="Distance surcharge per km beyond free radius (NGN) — PLACEHOLDER",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_deposit_percent",
        env_var="SELFSERVE_QUOTE_DEPOSIT_PERCENT",
        value_type=SettingValueType.integer,
        default=50,
        label="Deposit required to confirm a quote (% of total)",
        min_value=0,
        max_value=100,
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="selfserve_quote_feasibility_radius_meters",
        env_var="SELFSERVE_QUOTE_FEASIBILITY_RADIUS_METERS",
        value_type=SettingValueType.integer,
        default=2000,
        label=(
            "Max distance to nearest fiber access point for instant feasibility (m)"
        ),
        min_value=0,
    ),
    # Catalog-backed field-delivery charges. The configured offer must expose
    # one active one-time price; a wireless relocation fails closed when this
    # setting is empty or invalid, so neither portal can quote a free move by
    # accident.
    SettingSpec(
        domain=SettingDomain.projects,
        key="wireless_relocation_offer_id",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
        label="Wireless/radio relocation fee catalog offer",
    ),
    # write-flip flag:
    # OFF = quote acceptance write-through to the CRM (mirror path); ON =
    # native accept (sales/selfserve.accept_with_deposit → native sales
    # order pipeline). Default OFF until the coordinated write
    # window; flipping back is the cheap rollback.
    SettingSpec(
        domain=SettingDomain.projects,
        key="quotes_native_write_enabled",
        env_var="QUOTES_NATIVE_WRITE_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Quotes: native write path",
    ),
    # Quotes retain their independent read cutover. Project/task/work-order
    # customer reads are unconditionally native Sub paths.
    SettingSpec(
        domain=SettingDomain.projects,
        key="quotes_native_read_enabled",
        env_var="QUOTES_NATIVE_READ_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Quotes: native read path",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        key="vendor_quote_validity_days",
        env_var=None,
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
        max_value=365,
        label="Vendor quote validity (days)",
    ),
    SettingSpec(
        domain=SettingDomain.projects,
        # Optional guard rail, not the control. Staff approval is what decides
        # an advance; this only stops a request that is obviously out of policy
        # before it reaches an approver. 0 means no percentage policy — the
        # approver judges each advance on its merits. Advances can never exceed
        # the approved quote total regardless, which is arithmetic rather than
        # policy: you cannot advance more than the work is worth.
        key="vendor_advance_max_percent",
        env_var=None,
        value_type=SettingValueType.integer,
        default=0,
        min_value=0,
        max_value=100,
        label="Vendor advance cap (% of approved quote, 0 = no cap)",
    ),
    # --- AI provider transport (docs/designs/AI_SOT.md, ai.gateway) ----------
    # Every value defaults OFF/empty: the transport is inert until an operator
    # configures a provider. ai_enabled is the stored master switch, resolved
    # through the named resolver — there is no env override: this repo forbids
    # direct env decision inputs (tests/architecture/test_decision_input_
    # ownership), and env_var below seeds the stored value at bootstrap only.
    #
    # The api_key specs are is_secret and may hold an OpenBao reference
    # (``bao://mount/path#field``) rather than a literal key — resolved by
    # ai.security.resolve_provider_api_key via secrets.resolve_secret. Their
    # env_var is a seed/bootstrap input only; runtime never reads it as an
    # override (see the SettingSpec docstring).
    SettingSpec(
        domain=SettingDomain.integration,
        key="ai_enabled",
        env_var="AI_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="AI features enabled",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="lead_intake_auto_send_enabled",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=False,
        label="Inbox Lead intake: automatically send forms after AI classification",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="lead_intake_invitation_ttl_hours",
        env_var=None,
        value_type=SettingValueType.integer,
        default=24,
        min_value=1,
        max_value=24,
        label="Inbox Lead intake: invitation lifetime (hours)",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="intelligence_inbox_analyst_enabled",
        env_var="INTELLIGENCE_INBOX_ANALYST_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="AI inbox reply drafting and text polish enabled",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="voice_transcription_enabled",
        env_var="VOICE_TRANSCRIPTION_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Team Inbox voice transcription enabled",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="voice_transcription_base_url",
        env_var="VOICE_TRANSCRIPTION_BASE_URL",
        value_type=SettingValueType.string,
        default=None,
        label="Voice transcription: OpenAI-compatible base URL",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="voice_transcription_model",
        env_var="VOICE_TRANSCRIPTION_MODEL",
        value_type=SettingValueType.string,
        default=None,
        label="Voice transcription: model name",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="voice_transcription_api_key",
        env_var="VOICE_TRANSCRIPTION_API_KEY",
        value_type=SettingValueType.string,
        default=None,
        label="Voice transcription: API key OpenBao reference",
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="voice_transcription_timeout_seconds",
        env_var="VOICE_TRANSCRIPTION_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        min_value=1,
        max_value=120,
        label="Voice transcription: request timeout (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="voice_transcription_max_retries",
        env_var="VOICE_TRANSCRIPTION_MAX_RETRIES",
        value_type=SettingValueType.integer,
        default=1,
        min_value=0,
        max_value=3,
        label="Voice transcription: transient retry attempts",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_label",
        env_var="VLLM_LABEL",
        value_type=SettingValueType.string,
        default="primary",
        label="AI primary: provider label (audit/telemetry only)",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_base_url",
        env_var="VLLM_BASE_URL",
        value_type=SettingValueType.string,
        default=None,
        label="AI primary: OpenAI-compatible base URL",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_model",
        env_var="VLLM_MODEL",
        value_type=SettingValueType.string,
        default=None,
        label="AI primary: model name",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_api_key",
        env_var="VLLM_API_KEY",
        value_type=SettingValueType.string,
        default=None,
        label="AI primary: API key (literal or OpenBao reference)",
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_require_api_key",
        env_var="VLLM_REQUIRE_API_KEY",
        value_type=SettingValueType.boolean,
        default=False,
        label="AI primary: refuse to call the provider without an API key",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_timeout_seconds",
        env_var="VLLM_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        label="AI primary: request timeout (seconds)",
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_max_retries",
        env_var="VLLM_MAX_RETRIES",
        value_type=SettingValueType.integer,
        default=2,
        label="AI primary: retry attempts for transient failures",
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_max_tokens",
        env_var="VLLM_MAX_TOKENS",
        value_type=SettingValueType.integer,
        default=2048,
        label="AI primary: max response tokens",
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_secondary_label",
        env_var="VLLM_SECONDARY_LABEL",
        value_type=SettingValueType.string,
        default="secondary",
        label="AI secondary: provider label (audit/telemetry only)",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_secondary_base_url",
        env_var="VLLM_SECONDARY_BASE_URL",
        value_type=SettingValueType.string,
        default=None,
        label="AI secondary: OpenAI-compatible base URL (fallback endpoint)",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_secondary_model",
        env_var="VLLM_SECONDARY_MODEL",
        value_type=SettingValueType.string,
        default=None,
        label="AI secondary: model name",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_secondary_api_key",
        env_var="VLLM_SECONDARY_API_KEY",
        value_type=SettingValueType.string,
        default=None,
        label="AI secondary: API key (literal or OpenBao reference)",
        is_secret=True,
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_secondary_require_api_key",
        env_var="VLLM_SECONDARY_REQUIRE_API_KEY",
        value_type=SettingValueType.boolean,
        default=False,
        label="AI secondary: refuse to call the provider without an API key",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_secondary_timeout_seconds",
        env_var="VLLM_SECONDARY_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=30,
        label="AI secondary: request timeout (seconds)",
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_secondary_max_retries",
        env_var="VLLM_SECONDARY_MAX_RETRIES",
        value_type=SettingValueType.integer,
        default=1,
        label="AI secondary: retry attempts for transient failures",
        min_value=0,
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="vllm_secondary_max_tokens",
        env_var="VLLM_SECONDARY_MAX_TOKENS",
        value_type=SettingValueType.integer,
        default=2048,
        label="AI secondary: max response tokens",
        min_value=1,
    ),
    # Reverse-geocoding for location capture. Unset means no geocoder: the
    # reconciler reports every field unverifiable rather than failing a
    # capture, so this is safe to leave blank until the service is reachable.
    SettingSpec(
        domain=SettingDomain.integration,
        key="nominatim_base_url",
        env_var="NOMINATIM_BASE_URL",
        value_type=SettingValueType.string,
        default=None,
        label="Nominatim base URL (reverse-geocoding for location capture)",
    ),
    SettingSpec(
        domain=SettingDomain.integration,
        key="nominatim_timeout_seconds",
        env_var="NOMINATIM_TIMEOUT_SECONDS",
        value_type=SettingValueType.integer,
        default=5,
        label="Nominatim request timeout (seconds)",
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="channel_health_contracts",
        label="Sensitive channel health contracts",
        env_var=None,
        value_type=SettingValueType.json,
        default=DEFAULT_CHANNEL_HEALTH_CONTRACTS,
    ),
    # How long "remind me later" hides the location-confirmation prompt
    # (docs/designs/LOYALTY_AND_CAPTURE.md). Payment re-prompts regardless.
    SettingSpec(
        domain=SettingDomain.subscriber,
        key="loyalty_capture_prompt_snooze_days",
        env_var="LOYALTY_CAPTURE_PROMPT_SNOOZE_DAYS",
        value_type=SettingValueType.integer,
        default=30,
        label="Location prompt snooze (days)",
    ),
    # Customer-impact shields (outage/ticket) defer billing notices; this
    # bounds how long their evidence keeps suppressing before the normal
    # collections progression resumes (see billing_communication_policy).
    SettingSpec(
        domain=SettingDomain.billing,
        key="notice_shield_max_hours",
        env_var="NOTICE_SHIELD_MAX_HOURS",
        value_type=SettingValueType.integer,
        default=72,
        min_value=1,
        label="Billing notice shield expiry (hours)",
    ),
    # Scheduler booleans are decision inputs. Every mutable task gate must be
    # registered here or in control_registry; scheduler_config may not invent
    # an environment/database/default precedence path of its own.
    SettingSpec(
        domain=SettingDomain.billing,
        key="stale_overdue_lock_detect_enabled",
        env_var="STALE_OVERDUE_LOCK_DETECT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Stale overdue-lock detection",
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="cross_app_drift_detection_enabled",
        env_var="CROSS_APP_DRIFT_DETECTION_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Cross-application drift detection",
    ),
    # The request-audit middleware's five controls. They were writable through
    # `settings_api_custom` with NO spec at all — the custom handler validates
    # against its own `_AUDIT_SETTING_*` sets, so nothing required a
    # declaration. Declared here at their EXACT current shape (seed:
    # `seed_audit_settings`; runtime defaults: `app.main._default_audit_settings`)
    # so the two agree before the handler is routed through the spec.
    # `tests/architecture/test_audit_setting_defaults.py` pins that agreement.
    SettingSpec(
        domain=SettingDomain.audit,
        key="enabled",
        env_var=None,
        value_type=SettingValueType.boolean,
        default=True,
        label="Request audit logging",
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="methods",
        env_var=None,
        value_type=SettingValueType.list,
        default=["POST", "PUT", "PATCH", "DELETE"],
        label="Audited HTTP methods",
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="skip_paths",
        env_var=None,
        value_type=SettingValueType.list,
        default=["/static", "/web", "/health"],
        label="Path prefixes excluded from audit logging",
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="read_trigger_header",
        env_var=None,
        value_type=SettingValueType.string,
        default="x-audit-read",
        label="Header that opts a read request into audit logging",
    ),
    SettingSpec(
        domain=SettingDomain.audit,
        key="read_trigger_query",
        env_var=None,
        value_type=SettingValueType.string,
        default="audit",
        label="Query parameter that opts a read request into audit logging",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="cutover_balance_audit_enabled",
        env_var="BILLING_CUTOVER_BALANCE_AUDIT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Cutover balance invariant audit",
    ),
    SettingSpec(
        domain=SettingDomain.billing,
        key="funded_inactive_exposure_audit_enabled",
        env_var="BILLING_FUNDED_INACTIVE_EXPOSURE_AUDIT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Funded inactive exposure audit",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="ip_consistency_audit_enabled",
        env_var="RADIUS_IP_CONSISTENCY_AUDIT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="RADIUS IPv4 consistency audit",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="connectivity_shadow_audit_enabled",
        env_var="RADIUS_CONNECTIVITY_SHADOW_AUDIT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Connectivity shadow audit",
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="infra_availability_snapshot_enabled",
        env_var="INFRA_AVAILABILITY_SNAPSHOT_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Infrastructure availability snapshots",
    ),
    SettingSpec(
        domain=SettingDomain.network_monitoring,
        key="infra_availability_prune_enabled",
        env_var="INFRA_AVAILABILITY_PRUNE_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Infrastructure availability retention",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="operational_escalation_delivery_enabled",
        env_var="OPERATIONAL_ESCALATION_DELIVERY_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Operational escalation delivery",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="nextcloud_talk_staff_notifications_enabled",
        env_var="NEXTCLOUD_TALK_STAFF_NOTIFICATIONS_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
        label="Nextcloud Talk staff notifications",
    ),
    SettingSpec(
        domain=SettingDomain.catalog,
        key="nas_backup_retention_enabled",
        env_var="NAS_BACKUP_RETENTION_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="NAS backup retention cleanup",
    ),
    SettingSpec(
        domain=SettingDomain.comms,
        key="oauth_token_refresh_enabled",
        env_var="OAUTH_TOKEN_REFRESH_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="OAuth token refresh",
    ),
    SettingSpec(
        domain=SettingDomain.usage,
        key="bandwidth_processing_enabled",
        env_var="BANDWIDTH_PROCESSING_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Bandwidth observation processing",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="stale_infrastructure_check_enabled",
        env_var="STALE_INFRASTRUCTURE_CHECK_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Stale infrastructure observation check",
    ),
    SettingSpec(
        domain=SettingDomain.scheduler,
        key="event_old_cleanup_enabled",
        env_var="EVENT_OLD_CLEANUP_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Completed event retention cleanup",
    ),
    SettingSpec(
        domain=SettingDomain.radius,
        key="radius_sync_enabled",
        env_var="RADIUS_SYNC_ENABLED",
        value_type=SettingValueType.boolean,
        default=True,
        label="Configured RADIUS sync jobs",
    ),
    # Weekly NCC complaints digest email — default OFF. Sends a summary + a
    # link to the filing workbook (Monday 08:00 in the celery timezone).
    SettingSpec(
        domain=SettingDomain.notification,
        key="ncc_report_email_enabled",
        env_var="NCC_REPORT_EMAIL_ENABLED",
        value_type=SettingValueType.boolean,
        default=False,
    ),
    SettingSpec(
        # Service-owned idempotency cursor. This must be registered because
        # resolve_value deliberately ignores unregistered database keys.
        domain=SettingDomain.notification,
        key="ncc_report_email_last_sent_local_date",
        env_var=None,
        value_type=SettingValueType.string,
        default=None,
        label="NCC report email last sent date (managed)",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="ncc_report_email_lookback_days",
        env_var="NCC_REPORT_EMAIL_LOOKBACK_DAYS",
        value_type=SettingValueType.integer,
        default=7,
        min_value=1,
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="ncc_report_email_subject",
        env_var="NCC_REPORT_EMAIL_SUBJECT",
        value_type=SettingValueType.string,
        default="Weekly NCC Report",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="ncc_report_email_timezone",
        env_var="NCC_REPORT_EMAIL_TIMEZONE",
        value_type=SettingValueType.string,
        default="Africa/Lagos",
    ),
    SettingSpec(
        domain=SettingDomain.notification,
        key="ncc_report_email_to",
        env_var="NCC_REPORT_EMAIL_TO",
        value_type=SettingValueType.string,
        default="",
    ),
]

# These cadence and worker-tuning settings were previously resolved through
# scheduler-local call-site defaults. Registering them here makes the schema,
# default, coercion, and optional bootstrap environment name authoritative.
SETTINGS_SPECS.extend(
    [
        SettingSpec(
            domain=SettingDomain.billing,
            key="autopay_interval_seconds",
            env_var="BILLING_AUTOPAY_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=3600,
        ),
        SettingSpec(
            domain=SettingDomain.billing,
            key="cutover_balance_audit_interval_seconds",
            env_var="BILLING_CUTOVER_BALANCE_AUDIT_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=86400,
        ),
        SettingSpec(
            domain=SettingDomain.billing,
            key="funded_inactive_exposure_audit_interval_seconds",
            env_var="BILLING_FUNDED_INACTIVE_EXPOSURE_AUDIT_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=2592000,
        ),
        SettingSpec(
            domain=SettingDomain.billing,
            key="topup_reconciliation_interval_seconds",
            env_var="BILLING_TOPUP_RECONCILIATION_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=1800,
        ),
        SettingSpec(
            domain=SettingDomain.catalog,
            key="scheduled_plan_change_interval_seconds",
            env_var="SCHEDULED_PLAN_CHANGE_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=3600,
        ),
        SettingSpec(
            domain=SettingDomain.catalog,
            key="scheduled_status_change_interval_seconds",
            env_var="SCHEDULED_STATUS_CHANGE_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.catalog,
            key="vacation_hold_resume_interval_seconds",
            env_var="VACATION_HOLD_RESUME_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=3600,
        ),
        SettingSpec(
            domain=SettingDomain.collections,
            key="arrangement_check_interval_seconds",
            env_var="ARRANGEMENT_CHECK_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=86400,
        ),
        SettingSpec(
            domain=SettingDomain.integration,
            key="dotmac_erp_expense_status_refresh_interval_seconds",
            env_var="DOTMAC_ERP_EXPENSE_STATUS_REFRESH_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.integration,
            key="dotmac_erp_material_status_refresh_interval_seconds",
            env_var="DOTMAC_ERP_MATERIAL_STATUS_REFRESH_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.integration,
            key="dotmac_erp_outbox_interval_seconds",
            env_var="DOTMAC_ERP_OUTBOX_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=60,
        ),
        SettingSpec(
            domain=SettingDomain.integration,
            key="dotmac_erp_purchase_invoice_status_refresh_interval_seconds",
            env_var="DOTMAC_ERP_PURCHASE_INVOICE_STATUS_REFRESH_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.network,
            key="tr069_cleanup_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=86400,
        ),
        SettingSpec(
            domain=SettingDomain.network,
            key="tr069_genieacs_stale_cleanup_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=900,
        ),
        SettingSpec(
            domain=SettingDomain.network,
            key="tr069_health_check_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=7200,
        ),
        SettingSpec(
            domain=SettingDomain.network,
            key="tr069_metrics_scrape_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.network,
            key="tr069_ont_runtime_refresh_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=900,
        ),
        SettingSpec(
            domain=SettingDomain.network,
            key="tr069_sync_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=1800,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="channel_health_interval_seconds",
            env_var="CHANNEL_HEALTH_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=60,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="device_metrics_cleanup_interval_hours",
            env_var=None,
            value_type=SettingValueType.integer,
            default=6,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="device_projection_reconcile_interval_seconds",
            env_var="DEVICE_PROJECTION_RECONCILE_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=60,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="huawei_ont_status_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="infra_availability_prune_interval_seconds",
            env_var="INFRA_AVAILABILITY_PRUNE_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=86400,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="infra_availability_snapshot_interval_seconds",
            env_var="INFRA_AVAILABILITY_SNAPSHOT_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=86400,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="monitoring_coverage_interval_seconds",
            env_var="MONITORING_COVERAGE_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=600,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="monitoring_inventory_sync_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=600,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="nas_config_backup_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=3600,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="olt_backup_interval_hours",
            env_var=None,
            value_type=SettingValueType.integer,
            default=24,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="olt_health_retry_interval_minutes",
            env_var=None,
            value_type=SettingValueType.integer,
            default=5,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="ont_reconcile_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=900,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="router_config_backup_interval_hours",
            env_var=None,
            value_type=SettingValueType.integer,
            default=24,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="topology_lldp_poll_interval_minutes",
            env_var=None,
            value_type=SettingValueType.integer,
            default=60,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="topology_status_warm_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=180,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="topology_uisp_sync_interval_minutes",
            env_var=None,
            value_type=SettingValueType.integer,
            default=15,
        ),
        SettingSpec(
            domain=SettingDomain.network_monitoring,
            key="ufiber_onu_link_interval_minutes",
            env_var=None,
            value_type=SettingValueType.integer,
            default=60,
        ),
        SettingSpec(
            domain=SettingDomain.notification,
            key="operational_escalation_delivery_interval_seconds",
            env_var="OPERATIONAL_ESCALATION_DELIVERY_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=60,
        ),
        SettingSpec(
            domain=SettingDomain.provisioning,
            key="compensation_retry_interval_seconds",
            env_var="COMPENSATION_RETRY_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.radius,
            key="connectivity_shadow_audit_interval_seconds",
            env_var="RADIUS_CONNECTIVITY_SHADOW_AUDIT_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=21600,
        ),
        SettingSpec(
            domain=SettingDomain.radius,
            key="ip_consistency_audit_interval_seconds",
            env_var="RADIUS_IP_CONSISTENCY_AUDIT_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=21600,
        ),
        SettingSpec(
            domain=SettingDomain.radius,
            key="radius_sync_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="acs_task_soft_time_limit_seconds",
            env_var="CELERY_ACS_TASK_SOFT_TIME_LIMIT",
            value_type=SettingValueType.integer,
            default=240,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="acs_task_time_limit_seconds",
            env_var="CELERY_ACS_TASK_TIME_LIMIT",
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="event_old_cleanup_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=86400,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="event_retry_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="event_stale_cleanup_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=600,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="long_task_soft_time_limit_seconds",
            env_var="CELERY_LONG_TASK_SOFT_TIME_LIMIT",
            value_type=SettingValueType.integer,
            default=1740,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="long_task_time_limit_seconds",
            env_var="CELERY_LONG_TASK_TIME_LIMIT",
            value_type=SettingValueType.integer,
            default=1800,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="result_expires_seconds",
            env_var="CELERY_RESULT_EXPIRES",
            value_type=SettingValueType.integer,
            default=86400,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="stale_infrastructure_check_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=300,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="task_soft_time_limit_seconds",
            env_var="CELERY_TASK_SOFT_TIME_LIMIT",
            value_type=SettingValueType.integer,
            default=840,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="task_time_limit_seconds",
            env_var="CELERY_TASK_TIME_LIMIT",
            value_type=SettingValueType.integer,
            default=900,
        ),
        SettingSpec(
            domain=SettingDomain.scheduler,
            key="worker_prefetch_multiplier",
            env_var="CELERY_WORKER_PREFETCH_MULTIPLIER",
            value_type=SettingValueType.integer,
            default=1,
        ),
        SettingSpec(
            domain=SettingDomain.subscriber,
            key="quote_reconcile_interval_seconds",
            env_var=None,
            value_type=SettingValueType.integer,
            default=3600,
        ),
        SettingSpec(
            domain=SettingDomain.usage,
            key="device_login_sync_interval_seconds",
            env_var="DEVICE_LOGIN_SYNC_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=900,
        ),
        SettingSpec(
            domain=SettingDomain.usage,
            key="enforcement_reconciler_interval_seconds",
            env_var="ENFORCEMENT_RECONCILER_INTERVAL_SECONDS",
            value_type=SettingValueType.integer,
            default=600,
        ),
    ]
)

# Tombstone historical aliases whose decisions moved to canonical optional
# feature controls. Core customer-financial lifecycle settings are deleted from
# the registry instead of represented here.
_RETIRED_FEATURE_ALIAS_SPECS = frozenset(
    {
        (SettingDomain.gis, "sync_enabled"),
        (SettingDomain.usage, "radius_accounting_import_enabled"),
        (SettingDomain.usage, "radius_session_reap_enabled"),
        (SettingDomain.usage, "usage_warning_enabled"),
        (SettingDomain.usage, "fup_submonthly_rules_enabled"),
        (SettingDomain.scheduler, "crm_ticket_pull_enabled"),
        (SettingDomain.radius, "coa_enabled"),
        (SettingDomain.network, "mikrotik_session_kill_enabled"),
        (SettingDomain.network, "mikrotik_api_session_kick_enabled"),
        (SettingDomain.network, "address_list_block_enabled"),
        (SettingDomain.network, "olt_profile_sync_worker_enabled"),
        (SettingDomain.network, "wireguard_log_cleanup_enabled"),
        (SettingDomain.network, "wireguard_token_cleanup_enabled"),
        (SettingDomain.projects, "quotes_native_write_enabled"),
        (SettingDomain.projects, "quotes_native_read_enabled"),
    }
)
SETTINGS_SPECS = [
    spec
    for spec in SETTINGS_SPECS
    if (spec.domain, spec.key) not in _RETIRED_FEATURE_ALIAS_SPECS
]

DOMAIN_SETTINGS_SERVICE = {
    SettingDomain.auth: settings_service.auth_settings,
    SettingDomain.audit: settings_service.audit_settings,
    SettingDomain.billing: settings_service.billing_settings,
    SettingDomain.catalog: settings_service.catalog_settings,
    SettingDomain.subscriber: settings_service.subscriber_settings,
    SettingDomain.imports: settings_service.imports_settings,
    SettingDomain.notification: settings_service.notification_settings,
    SettingDomain.network: settings_service.network_settings,
    SettingDomain.network_monitoring: settings_service.network_monitoring_settings,
    SettingDomain.provisioning: settings_service.provisioning_settings,
    SettingDomain.geocoding: settings_service.geocoding_settings,
    SettingDomain.usage: settings_service.usage_settings,
    SettingDomain.radius: settings_service.radius_settings,
    SettingDomain.collections: settings_service.collections_settings,
    SettingDomain.lifecycle: settings_service.lifecycle_settings,
    SettingDomain.projects: settings_service.projects_settings,
    SettingDomain.inventory: settings_service.inventory_settings,
    SettingDomain.comms: settings_service.comms_settings,
    SettingDomain.tr069: settings_service.tr069_settings,
    SettingDomain.snmp: settings_service.snmp_settings,
    SettingDomain.bandwidth: settings_service.bandwidth_settings,
    SettingDomain.gis: settings_service.gis_settings,
    SettingDomain.scheduler: settings_service.scheduler_settings,
    SettingDomain.integration: settings_service.integration_settings,
}


#: Every spec by ``(domain, key)``. Built once, and construction is where a
#: duplicate declaration now DIES rather than hiding.
#:
#: `get_spec` used to linear-scan and return the FIRST match while `list_specs`
#: returned every match. Those two answers only agree while no key is declared
#: twice, and nothing checked — so when `notification.sms_max_length` was
#: declared twice, every resolver silently used the first spec, the admin
#: settings screen rendered the key twice, and the second declaration's bounds
#: read as authoritative while being dead. It took the kernel's registry, which
#: is a dict and therefore keeps the LAST, to make the disagreement visible.
#:
#: A dict removes the disagreement by construction: there is one answer, and a
#: second declaration cannot quietly become it.
_SPECS_BY_KEY: dict[tuple[str, str], SettingSpec] = {}
for _spec in SETTINGS_SPECS:
    _existing = _SPECS_BY_KEY.get((str(_spec.domain), _spec.key))
    if _existing is not None and _existing != _spec:
        raise RuntimeError(
            f"setting {_spec.domain}.{_spec.key} is declared twice with "
            "different definitions — the effective spec would depend on "
            "declaration order. Delete the dead declaration; do not merge "
            "them silently (tests/test_settings_kernel_parity.py"
            "::test_no_setting_is_declared_twice)"
        )
    _SPECS_BY_KEY[(str(_spec.domain), _spec.key)] = _spec
del _spec


def get_spec(domain: SettingDomain, key: str) -> SettingSpec | None:
    """The one spec for a key.

    A dict lookup rather than a scan over 574 specs, which also matters because
    this sits under every `resolve_value` call.
    """

    return _SPECS_BY_KEY.get((str(domain), key))


def list_specs(domain: SettingDomain) -> list[SettingSpec]:
    return [spec for spec in SETTINGS_SPECS if spec.domain == domain]


def resolve_value(db, domain: SettingDomain, key: str) -> Any:
    """Resolve a database-authoritative setting value.

    Delegates to `dotmac_kernel.settings_resolver`. Sub keeps this signature so
    that the 109 call sites do not change and this remains the one seam; what
    moved is WHO decides, not who is asked.

    Resolution is the kernel's: the operator tenant's row, then the platform
    row, then the spec default. Environment inputs are materialised into rows
    by the seed and are never consulted here — that is Sub's approved rule and
    the kernel's own (starter ADR-0011: settings resolution reads rows and
    defaults, never the environment).

    Caching is the KERNEL's, and no longer Sub's. `dotmac_kernel.settings_cache`
    owns the key, the scope segment, the TTL policy, what is never cached and
    what a write invalidates; Sub supplies only the Redis transport
    (`app/services/kernel_settings_cache_store.py`), installed at import of this
    module. The resolver consults that cache itself, which is why there is no
    cache call left in this function.

    The cache that used to sit here keyed on `settings:{domain}:{key}` with no
    scope segment — the cross-tenant leak the kernel's own module docstring
    cites `dotmac_erp` for. Adding a segment would have fixed the instance and
    kept the shape: a second key model, drifting. There is now one.
    """

    if get_spec(domain, key) is None:
        return None
    # The kernel's own member type, not a bare `str`: its signature asks for
    # one and its validation reads `.value`. Same conversion the bridge
    # makes for a spec's domain.
    return kernel_resolve_value(
        db,
        KernelSettingDomain(str(domain)),
        key,
        tenant_id=operator_tenant_id(),
    )


def resolve_boolean(db, domain: SettingDomain, key: str) -> bool:
    """Resolve a registered boolean without call-site fallback semantics."""

    spec = get_spec(domain, key)
    if spec is None or spec.value_type != SettingValueType.boolean:
        raise RuntimeError(f"Boolean setting must be registered: {domain.value}.{key}")
    value = resolve_value(db, domain, key)
    if not isinstance(value, bool):
        raise RuntimeError(
            f"Boolean setting resolved to a non-boolean: {domain.value}.{key}"
        )
    return value


def resolve_integer(db, domain: SettingDomain, key: str) -> int:
    """Resolve a registered integer without call-site fallback semantics."""

    spec = get_spec(domain, key)
    if spec is None or spec.value_type != SettingValueType.integer:
        raise RuntimeError(f"Integer setting must be registered: {domain.value}.{key}")
    value = resolve_value(db, domain, key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(
            f"Integer setting resolved to a non-integer: {domain.value}.{key}"
        )
    return value


def resolve_string(db, domain: SettingDomain, key: str) -> str:
    """Resolve a required registered string without call-site fallback semantics."""

    spec = get_spec(domain, key)
    if spec is None or spec.value_type != SettingValueType.string:
        raise RuntimeError(f"String setting must be registered: {domain.value}.{key}")
    value = resolve_value(db, domain, key)
    if not isinstance(value, str):
        raise RuntimeError(
            f"String setting resolved to a non-string: {domain.value}.{key}"
        )
    return value


def resolve_values_atomic(db, domain: SettingDomain, keys: list[str]) -> dict[str, Any]:
    """Read multiple database-authoritative settings in one pass.

    Delegates to `dotmac_kernel.settings_resolver.resolve_many`, which is the
    same thing this function used to do by hand: one query per scope level with
    `key IN (...)`, precedence applied in memory, then each value coerced and
    degraded to its default by the SAME `_finish` that `resolve_value` uses.

    What was here was a second copy of the resolution rules — coercion,
    `allowed`, the integer bounds, degrade-to-default — about fifty lines of
    them, beside a first copy in `resolve_value`. Two copies of a rule is the
    defect this whole cutover has been closing; this one was simply the largest.

    Behaviour preserved deliberately: a key with no registered spec, and a key
    that resolves to None, are OMITTED from the result rather than present with
    a null. Callers use `key in result` to mean "configured", so returning the
    key with None would flip that test.

    Missing rows resolve to their registered defaults. Environment inputs are
    bootstrap-only and are not consulted here.
    """

    if not keys:
        return {}

    resolved = kernel_resolve_many(
        db,
        KernelSettingDomain(str(domain)),
        keys,
        tenant_id=operator_tenant_id(),
    )
    return {
        key: value
        for key, value in resolved.items()
        if value is not None and get_spec(domain, key) is not None
    }


def extract_db_value(setting: "DomainSetting | None") -> object | None:
    if not setting:
        return None
    if setting.value_text is not None:
        return cast(object, setting.value_text)
    if setting.value_json is not None:
        return cast(object, setting.value_json)
    return None


def coerce_value(spec: SettingSpec, raw: object) -> tuple[object | None, str | None]:
    if raw is None:
        return None, None
    if spec.value_type == SettingValueType.boolean:
        if isinstance(raw, bool):
            return raw, None
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True, None
            if normalized in {"0", "false", "no", "off"}:
                return False, None
        return None, "Value must be boolean"
    if spec.value_type == SettingValueType.integer:
        if isinstance(raw, int):
            return raw, None
        if isinstance(raw, str):
            try:
                return int(raw), None
            except ValueError:
                return None, "Value must be an integer"
        return None, "Value must be an integer"
    if spec.value_type == SettingValueType.string:
        if isinstance(raw, str):
            value = raw
        else:
            value = str(raw)
        if spec.string_normalization is SettingStringNormalization.LOWERCASE:
            value = value.lower()
        return value, None
    if spec.value_type == SettingValueType.list:
        # A list setting reached through the settings form arrives as text. A
        # JSON array is parsed; anything else is read as a comma-separated
        # list, which is the established convention for these — the audit
        # `methods`/`skip_paths` handler did exactly this before the shape moved
        # to its owner. Without it, "POST, GET" would store as that string.
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return [], None
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    return None, "Value must be valid JSON"
                if not isinstance(parsed, list):
                    return None, "Value must be a JSON array"
                raw = parsed
            else:
                return [item.strip() for item in text.split(",") if item.strip()], None
        if isinstance(raw, tuple):
            raw = list(raw)
        if isinstance(raw, list):
            return [
                item.strip() if isinstance(item, str) else item
                for item in raw
                if not isinstance(item, str) or item.strip()
            ], None
        return None, "Value must be a list"
    if spec.value_type == SettingValueType.json:
        # An OBJECT, and only an object. The kernel's `json` type rejects
        # anything else, and a reader that cannot tell whether to expect
        # `.get(...)` or `[0]` is the ambiguity a value type exists to remove.
        # Sequences belong to `list` above.
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return {}, None
            try:
                parsed = json.loads(text)
            except ValueError:
                return None, "Value must be valid JSON"
            if not isinstance(parsed, dict):
                return None, "Value must be a JSON object"
            return parsed, None
        if isinstance(raw, dict):
            return raw, None
        return None, "Value must be a JSON object"
    return raw, None


def normalize_for_db(
    spec: SettingSpec, value: object
) -> tuple[str | None, object | None]:
    if spec.value_type == SettingValueType.boolean:
        # Both columns, deliberately. The seed writes both, the retired
        # per-domain handlers wrote both, and `_to_bool` in app.main reads
        # `value_json` first — leaving it NULL made a boolean row's shape depend
        # on which writer produced it.
        bool_value = bool(value)
        return ("true" if bool_value else "false"), bool_value
    if spec.value_type == SettingValueType.integer:
        parsed = _coerce_int_value(value)
        if parsed is None:
            # Should be prevented by validation in callers, but avoid crashing on bad inputs.
            return None, value
        return str(parsed), None
    if spec.value_type == SettingValueType.string:
        return str(value), None
    return None, value


# Register Sub's specs with the kernel registry at import time: a kernel read
# must never precede the declaration it depends on, and `resolve_value` above
# delegates to it. Imported HERE rather than at module top because the bridge
# reads `SETTINGS_SPECS` from this module — a top-level import would be a
# cycle, and by this line the specs exist.
from app.services.settings_kernel_bridge import (  # noqa: E402
    register_with_kernel,
)

register_with_kernel()

# Install the Redis transport for the KERNEL's settings cache, here and for the
# same reason as the line above: a resolution must not happen before the thing
# it depends on exists. An uninstalled store is not an error — the kernel's
# cache is simply inert — so a missed install shows up as a performance
# regression rather than a failure, which is how it would survive for months.
from app.services.kernel_settings_cache_store import (  # noqa: E402
    install as install_settings_cache_store,
)

install_settings_cache_store()
