"""Campaign parity: steps, canonical senders, scheduling, delivery, suppression.

The suppression tests are the load-bearing ones: a suppressed or unsubscribed
address must never be handed to the transport, whether it was suppressed before
the audience was built, between build and send, or mid-sequence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.comms_campaign import (
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignStatus,
)
from app.models.notification import (
    CommunicationSuppression,
    NotificationChannel,
    SuppressionReason,
    SuppressionScope,
)
from app.models.service_team import ServiceTeam, ServiceTeamType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.models.subscription_engine import SettingValueType
from app.schemas.campaigns import (
    CampaignCreate,
    CampaignSenderCreate,
    CampaignStepCreate,
    CampaignUpdate,
)
from app.services import comms_campaigns
from app.services import communication_eligibility as eligibility
from app.services.domain_settings import comms_settings
from app.tasks import notifications as notification_tasks


@pytest.fixture(autouse=True)
def _allow_immediate_notification_delivery(monkeypatch):
    """Keep delivery assertions independent of the CI runner's wall clock."""
    monkeypatch.setattr(
        "app.services.notification.quiet_hours_send_at",
        lambda _db: None,
    )


def _subscriber(db_session, *, email: str, first_name: str = "Ada") -> Subscriber:
    subscriber = Subscriber(
        first_name=first_name,
        last_name="Nwosu",
        email=email,
        phone="08035550114",
        status=SubscriberStatus.active,
        is_active=True,
        marketing_opt_in=True,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _team(db_session, name: str = "Marketing") -> ServiceTeam:
    existing = db_session.query(ServiceTeam).filter(ServiceTeam.name == name).first()
    if existing is not None:
        return existing
    team = ServiceTeam(name=name, team_type=ServiceTeamType.support.value)
    db_session.add(team)
    db_session.flush()
    return team


def _at(value: datetime | None) -> datetime | None:
    """Normalize a stored timestamp to UTC (SQLite drops the tzinfo)."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@pytest.fixture
def captured_email(monkeypatch) -> list[dict]:
    """Capture emails only when the notification outbox reaches the transport."""
    sent: list[dict] = []

    def _fake_send_email(*args, **kwargs):
        sent.append(kwargs)
        return True

    monkeypatch.setattr(
        notification_tasks.email_service,
        "send_email",
        _fake_send_email,
    )
    return sent


def _deliver_queued(db_session) -> dict[str, int]:
    return notification_tasks._deliver_notification_queue_stats(  # noqa: SLF001
        db_session, batch_size=100
    )


def _campaign(db_session, **overrides):
    payload = {
        "name": "July promo",
        "channel": "email",
        "subject": "Faster fibre in July",
        "body_html": "<p>Hello {{first_name}}</p>",
        "body_text": "Hello {{first_name}}",
        "service_team_id": _team(db_session).id,
    }
    payload.update(overrides)
    return comms_campaigns.create_campaign(db_session, CampaignCreate(**payload))


def _enable_periodic_campaign_admission(db_session) -> None:
    comms_settings.ensure_by_key(
        db_session,
        key="campaign_processing_enabled",
        value_type=SettingValueType.boolean,
        value_text="true",
        value_json=True,
    )


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------


def test_suppressed_address_is_excluded_from_the_audience(db_session, captured_email):
    _subscriber(db_session, email="keep@example.com")
    _subscriber(db_session, email="gone@example.com")
    eligibility.suppress(
        db_session,
        channel=NotificationChannel.email,
        address="GONE@example.com",
        reason=SuppressionReason.unsubscribe,
    )
    campaign = _campaign(db_session)

    audience = comms_campaigns.build_recipient_list(db_session, campaign.id)
    result = comms_campaigns.send_campaign_batch(db_session, campaign.id)

    assert audience.created == 1
    assert audience.skipped_reasons["suppressed"] == 1
    assert result.queued == 1
    assert captured_email == []
    assert _deliver_queued(db_session)["delivered"] == 1
    assert [item["to_email"] for item in captured_email] == ["keep@example.com"]


def test_address_suppressed_after_the_build_is_never_sent_to(
    db_session, captured_email
):
    _subscriber(db_session, email="keep@example.com")
    _subscriber(db_session, email="late@example.com")
    campaign = _campaign(db_session)

    audience = comms_campaigns.build_recipient_list(db_session, campaign.id)
    assert audience.created == 2

    # The unsubscribe lands between the audience build and the send.
    eligibility.suppress(
        db_session,
        channel=NotificationChannel.email,
        address="late@example.com",
        reason=SuppressionReason.unsubscribe,
    )
    result = comms_campaigns.send_campaign_batch(db_session, campaign.id)

    recipients = {
        row.address: row
        for row in db_session.query(CampaignRecipient).filter(
            CampaignRecipient.campaign_id == campaign.id
        )
    }
    assert captured_email == []
    assert result.queued == 1
    # The ledger does not reach into campaign tables to retire recipients -- it
    # does not know campaigns exist. The send gate is what blocks, which is why
    # this counts as suppressed here rather than being quietly pre-retired.
    assert result.suppressed == 1
    assert (
        recipients["late@example.com"].status
        == CampaignRecipientStatus.suppressed.value
    )
    assert recipients["late@example.com"].suppressed_at is not None
    assert recipients["late@example.com"].attempt_count == 0
    # Suppressed rows do not inflate the audience counter.
    assert campaign.total_recipients == 1
    assert _deliver_queued(db_session)["delivered"] == 1
    assert [item["to_email"] for item in captured_email] == ["keep@example.com"]


def test_send_batch_rechecks_suppression_it_did_not_retire(db_session, captured_email):
    """Belt-and-braces: a suppression row that bypassed _suppress_pending_recipients
    (e.g. written directly by an importer) must still block the send."""
    _subscriber(db_session, email="raw@example.com")
    campaign = _campaign(db_session)
    comms_campaigns.build_recipient_list(db_session, campaign.id)

    db_session.add(
        CommunicationSuppression(
            channel=NotificationChannel.email,
            address="raw@example.com",
            raw_address="raw@example.com",
            scope=SuppressionScope.all,
            reason=SuppressionReason.bounce,
        )
    )
    db_session.flush()

    result = comms_campaigns.send_campaign_batch(db_session, campaign.id)

    recipient = (
        db_session.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .one()
    )
    assert captured_email == []
    assert result.queued == 0
    assert result.suppressed == 1
    assert recipient.status == CampaignRecipientStatus.suppressed.value


def test_unsubscribe_token_suppresses_the_address_globally(db_session, captured_email):
    _subscriber(db_session, email="churn@example.com")
    first = _campaign(db_session, name="Blast one")
    comms_campaigns.build_recipient_list(db_session, first.id)
    comms_campaigns.send_campaign_batch(db_session, first.id)

    recipient = (
        db_session.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == first.id)
        .one()
    )
    assert recipient.unsubscribe_token

    suppression = comms_campaigns.unsubscribe_by_token(
        db_session, recipient.unsubscribe_token
    )
    assert suppression.address == "churn@example.com"
    assert suppression.reason is SuppressionReason.unsubscribe
    # Refusing a promo is NOT permission to stop their invoice.
    assert suppression.scope is SuppressionScope.marketing
    # The campaign that prompted it is provenance, not a column on a
    # platform-wide table.
    assert f"campaign={first.id}" in suppression.note

    # A *later, unrelated* campaign must not reach the unsubscribed address.
    captured_email.clear()
    second = _campaign(db_session, name="Blast two")
    audience = comms_campaigns.build_recipient_list(db_session, second.id)
    result = comms_campaigns.send_campaign_batch(db_session, second.id)

    assert audience.created == 0
    assert audience.skipped_reasons["suppressed"] == 1
    assert result.queued == 0
    assert captured_email == []


def test_unsubscribe_link_is_appended_to_the_email_body(db_session, captured_email):
    _subscriber(db_session, email="reader@example.com")
    campaign = _campaign(db_session)
    comms_campaigns.build_recipient_list(db_session, campaign.id)
    comms_campaigns.send_campaign_batch(db_session, campaign.id)
    assert captured_email == []
    assert _deliver_queued(db_session)["delivered"] == 1

    recipient = (
        db_session.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .one()
    )
    body_html = captured_email[0]["body_html"]
    assert "Hello Ada" in body_html  # variable substitution ran
    assert recipient.unsubscribe_token in body_html
    assert "/campaigns/public/unsubscribe/" in body_html


def test_suppression_after_queue_cancels_without_calling_transport(
    db_session, captured_email
):
    subscriber = _subscriber(db_session, email="queued@example.com")
    campaign = _campaign(db_session)
    comms_campaigns.build_recipient_list(db_session, campaign.id)
    result = comms_campaigns.send_campaign_batch(db_session, campaign.id)
    assert result.queued == 1

    eligibility.suppress(
        db_session,
        channel=NotificationChannel.email,
        address=subscriber.email,
        reason=SuppressionReason.unsubscribe,
    )
    stats = _deliver_queued(db_session)

    recipient = db_session.query(CampaignRecipient).one()
    assert stats["suppressed"] == 1
    assert captured_email == []
    assert recipient.status == CampaignRecipientStatus.skipped.value
    assert campaign.status == CampaignStatus.completed.value


def test_unknown_unsubscribe_token_is_a_404(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        comms_campaigns.unsubscribe_by_token(db_session, "not-a-real-token")
    assert excinfo.value.status_code == 404


def test_a_complaint_escalates_an_unsubscribe_and_is_never_downgraded(db_session):
    """One row per (channel, address) -- and severity only ever climbs.

    A spam complaint means the address is unusable, not merely uninterested. If
    a later unsubscribe click could pull the scope back down to `marketing`, we
    would resume sending invoices to an address that reported us.
    """
    eligibility.suppress(
        db_session,
        channel=NotificationChannel.email,
        address="dupe@example.com",
        reason=SuppressionReason.unsubscribe,
    )
    eligibility.suppress(
        db_session,
        channel=NotificationChannel.email,
        address="DUPE@example.com",  # same address, dodging via case
        scope=SuppressionScope.all,
        reason=SuppressionReason.complaint,
    )

    rows = db_session.query(CommunicationSuppression).all()
    assert len(rows) == 1
    assert rows[0].scope is SuppressionScope.all

    # A late unsubscribe must not de-escalate the complaint.
    eligibility.suppress(
        db_session,
        channel=NotificationChannel.email,
        address="dupe@example.com",
        scope=SuppressionScope.marketing,
        reason=SuppressionReason.unsubscribe,
    )
    db_session.refresh(rows[0])
    assert rows[0].scope is SuppressionScope.all

    assert eligibility.unsuppress(
        db_session, channel=NotificationChannel.email, address="dupe@example.com"
    )
    assert eligibility.may_send(
        db_session,
        channel=NotificationChannel.email,
        address="dupe@example.com",
        category="marketing",
    )


# ---------------------------------------------------------------------------
# Step sequencing
# ---------------------------------------------------------------------------


def test_step_waits_for_the_previous_outbox_stage_to_finish(db_session, captured_email):
    _subscriber(db_session, email="queued-stage@example.com")
    campaign = _campaign(db_session, name="Drip", campaign_type="nurture")
    comms_campaigns.create_campaign_step(
        db_session,
        campaign.id,
        CampaignStepCreate(subject="Next", body_html="<p>Next</p>"),
    )
    start = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    comms_campaigns.build_recipient_list(db_session, campaign.id)
    comms_campaigns.send_campaign_batch(db_session, campaign.id, now=start)

    waiting = comms_campaigns.process_due_campaign_steps(db_session, now=start)
    assert waiting["advanced"] == 0
    assert (
        db_session.query(CampaignRecipient)
        .filter(CampaignRecipient.step_id.is_not(None))
        .count()
        == 0
    )

    assert _deliver_queued(db_session)["delivered"] == 1
    advanced = comms_campaigns.process_due_campaign_steps(db_session, now=start)
    assert advanced["advanced"] == 1
    assert advanced["queued"] == 1


def test_steps_fire_in_order_and_only_when_due(db_session, captured_email):
    _subscriber(db_session, email="lead@example.com")
    campaign = _campaign(db_session, name="Onboarding", campaign_type="nurture")
    comms_campaigns.create_campaign_step(
        db_session,
        campaign.id,
        CampaignStepCreate(
            name="Day 2 nudge",
            subject="Getting started",
            body_html="<p>Step one</p>",
            delay_days=2,
        ),
    )
    comms_campaigns.create_campaign_step(
        db_session,
        campaign.id,
        CampaignStepCreate(
            name="Day 5 nudge",
            subject="Need a hand?",
            body_html="<p>Step two</p>",
            delay_days=3,
        ),
    )

    start = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    comms_campaigns.build_recipient_list(db_session, campaign.id)
    initial = comms_campaigns.send_campaign_batch(db_session, campaign.id, now=start)
    assert initial.queued == 1
    assert _deliver_queued(db_session)["delivered"] == 1
    assert campaign.status == CampaignStatus.completed.value
    captured_email.clear()

    # Day 1: nothing is due yet.
    idle = comms_campaigns.process_due_campaign_steps(
        db_session, now=start + timedelta(days=1)
    )
    assert idle["advanced"] == 0
    assert captured_email == []

    # Day 3: step one (delay 2d) is due; step two (cumulative 5d) is not.
    first = comms_campaigns.process_due_campaign_steps(
        db_session, now=start + timedelta(days=3)
    )
    assert first["advanced"] == 1
    assert first["queued"] == 1
    assert captured_email == []
    assert _deliver_queued(db_session)["delivered"] == 1
    assert captured_email[-1]["body_html"].startswith("<p>Step one</p>")
    assert campaign.status == CampaignStatus.completed.value

    # Day 4: still inside step two's cumulative delay.
    assert (
        comms_campaigns.process_due_campaign_steps(
            db_session, now=start + timedelta(days=4)
        )["advanced"]
        == 0
    )

    # Day 6: step two (2 + 3 = 5 days after start) is now due.
    second = comms_campaigns.process_due_campaign_steps(
        db_session, now=start + timedelta(days=6)
    )
    assert second["advanced"] == 1
    assert second["queued"] == 1
    assert _deliver_queued(db_session)["delivered"] == 1
    assert captured_email[-1]["body_html"].startswith("<p>Step two</p>")

    # Nothing left to advance, and no step is ever materialized twice.
    assert (
        comms_campaigns.process_due_campaign_steps(
            db_session, now=start + timedelta(days=30)
        )["advanced"]
        == 0
    )
    step_rows = (
        db_session.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.step_id.is_not(None))
        .all()
    )
    assert len(step_rows) == 2
    assert all(
        row.status == CampaignRecipientStatus.delivered.value for row in step_rows
    )


def test_a_step_does_not_roll_forward_to_an_unsubscribed_recipient(
    db_session, captured_email
):
    _subscriber(db_session, email="stays@example.com")
    _subscriber(db_session, email="leaves@example.com")
    campaign = _campaign(db_session, name="Drip", campaign_type="nurture")
    comms_campaigns.create_campaign_step(
        db_session,
        campaign.id,
        CampaignStepCreate(
            subject="Follow up", body_html="<p>Follow up</p>", delay_days=1
        ),
    )

    start = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    comms_campaigns.build_recipient_list(db_session, campaign.id)
    comms_campaigns.send_campaign_batch(db_session, campaign.id, now=start)
    assert _deliver_queued(db_session)["delivered"] == 2

    leaver = (
        db_session.query(CampaignRecipient)
        .filter(CampaignRecipient.address == "leaves@example.com")
        .one()
    )
    comms_campaigns.unsubscribe_by_token(db_session, leaver.unsubscribe_token)
    captured_email.clear()

    result = comms_campaigns.process_due_campaign_steps(
        db_session, now=start + timedelta(days=2)
    )

    assert result["created"] == 1
    assert result["queued"] == 1
    assert captured_email == []
    assert _deliver_queued(db_session)["delivered"] == 1
    assert [item["to_email"] for item in captured_email] == ["stays@example.com"]
    step_addresses = {
        row.address
        for row in db_session.query(CampaignRecipient).filter(
            CampaignRecipient.step_id.is_not(None)
        )
    }
    assert step_addresses == {"stays@example.com"}


def test_step_content_falls_back_to_the_campaign_subject(db_session, captured_email):
    _subscriber(db_session, email="fallback@example.com")
    campaign = _campaign(db_session, name="Drip", campaign_type="nurture")
    comms_campaigns.create_campaign_step(
        db_session,
        campaign.id,
        CampaignStepCreate(body_html="<p>Only a body</p>", delay_days=1),
    )

    start = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    comms_campaigns.build_recipient_list(db_session, campaign.id)
    comms_campaigns.send_campaign_batch(db_session, campaign.id, now=start)
    assert _deliver_queued(db_session)["delivered"] == 1
    captured_email.clear()

    comms_campaigns.process_due_campaign_steps(
        db_session, now=start + timedelta(days=2)
    )
    assert _deliver_queued(db_session)["delivered"] == 1

    # The step has no subject, so the campaign's subject is reused.
    assert "Faster fibre in July" in captured_email[0]["subject"]
    assert captured_email[0]["body_html"].startswith("<p>Only a body</p>")


def test_a_step_with_recipients_cannot_be_deleted(db_session, captured_email):
    from fastapi import HTTPException

    _subscriber(db_session, email="locked@example.com")
    campaign = _campaign(db_session, name="Drip", campaign_type="nurture")
    step = comms_campaigns.create_campaign_step(
        db_session,
        campaign.id,
        CampaignStepCreate(subject="Ping", body_html="<p>Ping</p>", delay_days=1),
    )
    start = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    comms_campaigns.build_recipient_list(db_session, campaign.id)
    comms_campaigns.send_campaign_batch(db_session, campaign.id, now=start)
    assert _deliver_queued(db_session)["delivered"] == 1
    comms_campaigns.process_due_campaign_steps(
        db_session, now=start + timedelta(days=2)
    )

    with pytest.raises(HTTPException) as excinfo:
        comms_campaigns.delete_campaign_step(db_session, campaign.id, step.id)
    assert excinfo.value.status_code == 409


# ---------------------------------------------------------------------------
# Canonical sender profiles
# ---------------------------------------------------------------------------


def test_campaign_queues_its_canonical_sender_key(
    db_session, captured_email, monkeypatch
):
    _subscriber(db_session, email="target@example.com")

    def _configured_sender(_db, *, sender_key=None, activity=None):
        return {
            "sender_key": sender_key,
            "from_email": "marketing@dotmac.io",
            "activity": activity,
        }

    monkeypatch.setattr(
        comms_campaigns.email_service, "get_smtp_config", _configured_sender
    )
    sender = comms_campaigns.create_sender(
        db_session,
        CampaignSenderCreate(
            name="Dotmac Marketing",
            sender_key="marketing",
        ),
    )
    campaign = _campaign(db_session, campaign_sender_id=sender.id)

    comms_campaigns.build_recipient_list(db_session, campaign.id)
    result = comms_campaigns.send_campaign_batch(db_session, campaign.id)

    assert result.queued == 1
    assert captured_email == []
    assert _deliver_queued(db_session)["delivered"] == 1
    assert captured_email[0]["sender_key"] == "marketing"
    assert captured_email[0]["to_email"] == "target@example.com"


def test_unconfigured_campaign_sender_fails_closed(db_session, monkeypatch):
    from fastapi import HTTPException

    _subscriber(db_session, email="target@example.com")
    sender = comms_campaigns.create_sender(
        db_session,
        CampaignSenderCreate(name="Marketing", sender_key="marketing"),
    )
    campaign = _campaign(db_session, campaign_sender_id=sender.id)
    comms_campaigns.build_recipient_list(db_session, campaign.id)

    monkeypatch.setattr(
        comms_campaigns.email_service,
        "get_smtp_config",
        lambda _db, **_kwargs: {
            "sender_key": "default",
            "from_email": "billing@dotmac.io",
        },
    )

    with pytest.raises(HTTPException) as excinfo:
        comms_campaigns.send_campaign_batch(db_session, campaign.id)
    assert excinfo.value.status_code == 409


def test_campaign_without_a_profile_queues_the_team_sender(db_session, captured_email):
    _subscriber(db_session, email="target@example.com")
    campaign = _campaign(db_session)

    comms_campaigns.build_recipient_list(db_session, campaign.id)
    result = comms_campaigns.send_campaign_batch(db_session, campaign.id)

    assert result.queued == 1
    assert captured_email == []
    assert _deliver_queued(db_session)["delivered"] == 1
    assert captured_email[0]["to_email"] == "target@example.com"


def test_inactive_campaign_sender_fails_closed(db_session):
    from fastapi import HTTPException

    _subscriber(db_session, email="target@example.com")
    sender = comms_campaigns.create_sender(
        db_session,
        CampaignSenderCreate(name="Marketing", sender_key="marketing"),
    )
    campaign = _campaign(db_session, campaign_sender_id=sender.id)
    comms_campaigns.build_recipient_list(db_session, campaign.id)
    sender.is_active = False
    db_session.flush()

    with pytest.raises(HTTPException) as excinfo:
        comms_campaigns.send_campaign_batch(db_session, campaign.id)
    assert excinfo.value.status_code == 409
    recipient = db_session.query(CampaignRecipient).one()
    assert recipient.status == CampaignRecipientStatus.pending.value
    assert recipient.attempt_count == 0


# ---------------------------------------------------------------------------
# Scheduling + send windows
# ---------------------------------------------------------------------------


def test_periodic_campaign_admission_is_rejected_while_gate_is_closed(db_session):
    from fastapi import HTTPException

    scheduled_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    with pytest.raises(HTTPException) as create_error:
        _campaign(db_session, scheduled_at=scheduled_at)
    assert create_error.value.status_code == 409

    draft = _campaign(db_session)
    with pytest.raises(HTTPException) as update_error:
        comms_campaigns.update_campaign(
            db_session,
            draft.id,
            CampaignUpdate(
                status=CampaignStatus.scheduled.value,
                scheduled_at=scheduled_at,
            ),
        )
    assert update_error.value.status_code == 409


def test_scheduled_campaign_is_picked_up_when_due(db_session, captured_email):
    _subscriber(db_session, email="target@example.com")
    _enable_periodic_campaign_admission(db_session)
    scheduled_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    campaign = _campaign(db_session, scheduled_at=scheduled_at)
    assert campaign.status == CampaignStatus.scheduled.value

    early = comms_campaigns.process_due_campaigns(
        db_session, now=scheduled_at - timedelta(minutes=5)
    )
    assert early["campaigns"] == 0
    assert captured_email == []

    due = comms_campaigns.process_due_campaigns(
        db_session, now=scheduled_at + timedelta(minutes=1)
    )
    assert due["built"] == 1
    assert due["queued"] == 1
    assert captured_email == []
    assert _deliver_queued(db_session)["delivered"] == 1
    assert campaign.status == CampaignStatus.completed.value


def test_a_due_campaign_outside_its_send_window_is_deferred(db_session, captured_email):
    _subscriber(db_session, email="target@example.com")
    _enable_periodic_campaign_admission(db_session)
    scheduled_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    campaign = _campaign(
        db_session,
        scheduled_at=scheduled_at,
        # 09:00-17:00 Africa/Lagos == 08:00-16:00 UTC.
        send_window_start_hour=9,
        send_window_end_hour=17,
        send_window_timezone="Africa/Lagos",
    )

    # 06:00 UTC == 07:00 Lagos: before the window opens.
    outside = comms_campaigns.process_due_campaigns(
        db_session, now=datetime(2026, 7, 2, 6, 0, tzinfo=UTC)
    )
    assert outside["deferred"] == 1
    assert outside["sent"] == 0
    assert campaign.status == CampaignStatus.scheduled.value
    assert captured_email == []

    # 10:00 UTC == 11:00 Lagos: inside the window.
    inside = comms_campaigns.process_due_campaigns(
        db_session, now=datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
    )
    assert inside["queued"] == 1
    assert _deliver_queued(db_session)["delivered"] == 1
    assert campaign.status == CampaignStatus.completed.value


def test_send_window_wrapping_midnight(db_session):
    campaign = _campaign(
        db_session,
        send_window_start_hour=20,
        send_window_end_hour=6,
        send_window_timezone="UTC",
    )
    assert comms_campaigns.within_send_window(
        campaign, datetime(2026, 7, 1, 22, 0, tzinfo=UTC)
    )
    assert comms_campaigns.within_send_window(
        campaign, datetime(2026, 7, 1, 3, 0, tzinfo=UTC)
    )
    assert not comms_campaigns.within_send_window(
        campaign, datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    )


def test_a_campaign_without_a_window_always_sends(db_session):
    campaign = _campaign(db_session)
    assert comms_campaigns.within_send_window(
        campaign, datetime(2026, 7, 1, 3, 0, tzinfo=UTC)
    )


def test_send_window_requires_both_bounds_and_a_valid_timezone(db_session):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as incomplete:
        _campaign(db_session, send_window_start_hour=9)
    assert incomplete.value.status_code == 400

    with pytest.raises(HTTPException) as invalid_timezone:
        _campaign(
            db_session,
            send_window_start_hour=9,
            send_window_end_hour=17,
            send_window_timezone="Not/A_Timezone",
        )
    assert invalid_timezone.value.status_code == 400


def test_due_campaign_processing_continues_a_second_batch(db_session):
    for index in range(101):
        _subscriber(db_session, email=f"batch-{index}@example.com")
    _enable_periodic_campaign_admission(db_session)
    scheduled_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    campaign = _campaign(db_session, scheduled_at=scheduled_at)

    first = comms_campaigns.process_due_campaigns(
        db_session, now=scheduled_at + timedelta(minutes=1)
    )
    second = comms_campaigns.process_due_campaigns(
        db_session, now=scheduled_at + timedelta(minutes=2)
    )

    assert first["built"] == 1
    assert first["queued"] == 100
    assert second["built"] == 0
    assert second["queued"] == 1
    assert campaign.status == CampaignStatus.sending.value


# ---------------------------------------------------------------------------
# Delivery tracking
# ---------------------------------------------------------------------------


def test_delivery_attempts_and_confirmation_are_tracked(db_session, captured_email):
    _subscriber(db_session, email="target@example.com")
    campaign = _campaign(db_session)
    now = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)

    comms_campaigns.build_recipient_list(db_session, campaign.id)
    result = comms_campaigns.send_campaign_batch(db_session, campaign.id, now=now)

    recipient = (
        db_session.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .one()
    )
    assert recipient.attempt_count == 1
    assert _at(recipient.last_attempt_at) == now
    assert recipient.status == CampaignRecipientStatus.queued.value
    assert recipient.sent_at is None
    assert recipient.delivered_at is None
    assert result.queued == 1
    assert campaign.sent_count == 0
    assert campaign.delivered_count == 0
    assert captured_email == []

    assert _deliver_queued(db_session)["delivered"] == 1

    assert recipient.status == CampaignRecipientStatus.delivered.value
    assert recipient.delivered_at is not None
    assert campaign.delivered_count == 1
    assert campaign.sent_count == 0
    assert campaign.status == CampaignStatus.completed.value


def test_a_failed_send_records_the_reason_and_the_attempt(db_session, monkeypatch):
    _subscriber(db_session, email="target@example.com")

    monkeypatch.setattr(
        notification_tasks.email_service,
        "send_email",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(notification_tasks, "_max_retries", lambda _db: 1)
    campaign = _campaign(db_session)
    now = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)

    comms_campaigns.build_recipient_list(db_session, campaign.id)
    result = comms_campaigns.send_campaign_batch(db_session, campaign.id, now=now)
    stats = _deliver_queued(db_session)

    recipient = (
        db_session.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .one()
    )
    assert result.queued == 1
    assert stats["failed"] == 1
    assert recipient.status == CampaignRecipientStatus.failed.value
    assert recipient.failed_reason
    assert recipient.attempt_count == 1
    assert _at(recipient.last_attempt_at) == now
    assert campaign.failed_count == 1
