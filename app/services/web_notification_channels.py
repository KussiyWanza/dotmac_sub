"""Admin surface for the notification channel policy.

Presentation only: every channel decision and every write goes through
``app.services.notification_channel_policy``, which owns the policy. This module
composes the event catalogue, the stored policy and transport readiness into one
page context, and turns form input back into a single canonical write.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.models.notification import NotificationChannel
from app.services import customer_experience_communications, web_notifications
from app.services import notification_channel_policy as channel_policy
from app.services.customer_notification_policy import channel_disabled_in_config
from app.services.events.handlers.notification import (
    event_catalogue,
    event_categories,
)

#: Form fields are namespaced so one POST carries the whole matrix.
DEFAULT_FIELD = "default"
CATEGORY_FIELD_PREFIX = "category__"
EVENT_FIELD_PREFIX = "event__"
DOCUMENT_ENABLED_FIELD_PREFIX = "document_enabled__"

_CHANNEL_LABELS = {
    NotificationChannel.email: "Email",
    NotificationChannel.sms: "SMS",
    NotificationChannel.whatsapp: "WhatsApp",
    NotificationChannel.push: "Push",
}

_READINESS_URLS = {
    NotificationChannel.email: "/admin/system/email",
    NotificationChannel.whatsapp: "/admin/integrations/whatsapp/config",
}


def _channel_readiness(db: Session) -> dict[str, tuple[bool, str]]:
    """Reuse the readiness probes the bulk-send setup page already relies on."""
    return {
        NotificationChannel.email.value: web_notifications._email_channel_ready(db),
        NotificationChannel.sms.value: web_notifications._sms_channel_ready(db),
        NotificationChannel.whatsapp.value: web_notifications._whatsapp_channel_ready(
            db
        ),
        # Push has no provider-side configuration to probe; delivery depends on
        # whether the subscriber has registered a device token.
        NotificationChannel.push.value: (True, "Delivered to registered devices"),
    }


def channel_policy_context(db: Session) -> dict[str, Any]:
    """Build the events x channels matrix with its effective resolution."""
    policy = channel_policy.get_channel_policy(db)
    legacy = channel_policy.legacy_event_overrides(db)
    readiness = _channel_readiness(db)

    default_channels: list[str] = list(policy["default"])
    category_overrides: Mapping[str, list[str]] = policy["categories"]
    event_overrides: Mapping[str, list[str]] = policy["events"]
    document_enabled = customer_experience_communications.document_change_notification_policy(
        db
    )

    channels = [
        {
            "id": channel.value,
            "label": _CHANNEL_LABELS.get(channel, channel.value.capitalize()),
            "ready": readiness.get(channel.value, (False, "Unknown"))[0],
            "message": readiness.get(channel.value, (False, "Unknown"))[1],
            # `disabled` is a deliberate config state (e.g. SMS retired via
            # sms_enabled=false), distinct from `ready` which is a transient
            # transport probe. Only a disabled channel is made unselectable and
            # dropped from writes; a not-ready-but-enabled channel keeps its
            # warning but stays selectable so a brief probe failure never drops
            # an operator's routing.
            "disabled": channel_disabled_in_config(db, channel),
            "settings_url": _READINESS_URLS.get(channel),
        }
        for channel in channel_policy.SELECTABLE_CHANNELS
    ]

    categories = [
        {
            "name": category,
            "field": f"{CATEGORY_FIELD_PREFIX}{category}",
            "selected": list(category_overrides.get(category, [])),
            "inherits": default_channels,
        }
        for category in event_categories()
    ]

    events = []
    for entry in event_catalogue():
        override = list(event_overrides.get(entry.template_code, []))
        category_default = list(category_overrides.get(entry.category, []))
        legacy_override = legacy.get(entry.template_code)

        if legacy_override:
            effective, source = legacy_override, "legacy setting"
        elif override:
            effective, source = override, "event override"
        elif category_default:
            effective, source = category_default, "category"
        elif default_channels:
            effective, source = default_channels, "global default"
        else:
            effective, source = list(entry.default_channels), "code default"

        events.append(
            {
                "event_type": entry.event_type,
                "template_code": entry.template_code,
                "category": entry.category,
                "subject": entry.subject,
                "field": f"{EVENT_FIELD_PREFIX}{entry.template_code}",
                "enabled_field": (
                    f"{DOCUMENT_ENABLED_FIELD_PREFIX}{entry.template_code}"
                    if entry.template_code in document_enabled
                    else None
                ),
                "enabled": document_enabled.get(entry.template_code, True),
                "selected": override,
                "code_default": list(entry.default_channels),
                "effective": effective,
                "source": source,
                "legacy_override": legacy_override,
            }
        )

    return {
        "channel_policy_channels": channels,
        "channel_policy_default": default_channels,
        "channel_policy_default_field": DEFAULT_FIELD,
        "channel_policy_categories": categories,
        "channel_policy_events": events,
        "channel_policy_has_legacy": bool(legacy),
        "channel_policy_legacy_count": len(legacy),
    }


def save_channel_policy(
    db: Session, form: Mapping[str, Any]
) -> channel_policy.ChannelPolicyDocument:
    """Turn one posted matrix into a single canonical policy write.

    Checkbox groups only appear in the payload when at least one box is ticked,
    so an untouched row correctly clears its override and falls back through the
    precedence chain.
    """
    getlist = getattr(form, "getlist", None)

    # Defend the write: never persist a route to a channel that is explicitly
    # DISABLED in config (e.g. SMS while retired). This is a deliberate operator
    # state, not the transient readiness probe — keying on readiness here would
    # silently drop email routes during a brief SMTP hiccup. The UI already
    # disables these checkboxes; this covers a hand-crafted POST.
    unavailable = {
        channel.value
        for channel in channel_policy.SELECTABLE_CHANNELS
        if channel_disabled_in_config(db, channel)
    }

    def _values(field: str) -> list[str]:
        if getlist is not None:
            raw_values = [str(item) for item in getlist(field)]
        else:
            raw = form.get(field)
            if raw is None:
                raw_values = []
            elif isinstance(raw, str):
                raw_values = [item for item in raw.split(",") if item.strip()]
            else:
                raw_values = [str(item) for item in raw]
        return [value for value in raw_values if value not in unavailable]

    categories = {
        category: _values(f"{CATEGORY_FIELD_PREFIX}{category}")
        for category in event_categories()
    }
    events = {
        entry.template_code: _values(f"{EVENT_FIELD_PREFIX}{entry.template_code}")
        for entry in event_catalogue()
    }
    document_enabled = {
        entry.template_code: bool(_values(f"{DOCUMENT_ENABLED_FIELD_PREFIX}{entry.template_code}"))
        for entry in event_catalogue()
        if entry.template_code
        in customer_experience_communications.DOCUMENT_CHANGE_NOTIFICATION_EVENTS
    }
    if document_enabled:
        customer_experience_communications.set_document_change_notification_policy(
            db,
            document_enabled,
        )

    return channel_policy.set_channel_policy(
        db,
        default=_values(DEFAULT_FIELD),
        categories=categories,
        events=events,
    )
