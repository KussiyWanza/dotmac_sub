"""Channel-policy admin surface: canonical write, precedence display, no rivals."""

from pathlib import Path

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.notification import NotificationChannel
from app.models.subscription_engine import SettingValueType
from app.services import customer_experience_communications
from app.services import notification_channel_policy as channel_policy
from app.services import web_notification_channels as channels_view
from app.services.domain_errors import DomainError
from app.services.events.handlers.notification import (
    event_catalogue,
    event_categories,
)
from app.services.notification_channel_policy import resolve_notification_channels
from app.services.settings_cache import SettingsCache


class _Form(dict):
    """Mimic Starlette's multi-value form mapping."""

    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


def _invalidate() -> None:
    SettingsCache.invalidate(
        SettingDomain.notification.value, channel_policy.POLICY_SETTING_KEY
    )


# --- canonical writer -------------------------------------------------------


def test_set_channel_policy_round_trips(db_session):
    channel_policy.set_channel_policy(
        db_session,
        default=["email"],
        categories={"service": ["whatsapp", "email"]},
        events={"outage_area": ["whatsapp"]},
    )
    db_session.commit()
    _invalidate()

    stored = channel_policy.get_channel_policy(db_session)
    assert stored["default"] == ["email"]
    assert stored["categories"]["service"] == ["whatsapp", "email"]
    assert stored["events"]["outage_area"] == ["whatsapp"]


def test_set_channel_policy_drops_empty_selections(db_session):
    """An empty row must fall through, never send on no channel at all."""
    channel_policy.set_channel_policy(
        db_session,
        default=["email"],
        categories={"service": [], "billing": ["sms"]},
        events={"outage_area": []},
    )
    db_session.commit()
    _invalidate()

    stored = channel_policy.get_channel_policy(db_session)
    assert "service" not in stored["categories"]
    assert stored["categories"]["billing"] == ["sms"]
    assert stored["events"] == {}


def test_set_channel_policy_rejects_unknown_channel(db_session):
    with pytest.raises(DomainError) as excinfo:
        channel_policy.set_channel_policy(db_session, default=["carrier_pigeon"])
    assert "carrier_pigeon" in str(excinfo.value)


def test_set_channel_policy_rejects_non_customer_channel(db_session):
    """webhook/websocket are transports, not operator-selectable channels."""
    with pytest.raises(DomainError):
        channel_policy.set_channel_policy(db_session, default=["webhook"])


def test_written_policy_drives_resolution(db_session):
    channel_policy.set_channel_policy(
        db_session,
        default=["email"],
        categories={},
        events={"outage_area": ["whatsapp"]},
    )
    db_session.commit()
    _invalidate()

    assert resolve_notification_channels(
        db_session,
        template_code="outage_area",
        category="service",
        default_channels=(NotificationChannel.email,),
    ) == (NotificationChannel.whatsapp,)


def test_whatsapp_is_selectable_for_lifecycle_events(db_session):
    """Approved 2026-07-23: WhatsApp is a customer lifecycle channel."""
    assert NotificationChannel.whatsapp in channel_policy.SELECTABLE_CHANNELS

    channel_policy.set_channel_policy(
        db_session,
        categories={"service": ["whatsapp"], "billing": ["whatsapp"]},
    )
    db_session.commit()
    _invalidate()

    assert resolve_notification_channels(
        db_session,
        template_code="subscription_suspended",
        category="billing",
        default_channels=(NotificationChannel.email,),
    ) == (NotificationChannel.whatsapp,)


# --- page context -----------------------------------------------------------


def test_context_lists_every_event_and_selectable_channel(db_session):
    context = channels_view.channel_policy_context(db_session)

    assert len(context["channel_policy_events"]) == len(event_catalogue())
    assert len(context["channel_policy_categories"]) == len(event_categories())
    assert [channel["id"] for channel in context["channel_policy_channels"]] == [
        channel.value for channel in channel_policy.SELECTABLE_CHANNELS
    ]


def test_context_lists_document_change_events(db_session):
    rows = {
        row["template_code"]: row
        for row in channels_view.channel_policy_context(db_session)[
            "channel_policy_events"
        ]
    }

    for template_code in (
        "project_status_changed",
        "project_task_status_changed",
        "project_updated",
        "project_task_updated",
        "support_ticket_status_changed",
        "support_ticket_updated",
        "work_order_complete",
    ):
        assert template_code in rows

    assert rows["project_updated"]["effective"] == ["email"]
    assert rows["project_updated"]["enabled"] is False
    assert rows["project_updated"]["enabled_field"] == "document_enabled__project_updated"
    assert rows["work_order_complete"]["effective"] == ["email", "whatsapp", "push"]
    assert rows["work_order_complete"]["enabled"] is True


def test_context_reports_effective_channel_and_its_source(db_session):
    channel_policy.set_channel_policy(
        db_session,
        default=["email"],
        categories={"service": ["sms"]},
        events={"outage_area": ["whatsapp"]},
    )
    db_session.commit()
    _invalidate()

    rows = {
        row["template_code"]: row
        for row in channels_view.channel_policy_context(db_session)[
            "channel_policy_events"
        ]
    }

    assert rows["outage_area"]["effective"] == ["whatsapp"]
    assert rows["outage_area"]["source"] == "event override"
    assert rows["outage_last_mile"]["effective"] == ["sms"]
    assert rows["outage_last_mile"]["source"] == "category"

    billing_row = next(
        row for row in rows.values() if row["category"] not in {"service"}
    )
    assert billing_row["source"] in {"global default", "category", "code default"}


def test_context_surfaces_legacy_override_that_outranks_the_page(db_session):
    """A legacy per-event setting wins; the page must say so, not lie."""
    db_session.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key="notification_event_outage_area_channels",
            value_type=SettingValueType.string,
            value_text="sms",
            is_active=True,
        )
    )
    db_session.commit()
    SettingsCache.invalidate(
        SettingDomain.notification.value, "notification_event_outage_area_channels"
    )

    channel_policy.set_channel_policy(db_session, events={"outage_area": ["whatsapp"]})
    db_session.commit()
    _invalidate()

    context = channels_view.channel_policy_context(db_session)
    assert context["channel_policy_has_legacy"] is True

    row = next(
        item
        for item in context["channel_policy_events"]
        if item["template_code"] == "outage_area"
    )
    assert row["effective"] == ["sms"]
    assert row["source"] == "legacy setting"


# --- form handling ----------------------------------------------------------


def test_save_from_form_writes_one_policy(db_session):
    form = _Form(
        {
            channels_view.DEFAULT_FIELD: ["email"],
            f"{channels_view.CATEGORY_FIELD_PREFIX}service": ["whatsapp", "email"],
            f"{channels_view.EVENT_FIELD_PREFIX}outage_area": ["whatsapp"],
            f"{channels_view.DOCUMENT_ENABLED_FIELD_PREFIX}project_status_changed": [
                "1"
            ],
        }
    )
    channels_view.save_channel_policy(db_session, form)
    db_session.commit()
    _invalidate()

    stored = channel_policy.get_channel_policy(db_session)
    assert stored["default"] == ["email"]
    assert stored["categories"]["service"] == ["whatsapp", "email"]
    assert stored["events"]["outage_area"] == ["whatsapp"]
    assert customer_experience_communications.document_change_notification_enabled(
        db_session,
        "project_status_changed",
    )
    assert not customer_experience_communications.document_change_notification_enabled(
        db_session,
        "support_ticket_status_changed",
    )


def test_unticked_row_clears_its_override(db_session):
    channel_policy.set_channel_policy(db_session, events={"outage_area": ["whatsapp"]})
    db_session.commit()
    _invalidate()

    channels_view.save_channel_policy(
        db_session, _Form({channels_view.DEFAULT_FIELD: ["email"]})
    )
    db_session.commit()
    _invalidate()

    assert channel_policy.get_channel_policy(db_session)["events"] == {}


# --- no rival channel pickers ----------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RETIRED_CHANNEL_KEYS = (
    "reminder_channel",
    "blocking_wave_channel",
    "pre_block_wave1_channel",
    "pre_block_wave2_channel",
)


def test_retired_channel_settings_are_gone_from_config_surface():
    """Notification channel selection lives in exactly one place."""
    from app.services import web_system_config

    configured = set(web_system_config.REMINDER_KEYS) | set(
        web_system_config.BILLING_NOTIF_KEYS
    )
    assert configured.isdisjoint(_RETIRED_CHANNEL_KEYS)

    for template in ("reminders.html", "billing_notifications.html"):
        body = (
            _REPO_ROOT / "templates" / "admin" / "system" / "config" / template
        ).read_text()
        for key in _RETIRED_CHANNEL_KEYS:
            assert f'name="{key}"' not in body, f"{template} still posts {key}"
