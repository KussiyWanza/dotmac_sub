from __future__ import annotations

import html
import re
import secrets
import zoneinfo
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.comms_campaign import (
    Campaign,
    CampaignChannel,
    CampaignRecipient,
    CampaignRecipientStatus,
    CampaignSender,
    CampaignStatus,
    CampaignStep,
    CampaignType,
)
from app.models.domain_settings import SettingDomain
from app.models.notification import (
    CommunicationSuppression,
    NotificationChannel,
    NotificationStatus,
    SuppressionReason,
    SuppressionScope,
)
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.models.team_inbox import (
    InboxConversation,
)
from app.services import (
    communication_eligibility,
    team_inbox_campaigns,
    team_inbox_routing,
    team_outbound,
)
from app.services import email as email_service
from app.services.common import coerce_uuid
from app.services.communication_intents import (
    CommunicationClass,
    CommunicationIntent,
    submit,
)
from app.services.customer_identity_normalization import normalize_phone_identifier
from app.services.settings_spec import resolve_boolean

NON_CONTACTABLE_STATUSES = {
    SubscriberStatus.disabled.value,
    SubscriberStatus.canceled.value,
}

DEFAULT_SEND_WINDOW_TIMEZONE = "Africa/Lagos"
UNSUBSCRIBE_PATH = "/api/v1/campaigns/public/unsubscribe"

# Recipient states that must never be handed to the transport again.
TERMINAL_RECIPIENT_STATUSES = {
    CampaignRecipientStatus.suppressed.value,
    CampaignRecipientStatus.skipped.value,
}

_VARIABLE_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class CampaignAudienceBuildResult:
    campaign_id: UUID
    created: int
    skipped: int
    existing: int
    total_recipients: int
    skipped_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CampaignSendResult:
    campaign_id: UUID
    queued: int
    sent: int
    failed: int
    skipped: int
    completed: bool
    suppressed: int = 0


@dataclass(frozen=True)
class CampaignStepMaterializeResult:
    campaign_id: UUID
    step_id: UUID
    created: int
    suppressed: int


@dataclass(frozen=True)
class RenderedCampaignMessage:
    subject: str
    body_html: str | None
    body_text: str | None
    metadata: dict[str, object]
    from_address: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _assert_periodic_campaign_admission_enabled(db: Session) -> None:
    """Reject new scheduled intent while the rollout admission gate is closed."""

    if resolve_boolean(db, SettingDomain.comms, "campaign_processing_enabled"):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "Scheduled campaign admission is disabled. Draft and manual-send "
            "workflows remain available."
        ),
    )


def _campaign_or_404(db: Session, campaign_id: str | UUID) -> Campaign:
    campaign = db.get(Campaign, coerce_uuid(campaign_id))
    if campaign is None or not campaign.is_active:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _campaign_for_update_or_404(db: Session, campaign_id: str | UUID) -> Campaign:
    campaign = (
        db.query(Campaign)
        .filter(Campaign.id == coerce_uuid(campaign_id))
        .filter(Campaign.is_active.is_(True))
        .with_for_update()
        .one_or_none()
    )
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def _validate_campaign_values(campaign: Campaign) -> None:
    if campaign.channel not in {item.value for item in CampaignChannel}:
        raise HTTPException(status_code=400, detail="Invalid campaign channel")
    if campaign.campaign_type not in {item.value for item in CampaignType}:
        raise HTTPException(status_code=400, detail="Invalid campaign type")
    if campaign.channel == CampaignChannel.email.value and not (
        campaign.body_html or campaign.body_text
    ):
        raise HTTPException(status_code=400, detail="Email body is required")
    if campaign.channel == CampaignChannel.email.value and not campaign.subject:
        raise HTTPException(status_code=400, detail="Email subject is required")
    if campaign.channel == CampaignChannel.whatsapp.value and not (
        campaign.body_text or campaign.whatsapp_template_name
    ):
        raise HTTPException(
            status_code=400,
            detail="WhatsApp campaign needs body_text or a template name",
        )
    has_window_start = campaign.send_window_start_hour is not None
    has_window_end = campaign.send_window_end_hour is not None
    if has_window_start != has_window_end:
        raise HTTPException(
            status_code=400,
            detail="Send window start and end must be configured together",
        )
    if campaign.send_window_timezone:
        try:
            zoneinfo.ZoneInfo(campaign.send_window_timezone.strip())
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="Invalid send window timezone"
            ) from exc


#: Everything a campaign sends is marketing. This is what makes an unsubscribe
#: actually stop it -- a marketing-scoped suppression blocks this category and
#: leaves invoices alone.
MARKETING_CATEGORY = "marketing"


#: Financial segment values. Marketing that cannot see service state is the
#: reason a separate CRM was worth replacing — not upselling a subscriber who
#: is in arrears is the concrete case. The values name the *canonical*
#: financial meanings rather than inventing a generic "debt" notion:
#: ``customer.financial_position`` deliberately keeps due and overdue distinct.
FINANCIAL_SEGMENTS = {
    "no_due_debt",
    "has_due_debt",
    "has_overdue_debt",
}


def _financial_segment(campaign: Campaign) -> str | None:
    segment = (
        campaign.segment_filter if isinstance(campaign.segment_filter, dict) else {}
    )
    value = str(segment.get("financial_position") or "").strip().lower()
    return value if value in FINANCIAL_SEGMENTS else None


def _financial_excluded(db: Session, campaign: Campaign, subscribers: list) -> set:
    """Subscriber ids the financial segment excludes.

    Read in bulk for the same reason suppression is: a campaign audience is
    thousands of accounts, and the per-account financial reads issue several
    queries each. The predicate comes from ``invoice_collectibility``, the
    owner of what due and overdue mean, so a segment can never disagree with
    the customer's own invoice page.
    """
    wanted = _financial_segment(campaign)
    if wanted is None or not subscribers:
        return set()
    from app.services.invoice_collectibility import (
        accounts_with_due_debt,
        accounts_with_overdue_debt,
    )

    ids = [item.id for item in subscribers]
    if wanted == "has_overdue_debt":
        matching = accounts_with_overdue_debt(db, ids)
        return {item.id for item in subscribers if item.id not in matching}
    matching = accounts_with_due_debt(db, ids)
    if wanted == "has_due_debt":
        return {item.id for item in subscribers if item.id not in matching}
    # no_due_debt
    return {item.id for item in subscribers if item.id in matching}


def _blocked_addresses(
    db: Session, *, channel, addresses: list[str | None]
) -> set[str]:
    """Addresses the platform ledger says we may not send MARKETING to.

    One helper, used by every campaign path that needs the answer. Three inline
    copies of `filter_eligible` would be three chances to drift -- which is the
    bug this whole slice exists to remove.
    """
    clean = [a for a in addresses if a]
    if not clean:
        return set()
    eligible = set(
        communication_eligibility.filter_eligible(
            db,
            channel=channel,
            addresses=clean,
            category=MARKETING_CATEGORY,
        )
    )
    return {a for a in clean if a not in eligible}


def _segment_query(db: Session, campaign: Campaign):
    segment = (
        campaign.segment_filter if isinstance(campaign.segment_filter, dict) else {}
    )
    query = db.query(Subscriber).outerjoin(
        Reseller, Subscriber.reseller_id == Reseller.id
    )
    query = query.filter(Subscriber.is_active.is_(True))
    query = query.filter(Subscriber.status.notin_(NON_CONTACTABLE_STATUSES))
    query = query.filter(Subscriber.marketing_opt_in.is_(True))
    query = query.filter(
        or_(Subscriber.reseller_id.is_(None), Reseller.is_active.is_(True))
    )

    raw_status = segment.get("status")
    if raw_status:
        statuses = raw_status if isinstance(raw_status, list) else [raw_status]
        query = query.filter(Subscriber.status.in_([str(item) for item in statuses]))

    raw_reseller_id = segment.get("reseller_id")
    if raw_reseller_id:
        query = query.filter(Subscriber.reseller_id == coerce_uuid(raw_reseller_id))

    raw_category = segment.get("subscriber_category") or segment.get("category")
    if raw_category:
        categories = raw_category if isinstance(raw_category, list) else [raw_category]
        category_values = [str(item).strip().lower() for item in categories if item]
        if category_values:
            query = query.filter(
                Subscriber.metadata_["subscriber_category"].astext.in_(category_values)
            )

    raw_billing_mode = segment.get("billing_mode")
    if raw_billing_mode:
        modes = (
            raw_billing_mode
            if isinstance(raw_billing_mode, list)
            else [raw_billing_mode]
        )
        query = query.filter(Subscriber.billing_mode.in_([str(item) for item in modes]))

    limit = segment.get("limit")
    if isinstance(limit, int) and limit > 0:
        query = query.limit(limit)
    return query


def _recipient_address(
    campaign: Campaign, subscriber: Subscriber
) -> tuple[str | None, str | None]:
    if campaign.channel == CampaignChannel.email.value:
        email = team_inbox_routing.normalize_email_address(subscriber.email)
        return email, email
    if campaign.channel == CampaignChannel.whatsapp.value:
        phone = normalize_phone_identifier(subscriber.phone)
        return phone, None
    return None, None


# ---------------------------------------------------------------------------
# Suppression / unsubscribe
#
# Suppression is the one control in this module that is correctness-critical:
# a suppressed address must never receive a campaign message. It is enforced
# twice on purpose — once when the audience is built (so the list is honest)
# and again immediately before each send (because an unsubscribe can land
# between the build and the send).
# ---------------------------------------------------------------------------


def unsubscribe_by_token(
    db: Session, token: str, *, source: str = "unsubscribe_link"
) -> CommunicationSuppression:
    """Honor a one-click unsubscribe link.

    The token is minted per recipient row, so it identifies both the address to
    suppress and the campaign that prompted it.

    This writes to the PLATFORM ledger, not a campaign-local table. That is the
    whole point: an unsubscribe must silence the customer everywhere, not just
    in the campaign module that happened to carry the link. It is scoped to
    ``marketing`` -- the customer refused promotions, not their invoice.
    """
    clean_token = (token or "").strip()
    if not clean_token:
        raise HTTPException(status_code=404, detail="Unknown unsubscribe token")
    recipient = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.unsubscribe_token == clean_token)
        .first()
    )
    if recipient is None:
        raise HTTPException(status_code=404, detail="Unknown unsubscribe token")
    campaign = db.get(Campaign, recipient.campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Unknown unsubscribe token")

    suppression = communication_eligibility.suppress(
        db,
        channel=campaign.channel,
        address=recipient.address,
        scope=SuppressionScope.marketing,
        reason=SuppressionReason.unsubscribe,
        subscriber_id=recipient.subscriber_id,
        note=f"campaign={campaign.id} source={source}",
        created_by=source,
    )
    return suppression


# ---------------------------------------------------------------------------
# Sender profiles
# ---------------------------------------------------------------------------


def list_senders(
    db: Session, *, is_active: bool | None = None, limit: int = 50, offset: int = 0
) -> list[CampaignSender]:
    query = db.query(CampaignSender)
    if is_active is not None:
        query = query.filter(CampaignSender.is_active.is_(is_active))
    return query.order_by(CampaignSender.name.asc()).limit(limit).offset(offset).all()


def _sender_or_404(db: Session, sender_id: str | UUID) -> CampaignSender:
    sender = db.get(CampaignSender, coerce_uuid(sender_id))
    if sender is None:
        raise HTTPException(status_code=404, detail="Campaign sender not found")
    return sender


def create_sender(db: Session, payload) -> CampaignSender:
    sender = CampaignSender(
        name=payload.name,
        sender_key=payload.sender_key.strip().lower(),
        is_active=payload.is_active,
        metadata_=payload.metadata or {},
    )
    db.add(sender)
    db.flush()
    return sender


def update_sender(db: Session, sender_id: str | UUID, payload) -> CampaignSender:
    sender = _sender_or_404(db, sender_id)
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        if field_name == "metadata":
            sender.metadata_ = value or {}
        elif field_name == "sender_key" and value is not None:
            sender.sender_key = value.strip().lower()
        else:
            setattr(sender, field_name, value)
    db.flush()
    return sender


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def list_campaign_steps(db: Session, campaign_id: str | UUID) -> list[CampaignStep]:
    return (
        db.query(CampaignStep)
        .filter(CampaignStep.campaign_id == coerce_uuid(campaign_id))
        .order_by(CampaignStep.step_index.asc())
        .all()
    )


def _step_or_404(
    db: Session, campaign_id: str | UUID, step_id: str | UUID
) -> CampaignStep:
    step = db.get(CampaignStep, coerce_uuid(step_id))
    if step is None or step.campaign_id != coerce_uuid(campaign_id):
        raise HTTPException(status_code=404, detail="Campaign step not found")
    return step


def _validate_step_content(campaign: Campaign, step: CampaignStep) -> None:
    if campaign.channel != CampaignChannel.email.value:
        return
    if not (
        step.body_html or step.body_text or campaign.body_html or campaign.body_text
    ):
        raise HTTPException(status_code=400, detail="Step body is required")
    if not (step.subject or campaign.subject):
        raise HTTPException(status_code=400, detail="Step subject is required")


def create_campaign_step(db: Session, campaign_id: str | UUID, payload) -> CampaignStep:
    campaign = _campaign_or_404(db, campaign_id)
    if campaign.campaign_type != CampaignType.nurture.value:
        raise HTTPException(status_code=409, detail="Only nurture campaigns have steps")
    if campaign.status == CampaignStatus.sending.value:
        raise HTTPException(
            status_code=409, detail="Sending campaigns cannot be edited"
        )
    step_index = payload.step_index
    if step_index is None:
        highest = (
            db.query(CampaignStep.step_index)
            .filter(CampaignStep.campaign_id == campaign.id)
            .order_by(CampaignStep.step_index.desc())
            .first()
        )
        step_index = 0 if highest is None else int(highest[0]) + 1
    clash = (
        db.query(CampaignStep.id)
        .filter(CampaignStep.campaign_id == campaign.id)
        .filter(CampaignStep.step_index == step_index)
        .first()
    )
    if clash is not None:
        raise HTTPException(status_code=409, detail="Step index already used")

    step = CampaignStep(
        campaign_id=campaign.id,
        step_index=step_index,
        name=payload.name,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        delay_days=payload.delay_days,
        delay_hours=payload.delay_hours,
        is_active=payload.is_active,
    )
    _validate_step_content(campaign, step)
    db.add(step)
    db.flush()
    return step


def update_campaign_step(
    db: Session, campaign_id: str | UUID, step_id: str | UUID, payload
) -> CampaignStep:
    campaign = _campaign_or_404(db, campaign_id)
    step = _step_or_404(db, campaign.id, step_id)
    if campaign.status == CampaignStatus.sending.value:
        raise HTTPException(
            status_code=409, detail="Sending campaigns cannot be edited"
        )
    if (
        db.query(CampaignRecipient.id)
        .filter(CampaignRecipient.step_id == step.id)
        .first()
        is not None
    ):
        raise HTTPException(
            status_code=409,
            detail="Step already has recipients and cannot be edited",
        )
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(step, field_name, value)
    _validate_step_content(campaign, step)
    db.flush()
    return step


def delete_campaign_step(
    db: Session, campaign_id: str | UUID, step_id: str | UUID
) -> None:
    campaign = _campaign_or_404(db, campaign_id)
    step = _step_or_404(db, campaign.id, step_id)
    sent = (
        db.query(CampaignRecipient.id)
        .filter(CampaignRecipient.step_id == step.id)
        .first()
    )
    if sent is not None:
        raise HTTPException(
            status_code=409, detail="Step already has recipients and cannot be deleted"
        )
    db.delete(step)
    db.flush()


# ---------------------------------------------------------------------------
# Send windows
# ---------------------------------------------------------------------------


def _send_window_timezone(campaign: Campaign) -> zoneinfo.ZoneInfo:
    name = (campaign.send_window_timezone or DEFAULT_SEND_WINDOW_TIMEZONE).strip()
    return zoneinfo.ZoneInfo(name)


def within_send_window(campaign: Campaign, now: datetime) -> bool:
    """True when `now` falls inside the campaign's local send window.

    A campaign without a window is always sendable. Windows that wrap midnight
    (start > end, e.g. 20:00 -> 06:00) are supported.
    """
    start = campaign.send_window_start_hour
    end = campaign.send_window_end_hour
    if start is None or end is None:
        return True
    if start == end:
        return True
    local_hour = now.astimezone(_send_window_timezone(campaign)).hour
    if start < end:
        return start <= local_hour < end
    return local_hour >= start or local_hour < end


def create_campaign(
    db: Session, payload, *, created_by_system_user_id: str | UUID | None = None
) -> Campaign:
    if payload.scheduled_at is not None:
        _assert_periodic_campaign_admission_enabled(db)
    campaign = Campaign(
        name=payload.name,
        campaign_type=payload.campaign_type,
        channel=payload.channel,
        status=CampaignStatus.scheduled.value
        if payload.scheduled_at
        else CampaignStatus.draft.value,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        whatsapp_template_name=payload.whatsapp_template_name,
        whatsapp_template_language=payload.whatsapp_template_language,
        whatsapp_template_components=payload.whatsapp_template_components,
        segment_filter=payload.segment_filter or {},
        scheduled_at=payload.scheduled_at,
        send_window_start_hour=payload.send_window_start_hour,
        send_window_end_hour=payload.send_window_end_hour,
        send_window_timezone=payload.send_window_timezone,
        created_by_system_user_id=coerce_uuid(created_by_system_user_id),
        campaign_sender_id=payload.campaign_sender_id,
        service_team_id=payload.service_team_id,
        connector_config_id=payload.connector_config_id,
        metadata_=payload.metadata or {},
    )
    if payload.campaign_sender_id is not None:
        _sender_or_404(db, payload.campaign_sender_id)
    _validate_campaign_values(campaign)
    db.add(campaign)
    db.flush()
    return campaign


def update_campaign(db: Session, campaign_id: str | UUID, payload) -> Campaign:
    campaign = _campaign_or_404(db, campaign_id)
    if campaign.status == CampaignStatus.sending.value:
        raise HTTPException(
            status_code=409, detail="Sending campaigns cannot be edited"
        )
    changes = payload.model_dump(exclude_unset=True)
    if (
        changes.get("status") == CampaignStatus.scheduled.value
        and campaign.status != CampaignStatus.scheduled.value
    ):
        _assert_periodic_campaign_admission_enabled(db)
    for field_name, value in changes.items():
        if field_name == "metadata":
            campaign.metadata_ = value or {}
        elif field_name == "campaign_sender_id" and value is not None:
            campaign.campaign_sender_id = _sender_or_404(db, value).id
        else:
            setattr(campaign, field_name, value)
    _validate_campaign_values(campaign)
    db.flush()
    return campaign


def list_campaigns(
    db: Session,
    *,
    status: str | None = None,
    channel: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Campaign]:
    query = db.query(Campaign).filter(Campaign.is_active.is_(True))
    if status:
        query = query.filter(Campaign.status == status)
    if channel:
        query = query.filter(Campaign.channel == channel)
    return (
        query.order_by(Campaign.created_at.desc(), Campaign.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def list_campaign_recipients(
    db: Session,
    campaign_id: str | UUID,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[CampaignRecipient]:
    query = db.query(CampaignRecipient).filter(
        CampaignRecipient.campaign_id == coerce_uuid(campaign_id)
    )
    if status:
        query = query.filter(CampaignRecipient.status == status)
    return (
        query.order_by(CampaignRecipient.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )


def build_recipient_list(
    db: Session,
    campaign_id: str | UUID,
    *,
    limit: int | None = None,
) -> CampaignAudienceBuildResult:
    campaign = _campaign_or_404(db, campaign_id)
    _validate_campaign_values(campaign)
    created = 0
    existing = 0
    skipped: Counter[str] = Counter()
    query = _segment_query(db, campaign)
    if limit is not None and limit > 0:
        query = query.limit(limit)

    subscribers = query.all()
    # Suppression is checked in bulk here so a large audience build stays one
    # extra query rather than one per candidate.
    blocked = _blocked_addresses(
        db,
        channel=campaign.channel,
        addresses=[_recipient_address(campaign, item)[0] for item in subscribers],
    )
    financially_excluded = _financial_excluded(db, campaign, subscribers)

    for subscriber in subscribers:
        address, email = _recipient_address(campaign, subscriber)
        if not address:
            skipped["missing_address"] += 1
            continue
        if (
            subscriber.status.value in NON_CONTACTABLE_STATUSES
            or not subscriber.is_active
        ):
            skipped["inactive_subscriber"] += 1
            continue
        if subscriber.reseller is not None and not subscriber.reseller.is_active:
            skipped["inactive_reseller"] += 1
            continue
        if address in blocked:
            skipped["suppressed"] += 1
            continue
        if subscriber.id in financially_excluded:
            skipped["financial_position"] += 1
            continue
        already_exists = (
            db.query(CampaignRecipient.id)
            .filter(CampaignRecipient.campaign_id == campaign.id)
            .filter(CampaignRecipient.subscriber_id == subscriber.id)
            .filter(CampaignRecipient.step_id.is_(None))
            .first()
            is not None
        )
        if already_exists:
            existing += 1
            continue
        recipient = CampaignRecipient(
            campaign_id=campaign.id,
            subscriber_id=subscriber.id,
            address=address,
            email=email,
            status=CampaignRecipientStatus.pending.value,
            unsubscribe_token=_new_unsubscribe_token(),
            metadata_={"source": "native_campaign_audience"},
        )
        db.add(recipient)
        db.flush()
        created += 1

    campaign.total_recipients = _countable_recipients(db, campaign)
    metadata = dict(campaign.metadata_ or {})
    metadata["last_audience_build"] = {
        "created": created,
        "existing": existing,
        "skipped": sum(skipped.values()),
        "skipped_reasons": dict(skipped),
        "built_at": _now().isoformat(),
    }
    campaign.metadata_ = metadata
    db.flush()
    return CampaignAudienceBuildResult(
        campaign_id=campaign.id,
        created=created,
        skipped=sum(skipped.values()),
        existing=existing,
        total_recipients=campaign.total_recipients,
        skipped_reasons=dict(skipped),
    )


def _conversation_for_recipient(
    db: Session,
    *,
    campaign: Campaign,
    recipient: CampaignRecipient,
    now: datetime,
) -> InboxConversation:
    """Request the campaign conversation from the inbox owner family —
    campaigns decide audience/content; the inbox owns its rows
    (test_team_inbox_boundaries)."""
    return team_inbox_campaigns.ensure_campaign_conversation(
        db,
        subscriber_id=recipient.subscriber_id,
        channel_type=campaign.channel,
        campaign_id=campaign.id,
        campaign_recipient_id=recipient.id,
        subject=campaign.subject or campaign.name,
        contact_address=recipient.address,
        service_team_id=campaign.service_team_id,
        now=now,
    )


def _new_unsubscribe_token() -> str:
    return secrets.token_urlsafe(32)[:64]


def _countable_recipients(db: Session, campaign: Campaign) -> int:
    """Recipients that count towards the campaign audience.

    Skipped and suppressed rows are excluded: neither was, nor will be, mailed.
    """
    return (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.status.notin_(sorted(TERMINAL_RECIPIENT_STATUSES)))
        .count()
    )


def unsubscribe_url(db: Session, recipient: CampaignRecipient) -> str | None:
    if not recipient.unsubscribe_token:
        return None
    from app.services.email import _get_app_url

    base = (_get_app_url(db) or "").rstrip("/")
    return f"{base}{UNSUBSCRIBE_PATH}/{recipient.unsubscribe_token}"


def _render_template(template: str | None, variables: dict[str, str]) -> str | None:
    if not template:
        return template

    def _replace(match: re.Match[str]) -> str:
        return variables.get(match.group(1), match.group(0))

    return _VARIABLE_PATTERN.sub(_replace, template)


def _step_content(
    campaign: Campaign, step: CampaignStep | None
) -> tuple[str | None, str | None, str | None]:
    """Subject/html/text for this send, with step content overriding the campaign."""
    if step is None:
        return campaign.subject, campaign.body_html, campaign.body_text
    subject = step.subject or campaign.subject
    body_html = step.body_html or (None if step.body_text else campaign.body_html)
    body_text = step.body_text or (None if step.body_html else campaign.body_text)
    return subject, body_html, body_text


def _append_unsubscribe_footer(
    body_html: str | None, body_text: str | None, url: str
) -> tuple[str | None, str | None]:
    escaped = html.escape(url, quote=True)
    footer_html = (
        f'<p style="font-size:12px;color:#888">'
        f'<a href="{escaped}">Unsubscribe from these emails</a></p>'
    )
    return (
        f"{body_html}{footer_html}" if body_html else body_html,
        f"{body_text}\n\nUnsubscribe: {url}" if body_text else body_text,
    )


def _sender_delivery_metadata(
    db: Session, campaign: Campaign
) -> tuple[dict[str, object], str | None]:
    if campaign.channel != CampaignChannel.email.value:
        return (
            {
                "service_team_id": (
                    str(campaign.service_team_id) if campaign.service_team_id else None
                )
            },
            None,
        )
    default_sender = team_outbound.resolve_team_email_sender(
        db,
        service_team_id=campaign.service_team_id,
        fallback_activity="notification_queue",
    )
    activity = default_sender.activity or "notification_queue"
    if campaign.sender is not None:
        if not campaign.sender.is_active:
            raise HTTPException(status_code=409, detail="Campaign sender is inactive")
        requested_key = campaign.sender.sender_key
        config = email_service.get_smtp_config(
            db,
            sender_key=requested_key,
            activity=activity,
        )
    else:
        requested_key = default_sender.sender_key
        config = default_sender.config
    resolved_key = str(config.get("sender_key") or "") or None
    if requested_key and resolved_key != requested_key:
        raise HTTPException(
            status_code=409,
            detail=f"Campaign sender is not configured: {requested_key}",
        )
    return (
        {
            "sender_key": resolved_key,
            "activity": activity,
            "service_team_id": (
                str(campaign.service_team_id) if campaign.service_team_id else None
            ),
        },
        str(config.get("from_email") or config.get("from_addr") or "") or None,
    )


def _render_message_for_recipient(
    db: Session,
    campaign: Campaign,
    recipient: CampaignRecipient,
    *,
    delivery_metadata: dict[str, object],
    from_address: str | None,
    step: CampaignStep | None = None,
) -> RenderedCampaignMessage:
    metadata: dict[str, object] = {
        "source_route": "native_campaign",
        "campaign_id": str(campaign.id),
        "campaign_recipient_id": str(recipient.id),
        **delivery_metadata,
    }
    if step is not None:
        metadata["campaign_step_id"] = str(step.id)
        metadata["campaign_step_index"] = step.step_index
    if (
        campaign.channel == CampaignChannel.whatsapp.value
        and campaign.whatsapp_template_name
    ):
        metadata["whatsapp_template"] = {
            "name": campaign.whatsapp_template_name,
            "language": campaign.whatsapp_template_language or "en",
            "variables": campaign.whatsapp_template_components or {},
        }

    subject, body_html, body_text = _step_content(campaign, step)
    subscriber = recipient.subscriber
    link = unsubscribe_url(db, recipient) or ""
    variables = {
        "first_name": getattr(subscriber, "first_name", None) or "",
        "last_name": getattr(subscriber, "last_name", None) or "",
        "email": recipient.email or "",
        "unsubscribe_url": link,
        "campaign_name": campaign.name,
    }
    subject = _render_template(subject, variables)
    body_html = _render_template(body_html, variables)
    body_text = _render_template(body_text, variables)

    # Every marketing email carries an unsubscribe path. If the template did not
    # place the link itself, append a footer rather than ship a mail with no way out.
    if (
        campaign.channel == CampaignChannel.email.value
        and link
        and link not in (body_html or "")
        and link not in (body_text or "")
    ):
        body_html, body_text = _append_unsubscribe_footer(body_html, body_text, link)

    metadata["body_html"] = body_html
    metadata["body_text"] = body_text
    return RenderedCampaignMessage(
        subject=subject or campaign.name,
        body_html=body_html,
        body_text=body_text,
        metadata=metadata,
        from_address=from_address,
    )


def send_campaign_batch(
    db: Session,
    campaign_id: str | UUID,
    *,
    batch_size: int = 100,
    now: datetime | None = None,
) -> CampaignSendResult:
    campaign = _campaign_for_update_or_404(db, campaign_id)
    _validate_campaign_values(campaign)
    current_time = now or _now()
    if campaign.status in {
        CampaignStatus.canceled.value,
        CampaignStatus.completed.value,
    }:
        raise HTTPException(status_code=409, detail="Campaign is not sendable")
    if campaign.sending_started_at is None:
        campaign.sending_started_at = current_time
    campaign.status = CampaignStatus.sending.value

    recipients = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.status == CampaignRecipientStatus.pending.value)
        .order_by(CampaignRecipient.created_at.asc(), CampaignRecipient.id.asc())
        .limit(batch_size)
        .all()
    )

    # Re-check suppression at send time, not just at audience-build time: an
    # unsubscribe can land in between, and a suppressed address must never be
    # handed to the transport.
    blocked = _blocked_addresses(
        db,
        channel=campaign.channel,
        addresses=[recipient.address for recipient in recipients],
    )
    steps = {step.id: step for step in list_campaign_steps(db, campaign.id)}
    delivery_metadata, from_address = (
        _sender_delivery_metadata(db, campaign) if recipients else ({}, None)
    )

    queued = sent = failed = skipped = suppressed = 0
    for recipient in recipients:
        subscriber = recipient.subscriber
        if (
            not subscriber.is_active
            or subscriber.status.value in NON_CONTACTABLE_STATUSES
        ):
            recipient.status = CampaignRecipientStatus.skipped.value
            recipient.failed_reason = "inactive_subscriber"
            skipped += 1
            continue
        if subscriber.reseller is not None and not subscriber.reseller.is_active:
            recipient.status = CampaignRecipientStatus.skipped.value
            recipient.failed_reason = "inactive_reseller"
            skipped += 1
            continue
        if recipient.address in blocked:
            recipient.status = CampaignRecipientStatus.suppressed.value
            recipient.suppressed_at = current_time
            recipient.failed_reason = "Address is on the campaign suppression list"
            suppressed += 1
            continue

        recipient.attempt_count += 1
        recipient.last_attempt_at = current_time
        rendered = _render_message_for_recipient(
            db,
            campaign,
            recipient,
            delivery_metadata=delivery_metadata,
            from_address=from_address,
            step=steps.get(recipient.step_id) if recipient.step_id else None,
        )
        channel = NotificationChannel(campaign.channel)
        result = submit(
            db,
            CommunicationIntent(
                subscriber_id=recipient.subscriber_id,
                event_type="campaign.send",
                category=MARKETING_CATEGORY,
                communication_class=CommunicationClass.marketing,
                subject=rendered.subject,
                body=rendered.body_text or rendered.body_html,
                channels=(channel,),
                include_reseller=False,
                persist_policy_suppressions=False,
                recipients={channel: recipient.address},
                metadata=rendered.metadata,
                dedupe_key=f"campaign:recipient:{recipient.id}",
            ),
        )
        notification = next(
            (
                item
                for item in result.queued
                if item.audience_type == "subscriber"
                and item.status == NotificationStatus.queued
            ),
            None,
        )
        if notification is None:
            recipient.failed_reason = ",".join(result.suppressed) or "policy_suppressed"
            if result.suppressed:
                recipient.status = CampaignRecipientStatus.suppressed.value
                recipient.suppressed_at = current_time
                suppressed += 1
            else:
                recipient.status = CampaignRecipientStatus.skipped.value
                skipped += 1
        else:
            conversation = _conversation_for_recipient(
                db, campaign=campaign, recipient=recipient, now=current_time
            )
            recipient.conversation_id = conversation.id
            message = team_inbox_campaigns.record_campaign_message(
                db,
                conversation=conversation,
                channel_type=campaign.channel,
                notification_id=notification.id,
                subject=rendered.subject,
                body=rendered.body_html or rendered.body_text,
                from_address=rendered.from_address,
                to_address=recipient.address,
                metadata=rendered.metadata,
            )
            recipient.notification_id = notification.id
            recipient.message_id = message.id
            recipient.status = CampaignRecipientStatus.queued.value
            queued += 1

    # Sessions run with autoflush=False, so the per-recipient status changes
    # above are still pending in the identity map. Flush before the aggregate
    # queries below, otherwise the counters and the completion check both read
    # the pre-send state and the campaign never leaves `sending`.
    db.flush()

    _refresh_counts(db, campaign)
    outstanding = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(
            CampaignRecipient.status.in_(
                [
                    CampaignRecipientStatus.pending.value,
                    CampaignRecipientStatus.queued.value,
                ]
            )
        )
        .count()
    )
    completed = outstanding == 0
    if completed:
        campaign.status = CampaignStatus.completed.value
        campaign.completed_at = current_time
    db.flush()
    return CampaignSendResult(
        campaign_id=campaign.id,
        queued=queued,
        sent=sent,
        failed=failed,
        skipped=skipped,
        completed=completed,
        suppressed=suppressed,
    )


def _refresh_counts(db: Session, campaign: Campaign) -> None:
    counts = Counter(
        status
        for (status,) in db.query(CampaignRecipient.status)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .all()
    )
    campaign.total_recipients = sum(counts.values()) - sum(
        counts.get(status, 0) for status in TERMINAL_RECIPIENT_STATUSES
    )
    campaign.sent_count = counts.get(CampaignRecipientStatus.sent.value, 0)
    campaign.delivered_count = counts.get(CampaignRecipientStatus.delivered.value, 0)
    campaign.failed_count = counts.get(CampaignRecipientStatus.failed.value, 0)
    campaign.opened_count = counts.get(CampaignRecipientStatus.opened.value, 0)
    campaign.clicked_count = counts.get(CampaignRecipientStatus.clicked.value, 0)


def refresh_campaign_delivery_state(db: Session, campaign_id: UUID) -> None:
    """Refresh campaign counters and completion after an outbox delivery."""
    db.flush()
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        return
    _refresh_counts(db, campaign)
    outstanding = (
        db.query(CampaignRecipient.id)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(
            CampaignRecipient.status.in_(
                [
                    CampaignRecipientStatus.pending.value,
                    CampaignRecipientStatus.queued.value,
                ]
            )
        )
        .first()
    )
    if outstanding is None:
        campaign.status = CampaignStatus.completed.value
        campaign.completed_at = _now()
    db.flush()


def process_due_campaigns(
    db: Session, *, now: datetime | None = None, limit: int = 20
) -> dict[str, int]:
    current_time = now or _now()
    campaigns = (
        db.query(Campaign)
        .filter(Campaign.is_active.is_(True))
        .filter(
            or_(
                and_(
                    Campaign.status == CampaignStatus.scheduled.value,
                    Campaign.scheduled_at.is_not(None),
                    Campaign.scheduled_at <= current_time,
                ),
                Campaign.status == CampaignStatus.sending.value,
            )
        )
        .order_by(Campaign.scheduled_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )
    built = queued = sent = failed = deferred = 0
    for campaign in campaigns:
        # Outside its send window the campaign simply stays `scheduled` and is
        # picked up by a later beat.
        if not within_send_window(campaign, current_time):
            deferred += 1
            continue
        was_scheduled = campaign.status == CampaignStatus.scheduled.value
        try:
            with db.begin_nested():
                if was_scheduled:
                    build_recipient_list(db, campaign.id)
                result = send_campaign_batch(db, campaign.id, now=current_time)
                queued += result.queued
                sent += result.sent
                failed += result.failed
            if was_scheduled:
                built += 1
        except Exception:
            campaign.status = CampaignStatus.failed.value
            metadata = dict(campaign.metadata_ or {})
            metadata["last_processing_error_at"] = current_time.isoformat()
            campaign.metadata_ = metadata
            failed += 1
    return {
        "campaigns": len(campaigns),
        "built": built,
        "queued": queued,
        "sent": sent,
        "failed": failed,
        "deferred": deferred,
    }


# ---------------------------------------------------------------------------
# Step sequencing
# ---------------------------------------------------------------------------


def _step_due_at(campaign: Campaign, steps: list[CampaignStep], index: int) -> datetime:
    """Absolute due time of `steps[index]`.

    `delay_days`/`delay_hours` are relative to the *previous stage*, so the
    offset from `sending_started_at` is the running sum up to and including
    this step. That keeps a sequence editable without re-basing every later step.
    """
    assert campaign.sending_started_at is not None
    offset = timedelta()
    for step in steps[: index + 1]:
        offset += timedelta(days=step.delay_days, hours=step.delay_hours)
    started = campaign.sending_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return started + offset


def materialize_step_recipients(
    db: Session,
    campaign: Campaign,
    step: CampaignStep,
    *,
    now: datetime | None = None,
) -> CampaignStepMaterializeResult:
    """Create the recipient rows for one step of a sequence.

    A step targets exactly the people the initial send actually reached — rows
    that failed, were skipped, or were suppressed do not roll forward.
    """
    current_time = now or _now()
    seeds = (
        db.query(CampaignRecipient)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.step_id.is_(None))
        .filter(
            CampaignRecipient.status.in_(
                [
                    CampaignRecipientStatus.sent.value,
                    CampaignRecipientStatus.delivered.value,
                    CampaignRecipientStatus.opened.value,
                    CampaignRecipientStatus.clicked.value,
                ]
            )
        )
        .all()
    )
    existing = {
        subscriber_id
        for (subscriber_id,) in db.query(CampaignRecipient.subscriber_id)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.step_id == step.id)
        .all()
    }
    # Consent is asked of the platform ledger, not a campaign-local table.
    blocked = _blocked_addresses(
        db,
        channel=campaign.channel,
        addresses=[seed.address for seed in seeds],
    )

    created = suppressed = 0
    for seed in seeds:
        if seed.subscriber_id in existing:
            continue
        if seed.address in blocked:
            suppressed += 1
            continue
        db.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                subscriber_id=seed.subscriber_id,
                step_id=step.id,
                address=seed.address,
                email=seed.email,
                status=CampaignRecipientStatus.pending.value,
                unsubscribe_token=_new_unsubscribe_token(),
                metadata_={
                    "source": "native_campaign_step",
                    "campaign_step_index": step.step_index,
                },
            )
        )
        created += 1
    db.flush()

    metadata = dict(campaign.metadata_ or {})
    materialized = dict(metadata.get("materialized_steps") or {})
    materialized[str(step.id)] = current_time.isoformat()
    metadata["materialized_steps"] = materialized
    campaign.metadata_ = metadata
    db.flush()

    return CampaignStepMaterializeResult(
        campaign_id=campaign.id,
        step_id=step.id,
        created=created,
        suppressed=suppressed,
    )


def _step_is_materialized(db: Session, campaign: Campaign, step: CampaignStep) -> bool:
    materialized = dict((campaign.metadata_ or {}).get("materialized_steps") or {})
    return str(step.id) in materialized or (
        db.query(CampaignRecipient.id)
        .filter(CampaignRecipient.campaign_id == campaign.id)
        .filter(CampaignRecipient.step_id == step.id)
        .first()
        is not None
    )


def process_due_campaign_steps(
    db: Session, *, now: datetime | None = None, limit: int = 20
) -> dict[str, int]:
    """Advance nurture sequences: materialize and send any step that is due.

    Steps are strictly ordered — step N is never materialized before step N-1
    has been, even if its own due time has passed (e.g. after a backlog).
    """
    current_time = now or _now()
    campaigns = (
        db.query(Campaign)
        .filter(Campaign.is_active.is_(True))
        .filter(Campaign.campaign_type == CampaignType.nurture.value)
        # A sequence advances only after the notification outbox has projected
        # the previous stage to a terminal campaign state. Processing `sending`
        # campaigns would materialize a zero-recipient step while the prior
        # stage was merely queued, permanently skipping it.
        .filter(Campaign.status == CampaignStatus.completed.value)
        .filter(Campaign.sending_started_at.is_not(None))
        .order_by(Campaign.sending_started_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )

    advanced = created = queued = sent = deferred = 0
    for campaign in campaigns:
        if not within_send_window(campaign, current_time):
            deferred += 1
            continue
        steps = [
            step for step in list_campaign_steps(db, campaign.id) if step.is_active
        ]
        for index, step in enumerate(steps):
            if _step_is_materialized(db, campaign, step):
                continue
            if current_time < _step_due_at(campaign, steps, index):
                break
            result = materialize_step_recipients(db, campaign, step, now=current_time)
            if result.created == 0:
                # Nothing to send for this step; still let the sequence advance.
                continue
            created += result.created
            advanced += 1
            campaign.status = CampaignStatus.sending.value
            campaign.completed_at = None
            send_result = send_campaign_batch(db, campaign.id, now=current_time)
            queued += send_result.queued
            sent += send_result.sent
            # One step per beat keeps a long backlog from firing a whole
            # sequence into a subscriber's inbox at once.
            break

    return {
        "campaigns": len(campaigns),
        "advanced": advanced,
        "created": created,
        "queued": queued,
        "sent": sent,
        "deferred": deferred,
    }


# Commit-owning entry points. The API layer must not own transaction boundaries
# (see the SOT service-ownership contract); these wrap the flush-only builders
# above so the campaign service decides when its work is durable.
def create_campaign_committed(
    db: Session, payload, *, created_by_system_user_id: str | UUID | None = None
) -> Campaign:
    campaign = create_campaign(
        db, payload, created_by_system_user_id=created_by_system_user_id
    )
    db.commit()
    db.refresh(campaign)
    return campaign


def update_campaign_committed(
    db: Session, campaign_id: str | UUID, payload
) -> Campaign:
    campaign = update_campaign(db, campaign_id, payload)
    db.commit()
    db.refresh(campaign)
    return campaign


def build_recipient_list_committed(
    db: Session, campaign_id: str | UUID, *, limit: int | None = None
) -> CampaignAudienceBuildResult:
    result = build_recipient_list(db, campaign_id, limit=limit)
    db.commit()
    return result


def send_campaign_batch_committed(
    db: Session,
    campaign_id: str | UUID,
    *,
    batch_size: int = 100,
    now: datetime | None = None,
) -> CampaignSendResult:
    result = send_campaign_batch(db, campaign_id, batch_size=batch_size, now=now)
    db.commit()
    return result


def create_campaign_step_committed(
    db: Session, campaign_id: str | UUID, payload
) -> CampaignStep:
    step = create_campaign_step(db, campaign_id, payload)
    db.commit()
    db.refresh(step)
    return step


def update_campaign_step_committed(
    db: Session, campaign_id: str | UUID, step_id: str | UUID, payload
) -> CampaignStep:
    step = update_campaign_step(db, campaign_id, step_id, payload)
    db.commit()
    db.refresh(step)
    return step


def delete_campaign_step_committed(
    db: Session, campaign_id: str | UUID, step_id: str | UUID
) -> None:
    delete_campaign_step(db, campaign_id, step_id)
    db.commit()


def create_sender_committed(db: Session, payload) -> CampaignSender:
    sender = create_sender(db, payload)
    db.commit()
    db.refresh(sender)
    return sender


def update_sender_committed(
    db: Session, sender_id: str | UUID, payload
) -> CampaignSender:
    sender = update_sender(db, sender_id, payload)
    db.commit()
    db.refresh(sender)
    return sender


def unsubscribe_by_token_committed(
    db: Session, token: str, *, source: str = "unsubscribe_link"
) -> CommunicationSuppression:
    suppression = unsubscribe_by_token(db, token, source=source)
    db.commit()
    db.refresh(suppression)
    return suppression


def process_due_campaign_steps_committed(
    db: Session, *, now: datetime | None = None, limit: int = 20
) -> dict[str, int]:
    result = process_due_campaign_steps(db, now=now, limit=limit)
    db.commit()
    return result
