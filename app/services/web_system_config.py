"""Service helpers for all configuration/settings pages (08_config features)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingUpdate
from app.services import settings_spec
from app.services.billing import collection_account_directory
from app.services.billing_settings import resolve_payment_due_days
from app.services.domain_settings import DomainSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Generic setting read / write helpers
# ---------------------------------------------------------------------------


def _read_settings(
    db: Session, domain: SettingDomain, keys: list[str]
) -> dict[str, str]:
    """Read a batch of DomainSettings, returning {key: value_text}."""
    stmt = (
        select(DomainSetting)
        .where(DomainSetting.domain == domain)
        .where(DomainSetting.key.in_(keys))
    )
    rows = db.scalars(stmt).all()
    result: dict[str, str] = dict.fromkeys(keys, "")
    for row in rows:
        result[row.key] = row.value_text or ""
    return result


def _save_settings(
    db: Session,
    domain: SettingDomain,
    data: Mapping[str, str],
    keys: list[str],
    *,
    secret_keys: set[str] | None = None,
    use_specs: bool = False,
) -> None:
    """Upsert a batch of DomainSettings."""
    secret_keys = secret_keys or set()
    spec_updates: list[tuple[str, DomainSettingUpdate]] = []
    if use_specs:
        for key in keys:
            spec = settings_spec.get_spec(domain, key)
            if not spec:
                continue
            value = _coerce_spec_form_value(spec, data.get(key))
            value_text, value_json = settings_spec.normalize_for_db(spec, value)
            setting_value_json = cast(dict | list | bool | int | str | None, value_json)
            spec_updates.append(
                (
                    key,
                    DomainSettingUpdate(
                        value_type=spec.value_type,
                        value_text=value_text,
                        value_json=setting_value_json,
                        is_secret=spec.is_secret or key in secret_keys,
                        is_active=True,
                    ),
                )
            )

    spec_keys = {key for key, _ in spec_updates}
    spec_service = DomainSettings(domain) if spec_updates else None
    if spec_service:
        for key, payload in spec_updates:
            spec_service.upsert_by_key(db, key, payload)

    for key in keys:
        if key in spec_keys:
            continue
        value = (data.get(key) or "").strip()
        stmt = select(DomainSetting).where(
            DomainSetting.domain == domain,
            DomainSetting.key == key,
        )
        setting = db.scalars(stmt).first()
        if setting:
            setting.value_text = value
            setting.is_secret = key in secret_keys
        else:
            setting = DomainSetting(
                domain=domain,
                key=key,
                value_text=value,
                value_type=SettingValueType.string,
                is_secret=key in secret_keys,
            )
            db.add(setting)
    db.flush()
    db.commit()


def _coerce_spec_form_value(
    spec: settings_spec.SettingSpec, raw_value: object
) -> object | None:
    raw = "" if raw_value is None else str(raw_value).strip()
    if raw == "" and spec.default is not None:
        raw_value = spec.default
    else:
        raw_value = raw

    value, error = settings_spec.coerce_value(spec, raw_value)
    label = spec.label or spec.key
    if error:
        raise ValueError(f"{label}: {error}.")
    if spec.required and (value is None or value == ""):
        raise ValueError(f"{label} is required.")
    if spec.allowed and value is not None and value not in spec.allowed:
        allowed = ", ".join(sorted(spec.allowed))
        raise ValueError(f"{label} must be one of: {allowed}.")
    if spec.value_type == SettingValueType.integer and value is not None:
        parsed = value if isinstance(value, int) else None
        if parsed is None:
            raise ValueError(f"{label}: Value must be an integer.")
        if spec.min_value is not None and parsed < spec.min_value:
            raise ValueError(f"{label} must be at least {spec.min_value}.")
        if spec.max_value is not None and parsed > spec.max_value:
            raise ValueError(f"{label} must be at most {spec.max_value}.")
    return value


# ---------------------------------------------------------------------------
# 8.5 System Preferences & Security
# ---------------------------------------------------------------------------
PREFERENCE_KEYS = [
    "admin_mfa_required",
]


def get_preferences_context(db: Session) -> dict:
    preferences = _read_settings(db, SettingDomain.auth, PREFERENCE_KEYS)
    if not preferences.get("admin_mfa_required"):
        legacy = _read_settings(db, SettingDomain.auth, ["force_2fa"])
        preferences["admin_mfa_required"] = legacy.get("force_2fa", "")
    return {"preferences": preferences}


def save_preferences(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.auth, data, PREFERENCE_KEYS, use_specs=True)


# 8.7 Subscriber Settings — REMOVED. The page's keys were not consumed by
# subscriber creation, welcome messaging, search, statistics, or portal auth.
# Real subscriber defaults live on their feature-specific forms/workflows.


# ---------------------------------------------------------------------------
# 8.8 Customer Portal Configuration
# ---------------------------------------------------------------------------
PORTAL_KEYS = [
    # Domain routing
    "selfcare_domain",
    "selfcare_redirect_root",
    "admin_domain",
    "reseller_domain",
]


def get_portal_config_context(db: Session) -> dict:
    return {"portal_settings": _read_settings(db, SettingDomain.auth, PORTAL_KEYS)}


def save_portal_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.auth, data, PORTAL_KEYS, use_specs=True)


# 8.10 Data Retention — REMOVED. The page's retention keys were inert: no cleanup
# task or reporting path consumed them. Real retention controls live on the
# feature-specific pages/tasks that enforce them, such as Monitoring, Bandwidth,
# Restore Tool, NAS Backups, and WireGuard logs.


# 8.11 Finance Automation — REMOVED. The page's toggles (auto_invoice_enabled,
# auto_blocking_enabled, deactivation_enabled, ip_reclamation_enabled, …) were
# inert: nothing read them. Billing automation is governed by the single control
# plane (control_registry / module_manager), so this dead, misleading config was
# deleted rather than left as a footgun that looks like it controls billing.


# ---------------------------------------------------------------------------
# 8.12 Billing & Invoice Settings
# ---------------------------------------------------------------------------
BILLING_KEYS = [
    "payment_period",
    "billing_day",
    "use_creation_date",
    "payment_due_days",
    "expiry_reminder_days",
    "invoice_reminder_days",
    "minimum_balance",
    "invoice_number_format",
    "receipt_number_format",
    "credit_note_format",
    "proforma_enabled",
    "proforma_generation_day",
    "proforma_payment_period",
    "zero_total_invoices",
    "invoice_caching",
    # Prepaid customer defaults
    "prepaid_default_billing_day",
    "prepaid_default_payment_due_days",
    "prepaid_default_grace_period_days",
    "prepaid_default_min_balance",
    "prepaid_enforcement_currency",
    # Postpaid customer defaults
    "postpaid_default_billing_day",
    "postpaid_default_payment_due_days",
    "postpaid_default_grace_period_days",
    "postpaid_default_min_balance",
]

DIRECT_BANK_TRANSFER_KEYS = [
    "direct_bank_transfer_instructions",
]


def get_billing_config_context(db: Session) -> dict:
    billing = _read_settings(db, SettingDomain.billing, BILLING_KEYS)
    if not billing.get("payment_due_days"):
        billing["payment_due_days"] = str(resolve_payment_due_days(db))
    defaults = {
        "expiry_reminder_days": "7",
        "invoice_reminder_days": "7,1",
        "minimum_balance": "0",
    }
    for key, value in defaults.items():
        if not billing.get(key):
            billing[key] = value
    return {"billing": billing}


def _normalize_choice(
    data: dict[str, Any],
    key: str,
    label: str,
    allowed_values: set[str],
) -> None:
    value = str(data.get(key) or "").strip().lower()
    if not value:
        return
    if value not in allowed_values:
        allowed = ", ".join(sorted(allowed_values))
        raise ValueError(f"{label} must be one of: {allowed}.")
    data[key] = value


def _normalize_bool_setting(data: dict[str, Any], key: str, label: str) -> None:
    value = str(data.get(key) or "").strip().lower()
    if not value:
        return
    if value not in {"true", "false"}:
        raise ValueError(f"{label} must be true or false.")
    data[key] = value


def _normalize_int_setting(
    data: dict[str, Any],
    key: str,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    value = str(data.get(key) or "").strip()
    if not value:
        return
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    data[key] = str(parsed)


def _normalize_decimal_setting(
    data: dict[str, Any],
    key: str,
    label: str,
    *,
    minimum: Decimal,
) -> None:
    value = str(data.get(key) or "").strip()
    if not value:
        return
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a valid decimal value.") from exc
    if parsed < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    data[key] = format(parsed, "f")


def _normalize_csv_days(data: dict[str, Any], key: str, label: str) -> None:
    value = str(data.get(key) or "").strip()
    if not value:
        return
    parts = [part.strip() for part in value.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{label} must be a comma-separated list of day numbers.")
    normalized: list[str] = []
    for part in parts:
        try:
            parsed = int(part)
        except ValueError as exc:
            raise ValueError(
                f"{label} must be a comma-separated list of day numbers."
            ) from exc
        if parsed < 0 or parsed > 3650:
            raise ValueError(f"{label} values must be between 0 and 3650.")
        normalized.append(str(parsed))
    data[key] = ",".join(normalized)


def _normalized_billing_config(data: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    for key, label in (
        ("use_creation_date", "Use Customer Creation Date"),
        ("proforma_enabled", "Proforma Invoices"),
        ("zero_total_invoices", "Zero-Total Invoices"),
        ("invoice_caching", "Invoice PDF Caching"),
    ):
        _normalize_bool_setting(normalized, key, label)

    _normalize_choice(
        normalized,
        "payment_period",
        "Payment Period",
        {"monthly", "quarterly", "annual"},
    )
    _normalize_choice(
        normalized,
        "proforma_payment_period",
        "Proforma Payment Period",
        {"monthly", "quarterly", "annual"},
    )

    for key, label in (
        ("billing_day", "Billing Day"),
        ("prepaid_default_billing_day", "Prepaid Default Billing Day"),
        ("postpaid_default_billing_day", "Postpaid Default Billing Day"),
    ):
        _normalize_int_setting(normalized, key, label, minimum=1, maximum=28)

    for key, label in (
        ("payment_due_days", "Payment Due Days"),
        ("expiry_reminder_days", "Expiry Reminder Days"),
        ("proforma_generation_day", "Proforma Generation Day"),
        ("prepaid_default_payment_due_days", "Prepaid Default Payment Due Days"),
        ("prepaid_default_grace_period_days", "Prepaid Default Grace Period Days"),
        ("postpaid_default_payment_due_days", "Postpaid Default Payment Due Days"),
        ("postpaid_default_grace_period_days", "Postpaid Default Grace Period Days"),
    ):
        _normalize_int_setting(normalized, key, label, minimum=0, maximum=3650)

    for key, label in (
        ("minimum_balance", "Minimum Balance"),
        ("prepaid_default_min_balance", "Prepaid Default Minimum Balance"),
        ("postpaid_default_min_balance", "Postpaid Default Minimum Balance"),
    ):
        _normalize_decimal_setting(normalized, key, label, minimum=Decimal("0"))

    _normalize_csv_days(normalized, "invoice_reminder_days", "Invoice Reminder Days")
    return normalized


def save_billing_config(db: Session, data: Mapping[str, Any]) -> None:
    # ``_normalized_billing_config`` still performs the canonicalisation (lower-
    # casing enum choices, formatting decimals, validating CSV day-lists) and
    # ``use_specs`` layers spec-based type coercion/validation on top for the
    # keys that have a registered spec. Keys without a spec (payment_period,
    # billing_day, use_creation_date, the
    # invoice/receipt/credit-note number formats, and the proforma_* / zero_
    # total_invoices / invoice_caching toggles) are intentionally NOT spec'd:
    # nothing in the app reads them, so registering specs would create orphans.
    # They keep their prior raw-string behaviour via the fall-through path.
    _save_settings(
        db,
        SettingDomain.billing,
        _normalized_billing_config(data),
        BILLING_KEYS,
        use_specs=True,
    )


def get_direct_bank_transfer_context(db: Session) -> dict:
    """Instructions copy from settings; accounts from their owner, read-only.

    The accounts are shown here so staff can see what customers will be told,
    but they are edited at /admin/settings/billing/collection-accounts. Editing them in
    two places is what produced four divergent copies of the same bank account.
    """
    settings = _read_settings(db, SettingDomain.billing, DIRECT_BANK_TRANSFER_KEYS)
    return {
        "direct_bank_transfer": settings,
        "collection_accounts": collection_account_directory.enabled_transfer_accounts(
            db
        ),
        "collection_accounts_url": "/admin/settings/billing/collection-accounts",
    }


def save_direct_bank_transfer_config(db: Session, data: Mapping[str, Any]) -> None:
    """Persist the instructions copy only.

    This used to also write `direct_bank_transfer_accounts` and the legacy
    singular `direct_bank_transfer_*` keys. Nothing reads them any more —
    presentment resolves through `collection_accounts` — so continuing to write
    them would silently accept staff edits that never reach a customer.
    """
    payload = {
        "direct_bank_transfer_instructions": data.get(
            "direct_bank_transfer_instructions", ""
        ),
    }
    _save_settings(
        db,
        SettingDomain.billing,
        payload,
        ["direct_bank_transfer_instructions"],
        use_specs=True,
    )


# ---------------------------------------------------------------------------
# 8.13 Payment Methods (read-only list from models)
# ---------------------------------------------------------------------------


def get_payment_methods_context(db: Session) -> dict:
    """List payment methods/providers."""
    try:
        from app.models.billing import PaymentProvider

        stmt = select(PaymentProvider).order_by(PaymentProvider.name)
        methods = list(db.scalars(stmt).all())
    except Exception:
        logger.debug("PaymentProvider model not available")
        methods = []
    return {"payment_methods": methods}


# ---------------------------------------------------------------------------
# 8.18 Tax Configuration
# ---------------------------------------------------------------------------


def get_tax_config_context(db: Session) -> dict:
    """List tax rates."""
    try:
        from app.models.billing import TaxRate

        stmt = select(TaxRate).order_by(TaxRate.name)
        rates = list(db.scalars(stmt).all())
    except Exception:
        logger.debug("TaxRate model not available")
        rates = []
    return {"tax_rates": rates}


# ---------------------------------------------------------------------------
# 8.16 Billing Reminders
# ---------------------------------------------------------------------------
# Delivery channels are NOT configured here. ``notification_channel_policy`` is
# the single owner of channel selection; these keys only carry scheduling,
# copy and filtering.
REMINDER_KEYS = [
    "reminders_enabled",
    "reminder_send_time",
    "wave1_days",
    "wave1_subject",
    "wave1_email_template",
    "wave1_sms_template",
    "wave2_days",
    "wave2_subject",
    "wave2_email_template",
    "wave2_sms_template",
    "wave3_days",
    "wave3_subject",
    "wave3_email_template",
    "wave3_sms_template",
    "attach_invoices",
    "payment_method_filter",
]


def get_reminders_context(db: Session) -> dict:
    return {"reminders": _read_settings(db, SettingDomain.collections, REMINDER_KEYS)}


def save_reminders(db: Session, data: Mapping[str, Any]) -> None:
    # Routed through the spec path for consistency with the other settings
    # saves. None of the REMINDER_KEYS currently has a runtime reader, so none
    # is registered as a spec (registering them would create orphans); they all
    # take the raw-string fall-through until a consumer exists.
    _save_settings(db, SettingDomain.collections, data, REMINDER_KEYS, use_specs=True)


# ---------------------------------------------------------------------------
# 8.17 Billing Notifications
# ---------------------------------------------------------------------------
# As with REMINDER_KEYS: no channel keys here. See
# ``app.services.notification_channel_policy``.
BILLING_NOTIF_KEYS = [
    "billing_notif_send_hour",
    "blocking_wave_enabled",
    "blocking_wave_email_template",
    "blocking_wave_sms_template",
    "blocking_wave_bcc",
    "pre_block_wave1_enabled",
    "pre_block_wave1_days",
    "pre_block_wave1_email_template",
    "pre_block_wave1_sms_template",
    "pre_block_wave2_enabled",
    "pre_block_wave2_days",
    "pre_block_wave2_email_template",
    "pre_block_wave2_sms_template",
]


def get_billing_notifications_context(db: Session) -> dict:
    return {
        "billing_notif": _read_settings(
            db, SettingDomain.collections, BILLING_NOTIF_KEYS
        )
    }


def save_billing_notifications(db: Session, data: Mapping[str, Any]) -> None:
    # ``billing_notif_send_hour`` has a spec (integer 0-23, read by the
    # enforcement-window gate) and now gets spec type coercion/validation. The
    # blocking-wave / pre-block-wave keys have no runtime reader, so they are
    # deliberately left un-spec'd (avoiding orphans) and keep the raw-string
    # fall-through.
    _save_settings(
        db, SettingDomain.collections, data, BILLING_NOTIF_KEYS, use_specs=True
    )


# ---------------------------------------------------------------------------
# 8.19 Plan Change Configuration
# ---------------------------------------------------------------------------
PLAN_CHANGE_KEYS = [
    "refund_policy",
    "upgrade_fee",
    "downgrade_fee",
    "fee_tax_rate",
    "invoice_timing",
    "prepaid_rollover",
    "discount_transfer",
    "minimum_invoice_amount",
]


def get_plan_change_context(db: Session) -> dict:
    return {"plan_change": _read_settings(db, SettingDomain.billing, PLAN_CHANGE_KEYS)}


def save_plan_change(db: Session, data: Mapping[str, Any]) -> None:
    normalized = dict(data)
    refund_policy = str(normalized.get("refund_policy") or "").strip().lower()
    invoice_timing = str(normalized.get("invoice_timing") or "").strip().lower()
    prepaid_rollover = str(normalized.get("prepaid_rollover") or "").strip().lower()
    discount_transfer = str(normalized.get("discount_transfer") or "").strip().lower()

    allowed_refund_policies = {"none", "prorated", "full_within_days"}
    allowed_invoice_timing = {"immediate", "next_invoice"}
    allowed_booleanish = {"true", "false"}

    if refund_policy not in allowed_refund_policies:
        raise ValueError(
            "Refund Policy must be one of: none, prorated, full_within_days."
        )
    if invoice_timing not in allowed_invoice_timing:
        raise ValueError("Invoice Timing must be either immediate or next_invoice.")
    if prepaid_rollover not in allowed_booleanish:
        raise ValueError("Prepaid Rollover must be true or false.")
    if discount_transfer not in allowed_booleanish:
        raise ValueError("Discount Transfer must be true or false.")

    for key in (
        "upgrade_fee",
        "downgrade_fee",
        "fee_tax_rate",
        "minimum_invoice_amount",
    ):
        value = str(normalized.get(key) or "").strip()
        if not value:
            continue
        try:
            Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(
                f"{key.replace('_', ' ').title()} must be a valid decimal value."
            ) from exc

    normalized["refund_policy"] = refund_policy
    normalized["invoice_timing"] = invoice_timing
    normalized["prepaid_rollover"] = prepaid_rollover
    normalized["discount_transfer"] = discount_transfer
    # Every PLAN_CHANGE_KEYS entry is spec-registered. The lowercase
    # canonicalisation above is preserved so the coerced enum values match the
    # specs' ``allowed`` sets (refund_policy / invoice_timing) and the boolean
    # coercion (prepaid_rollover / discount_transfer); use_specs then applies
    # the spec typing/validation.
    _save_settings(
        db, SettingDomain.billing, normalized, PLAN_CHANGE_KEYS, use_specs=True
    )


# ---------------------------------------------------------------------------
# 8.20 RADIUS Configuration
# ---------------------------------------------------------------------------
RADIUS_KEYS = [
    "reject_ip_not_found",
    "reject_ip_blocked",
    "reject_ip_negative",
    "reject_ip_bad_mac",
    "reject_ip_bad_password",
    "nas_type_default",
    "debug_enabled",
    "debug_level",
    "debug_auto_off_minutes",
    "mac_binding_enabled",
    "max_mac_addresses",
    "ip_pool_location_linking",
    "periodic_restart_enabled",
    "restart_frequency",
    "restart_time",
    "captive_redirect_enabled",
    "captive_portal_ip",
    "captive_portal_url",
    "allow_unknown_nas",
    "default_nas_id",
    # PPPoE auto-generation
    "pppoe_username_prefix",
    "pppoe_username_padding",
    "pppoe_username_start",
    "pppoe_default_password_length",
]


def get_radius_config_context(db: Session) -> dict:
    return {"radius": _read_settings(db, SettingDomain.radius, RADIUS_KEYS)}


def save_radius_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(db, SettingDomain.radius, data, RADIUS_KEYS, use_specs=True)


# 8.22 CPE Configuration — REMOVED. These QoS/blocking/DHCP/WLAN defaults had
# no runtime consumers. Real CPE inventory, TR-069, and device API controls live
# under Network > CPEs and on the CPE forms/details.


# ---------------------------------------------------------------------------
# 8.23 Monitoring Configuration
# ---------------------------------------------------------------------------
MONITORING_KEYS = [
    "monitoring_vendors",
    "monitoring_device_types",
    "monitoring_groups",
    "server_health_disk_warn_pct",
    "server_health_disk_crit_pct",
    "server_health_mem_warn_pct",
    "server_health_mem_crit_pct",
    "server_health_load_warn",
    "server_health_load_crit",
    "network_health_warn_pct",
    "network_health_crit_pct",
    "device_metrics_retention_days",
    "alert_evaluation_interval_seconds",
    "ont_signal_warning_dbm",
    "ont_signal_critical_dbm",
    "ont_signal_alert_cooldown_minutes",
    "interface_walk_interval_seconds",
    "interface_discovery_interval_seconds",
    "hot_retention_hours",
    "cleanup_interval_seconds",
]


def get_monitoring_config_context(db: Session) -> dict:
    settings = _read_settings(
        db,
        SettingDomain.network_monitoring,
        [
            "monitoring_vendors",
            "monitoring_device_types",
            "monitoring_groups",
            "server_health_disk_warn_pct",
            "server_health_disk_crit_pct",
            "server_health_mem_warn_pct",
            "server_health_mem_crit_pct",
            "server_health_load_warn",
            "server_health_load_crit",
            "network_health_warn_pct",
            "network_health_crit_pct",
            "device_metrics_retention_days",
            "alert_evaluation_interval_seconds",
            "ont_signal_warning_dbm",
            "ont_signal_critical_dbm",
            "ont_signal_alert_cooldown_minutes",
        ],
    )
    settings.update(
        _read_settings(
            db,
            SettingDomain.snmp,
            [
                "interface_walk_interval_seconds",
                "interface_discovery_interval_seconds",
            ],
        )
    )
    settings.update(
        _read_settings(
            db,
            SettingDomain.bandwidth,
            [
                "hot_retention_hours",
                "cleanup_interval_seconds",
            ],
        )
    )
    # Provide defaults for list-type settings
    if not settings.get("monitoring_vendors"):
        settings["monitoring_vendors"] = (
            "MikroTik,Cisco,Ubiquiti,Huawei,TP-Link,Juniper,D-Link,Ericsson"
        )
    if not settings.get("monitoring_device_types"):
        settings["monitoring_device_types"] = (
            "Router,Switch,Server,Access Point,CPE,OLT,ONT,Firewall"
        )
    if not settings.get("monitoring_groups"):
        settings["monitoring_groups"] = "Core Infrastructure,Access Layer,Customer CPE"
    defaults = {
        "server_health_disk_warn_pct": "80",
        "server_health_disk_crit_pct": "90",
        "server_health_mem_warn_pct": "80",
        "server_health_mem_crit_pct": "90",
        "server_health_load_warn": "1.0",
        "server_health_load_crit": "1.5",
        "network_health_warn_pct": "90",
        "network_health_crit_pct": "70",
        "device_metrics_retention_days": "90",
        "alert_evaluation_interval_seconds": "60",
        "ont_signal_warning_dbm": "-25",
        "ont_signal_critical_dbm": "-28",
        "ont_signal_alert_cooldown_minutes": "30",
        "interface_walk_interval_seconds": "300",
        "interface_discovery_interval_seconds": "3600",
        "hot_retention_hours": "24",
        "cleanup_interval_seconds": "3600",
    }
    for key, value in defaults.items():
        if not settings.get(key):
            settings[key] = value
    return {"monitoring": settings}


def save_monitoring_config(db: Session, data: Mapping[str, Any]) -> None:
    _save_settings(
        db,
        SettingDomain.network_monitoring,
        data,
        [
            "monitoring_vendors",
            "monitoring_device_types",
            "monitoring_groups",
            "server_health_disk_warn_pct",
            "server_health_disk_crit_pct",
            "server_health_mem_warn_pct",
            "server_health_mem_crit_pct",
            "server_health_load_warn",
            "server_health_load_crit",
            "network_health_warn_pct",
            "network_health_crit_pct",
            "device_metrics_retention_days",
            "alert_evaluation_interval_seconds",
            "ont_signal_warning_dbm",
            "ont_signal_critical_dbm",
            "ont_signal_alert_cooldown_minutes",
        ],
        use_specs=True,
    )
    _save_settings(
        db,
        SettingDomain.snmp,
        data,
        [
            "interface_walk_interval_seconds",
            "interface_discovery_interval_seconds",
        ],
        use_specs=True,
    )
    _save_settings(
        db,
        SettingDomain.bandwidth,
        data,
        [
            "hot_retention_hours",
            "cleanup_interval_seconds",
        ],
        use_specs=True,
    )


# 8.25 Fair Usage Policy — REMOVED. These global reset/threshold fields were
# not consumed by enforcement, usage metering, or notifications. Live FUP
# controls are managed per catalog offer via the catalog FUP policy UI.


# ---------------------------------------------------------------------------
# 8.26 NAS Types
# ---------------------------------------------------------------------------
NAS_TYPE_KEYS = ["nas_types_list"]


def get_nas_types_context(db: Session) -> dict:
    settings = _read_settings(db, SettingDomain.network, NAS_TYPE_KEYS)
    if not settings.get("nas_types_list"):
        settings["nas_types_list"] = (
            "MikroTik (API: Yes)|Cisco|Ericsson|Linux PPPD|Ubiquiti|D-Link|Juniper|"
            "Cisco IOS|Cisco IOS XE|netElastic"
        )
    types = []
    for entry in settings["nas_types_list"].split("|"):
        entry = entry.strip()
        if entry:
            has_api = "(API: Yes)" in entry
            name = entry.replace("(API: Yes)", "").replace("(API: No)", "").strip()
            types.append({"name": name, "has_api": has_api})
    return {"nas_types": types, "nas_types_raw": settings["nas_types_list"]}


# 8.27 IPv6 Configuration — REMOVED. These defaults were not consumed by IPAM,
# subscriber provisioning, or ONT service-intent code.


def get_templates_context(db: Session) -> dict:
    """Return notification template library context from persisted templates."""
    from app.models.notification import NotificationChannel, NotificationTemplate

    rows = (
        db.query(NotificationTemplate)
        .order_by(NotificationTemplate.name.asc(), NotificationTemplate.channel.asc())
        .all()
    )
    templates = [
        {
            "name": template.name,
            "code": template.code,
            "channel": template.channel.value
            if hasattr(template.channel, "value")
            else str(template.channel),
            "subject": template.subject,
            "is_active": template.is_active,
        }
        for template in rows
    ]
    return {
        "templates_list": templates,
        "channels": [channel.value for channel in NotificationChannel],
    }


# ---------------------------------------------------------------------------
# Automated outage notification (ADR 0004)
# ---------------------------------------------------------------------------
# One consolidated config: the master enable/disable first, then the tuning
# knobs it governs. Presenting them together is the point — the thresholds are
# meaningless without knowing whether automation is armed, and an operator
# reaching for the kill switch mid-incident should not have to hunt for it.
OUTAGE_AUTO_NOTIFY_KEYS = [
    "outage_auto_notify_enabled",
    "outage_auto_notify_dry_run",
    "outage_auto_notify_settle_minutes",
    "outage_auto_notify_min_affected",
    "outage_auto_notify_max_incidents_per_run",
    "outage_auto_notify_interval_seconds",
]


def get_outage_auto_notify_context(db: Session) -> dict:
    """Resolved values plus the operating state the page leads with."""
    resolved = {
        key: settings_spec.resolve_value(db, SettingDomain.network_monitoring, key)
        for key in OUTAGE_AUTO_NOTIFY_KEYS
    }
    enabled = bool(resolved.get("outage_auto_notify_enabled"))
    dry_run = bool(resolved.get("outage_auto_notify_dry_run"))
    if not enabled:
        state, tone = "Disabled", "muted"
        summary = "Automation is off. Outage notifications are operator-only."
    elif dry_run:
        state, tone = "Dry run", "warning"
        summary = (
            "Automation selects and logs recipients but sends nothing. "
            "This is the verification phase from ADR 0004."
        )
    else:
        state, tone = "Live", "active"
        summary = "Automation is sending outage notifications to customers."
    return {
        "outage_auto_notify": resolved,
        "outage_auto_notify_state": state,
        "outage_auto_notify_tone": tone,
        "outage_auto_notify_summary": summary,
    }


def save_outage_auto_notify(db: Session, data: Mapping[str, Any]) -> None:
    """Persist the consolidated config through the registered specs.

    Every key has a spec, so bounds (minimum settling window, minimum interval)
    are enforced by ``settings_spec`` rather than re-implemented here. Unchecked
    HTML checkboxes are absent from the payload, so they are normalized to
    ``false`` instead of being read as "leave as-is".
    """
    payload = dict(data)
    for key in ("outage_auto_notify_enabled", "outage_auto_notify_dry_run"):
        payload[key] = (
            "true"
            if str(payload.get(key, "")).lower()
            in {
                "true",
                "1",
                "on",
                "yes",
            }
            else "false"
        )
    _save_settings(
        db,
        SettingDomain.network_monitoring,
        payload,
        OUTAGE_AUTO_NOTIFY_KEYS,
        use_specs=True,
    )
