"""PII-free, idempotent Fiber acquisition conversion projection owner."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.catalog import Subscription
from app.models.sales import Lead, LeadConversionMilestone, LeadOriginCapture
from app.services.events import emit_event
from app.services.events.types import Event, EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "sales.marketing_conversion_projection"
_PROJECT = OwnerCommandDefinition(
    owner=OWNER,
    concern="PII-free immutable Fiber conversion milestone projection",
    name="project_marketing_conversion_event",
)
_EVENT_NAMESPACE = UUID("b90d1781-c361-4f42-b492-5e6f9ed07650")
_QUALIFIED_STATUSES = frozenset({"qualified", "proposal", "negotiation", "won"})


class ConversionStage(StrEnum):
    visitor = "visitor"
    coverage_check = "coverage_check"
    lead = "lead"
    qualified_lead = "qualified_lead"
    payment = "payment"
    installation = "installation"
    activated_subscriber = "activated_subscriber"


@dataclass(frozen=True, slots=True)
class ConversionProjectionResult:
    milestone_ids: tuple[UUID, ...]


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _origin_for_event(db: Session, event: Event) -> LeadOriginCapture | None:
    origin_id = _uuid(event.payload.get("origin_capture_id"))
    if origin_id is not None:
        origin = db.get(LeadOriginCapture, origin_id)
        if origin is not None:
            return origin

    lead_id = _uuid(event.payload.get("lead_id"))
    if lead_id is not None:
        return db.scalar(
            select(LeadOriginCapture).where(LeadOriginCapture.lead_id == lead_id)
        )

    subscriber_id = event.account_id or event.subscriber_id
    if subscriber_id is None and event.subscription_id is not None:
        subscription = db.get(Subscription, event.subscription_id)
        subscriber_id = subscription.subscriber_id if subscription else None
    if subscriber_id is None:
        subscriber_id = _uuid(event.payload.get("subscriber_id"))
    if subscriber_id is None:
        return None
    return db.scalar(
        select(LeadOriginCapture)
        .join(Lead, Lead.id == LeadOriginCapture.lead_id)
        .where(
            Lead.subscriber_id == subscriber_id,
            LeadOriginCapture.external_form_id == "fiber-coverage-v1",
            LeadOriginCapture.journey_id.is_not(None),
        )
        .order_by(LeadOriginCapture.captured_at.asc(), LeadOriginCapture.id.asc())
        .limit(1)
    )


def _stages_for_event(event: Event) -> tuple[ConversionStage, ...]:
    if event.event_type is EventType.lead_created:
        return (ConversionStage.visitor, ConversionStage.lead)
    if event.event_type is EventType.fiber_coverage_evaluated:
        return (ConversionStage.coverage_check,)
    if event.event_type is EventType.lead_updated:
        return (
            (ConversionStage.qualified_lead,)
            if str(event.payload.get("status") or "") in _QUALIFIED_STATUSES
            else ()
        )
    if event.event_type is EventType.payment_received:
        return (ConversionStage.payment,)
    if event.event_type is EventType.appointment_scheduled:
        return (ConversionStage.installation,)
    if event.event_type is EventType.subscription_activated:
        return (ConversionStage.activated_subscriber,)
    return ()


def handles_conversion_event(event: Event) -> bool:
    return bool(_stages_for_event(event))


def _subject_key(origin: LeadOriginCapture) -> str:
    key = settings.conversion_ingest_api_key
    if not key:
        raise RuntimeError(
            "CONVERSION_INGEST_API_KEY is required for Fiber conversion projection"
        )
    party_id = origin.lead.party_id
    if party_id is None:
        raise RuntimeError("Attributed Lead is missing its canonical Party")
    return hmac.new(
        key.encode("utf-8"),
        str(party_id).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _occurred_at(
    origin: LeadOriginCapture, stage: ConversionStage, event: Event
) -> datetime:
    if stage is ConversionStage.visitor:
        return origin.captured_at
    if stage is ConversionStage.lead and origin.submitted_at is not None:
        return origin.submitted_at
    return event.occurred_at


def _payload(
    origin: LeadOriginCapture,
    *,
    stage: ConversionStage,
    event: Event,
    external_event_id: UUID,
    subject_key: str,
) -> dict[str, object]:
    value_amount = None
    currency_code = "NGN"
    if stage is ConversionStage.payment:
        raw_amount = event.payload.get("amount")
        value_amount = str(raw_amount) if raw_amount is not None else None
        currency_code = str(event.payload.get("currency") or "NGN")
    return {
        "external_event_id": str(external_event_id),
        "journey_id": str(origin.journey_id),
        "subject_key": subject_key,
        "stage": stage.value,
        "occurred_at": _occurred_at(origin, stage, event).isoformat(),
        "source": origin.utm_source or "website",
        "medium": origin.utm_medium,
        "campaign_name": origin.utm_campaign,
        "ad_name": origin.utm_content,
        "keyword": origin.utm_term,
        "landing_path": origin.landing_path,
        "external_campaign_id": origin.external_campaign_id,
        "external_ad_set_id": origin.external_ad_set_id,
        "external_ad_id": origin.external_ad_id,
        "value_amount": value_amount,
        "currency_code": currency_code,
    }


def project_conversion_event(
    db: Session,
    *,
    event: Event,
    context: CommandContext,
) -> ConversionProjectionResult:
    """Project applicable stages once and stage their outbound events atomically."""

    def operation() -> ConversionProjectionResult:
        stages = _stages_for_event(event)
        if not stages:
            return ConversionProjectionResult(milestone_ids=())
        origin = _origin_for_event(db, event)
        if (
            origin is None
            or origin.external_form_id != "fiber-coverage-v1"
            or origin.journey_id is None
        ):
            return ConversionProjectionResult(milestone_ids=())
        subject_key = _subject_key(origin)
        milestone_ids: list[UUID] = []
        for stage in stages:
            existing = db.scalar(
                select(LeadConversionMilestone).where(
                    LeadConversionMilestone.origin_capture_id == origin.id,
                    LeadConversionMilestone.stage == stage.value,
                )
            )
            if existing is not None:
                milestone_ids.append(existing.id)
                continue
            external_event_id = uuid5(
                _EVENT_NAMESPACE,
                f"{origin.id}:{stage.value}",
            )
            payload = _payload(
                origin,
                stage=stage,
                event=event,
                external_event_id=external_event_id,
                subject_key=subject_key,
            )
            milestone = LeadConversionMilestone(
                origin_capture_id=origin.id,
                external_event_id=external_event_id,
                source_event_id=event.event_id,
                stage=stage.value,
                subject_key=subject_key,
                occurred_at=_occurred_at(origin, stage, event),
                payload_json=payload,
            )
            db.add(milestone)
            db.flush()
            emit_event(
                db,
                EventType.marketing_conversion_ready,
                payload,
                actor=OWNER,
                subscriber_id=origin.lead.subscriber_id,
            )
            milestone_ids.append(milestone.id)
        return ConversionProjectionResult(milestone_ids=tuple(milestone_ids))

    return execute_owner_command(
        db,
        definition=_PROJECT,
        context=context,
        operation=operation,
    )
