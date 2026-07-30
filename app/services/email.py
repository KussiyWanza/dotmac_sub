import html
import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import (
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
    NotificationStatus,
)
from app.models.subscription_engine import SettingValueType
from app.schemas.notification import NotificationCreate
from app.schemas.settings import DomainSettingUpdate
from app.services.branding_config import get_brand
from app.services.domain_settings import notification_settings
from app.services.notification import notifications as notification_records
from app.services.settings_spec import resolve_value

logger = logging.getLogger(__name__)


def _resolve_secret_value(value: str | None) -> str | None:
    """Resolve OpenBao/env secret refs while preserving plain values."""
    if not value:
        return value
    try:
        from app.services.secrets import resolve_secret

        resolved = resolve_secret(value)
        return resolved if resolved else None
    except Exception:
        return value


def _bao_secret(path: str, field: str) -> str | None:
    """Resolve a secret from OpenBao, returning None on failure."""
    try:
        from app.services.secrets import get_secret

        val = get_secret(path, field)
        return val if val else None
    except Exception:
        return None


SMTP_DEFAULT_SENDER_KEY_SETTING = "smtp_default_sender_key"
SMTP_SENDER_KEY_PREFIX = "smtp_sender."
SMTP_ACTIVITY_KEY_PREFIX = "smtp_activity_sender."
SMTP_SENDER_ALLOWED_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
SMTP_ACTIVITY_CHOICES: list[tuple[str, str]] = [
    ("notification_queue", "Notification Queue"),
    ("notification_test", "Notification Template Tests"),
    ("billing_invoice", "Billing Invoices"),
    ("billing_payment_receipt", "Billing Payment Receipts"),
    ("support_ticket", "Support Tickets"),
    ("project_update", "Project Updates"),
    ("field_service", "Field Service Work Orders"),
    ("operations", "Operations"),
    ("subscription_welcome", "Subscription Welcome"),
    ("auth_password_reset", "Password Reset"),
    ("auth_email_verification", "Email Verification"),
    ("auth_user_invite", "User Invite"),
    ("observability_smtp_probe", "Inbound SMTP Health Probe"),
]

DOTMAC_RED = "#FF0000"
DOTMAC_GREEN = "#008000"
DOTMAC_WHITE = "#F4F4F9"
DOTMAC_BUTTON_TEXT = "#ffffff"


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    body_html: str
    body_text: str


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return None
    return value


def _env_int(name: str, default: int) -> int:
    raw = _env_value(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_value(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _setting_bool(db: Session | None, key: str, default: bool) -> bool:
    value = _setting_value(db, key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting_value(db: Session | None, key: str) -> str | None:
    return _setting_value_for_domain(db, SettingDomain.notification, key)


def _setting_value_for_domain(
    db: Session | None,
    domain: SettingDomain,
    key: str,
) -> str | None:
    if db is None:
        return None
    setting = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == domain)
        .filter(DomainSetting.key == key)
        .filter(DomainSetting.is_active.is_(True))
        .first()
    )
    if not setting:
        return None
    if setting.value_text:
        return str(setting.value_text)
    if setting.value_json is not None:
        return str(setting.value_json)
    return None


def _legacy_smtp_config(db: Session | None) -> dict:
    username = (
        _env_value("SMTP_USERNAME")
        or _env_value("SMTP_USER")
        or _setting_value(db, "smtp_username")
    )
    from_email = (
        _env_value("SMTP_FROM_EMAIL")
        or _env_value("SMTP_FROM")
        or _setting_value(db, "smtp_from_email")
        or "noreply@example.com"
    )
    default_tls = _setting_bool(db, "smtp_use_tls", True)
    default_ssl = _setting_bool(db, "smtp_use_ssl", False)
    use_tls = _env_bool("SMTP_USE_TLS", _env_bool("SMTP_TLS", default_tls))
    use_ssl = _env_bool("SMTP_USE_SSL", _env_bool("SMTP_SSL", default_ssl))
    return {
        "host": _env_value("SMTP_HOST")
        or _setting_value(db, "smtp_host")
        or "localhost",
        "port": _env_int("SMTP_PORT", 587),
        "username": username,
        "password": _resolve_secret_value(
            _env_value("SMTP_PASSWORD") or _setting_value(db, "smtp_password")
        )
        or _bao_secret("notifications", "smtp_password"),
        "use_tls": use_tls,
        "use_ssl": use_ssl,
        "from_email": from_email,
        "from_name": _env_value("SMTP_FROM_NAME")
        or _setting_value(db, "smtp_from_name")
        or get_brand()["from_name"],
        "user": username,
        "from_addr": from_email,
    }


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _coerce_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _setting_raw_value(setting: DomainSetting) -> Any:
    if setting.value_text is not None:
        return setting.value_text
    return setting.value_json


def _sender_setting_key(sender_key: str, field: str) -> str:
    return f"{SMTP_SENDER_KEY_PREFIX}{sender_key}.{field}"


def _valid_sender_key(sender_key: str) -> bool:
    normalized = sender_key.strip().lower()
    if not normalized:
        return False
    return all(ch in SMTP_SENDER_ALLOWED_KEY_CHARS for ch in normalized)


def list_smtp_senders(db: Session | None) -> list[dict[str, Any]]:
    if db is None:
        return []
    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.is_active.is_(True))
        .filter(DomainSetting.key.like(f"{SMTP_SENDER_KEY_PREFIX}%"))
        .all()
    )

    senders: dict[str, dict[str, Any]] = {}
    for setting in settings:
        parts = setting.key.split(".", 2)
        if len(parts) != 3:
            continue
        _, sender_key, field = parts
        if not sender_key:
            continue
        profile = senders.setdefault(
            sender_key,
            {
                "sender_key": sender_key,
                "host": "",
                "port": 587,
                "username": "",
                "password": None,  # nosec
                "has_password": False,  # nosec
                "from_email": "",
                "from_name": get_brand()["from_name"],
                "use_tls": True,
                "use_ssl": False,
                "is_active": True,
            },
        )
        raw = _setting_raw_value(setting)
        if field == "password":
            profile["has_password"] = bool(raw)
            continue
        if field == "port":
            profile["port"] = _coerce_int(raw, 587)
        elif field in {"use_tls", "use_ssl", "is_active"}:
            profile[field] = _coerce_bool(raw, field != "use_ssl")
        elif field in {"host", "username", "from_email", "from_name"}:
            profile[field] = "" if raw is None else str(raw)

    return [senders[key] for key in sorted(senders.keys())]


def get_default_smtp_sender_key(db: Session | None) -> str:
    if db is None:
        return "default"
    raw = _setting_value(db, SMTP_DEFAULT_SENDER_KEY_SETTING)
    key = (raw or "").strip().lower()
    return key or "default"


def get_smtp_activity_map(db: Session | None) -> dict[str, str]:
    if db is None:
        return {}
    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.is_active.is_(True))
        .filter(DomainSetting.key.like(f"{SMTP_ACTIVITY_KEY_PREFIX}%"))
        .all()
    )
    activity_map: dict[str, str] = {}
    for setting in settings:
        activity = setting.key.replace(SMTP_ACTIVITY_KEY_PREFIX, "", 1).strip()
        if not activity:
            continue
        value = _setting_raw_value(setting)
        if value is None:
            continue
        sender_key = str(value).strip().lower()
        if sender_key:
            activity_map[activity] = sender_key
    return activity_map


def set_default_smtp_sender_key(db: Session, sender_key: str) -> None:
    notification_settings.upsert_by_key(
        db,
        SMTP_DEFAULT_SENDER_KEY_SETTING,
        DomainSettingUpdate(
            value_type=SettingValueType.string,
            value_text=sender_key.strip().lower(),
            is_secret=False,
            is_active=True,
        ),
    )


def upsert_smtp_activity_mapping(
    db: Session, activity: str, sender_key: str | None
) -> None:
    key = f"{SMTP_ACTIVITY_KEY_PREFIX}{activity.strip()}"
    normalized = (sender_key or "").strip().lower()
    if normalized:
        notification_settings.upsert_by_key(
            db,
            key,
            DomainSettingUpdate(
                value_type=SettingValueType.string,
                value_text=normalized,
                is_active=True,
            ),
        )
        return
    existing = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key == key)
        .first()
    )
    if existing:
        notification_settings.upsert_by_key(
            db, key, DomainSettingUpdate(is_active=False)
        )


def upsert_smtp_sender(
    db: Session,
    *,
    sender_key: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    from_email: str,
    from_name: str | None,
    use_tls: bool,
    use_ssl: bool,
    is_active: bool,
) -> str:
    normalized_key = sender_key.strip().lower()
    if not _valid_sender_key(normalized_key):
        raise ValueError(
            "Sender key must use only lowercase letters, numbers, '-' or '_'"
        )
    if not host.strip():
        raise ValueError("SMTP host is required")
    if not from_email.strip():
        raise ValueError("From email is required")

    values: dict[str, tuple[SettingValueType, str | None, object | None, bool]] = {
        "host": (SettingValueType.string, host.strip(), None, False),
        "port": (SettingValueType.integer, str(int(port)), None, False),
        "username": (SettingValueType.string, (username or "").strip(), None, False),
        "from_email": (SettingValueType.string, from_email.strip(), None, False),
        "from_name": (
            SettingValueType.string,
            (from_name or get_brand()["from_name"]).strip() or get_brand()["from_name"],
            None,
            False,
        ),
        "use_tls": (
            SettingValueType.boolean,
            "true" if use_tls else "false",
            use_tls,
            False,
        ),
        "use_ssl": (
            SettingValueType.boolean,
            "true" if use_ssl else "false",
            use_ssl,
            False,
        ),
        "is_active": (
            SettingValueType.boolean,
            "true" if is_active else "false",
            is_active,
            False,
        ),
    }
    for field, (value_type, value_text, value_json, is_secret) in values.items():
        notification_settings.upsert_by_key(
            db,
            _sender_setting_key(normalized_key, field),
            DomainSettingUpdate(
                value_type=value_type,
                value_text=value_text,
                value_json=value_json,
                is_secret=is_secret,
                is_active=True,
            ),
        )

    if password is not None and password.strip():
        notification_settings.upsert_by_key(
            db,
            _sender_setting_key(normalized_key, "password"),
            DomainSettingUpdate(
                value_type=SettingValueType.string,
                value_text=password.strip(),
                is_secret=True,
                is_active=True,
            ),
        )

    return normalized_key


def deactivate_smtp_sender(db: Session, sender_key: str) -> None:
    prefix = f"{SMTP_SENDER_KEY_PREFIX}{sender_key.strip().lower()}."
    settings = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.notification)
        .filter(DomainSetting.key.like(f"{prefix}%"))
        .filter(DomainSetting.is_active.is_(True))
        .all()
    )
    for setting in settings:
        notification_settings.upsert_by_key(
            db,
            setting.key,
            DomainSettingUpdate(is_active=False),
        )


def _resolve_smtp_sender_config(
    db: Session,
    *,
    sender_key: str | None = None,
    activity: str | None = None,
) -> dict[str, Any] | None:
    available = {
        sender["sender_key"]: sender
        for sender in list_smtp_senders(db)
        if sender.get("is_active", True)
    }
    if not available:
        return None

    selected_key = (sender_key or "").strip().lower()
    if not selected_key and activity:
        selected_key = get_smtp_activity_map(db).get(activity.strip(), "")
    if not selected_key:
        selected_key = get_default_smtp_sender_key(db)
    if selected_key not in available:
        selected_key = sorted(available.keys())[0]

    selected = dict(available[selected_key])
    if selected.get("has_password"):
        password = _setting_value(db, _sender_setting_key(selected_key, "password"))
        if password:
            selected["password"] = _resolve_secret_value(password)
    selected["user"] = selected.get("username")
    selected["from_addr"] = selected.get("from_email")
    selected["sender_key"] = selected_key
    return selected


def _get_smtp_config(
    db: Session | None,
    *,
    sender_key: str | None = None,
    activity: str | None = None,
) -> dict:
    if db is not None:
        selected = _resolve_smtp_sender_config(
            db, sender_key=sender_key, activity=activity
        )
        if selected:
            return selected
    return _legacy_smtp_config(db)


def get_smtp_config(
    db: Session | None,
    *,
    sender_key: str | None = None,
    activity: str | None = None,
) -> dict:
    """Public accessor for resolved SMTP configuration."""
    return _get_smtp_config(db, sender_key=sender_key, activity=activity)


def _normalize_base_url(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    if not normalized.startswith(("http://", "https://")):
        normalized = f"https://{normalized}"
    return normalized.rstrip("/")


def _is_local_hostname(hostname: str | None) -> bool:
    normalized = (hostname or "").strip().lower()
    return normalized in {"", "localhost", "127.0.0.1", "::1"}


def _is_local_base_url(value: str | None) -> bool:
    if not value:
        return True
    normalized = value.strip()
    if not normalized:
        return True
    parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
    return _is_local_hostname(parsed.hostname)


def _configured_domain_url(db: Session | None, key: str) -> str:
    raw_value = _setting_value_for_domain(db, SettingDomain.auth, key) or ""
    if _is_local_base_url(raw_value):
        return ""
    return _normalize_base_url(raw_value)


def _configured_app_url(db: Session | None) -> str:
    return _normalize_base_url(_env_value("APP_URL") or _setting_value(db, "app_url"))


def _get_app_url(db: Session | None, *, next_login_path: str | None = None) -> str:
    preferred_domain_keys = ["selfcare_domain"]
    fallback_domain_keys = ["selfcare_domain", "admin_domain", "reseller_domain"]
    route = (next_login_path or "").strip()
    if route.startswith("/reseller"):
        preferred_domain_keys = ["reseller_domain"]
        fallback_domain_keys = ["selfcare_domain"]

    for key in preferred_domain_keys:
        domain_url = _configured_domain_url(db, key)
        if domain_url:
            return domain_url

    app_url = _configured_app_url(db)
    if app_url and not _is_local_base_url(app_url):
        return app_url

    for key in fallback_domain_keys:
        domain_url = _configured_domain_url(db, key)
        if domain_url:
            return domain_url

    if app_url:
        return app_url
    return "http://localhost:8000"


def _get_company_name(db: Session | None) -> str:
    if db is None:
        return get_brand()["legal_name"]
    try:
        from app.services import (
            web_system_company_info as web_system_company_info_service,
        )

        company_name = (
            web_system_company_info_service.get_company_info(db).get("company_name")
            or ""
        ).strip()
        if company_name:
            return company_name
    except Exception:
        logger.debug("Failed to load company name for email branding", exc_info=True)
    return get_brand()["legal_name"]


def _brand_accent_color() -> str:
    return DOTMAC_RED


def _absolute_asset_url(app_url: str, asset_url: str | None) -> str:
    value = (asset_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/"):
        return urljoin(f"{app_url.rstrip('/')}/", value.lstrip("/"))
    return value


def _get_email_branding_logo_url(db: Session | None) -> str:
    app_url = _get_app_url(db)
    if db is not None:
        try:
            logo_raw = resolve_value(db, SettingDomain.comms, "sidebar_logo_url")
            logo_url = _absolute_asset_url(
                app_url, str(logo_raw).strip() if logo_raw else ""
            )
            if logo_url:
                return logo_url
        except Exception:
            logger.debug(
                "Failed to load primary branding logo for email", exc_info=True
            )
        try:
            dark_logo_raw = resolve_value(
                db, SettingDomain.comms, "sidebar_logo_dark_url"
            )
            dark_logo_url = _absolute_asset_url(
                app_url, str(dark_logo_raw).strip() if dark_logo_raw else ""
            )
            if dark_logo_url:
                return dark_logo_url
        except Exception:
            logger.debug("Failed to load dark branding logo for email", exc_info=True)
    return _absolute_asset_url(app_url, "/static/branding/favicon/icon-192.png")


def _render_action_email_html(
    *,
    company_name: str,
    logo_url: str,
    title: str,
    accent_color: str,
    greeting: str,
    intro_html: str,
    action_url: str,
    action_label: str,
    expiry_minutes: int,
    details_html: str,
    closing_html: str,
    support_email: str,
    secondary_color: str | None = None,
    boxed: bool = False,
    details_suffix_html: str = "",
) -> str:
    logo_block = ""
    if logo_url:
        logo_block = f"""
  <div style="text-align: center; margin: 0 0 18px;">
    <img src="{html.escape(logo_url)}" alt="{html.escape(company_name)} logo" style="max-width: 160px; max-height: 64px; width: auto; height: auto;">
  </div>
"""
    else:
        logo_block = f"""
  <div style="text-align: center; margin: 0 0 18px;">
    <div style="display: inline-block; color: {DOTMAC_RED}; font-size: 22px; font-weight: 700;">{html.escape(company_name)}</div>
  </div>
"""
    support_href = html.escape(support_email)
    safe_company_name = html.escape(company_name)
    secondary_color = secondary_color or DOTMAC_GREEN
    body_background = DOTMAC_WHITE
    container_style = (
        "font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; "
        f"line-height: 1.8; color: #333; background-color: {DOTMAC_WHITE}; "
        "padding: 25px; position: relative; max-width: 680px; margin: 0 auto;"
    )
    if boxed:
        container_style += (
            " border: 1px solid #ccc; border-radius: 10px;"
            " box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);"
        )
    details_style = (
        f"background-color: {DOTMAC_WHITE}; border: 2px solid #e2e2e2; "
        "border-radius: 8px; padding: 20px; margin-bottom: 20px;"
        if boxed
        else f"background-color: {DOTMAC_WHITE}; padding: 0; margin: 22px 0;"
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="light dark">
  <meta name="supported-color-schemes" content="light dark">
  <style>
    @media (prefers-color-scheme: dark) {{
      .email-body {{ background-color: #111827 !important; }}
      .email-container {{ background-color: #111827 !important; color: #e5e7eb !important; }}
      .email-text {{ color: #d1d5db !important; }}
      .email-muted {{ color: #d1d5db !important; }}
      .email-details {{ background-color: #111827 !important; border-color: #374151 !important; }}
      .email-highlight-box {{ background-color: #1f2937 !important; border-color: #008000 !important; }}
    }}
  </style>
</head>
<body class="email-body" style="margin: 0; padding: 24px; background-color: {body_background};">
  <div class="email-container" style="{container_style}">
{logo_block}
    <div style="text-align: center; margin-bottom: 20px;">
      <h1 style="color: {accent_color}; font-size: 24px; margin: 0;">{html.escape(title)}</h1>
    </div>

    <p style="font-size: 16px; color: {accent_color}; margin-top: 20px;">{html.escape(greeting)}</p>

    <div class="email-text" style="font-size: 15px; color: #555; margin: 15px 0;">
      {intro_html}
    </div>

    <div class="email-details" style="{details_style}">
      {details_html}
      <p style="margin: 18px 0 0;">
        <a href="{html.escape(action_url)}" style="display: inline-block; padding: 12px 24px; background-color: {accent_color}; color: {DOTMAC_BUTTON_TEXT}; text-decoration: none; border-radius: 6px; font-weight: 600;">{html.escape(action_label)}</a>
      </p>
      <p class="email-muted" style="font-size: 14px; color: #666; margin: 16px 0 0;">If the button does not work, copy and paste this link into your browser:</p>
      <p style="font-size: 14px; margin: 8px 0 0; word-break: break-all;"><a href="{html.escape(action_url)}" style="color: {secondary_color}; text-decoration: none;">{html.escape(action_url)}</a></p>
      {details_suffix_html}
    </div>

    <div class="email-text" style="font-size: 15px; color: #555; margin: 15px 0;">
      {closing_html}
      <p style="margin: 0;">For help, contact us via <a href="mailto:{support_href}" style="color: {secondary_color}; text-decoration: none;">{support_href}</a>.</p>
    </div>

    <p class="email-text" style="font-size: 15px; color: #555; margin-bottom: 20px;">
      Thank you for choosing <strong style="color: {secondary_color}; font-size: 18px;">{safe_company_name}</strong>.
    </p>

    <p style="font-size: 15px; color: {accent_color}; text-align: right; font-style: italic;">
      Best regards,<br>
      <span style="color: {secondary_color}; font-weight: bold;">{safe_company_name} Support Team</span>
    </p>
  </div>
</body>
</html>
"""


def _format_expiry_duration(minutes: object) -> str:
    try:
        parsed = int(minutes)
    except (TypeError, ValueError):
        parsed = 60
    if parsed > 0 and parsed % 60 == 0:
        hours = parsed // 60
        unit = "hour" if hours == 1 else "hours"
        return f"{hours} {unit}"
    unit = "minute" if parsed == 1 else "minutes"
    return f"{parsed} {unit}"


def _create_smtp_client(
    host: str, port: int, use_ssl: bool, timeout: int | None = None
):
    if use_ssl:
        if timeout is None:
            return smtplib.SMTP_SSL(host, port)
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    if timeout is None:
        return smtplib.SMTP(host, port)
    return smtplib.SMTP(host, port, timeout=timeout)


def _smtp_timeout_seconds(db: Session | None = None) -> int:
    raw_timeout = (
        resolve_value(db, SettingDomain.notification, "smtp_test_timeout_seconds")
        if db
        else None
    )
    try:
        return max(1, int(raw_timeout)) if raw_timeout is not None else 10
    except (TypeError, ValueError):
        return 10


def send_email_with_config(
    config: dict,
    to_email: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    *,
    cc_addresses: list[str] | tuple[str, ...] | None = None,
    bcc_addresses: list[str] | tuple[str, ...] | None = None,
) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = (
        f"{config.get('from_name') or get_brand()['from_name']} <{config.get('from_email') or get_brand()['from_email']}>"
    )
    msg["To"] = to_email
    cc_recipients = list(dict.fromkeys(cc_addresses or ()))
    bcc_recipients = list(dict.fromkeys(bcc_addresses or ()))
    if cc_recipients:
        msg["Cc"] = ", ".join(cc_recipients)

    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        host = str(config.get("host") or "")
        if not host:
            return False
        port = int(config.get("port", 587) or 587)
        server = _create_smtp_client(
            host,
            port,
            bool(config.get("use_ssl")),
            timeout=int(config.get("timeout") or 10),
        )

        if config.get("use_tls") and not config.get("use_ssl"):
            server.starttls()

        username = config.get("username")
        password = config.get("password")
        if username and password:
            server.login(username, password)

        envelope_recipients = list(
            dict.fromkeys([to_email, *cc_recipients, *bcc_recipients])
        )
        server.sendmail(config.get("from_email"), envelope_recipients, msg.as_string())
        server.quit()
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed for %s: %s", to_email, exc)
        return False
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False


def send_email(
    db: Session | None,
    to_email: str,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    track: bool = True,
    sender_key: str | None = None,
    activity: str | None = None,
    notification_id: str | None = None,
    headers: dict[str, str] | None = None,
    sensitive_content: bool = False,
    cc_addresses: list[str] | tuple[str, ...] | None = None,
    bcc_addresses: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """
    Send an email via SMTP.

    Args:
        db: Database session for settings lookup and notification tracking
        to_email: Recipient email address
        subject: Email subject
        body_html: HTML body content
        body_text: Plain text body (optional, derived from HTML if not provided)
        track: Whether to create a Notification record for tracking
        headers: Optional transport headers; From, To, Cc, Bcc, and Subject are
            reserved

    Returns:
        True if email was sent successfully, False otherwise
    """
    if body_text is not None:
        tracked_body = body_text
    else:
        from app.services.email_template import html_to_text

        tracked_body = html_to_text(body_html)

    if db is not None and track and notification_id is None:
        queued = notification_records.create_customer_notification(
            db,
            NotificationCreate(
                channel=NotificationChannel.email,
                recipient=to_email,
                subject=subject,
                body=tracked_body,
                event_type=activity or "direct.email",
                category="general",
                metadata={
                    "body_html": body_html,
                    "body_text": tracked_body,
                    "sender_key": sender_key,
                    "activity": activity or "notification_queue",
                    "source": "email_service",
                    "cc": list(dict.fromkeys(cc_addresses or ())),
                    "bcc": list(dict.fromkeys(bcc_addresses or ())),
                },
            ),
        )
        return queued.status == NotificationStatus.queued

    config = _get_smtp_config(db, sender_key=sender_key, activity=activity)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config['from_name']} <{config['from_email']}>"
    msg["To"] = to_email
    cc_recipients = list(dict.fromkeys(cc_addresses or ()))
    bcc_recipients = list(dict.fromkeys(bcc_addresses or ()))
    if cc_recipients:
        msg["Cc"] = ", ".join(cc_recipients)
    for name, value in (headers or {}).items():
        normalized_name = str(name).strip()
        if normalized_name.lower() in {"from", "to", "cc", "bcc", "subject"}:
            raise ValueError(f"Reserved email header cannot be overridden: {name}")
        msg[normalized_name] = str(value)

    if body_text:
        msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    notification = None
    if db is not None and (notification_id or track):
        notification = notification_records.record_transport_attempt(
            db,
            notification_id=notification_id,
            channel=NotificationChannel.email,
            recipient=to_email,
            subject=subject,
            # Capability-bearing content exists only in this process for the
            # duration of the provider call. The durable outbox still records
            # transport state, recipient, and the non-secret subject.
            body=None if sensitive_content else tracked_body,
            commit=True,
        )

    provider_name = f"smtp:{config.get('sender_key', 'default')}"

    try:
        server = _create_smtp_client(
            config["host"],
            config["port"],
            bool(config["use_ssl"]),
            timeout=_smtp_timeout_seconds(db),
        )

        if config["use_tls"] and not config["use_ssl"]:
            server.starttls()

        if config["username"] and config["password"]:
            server.login(config["username"], config["password"])

        envelope_recipients = list(
            dict.fromkeys([to_email, *cc_recipients, *bcc_recipients])
        )
        server.sendmail(config["from_email"], envelope_recipients, msg.as_string())
        server.quit()

        if notification and db is not None:
            notification.status = NotificationStatus.delivered
            notification.last_error = None
            notification.sent_at = datetime.now(UTC)
            db.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    provider=provider_name,
                    provider_message_id=None,
                    status=DeliveryStatus.delivered,
                    response_code="sent",
                    response_body="SMTP send completed",
                )
            )
            db.commit()

        logger.info(
            "Email sent successfully to %s via sender %s",
            to_email,
            config.get("sender_key", "legacy"),
        )
        return True

    except smtplib.SMTPAuthenticationError as exc:
        if sensitive_content:
            logger.error(
                "SMTP authentication failed for sensitive email to %s", to_email
            )
        else:
            logger.error("SMTP authentication failed for %s: %s", to_email, exc)
        if notification and db is not None:
            notification.status = NotificationStatus.failed
            notification.last_error = "SMTP authentication failed"
            db.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    provider=provider_name,
                    provider_message_id=None,
                    status=DeliveryStatus.failed,
                    response_code="smtp_auth_failed",
                    response_body="SMTP authentication failed",
                )
            )
            db.commit()
        return False
    except Exception as e:
        if sensitive_content:
            logger.error(
                "Sensitive email transport failed for %s via sender %s",
                to_email,
                config.get("sender_key", "legacy"),
            )
        else:
            logger.error(
                "Failed to send email to %s via sender %s: %s",
                to_email,
                config.get("sender_key", "legacy"),
                e,
            )
        if notification and db is not None:
            notification.status = NotificationStatus.failed
            notification.last_error = (
                "Sensitive email transport failed" if sensitive_content else str(e)
            )
            db.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    provider=provider_name,
                    provider_message_id=None,
                    status=DeliveryStatus.failed,
                    response_code="smtp_error",
                    response_body=(
                        "Sensitive email transport failed"
                        if sensitive_content
                        else str(e)
                    ),
                )
            )
            db.commit()
        return False


def test_smtp_connection(
    config: dict,
    timeout_sec: int | None = None,
    db: Session | None = None,
) -> tuple[bool, str | None]:
    host = config.get("host")
    if not host:
        return False, "SMTP host is required"

    # Use configurable timeout, fallback to default of 10 seconds
    if timeout_sec is None:
        raw_timeout = (
            resolve_value(db, SettingDomain.notification, "smtp_test_timeout_seconds")
            if db
            else None
        )
        if raw_timeout is None:
            timeout_sec = 10
        else:
            try:
                if isinstance(raw_timeout, int):
                    timeout_sec = raw_timeout
                elif isinstance(raw_timeout, float):
                    timeout_sec = int(raw_timeout)
                elif isinstance(raw_timeout, str):
                    timeout_sec = int(raw_timeout)
                else:
                    raise TypeError("Unsupported timeout type")
            except (TypeError, ValueError):
                timeout_sec = 10

    server = None
    try:
        port = config.get("port", 587)
        use_ssl = bool(config.get("use_ssl"))
        use_tls = bool(config.get("use_tls"))

        server = _create_smtp_client(host, port, use_ssl, timeout=timeout_sec)

        server.ehlo()
        if use_tls and not use_ssl:
            server.starttls()
            server.ehlo()

        username = config.get("username")
        password = config.get("password")
        if username and password:
            server.login(username, password)

        server.noop()
        return True, None
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed during connection test: %s", exc)
        return False, "SMTP authentication failed"
    except (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, OSError) as exc:
        return False, f"SMTP connection failed: {exc}"
    except smtplib.SMTPException as exc:
        return False, f"SMTP error: {exc}"
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                logger.debug("SMTP quit failed after connection test", exc_info=True)


def render_password_reset_email(
    db: Session,
    to_email: str,
    reset_token: str,
    person_name: str | None = None,
    next_login_path: str | None = None,
    expires_minutes: int | None = None,
    token_in_fragment: bool = False,
) -> RenderedEmail:
    """Render a password reset email without sending or persisting it.

    Args:
        db: Database session
        to_email: Recipient email address
        reset_token: The JWT reset token
        person_name: Optional name to personalize the email
        expires_minutes: Actual token TTL; falls back to the configured setting

        token_in_fragment: Keep the bearer out of HTTP request/access logs
    """
    app_url = _get_app_url(db, next_login_path=next_login_path)
    query = {"token": reset_token}
    if next_login_path and next_login_path.startswith("/"):
        query["next_login"] = next_login_path
    token_separator = "#" if token_in_fragment else "?"
    reset_url = f"{app_url}/auth/reset-password{token_separator}{urlencode(query)}"

    # Prefer the actual token TTL; fall back to the configured setting
    expiry_minutes = expires_minutes or (
        resolve_value(db, SettingDomain.auth, "password_reset_expiry_minutes") or 1440
    )
    expiry_duration = _format_expiry_duration(expiry_minutes)

    greeting = f"Dear {person_name}," if person_name else "Dear Customer,"
    company_name = _get_company_name(db)
    logo_url = _get_email_branding_logo_url(db)
    support_email = (
        _setting_value(db, "smtp_from_email") or get_brand()["support_email"]
    ).strip()

    subject = "Password Reset Request"

    body_html = _render_action_email_html(
        company_name=company_name,
        logo_url=logo_url,
        title="Password Reset Request",
        accent_color=DOTMAC_RED,
        greeting=greeting,
        intro_html="""
<p style="margin: 0 0 12px;">We received a request to reset your password for your portal access.</p>
<p style="margin: 0;">Use the secure button below to create a new password and continue using your account.</p>
""".strip(),
        action_url=reset_url,
        action_label="Reset Password",
        expiry_minutes=expiry_minutes,
        details_html=f"""
<div class="email-highlight-box" style="background-color: #f8fafc; border: 1px solid {DOTMAC_GREEN}; border-left: 5px solid {DOTMAC_RED}; border-radius: 8px; padding: 18px;">
  <p style="font-size: 15px; margin: 0; line-height: 1.6;">
    <strong style="color: {DOTMAC_GREEN};">Action:</strong> <span class="email-muted" style="color: #555;">Reset your account password</span><br>
    <strong style="color: {DOTMAC_GREEN};">Email:</strong> <span class="email-muted" style="color: #555;">{html.escape(to_email)}</span><br>
    <strong style="color: {DOTMAC_GREEN};">Expires In:</strong> <span class="email-muted" style="color: #555;">{expiry_duration}</span>
  </p>
""".strip(),
        closing_html="""
<p style="margin: 0 0 12px;">If you did not request this password reset, you can safely ignore this email and your current password will remain unchanged.</p>
<p style="margin: 0 0 12px;">This is an automated message. Please do not reply directly to this email.</p>
""".strip(),
        support_email=support_email,
        secondary_color=DOTMAC_GREEN,
        boxed=False,
        details_suffix_html="</div>",
    )

    body_text = f"""{greeting}

We received a request to reset your password for {company_name}.

Click the link below to create a new password:
{reset_url}

This link will expire in {expiry_duration}.

If you didn't request a password reset, you can safely ignore this email.

This is an automated message. Please do not reply to this email.
"""

    return RenderedEmail(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
    )


def send_password_reset_email(
    db: Session,
    to_email: str,
    reset_token: str,
    person_name: str | None = None,
    next_login_path: str | None = None,
    expires_minutes: int | None = None,
    token_in_fragment: bool = False,
) -> bool:
    """Compatibility sender; recovery delivery uses the durable outbox."""

    rendered = render_password_reset_email(
        db,
        to_email=to_email,
        reset_token=reset_token,
        person_name=person_name,
        next_login_path=next_login_path,
        expires_minutes=expires_minutes,
        token_in_fragment=token_in_fragment,
    )
    return send_email(
        db,
        to_email,
        rendered.subject,
        rendered.body_html,
        rendered.body_text,
        activity="auth_password_reset",
    )


def send_email_verification_email(
    db: Session,
    to_email: str,
    verification_token: str,
    person_name: str | None = None,
    expires_minutes: int | None = None,
) -> bool:
    """
    Send an email-address verification email.

    Args:
        db: Database session
        to_email: Recipient email address
        verification_token: The JWT email-verification token
        person_name: Optional name to personalize the email
        expires_minutes: Actual token TTL; falls back to the configured setting

    Returns:
        True if email was sent successfully, False otherwise
    """
    app_url = _get_app_url(db)
    verify_url = (
        f"{app_url}/portal/auth/verify-email?{urlencode({'token': verification_token})}"
    )

    # Prefer the actual token TTL; fall back to the configured setting
    expiry_minutes = expires_minutes or (
        resolve_value(db, SettingDomain.auth, "email_verification_expiry_minutes")
        or 1440
    )
    expiry_duration = _format_expiry_duration(expiry_minutes)

    greeting = f"Dear {person_name}," if person_name else "Dear Customer,"
    company_name = _get_company_name(db)
    logo_url = _get_email_branding_logo_url(db)
    support_email = (
        _setting_value(db, "smtp_from_email") or get_brand()["support_email"]
    ).strip()

    subject = "Verify your email address"

    accent_color = _brand_accent_color()
    body_html = _render_action_email_html(
        company_name=company_name,
        logo_url=logo_url,
        title="Verify Your Email Address",
        accent_color=accent_color,
        greeting=greeting,
        intro_html="""
<p style="margin: 0 0 12px;">Please confirm that this is the email address for your portal account.</p>
<p style="margin: 0;">Use the secure button below to verify your email address.</p>
""".strip(),
        action_url=verify_url,
        action_label="Verify Email",
        expiry_minutes=expiry_minutes,
        details_html=f"""
<p style="font-size: 15px; margin: 0; line-height: 1.5;">
  <strong style="color: {DOTMAC_GREEN};">Action:</strong> <span class="email-muted" style="color: #555;">Verify your email address</span><br>
  <strong style="color: {DOTMAC_GREEN};">Email:</strong> <span class="email-muted" style="color: #555;">{html.escape(to_email)}</span><br>
  <strong style="color: {DOTMAC_GREEN};">Expires In:</strong> <span class="email-muted" style="color: #555;">{expiry_duration}</span>
</p>
""".strip(),
        closing_html="""
<p style="margin: 0 0 12px;">If you did not create an account or request this, you can safely ignore this email.</p>
<p style="margin: 0 0 12px;">This is an automated message. Please do not reply directly to this email.</p>
""".strip(),
        support_email=support_email,
        secondary_color=DOTMAC_GREEN,
    )

    body_text = f"""{greeting}

Please confirm that this is the email address for your {company_name} portal account.

Click the link below to verify your email address:
{verify_url}

This link will expire in {expiry_duration}.

If you did not create an account or request this, you can safely ignore this email.

This is an automated message. Please do not reply to this email.
"""

    return send_email(
        db,
        to_email,
        subject,
        body_html,
        body_text,
        activity="auth_email_verification",
    )


def render_user_invite_email(
    db: Session,
    to_email: str,
    reset_token: str,
    person_name: str | None = None,
    next_login_path: str | None = None,
    expires_minutes: int | None = None,
    action_path: str = "/auth/reset-password",
    token_in_fragment: bool = False,
) -> RenderedEmail:
    """
    Render a new user invitation email without sending or persisting it.

    Args:
        db: Database session
        to_email: Recipient email address
        reset_token: The JWT reset token
        person_name: Optional name to personalize the email
        expires_minutes: Actual token TTL; falls back to the configured setting
        action_path: Portal path that will consume the purpose-bound token
        token_in_fragment: Keep the bearer out of HTTP request/access logs
    """
    app_url = _get_app_url(db, next_login_path=next_login_path)
    query = {"token": reset_token}
    if next_login_path and next_login_path.startswith("/"):
        query["next_login"] = next_login_path
    normalized_action_path = (
        action_path if action_path.startswith("/") else f"/{action_path}"
    )
    token_separator = "#" if token_in_fragment else "?"
    reset_url = f"{app_url}{normalized_action_path}{token_separator}{urlencode(query)}"

    # Prefer the actual token TTL; fall back to the configured setting
    expiry_minutes = expires_minutes or (
        resolve_value(db, SettingDomain.auth, "user_invite_expiry_minutes") or 1440
    )
    expiry_duration = _format_expiry_duration(expiry_minutes)

    greeting = f"Dear {person_name}," if person_name else "Dear Customer,"
    company_name = _get_company_name(db)
    logo_url = _get_email_branding_logo_url(db)
    support_email = (
        _setting_value(db, "smtp_from_email") or get_brand()["support_email"]
    ).strip()

    subject = f"You're invited to {company_name}"

    accent_color = _brand_accent_color()
    body_html = _render_action_email_html(
        company_name=company_name,
        logo_url=logo_url,
        title=f"Welcome to {company_name}",
        accent_color=accent_color,
        greeting=greeting,
        intro_html="""
<p style="margin: 0 0 12px;">Your account has been created successfully.</p>
<p style="margin: 0;">Use the secure button below to set your password and complete your access to the portal.</p>
""".strip(),
        action_url=reset_url,
        action_label="Set Password",
        expiry_minutes=expiry_minutes,
        details_html=f"""
<p style="font-size: 15px; margin: 0; line-height: 1.5;">
  <strong style="color: {DOTMAC_GREEN};">Portal:</strong> <span class="email-muted" style="color: #555;">{html.escape(company_name)}</span><br>
  <strong style="color: {DOTMAC_GREEN};">Email:</strong> <span class="email-muted" style="color: #555;">{html.escape(to_email)}</span><br>
  <strong style="color: {DOTMAC_GREEN};">Expires In:</strong> <span class="email-muted" style="color: #555;">{expiry_duration}</span>
</p>
""".strip(),
        closing_html="""
<p style="margin: 0 0 12px;">If you need any assistance getting started, our support team is available to help you.</p>
<p style="margin: 0 0 12px;">This is an automated message. Please do not reply directly to this email.</p>
""".strip(),
        support_email=support_email,
        secondary_color=DOTMAC_GREEN,
    )

    body_text = f"""{greeting}

Welcome to {company_name}.

Use the link below to set your password:
{reset_url}

This link will expire in {expiry_duration}.

This is an automated message. Please do not reply to this email.
"""

    return RenderedEmail(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
    )


def send_user_invite_email(
    db: Session,
    to_email: str,
    reset_token: str,
    person_name: str | None = None,
    next_login_path: str | None = None,
    expires_minutes: int | None = None,
    action_path: str = "/auth/reset-password",
    track: bool = True,
    token_in_fragment: bool = False,
) -> bool:
    """Render and send a new user invitation email."""

    rendered = render_user_invite_email(
        db,
        to_email,
        reset_token,
        person_name=person_name,
        next_login_path=next_login_path,
        expires_minutes=expires_minutes,
        action_path=action_path,
        token_in_fragment=token_in_fragment,
    )
    return send_email(
        db,
        to_email,
        rendered.subject,
        rendered.body_html,
        rendered.body_text,
        track=track,
        activity="auth_user_invite",
    )
