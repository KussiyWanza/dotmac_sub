"""SMS configuration is split by class: operational values are settings,
credentials are held.

The SMS channel used to read everything through `sms._get_setting`, which
consulted the environment ABOVE the stored row and took a default from each
call site. Three consequences, all of which this module pins shut:

1. `sms_enabled` defaulted to `"true"` in `notification_adapter` and `"false"`
   in `sms`, `web_notifications` and `customer_notification_policy`, so
   `is_available()` could advertise a channel the send path refused.
2. The nine SMS values had no `SettingSpec`, so nothing declared their type,
   bounds or allowed values, and the settings UI could not show them.
3. `sms_api_key` / `sms_api_secret` were read from `domain_settings` rows —
   credentials living in the database, which ADR-0009 forbids.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.config import settings
from app.models.domain_settings import SettingDomain
from app.services.settings_spec import get_spec

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Operational SMS values — settings, owned by `control.settings_spec`.
SMS_SETTING_KEYS = (
    "sms_enabled",
    "sms_provider",
    "sms_from_number",
    "sms_username",
    "sms_webhook_url",
    "sms_api_timeout_seconds",
    "sms_max_length",
)

#: Credentials — held from boot, never settings.
SMS_CREDENTIAL_ATTRS = ("sms_api_key", "sms_api_secret")

SMS_READERS = (
    "app/services/sms.py",
    "app/services/notification_adapter.py",
    "app/services/web_notifications.py",
    "app/services/customer_notification_policy.py",
)


def test_every_operational_sms_value_is_declared() -> None:
    for key in SMS_SETTING_KEYS:
        assert get_spec(SettingDomain.notification, key) is not None, (
            f"notification.{key} is read at runtime but has no SettingSpec"
        )


def test_sms_enabled_fails_closed() -> None:
    """One default, and it is off.

    The divergent `"true"` was a known production incident: an unconfigured
    deployment presented SMS as live and queued sends into a provider that did
    not exist.
    """

    spec = get_spec(SettingDomain.notification, "sms_enabled")
    assert spec is not None and spec.default is False


def test_the_provider_vocabulary_is_declared() -> None:
    spec = get_spec(SettingDomain.notification, "sms_provider")
    assert spec is not None
    assert spec.allowed == {"", "twilio", "africastalking", "webhook"}
    # "" must stay legal: it is the unconfigured state the send path reports on.
    assert spec.default == ""


def test_credentials_are_held_not_settings() -> None:
    for attr in SMS_CREDENTIAL_ATTRS:
        assert hasattr(settings, attr), (
            f"{attr} must be materialised at boot in app.config (ADR-0009)"
        )
        assert get_spec(SettingDomain.notification, attr) is None, (
            f"{attr} is a credential and must not be a setting; a secret is "
            "held, never resolved"
        )


def test_no_sms_reader_reads_the_environment_directly() -> None:
    """Env is a declared bootstrap input, materialised by the seed.

    A live `os.getenv` in a reader is the override this slice removed.
    """

    offenders: list[str] = []
    for relative in SMS_READERS:
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"getenv", "environ"}
            ):
                offenders.append(f"{relative}:{node.lineno}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "environ"
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ):
                offenders.append(f"{relative}:{node.lineno}")
    assert not offenders, (
        f"SMS readers must resolve through the spec, not the environment: {offenders}"
    )
