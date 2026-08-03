"""Canonical lead identity, acquisition origin, and account-link commands.

Sales owns these business links while ``party.registry`` owns Party identity,
``communications.campaigns`` owns native campaign lifecycle, and Subscriber
services own accounts. Commands are idempotent for exact retries and refuse
silent repoints or attribution replacement.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.comms_campaign import Campaign, CampaignChannel, CampaignRecipient
from app.models.party import Party, PartyIdentityStatus
from app.models.sales import (
    Lead,
    LeadCaptureMethod,
    LeadOriginCapture,
    LeadSourcePlatform,
    LeadStatus,
)
from app.models.subscriber import Subscriber


class LeadLifecycleError(ValueError):
    pass


_LEAD_SOURCES = {
    "Facebook",
    "Instagram",
    "Whatsapp",
    "Email",
    "Referrer",
    "Instagram Ads",
    "Facebook Ads",
    "Google",
    "Website",
    "Portal",
}
_ROUTABLE_PARTY_STATUSES = {
    PartyIdentityStatus.active.value,
    PartyIdentityStatus.quarantined.value,
}
_METHOD_PLATFORM = {
    LeadCaptureMethod.landing_page.value: LeadSourcePlatform.website.value,
    LeadCaptureMethod.portal.value: LeadSourcePlatform.portal.value,
    LeadCaptureMethod.agent_declared.value: LeadSourcePlatform.agent.value,
    LeadCaptureMethod.referral.value: LeadSourcePlatform.referral.value,
    LeadCaptureMethod.reviewed_import.value: LeadSourcePlatform.legacy_import.value,
}
_PLATFORM_LEAD_SOURCES = {
    LeadSourcePlatform.meta.value: {"Facebook Ads", "Instagram Ads"},
    LeadSourcePlatform.google.value: {"Google"},
    LeadSourcePlatform.website.value: {"Website"},
    LeadSourcePlatform.portal.value: {"Portal"},
    LeadSourcePlatform.referral.value: {"Referrer"},
}
_CAPTURE_FIELDS = (
    "capture_method",
    "source_platform",
    "lead_source",
    "integration_inbox_id",
    "source_interaction_id",
    "capture_fingerprint",
    "campaign_id",
    "campaign_recipient_id",
    "external_campaign_id",
    "external_ad_set_id",
    "external_ad_id",
    "external_form_id",
    "external_click_id",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "landing_path",
    "capture_source",
    "capture_reason",
)


def _required(value: str | None, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise LeadLifecycleError(f"{field_name} is required")
    return normalized


def _optional(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _enum_value(value: Any, enum_cls, field_name: str) -> str:
    raw = value.value if hasattr(value, "value") else str(value or "").strip()
    try:
        return enum_cls(raw).value
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_cls)
        raise LeadLifecycleError(
            f"Invalid {field_name} '{raw}'; expected one of: {allowed}"
        ) from exc


def _routable_party(db: Session, party_id: UUID) -> Party:
    party = db.get(Party, party_id)
    if party is None:
        raise LeadLifecycleError(f"Party '{party_id}' was not found")
    if party.status not in _ROUTABLE_PARTY_STATUSES:
        raise LeadLifecycleError(
            f"Party '{party.id}' in status '{party.status}' cannot receive a lead"
        )
    return party


def _complete_lead_party_evidence(lead: Lead) -> bool:
    return bool(
        lead.party_bound_at is not None
        and str(lead.party_binding_source or "").strip()
        and str(lead.party_binding_reason or "").strip()
    )


def _complete_subscriber_link_evidence(lead: Lead) -> bool:
    return bool(
        lead.subscriber_linked_at is not None
        and str(lead.subscriber_link_source or "").strip()
        and str(lead.subscriber_link_reason or "").strip()
    )


def bind_lead_party(
    db: Session,
    *,
    lead_id: UUID,
    party_id: UUID,
    source: str,
    reason: str,
) -> Lead:
    """Bind one Lead to reviewed Party identity without creating an account."""

    lead = db.get(Lead, lead_id)
    if lead is None:
        raise LeadLifecycleError(f"Lead '{lead_id}' was not found")
    initialize_lead_party(
        db,
        lead=lead,
        party_id=party_id,
        source=source,
        reason=reason,
    )
    db.flush()
    return lead


def create_party_lead(
    db: Session,
    *,
    lead_id: UUID | None = None,
    party_id: UUID,
    title: str,
    lead_source: str,
    binding_source: str,
    binding_reason: str,
    origin_capture: dict[str, Any],
    region: str | None = None,
    address: str | None = None,
    notes: str | None = None,
    metadata: dict[str, Any] | None = None,
    owner_agent_id: UUID | None = None,
    pipeline_id: UUID | None = None,
    stage_id: UUID | None = None,
    status: LeadStatus = LeadStatus.new,
    estimated_value: Decimal | None = None,
    currency: str | None = None,
    probability: int | None = None,
    expected_close_date: date | None = None,
    lost_reason: str | None = None,
    is_active: bool = True,
) -> Lead:
    """Create one Party-first Lead without committing the caller's transaction.

    Capture adapters use this command instead of constructing ``Lead`` rows or
    projecting attribution fields themselves. The caller remains responsible
    for its own domain record and commits the combined transaction only after
    every owner has accepted its state.
    """

    lead = Lead(
        id=lead_id,
        title=_required(title, "title"),
        status=_enum_value(status, LeadStatus, "status"),
        lead_source=lead_source,
        region=_optional(region),
        address=_optional(address),
        notes=_optional(notes),
        metadata_=dict(metadata) if metadata else None,
        owner_agent_id=owner_agent_id,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        estimated_value=estimated_value,
        currency=_optional(currency),
        probability=probability,
        expected_close_date=expected_close_date,
        lost_reason=_optional(lost_reason),
        is_active=is_active,
    )
    if lead.status in {LeadStatus.won.value, LeadStatus.lost.value}:
        lead.closed_at = datetime.now(UTC)
    initialize_lead_party(
        db,
        lead=lead,
        party_id=party_id,
        source=binding_source,
        reason=binding_reason,
    )
    db.add(lead)
    db.flush()
    capture_lead_origin(
        db,
        lead_id=lead.id,
        lead_source=lead_source,
        capture=origin_capture,
    )
    return lead


def initialize_lead_party(
    db: Session,
    *,
    lead: Lead,
    party_id: UUID,
    source: str,
    reason: str,
) -> Lead:
    """Apply the canonical Party binding before or after Lead insertion."""

    party = _routable_party(db, party_id)
    normalized_source = _required(source, "source")
    normalized_reason = _required(reason, "reason")
    if lead.subscriber_id is not None:
        subscriber = db.get(Subscriber, lead.subscriber_id)
        if subscriber is None:
            raise LeadLifecycleError(
                f"Lead '{lead.id}' references a missing Subscriber"
            )
        if subscriber.party_id is not None and subscriber.party_id != party.id:
            raise LeadLifecycleError(
                "Lead Party does not match the reviewed Subscriber Party"
            )
    if lead.party_id is not None:
        if lead.party_id != party.id:
            raise LeadLifecycleError(
                f"Lead '{lead.id}' is already bound to Party '{lead.party_id}'; "
                "use the reviewed merge/repoint workflow"
            )
        if not _complete_lead_party_evidence(lead):
            raise LeadLifecycleError("Lead has incomplete Party binding evidence")
        return lead
    lead.party_id = party.id
    lead.party_bound_at = datetime.now(UTC)
    lead.party_binding_source = normalized_source
    lead.party_binding_reason = normalized_reason
    return lead


def attach_lead_subscriber(
    db: Session,
    *,
    lead_id: UUID,
    subscriber_id: UUID,
    source: str,
    reason: str,
) -> Lead:
    """Attach an account only when Lead and Subscriber identify the same Party."""

    normalized_source = _required(source, "source")
    normalized_reason = _required(reason, "reason")
    lead = db.get(Lead, lead_id)
    if lead is None or lead.party_id is None:
        raise LeadLifecycleError(
            f"Lead '{lead_id}' must have a reviewed Party binding first"
        )
    _routable_party(db, lead.party_id)
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise LeadLifecycleError(f"Subscriber '{subscriber_id}' was not found")
    if subscriber.party_id is None:
        raise LeadLifecycleError(
            f"Subscriber '{subscriber.id}' must have a reviewed Party binding first"
        )
    if subscriber.party_id != lead.party_id:
        raise LeadLifecycleError("Subscriber Party does not match the Lead Party")
    if lead.subscriber_id is not None and lead.subscriber_id != subscriber.id:
        raise LeadLifecycleError(
            f"Lead '{lead.id}' is already attached to Subscriber "
            f"'{lead.subscriber_id}'; use the reviewed account-repoint workflow"
        )
    if lead.subscriber_id == subscriber.id and _complete_subscriber_link_evidence(lead):
        return lead
    if lead.subscriber_id == subscriber.id and any(
        value is not None
        for value in (
            lead.subscriber_linked_at,
            lead.subscriber_link_source,
            lead.subscriber_link_reason,
        )
    ):
        raise LeadLifecycleError("Lead has incomplete Subscriber-link evidence")
    lead.subscriber_id = subscriber.id
    lead.subscriber_linked_at = datetime.now(UTC)
    lead.subscriber_link_source = normalized_source
    lead.subscriber_link_reason = normalized_reason
    db.flush()
    return lead


def validate_lead_subscriber_alignment(
    db: Session,
    *,
    lead: Lead,
    subscriber: Subscriber,
) -> None:
    """Fail if a quote/ticket account diverges from its Lead identity."""

    if lead.party_id is not None:
        if subscriber.party_id is None:
            raise LeadLifecycleError(
                "Subscriber must have a reviewed Party binding for this Lead"
            )
        if subscriber.party_id != lead.party_id:
            raise LeadLifecycleError("Subscriber Party does not match the Lead Party")
        if lead.subscriber_id is not None and lead.subscriber_id != subscriber.id:
            raise LeadLifecycleError(
                "Downstream Subscriber does not match the reviewed Lead account"
            )
        return
    if lead.subscriber_id != subscriber.id:
        raise LeadLifecycleError(
            "Legacy unbound Lead and downstream record must use the same Subscriber"
        )


def stage_quote_acceptance(db: Session, *, lead: Lead) -> None:
    """Stage the sole sales transition that marks a Lead Won."""

    if not lead.is_active:
        raise LeadLifecycleError("Inactive Lead cannot receive a Quote outcome")
    if lead.status in {LeadStatus.won.value, LeadStatus.lost.value}:
        if lead.status != LeadStatus.won.value:
            raise LeadLifecycleError(
                "Quote acceptance conflicts with the Lead's existing closed status"
            )
        return
    lead.status = LeadStatus.won.value
    lead.closed_at = lead.closed_at or datetime.now(UTC)
    db.flush()


def _capture_values(payload: dict[str, Any], *, lead_source: str) -> dict[str, Any]:
    capture_method = _enum_value(
        payload.get("capture_method"), LeadCaptureMethod, "capture_method"
    )
    source_platform = _enum_value(
        payload.get("source_platform"), LeadSourcePlatform, "source_platform"
    )
    if lead_source not in _LEAD_SOURCES:
        raise LeadLifecycleError(f"Invalid normalized lead_source '{lead_source}'")
    expected_platform = _METHOD_PLATFORM.get(capture_method)
    if expected_platform is not None and source_platform != expected_platform:
        raise LeadLifecycleError(
            f"{capture_method} requires source_platform='{expected_platform}'"
        )
    expected_sources = _PLATFORM_LEAD_SOURCES.get(source_platform)
    if expected_sources is not None and lead_source not in expected_sources:
        raise LeadLifecycleError(
            f"source_platform='{source_platform}' conflicts with lead_source "
            f"'{lead_source}'"
        )
    landing_path = _optional(payload.get("landing_path"))
    if landing_path and any(token in landing_path for token in ("://", "?", "#")):
        raise LeadLifecycleError(
            "landing_path must be a path without scheme, query, or fragment"
        )
    values = {
        "capture_method": capture_method,
        "source_platform": source_platform,
        "lead_source": lead_source,
        "integration_inbox_id": payload.get("integration_inbox_id"),
        "source_interaction_id": _optional(payload.get("source_interaction_id")),
        "capture_fingerprint": _optional(payload.get("capture_fingerprint")),
        "campaign_id": payload.get("campaign_id"),
        "campaign_recipient_id": payload.get("campaign_recipient_id"),
        "external_campaign_id": _optional(payload.get("external_campaign_id")),
        "external_ad_set_id": _optional(payload.get("external_ad_set_id")),
        "external_ad_id": _optional(payload.get("external_ad_id")),
        "external_form_id": _optional(payload.get("external_form_id")),
        "external_click_id": _optional(payload.get("external_click_id")),
        "utm_source": _optional(payload.get("utm_source")),
        "utm_medium": _optional(payload.get("utm_medium")),
        "utm_campaign": _optional(payload.get("utm_campaign")),
        "utm_content": _optional(payload.get("utm_content")),
        "utm_term": _optional(payload.get("utm_term")),
        "landing_path": landing_path,
        "capture_source": _required(payload.get("capture_source"), "capture_source"),
        "capture_reason": _required(payload.get("capture_reason"), "capture_reason"),
    }
    if capture_method == LeadCaptureMethod.campaign_response.value:
        if (
            source_platform != LeadSourcePlatform.sub_campaign.value
            or values["campaign_id"] is None
            or values["campaign_recipient_id"] is None
        ):
            raise LeadLifecycleError(
                "campaign_response requires a native campaign, recipient, and "
                "source_platform='sub_campaign'"
            )
    if capture_method == LeadCaptureMethod.ad_lead_form_webhook.value:
        if (
            source_platform
            not in {
                LeadSourcePlatform.meta.value,
                LeadSourcePlatform.google.value,
            }
            or not values["external_campaign_id"]
        ):
            raise LeadLifecycleError(
                "ad_lead_form_webhook requires meta/google platform and an "
                "external_campaign_id"
            )
    return values


def capture_lead_origin(
    db: Session,
    *,
    lead_id: UUID,
    lead_source: str,
    capture: dict[str, Any],
) -> LeadOriginCapture:
    """Capture immutable lead-creation origin and its legacy projection."""

    lead = db.get(Lead, lead_id)
    if lead is None or lead.party_id is None:
        raise LeadLifecycleError(
            f"Lead '{lead_id}' must have a reviewed Party binding first"
        )
    _routable_party(db, lead.party_id)
    values = _capture_values(capture, lead_source=lead_source)
    campaign_id = values["campaign_id"]
    recipient_id = values["campaign_recipient_id"]
    campaign = db.get(Campaign, campaign_id) if campaign_id else None
    if campaign_id is not None and campaign is None:
        raise LeadLifecycleError(f"Campaign '{campaign_id}' was not found")
    if campaign is not None and values["capture_method"] == (
        LeadCaptureMethod.campaign_response.value
    ):
        expected_lead_source = {
            CampaignChannel.email.value: "Email",
            CampaignChannel.whatsapp.value: "Whatsapp",
        }.get(campaign.channel)
        if expected_lead_source is None or lead_source != expected_lead_source:
            raise LeadLifecycleError(
                "Native Campaign channel conflicts with the captured lead_source"
            )
    recipient = db.get(CampaignRecipient, recipient_id) if recipient_id else None
    if recipient_id is not None and recipient is None:
        raise LeadLifecycleError(f"CampaignRecipient '{recipient_id}' was not found")
    if recipient is not None:
        if recipient.campaign_id != campaign_id:
            raise LeadLifecycleError(
                "CampaignRecipient does not belong to the captured Campaign"
            )
        recipient_subscriber = db.get(Subscriber, recipient.subscriber_id)
        if (
            recipient_subscriber is None
            or recipient_subscriber.party_id != lead.party_id
        ):
            raise LeadLifecycleError(
                "CampaignRecipient Subscriber Party does not match the Lead Party"
            )
    existing = (
        db.query(LeadOriginCapture)
        .filter(LeadOriginCapture.lead_id == lead.id)
        .one_or_none()
    )
    if existing is not None:
        mismatches = [
            field
            for field in _CAPTURE_FIELDS
            if getattr(existing, field) != values[field]
        ]
        captured_at = capture.get("captured_at")
        if captured_at is not None and existing.captured_at != captured_at:
            mismatches.append("captured_at")
        if mismatches:
            raise LeadLifecycleError(
                "Lead origin is immutable; conflicting fields: "
                + ", ".join(sorted(mismatches))
            )
        if (
            lead.lead_source != existing.lead_source
            or lead.campaign_id != existing.campaign_id
            or lead.campaign_recipient_id != existing.campaign_recipient_id
        ):
            raise LeadLifecycleError(
                "Lead origin compatibility projection has drifted from its capture"
            )
        return existing

    if lead.lead_source is not None and lead.lead_source != lead_source:
        raise LeadLifecycleError("Lead source conflicts with origin capture")
    if campaign_id is not None and lead.campaign_id not in {None, campaign_id}:
        raise LeadLifecycleError("Lead campaign conflicts with origin capture")
    if recipient_id is not None and lead.campaign_recipient_id not in {
        None,
        recipient_id,
    }:
        raise LeadLifecycleError(
            "Lead campaign recipient conflicts with origin capture"
        )

    captured_at = capture.get("captured_at") or datetime.now(UTC)
    origin = LeadOriginCapture(
        **values,
        captured_at=captured_at,
    )
    origin.lead_id = lead.id
    db.add(origin)
    lead.lead_source = lead_source
    if campaign_id is not None:
        lead.campaign_id = campaign_id
    if recipient_id is not None:
        lead.campaign_recipient_id = recipient_id
    db.flush()
    return origin
