"""SMS must fail closed when unconfigured.

Production evidence (2026-07-23): 0 SMS ever delivered, against 4,053
`expired_in_queue` and 716 `send_failed` rows. `sms_enabled` defaulted to
"true" and the provider defaulted to "webhook" with no webhook URL, so a
deployment that had never configured SMS still presented the channel as live
and queued sends into nothing.
"""

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services import sms as sms_service
from app.services import web_notifications


def _set(db, key: str, value: str) -> None:
    """Write a notification setting the way an operator would."""

    db.add(
        DomainSetting(
            domain=SettingDomain.notification,
            key=key,
            value_type=SettingValueType.string,
            value_text=value,
            is_active=True,
        )
    )
    db.commit()


# "Unconfigured" is now literally no rows: the SMS values are declared settings,
# so an absent row resolves to the SPEC's default. The suite used to stub the
# reader; driving the real resolution path also proves the defaults are right,
# which is the bug class that produced the incident above.


def test_unconfigured_sms_does_not_send(db_session):
    """The default must be off, not 'on and pointed at nothing'."""
    assert (
        sms_service.send_sms(db_session, "+2348000000000", "hi", track=False) is False
    )


def test_unconfigured_sms_reports_not_ready(db_session):
    ready, message = web_notifications._sms_channel_ready(db_session)
    assert ready is False
    assert "disabled" in message.lower()


def test_readiness_probe_agrees_with_the_send_path(db_session):
    """A channel the operator is told is ready must actually attempt a send.

    These two drifting apart is how a dead channel stays invisible.
    """
    ready, _ = web_notifications._sms_channel_ready(db_session)
    sent = sms_service.send_sms(db_session, "+2348000000000", "hi", track=False)
    assert ready == sent


def test_enabled_without_a_provider_is_not_ready(db_session, monkeypatch):
    monkeypatch.setattr(
        web_notifications.sms_service, "_sms_credentials", lambda: ("", "")
    )
    _set(db_session, "sms_enabled", "true")
    ready, message = web_notifications._sms_channel_ready(db_session)
    assert ready is False
    assert "provider" in message.lower()


def test_enabled_provider_still_needs_its_credentials(db_session, monkeypatch):
    credentials = {"key": "", "secret": ""}
    monkeypatch.setattr(
        web_notifications.sms_service,
        "_sms_credentials",
        lambda: (credentials["key"], credentials["secret"]),
    )
    _set(db_session, "sms_enabled", "true")
    _set(db_session, "sms_provider", "africastalking")

    ready, message = web_notifications._sms_channel_ready(db_session)
    assert ready is False
    assert "api key" in message.lower()

    credentials["key"] = "k"
    ready, _ = web_notifications._sms_channel_ready(db_session)
    assert ready is True


def test_explicit_enable_is_required_not_merely_non_false(db_session, monkeypatch):
    """ "" or a typo must not read as enabled.

    The spec's boolean coercion rejects them and resolution falls back to the
    declared default, which is off. No stub decides this any more.
    """
    monkeypatch.setattr(
        web_notifications.sms_service, "_sms_credentials", lambda: ("k", "s")
    )
    row = DomainSetting(
        domain=SettingDomain.notification,
        key="sms_enabled",
        value_type=SettingValueType.string,
        value_text="true",
        is_active=True,
    )
    db_session.add(row)
    db_session.commit()

    for value in ("", "  ", "maybe", "0", "no"):
        row.value_text = value
        db_session.commit()
        ready, _ = web_notifications._sms_channel_ready(db_session)
        assert ready is False, f"{value!r} should not enable SMS"


# --- SMS retired via the channel disable mechanism --------------------------


def test_absent_sms_config_reads_as_disabled(db_session):
    """Retirement is the default: with no sms_enabled row, SMS is disabled, so
    a spec that still defaults to SMS is cancelled cleanly at queue time rather
    than created and left to fail."""
    from app.models.notification import NotificationChannel
    from app.services import customer_notification_policy as policy

    assert (
        policy.channel_disabled_in_config(db_session, NotificationChannel.sms) is True
    )


def test_a_future_plugin_re_enables_sms_by_flipping_the_flag(db_session):
    """Nothing is deleted — enabling the channel brings it back."""
    from app.models.notification import NotificationChannel
    from app.services import customer_notification_policy as policy

    _set(db_session, "sms_enabled", "true")
    assert (
        policy.channel_disabled_in_config(db_session, NotificationChannel.sms) is False
    )


def test_matrix_marks_a_disabled_channel_unavailable(monkeypatch):
    """A config-disabled channel (retired SMS) is marked unavailable; a merely
    not-ready-but-enabled channel is not."""
    from app.services import notification_channel_policy as channel_policy
    from app.services import web_notification_channels as view

    monkeypatch.setattr(
        view,
        "_channel_readiness",
        lambda db: {
            "email": (True, ""),
            "sms": (False, "SMS is disabled"),
            "whatsapp": (False, "not configured yet"),
        },
    )
    monkeypatch.setattr(
        view,
        "channel_disabled_in_config",
        lambda db, channel: channel.value == "sms",
    )
    monkeypatch.setattr(
        channel_policy,
        "get_channel_policy",
        lambda db: {"default": [], "categories": {}, "events": {}},
    )
    monkeypatch.setattr(channel_policy, "legacy_event_overrides", lambda db: {})

    channels = {
        c["id"]: c for c in view.channel_policy_context(None)["channel_policy_channels"]
    }
    assert channels["sms"]["disabled"] is True
    # whatsapp is not ready but NOT disabled -> stays selectable with a warning
    assert channels["whatsapp"]["disabled"] is False


def test_save_drops_a_hand_posted_disabled_channel(monkeypatch):
    """The checkbox is disabled in the UI; the writer defends the POST too.

    Only a config-disabled channel is dropped, never a merely not-ready one."""
    from app.services import notification_channel_policy as channel_policy
    from app.services import web_notification_channels as view

    monkeypatch.setattr(
        view,
        "channel_disabled_in_config",
        lambda db, channel: channel.value == "sms",
    )
    written = {}
    monkeypatch.setattr(
        channel_policy,
        "set_channel_policy",
        lambda db, **kw: written.update(kw) or {},
    )

    class _Form(dict):
        def getlist(self, key):
            v = self.get(key, [])
            return v if isinstance(v, list) else [v]

    view.save_channel_policy(None, _Form({view.DEFAULT_FIELD: ["email", "sms"]}))
    assert written["default"] == ["email"]
