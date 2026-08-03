"""PII-free, read-only audit of Party-to-support customer lifecycle links.

The audit reports aggregate convergence only. It does not emit names, contact
details, UUIDs, free-text attribution values, or metadata, and it never repairs
or infers identity, origin, account, subscription, sales, or support state.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from uuid import UUID

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models.catalog import Subscription, SubscriptionStatus
from app.models.comms_campaign import Campaign, CampaignChannel, CampaignRecipient
from app.models.customer_experience import CustomerExperienceHandoff
from app.models.party import Party, PartyContactPoint, PartyIdentityStatus
from app.models.project import Project
from app.models.provisioning import ServiceOrder, ServiceOrderStatus
from app.models.referral_native import Referral
from app.models.sales import (
    Lead,
    LeadCaptureMethod,
    LeadOriginCapture,
    LeadSourcePlatform,
    Quote,
    SalesOrder,
)
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.models.vendor_routes import InstallationProject, InstallationProjectStatus

_REQUIRED_COLUMNS = {
    "parties": {"id", "status"},
    "subscribers": {"id", "party_id", "sales_order_id"},
    "leads": {
        "id",
        "party_id",
        "party_bound_at",
        "party_binding_source",
        "party_binding_reason",
        "subscriber_id",
        "subscriber_linked_at",
        "subscriber_link_source",
        "subscriber_link_reason",
        "lead_source",
        "campaign_id",
        "campaign_recipient_id",
    },
    "lead_origin_captures": {
        "lead_id",
        "capture_method",
        "source_platform",
        "lead_source",
        "campaign_id",
        "campaign_recipient_id",
        "capture_source",
        "capture_reason",
    },
    "quotes": {"id", "lead_id", "subscriber_id"},
    "sales_orders": {"id", "quote_id", "subscriber_id"},
    "projects": {"id", "quote_id", "sales_order_id", "subscriber_id", "status"},
    "installation_projects": {"id", "project_id", "subscriber_id", "status"},
    "service_orders": {
        "id",
        "subscriber_id",
        "subscription_id",
        "sales_order_id",
        "sales_order_line_id",
        "project_id",
        "installation_project_id",
        "status",
        "implementation_verification_event_id",
    },
    "customer_experience_handoffs": {
        "id",
        "subscriber_id",
        "subscription_id",
        "sales_order_id",
        "project_id",
        "installation_project_id",
        "service_order_id",
        "status",
    },
    "subscriptions": {"id", "subscriber_id", "status"},
    "support_tickets": {
        "lead_id",
        "subscriber_id",
        "customer_account_id",
        "customer_person_id",
    },
    "campaigns": {"id", "channel"},
    "campaign_recipients": {"id", "campaign_id", "subscriber_id"},
    "party_contact_points": {"party_id", "is_active"},
    "referrals": {
        "id",
        "referred_party_id",
        "party_bound_at",
        "party_binding_source",
        "party_binding_reason",
        "referred_subscriber_id",
        "subscriber_linked_at",
        "subscriber_link_source",
        "subscriber_link_reason",
        "referred_lead_id",
        "metadata",
        "is_active",
    },
}
_ROUTABLE_PARTY_STATUSES = {
    PartyIdentityStatus.active.value,
    PartyIdentityStatus.quarantined.value,
}
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
_CAPTURE_METHODS = {item.value for item in LeadCaptureMethod}
_SOURCE_PLATFORMS = {item.value for item in LeadSourcePlatform}
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


def _value(value: Any) -> str:
    return str(value.value if hasattr(value, "value") else value)


def _complete(values: tuple[Any, ...]) -> bool:
    timestamp, source, reason = values
    return bool(
        timestamp is not None
        and str(source or "").strip()
        and str(reason or "").strip()
    )


def _any(values: tuple[Any, ...]) -> bool:
    return any(value is not None for value in values)


def _lead_party_evidence(lead: Lead) -> tuple[Any, ...]:
    return (
        lead.party_bound_at,
        lead.party_binding_source,
        lead.party_binding_reason,
    )


def _lead_subscriber_evidence(lead: Lead) -> tuple[Any, ...]:
    return (
        lead.subscriber_linked_at,
        lead.subscriber_link_source,
        lead.subscriber_link_reason,
    )


def _controlled_counts(
    values: list[str],
    *,
    allowed: set[str],
) -> dict[str, int]:
    counts = Counter(value if value in allowed else "other" for value in values)
    return {key: int(counts.get(key, 0)) for key in sorted(allowed | {"other"})}


def _lead_identity_counts(
    leads: list[Lead],
    *,
    parties: dict[UUID, str],
    subscribers: dict[UUID, UUID | None],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for lead in leads:
        counts["total"] += 1
        if lead.party_id is not None and lead.subscriber_id is None:
            counts["party_only"] += 1
        elif lead.party_id is None and lead.subscriber_id is not None:
            counts["subscriber_only_legacy"] += 1
        elif lead.party_id is not None and lead.subscriber_id is not None:
            counts["party_and_subscriber"] += 1
        else:
            counts["invalid_without_identity"] += 1

        party_evidence = _lead_party_evidence(lead)
        subscriber_evidence = _lead_subscriber_evidence(lead)
        issue = False
        if lead.party_id is None:
            counts["legacy_unbound"] += 1
            if _any(party_evidence):
                counts["incomplete_party_evidence"] += 1
                issue = True
        else:
            counts["party_bound"] += 1
            if not _complete(party_evidence):
                counts["incomplete_party_evidence"] += 1
                issue = True
            if parties.get(lead.party_id) not in _ROUTABLE_PARTY_STATUSES:
                counts["missing_or_nonroutable_party"] += 1
                issue = True

        if lead.subscriber_id is None:
            if _any(subscriber_evidence):
                counts["incomplete_subscriber_evidence"] += 1
                issue = True
        else:
            subscriber_party_id = subscribers.get(lead.subscriber_id, "missing")
            if subscriber_party_id == "missing":
                counts["missing_subscriber"] += 1
                issue = True
            elif lead.party_id is not None:
                if not _complete(subscriber_evidence):
                    counts["incomplete_subscriber_evidence"] += 1
                    issue = True
                if subscriber_party_id is None:
                    counts["subscriber_without_party"] += 1
                    issue = True
                elif subscriber_party_id != lead.party_id:
                    counts["party_mismatch"] += 1
                    issue = True
            elif _any(subscriber_evidence):
                counts["unexpected_legacy_subscriber_evidence"] += 1
                issue = True
        if not issue:
            counts["aligned"] += 1

    keys = (
        "total",
        "party_only",
        "subscriber_only_legacy",
        "party_and_subscriber",
        "invalid_without_identity",
        "party_bound",
        "legacy_unbound",
        "incomplete_party_evidence",
        "missing_or_nonroutable_party",
        "incomplete_subscriber_evidence",
        "unexpected_legacy_subscriber_evidence",
        "missing_subscriber",
        "subscriber_without_party",
        "party_mismatch",
        "aligned",
    )
    return {key: int(counts[key]) for key in keys}


def _origin_counts(
    captures: list[LeadOriginCapture],
    *,
    leads: dict[UUID, Lead],
    campaigns: dict[UUID, str],
    recipients: dict[UUID, tuple[UUID, UUID]],
    subscribers: dict[UUID, UUID | None],
) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    methods: list[str] = []
    platforms: list[str] = []
    sources: list[str] = []
    captured_leads: set[UUID] = set()
    for capture in captures:
        counts["total"] += 1
        captured_leads.add(capture.lead_id)
        method = _value(capture.capture_method)
        platform = _value(capture.source_platform)
        methods.append(method)
        platforms.append(platform)
        sources.append(capture.lead_source)
        issue = False
        lead = leads.get(capture.lead_id)
        if lead is None:
            counts["missing_lead"] += 1
            issue = True
        if (
            not str(capture.capture_source or "").strip()
            or not str(capture.capture_reason or "").strip()
        ):
            counts["incomplete_evidence"] += 1
            issue = True
        if capture.campaign_id is not None and capture.campaign_id not in campaigns:
            counts["missing_native_campaign"] += 1
            issue = True
        recipient = (
            recipients.get(capture.campaign_recipient_id)
            if capture.campaign_recipient_id is not None
            else None
        )
        if capture.campaign_recipient_id is not None and recipient is None:
            counts["missing_native_recipient"] += 1
            issue = True
        if recipient is not None:
            if recipient[0] != capture.campaign_id:
                counts["recipient_campaign_mismatch"] += 1
                issue = True
            recipient_party_id = subscribers.get(recipient[1], "missing")
            if lead is None or recipient_party_id != lead.party_id:
                counts["recipient_party_mismatch"] += 1
                issue = True
        contract_invalid = (
            method not in _CAPTURE_METHODS
            or platform not in _SOURCE_PLATFORMS
            or capture.lead_source not in _LEAD_SOURCES
        )
        expected_platform = _METHOD_PLATFORM.get(method)
        if expected_platform is not None and platform != expected_platform:
            contract_invalid = True
        expected_sources = _PLATFORM_LEAD_SOURCES.get(platform)
        if expected_sources is not None and capture.lead_source not in expected_sources:
            contract_invalid = True
        if method == LeadCaptureMethod.campaign_response.value and (
            platform != LeadSourcePlatform.sub_campaign.value
            or capture.campaign_id is None
            or capture.campaign_recipient_id is None
        ):
            contract_invalid = True
        campaign_channel = (
            campaigns.get(capture.campaign_id)
            if capture.campaign_id is not None
            else None
        )
        campaign_source_mismatch = (
            campaign_channel == CampaignChannel.email.value
            and capture.lead_source != "Email"
        ) or (
            campaign_channel == CampaignChannel.whatsapp.value
            and capture.lead_source != "Whatsapp"
        )
        if (
            method == LeadCaptureMethod.campaign_response.value
            and campaign_source_mismatch
        ):
            contract_invalid = True
        if method == LeadCaptureMethod.ad_lead_form_webhook.value and (
            platform
            not in {LeadSourcePlatform.meta.value, LeadSourcePlatform.google.value}
            or not str(capture.external_campaign_id or "").strip()
        ):
            contract_invalid = True
        if contract_invalid:
            counts["invalid_capture_contract"] += 1
            issue = True
        if lead is not None and (
            lead.lead_source != capture.lead_source
            or lead.campaign_id != capture.campaign_id
            or lead.campaign_recipient_id != capture.campaign_recipient_id
        ):
            counts["lead_projection_mismatch"] += 1
            issue = True
        if not issue:
            counts["aligned"] += 1

    counts["leads_without_capture"] = len(set(leads) - captured_leads)
    result: dict[str, Any] = {
        key: int(counts[key])
        for key in (
            "total",
            "leads_without_capture",
            "missing_lead",
            "incomplete_evidence",
            "missing_native_campaign",
            "missing_native_recipient",
            "recipient_campaign_mismatch",
            "recipient_party_mismatch",
            "invalid_capture_contract",
            "lead_projection_mismatch",
            "aligned",
        )
    }
    result["by_capture_method"] = _controlled_counts(methods, allowed=_CAPTURE_METHODS)
    result["by_source_platform"] = _controlled_counts(
        platforms, allowed=_SOURCE_PLATFORMS
    )
    result["by_lead_source"] = _controlled_counts(sources, allowed=_LEAD_SOURCES)
    return result


def _referral_counts(
    referrals: list[Referral],
    *,
    parties: dict[UUID, str],
    subscribers: dict[UUID, UUID | None],
    leads: dict[UUID, Lead],
    captures: dict[UUID, LeadOriginCapture],
    contact_party_ids: set[UUID],
) -> dict[str, int]:
    counts: Counter[str] = Counter(total=len(referrals))
    for referral in referrals:
        issue = False
        party_evidence = (
            referral.party_bound_at,
            referral.party_binding_source,
            referral.party_binding_reason,
        )
        subscriber_evidence = (
            referral.subscriber_linked_at,
            referral.subscriber_link_source,
            referral.subscriber_link_reason,
        )
        if referral.referred_party_id is None:
            if referral.referred_subscriber_id is not None:
                counts["subscriber_only_legacy"] += 1
            else:
                counts["without_identity"] += 1
            if _any(party_evidence):
                counts["incomplete_party_evidence"] += 1
            issue = True
        else:
            counts["party_bound"] += 1
            if not _complete(party_evidence):
                counts["incomplete_party_evidence"] += 1
                issue = True
            if parties.get(referral.referred_party_id) not in _ROUTABLE_PARTY_STATUSES:
                counts["missing_or_nonroutable_party"] += 1
                issue = True
            if referral.referred_party_id not in contact_party_ids:
                counts["party_without_contact_point"] += 1
                issue = True

        lead = (
            leads.get(referral.referred_lead_id)
            if referral.referred_lead_id is not None
            else None
        )
        if lead is None:
            counts["missing_lead"] += 1
            issue = True
        elif referral.referred_party_id is not None:
            if lead.party_id != referral.referred_party_id:
                counts["lead_party_mismatch"] += 1
                issue = True
            capture = captures.get(lead.id)
            if (
                capture is None
                or _value(capture.capture_method) != LeadCaptureMethod.referral.value
                or _value(capture.source_platform) != LeadSourcePlatform.referral.value
                or capture.lead_source != "Referrer"
            ):
                counts["missing_or_invalid_referral_origin"] += 1
                issue = True

        if referral.referred_subscriber_id is None:
            counts["awaiting_account_conversion"] += 1
            party_status = (
                parties.get(referral.referred_party_id)
                if referral.referred_party_id is not None
                else None
            )
            if party_status == PartyIdentityStatus.quarantined.value:
                counts["quarantined_awaiting_account_adjudication"] += 1
            elif party_status == PartyIdentityStatus.active.value:
                counts["active_awaiting_account_conversion"] += 1
            if _any(subscriber_evidence):
                counts["incomplete_subscriber_evidence"] += 1
                issue = True
        elif referral.referred_party_id is not None:
            counts["account_attached"] += 1
            if not _complete(subscriber_evidence):
                counts["incomplete_subscriber_evidence"] += 1
                issue = True
            subscriber_party_id = subscribers.get(
                referral.referred_subscriber_id, "missing"
            )
            if subscriber_party_id == "missing":
                counts["missing_subscriber"] += 1
                issue = True
            elif subscriber_party_id != referral.referred_party_id:
                counts["subscriber_party_mismatch"] += 1
                issue = True
            if (
                lead is not None
                and lead.subscriber_id != referral.referred_subscriber_id
            ):
                counts["lead_subscriber_mismatch"] += 1
                issue = True
        elif _any(subscriber_evidence):
            counts["unexpected_legacy_subscriber_evidence"] += 1
            issue = True

        metadata = referral.metadata_ if isinstance(referral.metadata_, dict) else {}
        capture_metadata = metadata.get("capture")
        if isinstance(capture_metadata, dict) and any(
            capture_metadata.get(key) for key in ("name", "email", "phone")
        ):
            counts["legacy_capture_pii_metadata"] += 1
            issue = True
        if not issue:
            counts["aligned"] += 1

    keys = (
        "total",
        "party_bound",
        "subscriber_only_legacy",
        "without_identity",
        "incomplete_party_evidence",
        "missing_or_nonroutable_party",
        "party_without_contact_point",
        "missing_lead",
        "lead_party_mismatch",
        "missing_or_invalid_referral_origin",
        "awaiting_account_conversion",
        "quarantined_awaiting_account_adjudication",
        "active_awaiting_account_conversion",
        "account_attached",
        "incomplete_subscriber_evidence",
        "unexpected_legacy_subscriber_evidence",
        "missing_subscriber",
        "subscriber_party_mismatch",
        "lead_subscriber_mismatch",
        "legacy_capture_pii_metadata",
        "aligned",
    )
    return {key: int(counts[key]) for key in keys}


def _quote_counts(
    quotes: list[Quote],
    *,
    leads: dict[UUID, Lead],
    subscribers: dict[UUID, UUID | None],
) -> dict[str, int]:
    counts = Counter(total=len(quotes))
    for quote in quotes:
        if quote.lead_id is None:
            counts["without_lead"] += 1
            continue
        counts["with_lead"] += 1
        lead = leads.get(quote.lead_id)
        if lead is None:
            counts["missing_lead"] += 1
            continue
        subscriber_party_id = (
            subscribers.get(quote.subscriber_id, "missing")
            if quote.subscriber_id is not None
            else "missing"
        )
        if subscriber_party_id == "missing":
            counts["missing_subscriber"] += 1
        elif lead.party_id is not None and subscriber_party_id is None:
            counts["subscriber_without_party"] += 1
        elif lead.party_id is not None and subscriber_party_id != lead.party_id:
            counts["lead_party_mismatch"] += 1
        elif lead.party_id is None and lead.subscriber_id != quote.subscriber_id:
            counts["legacy_subscriber_mismatch"] += 1
        else:
            counts["aligned_with_lead"] += 1
    keys = (
        "total",
        "without_lead",
        "with_lead",
        "missing_lead",
        "missing_subscriber",
        "subscriber_without_party",
        "lead_party_mismatch",
        "legacy_subscriber_mismatch",
        "aligned_with_lead",
    )
    return {key: int(counts[key]) for key in keys}


def _sales_order_counts(
    orders: list[SalesOrder],
    *,
    quotes: dict[UUID, Quote],
    subscribers: dict[UUID, UUID | None],
) -> dict[str, int]:
    counts = Counter(total=len(orders))
    for order in orders:
        if order.quote_id is None:
            counts["without_quote"] += 1
            continue
        counts["with_quote"] += 1
        quote = quotes.get(order.quote_id)
        if quote is None:
            counts["missing_quote"] += 1
        elif order.subscriber_id != quote.subscriber_id:
            counts["subscriber_mismatch"] += 1
            quote_party_id = (
                subscribers.get(quote.subscriber_id)
                if quote.subscriber_id is not None
                else None
            )
            if subscribers.get(order.subscriber_id) != quote_party_id:
                counts["party_mismatch"] += 1
        else:
            counts["aligned_with_quote"] += 1
    keys = (
        "total",
        "without_quote",
        "with_quote",
        "missing_quote",
        "subscriber_mismatch",
        "party_mismatch",
        "aligned_with_quote",
    )
    return {key: int(counts[key]) for key in keys}


def _subscriber_sales_order_counts(
    subscriber_rows: list[Subscriber],
    *,
    orders: dict[UUID, SalesOrder],
) -> dict[str, int]:
    counts = Counter(total=len(subscriber_rows))
    for subscriber in subscriber_rows:
        if subscriber.sales_order_id is None:
            counts["without_sales_order"] += 1
            continue
        counts["with_sales_order"] += 1
        order = orders.get(subscriber.sales_order_id)
        if order is None:
            counts["missing_sales_order"] += 1
        elif order.subscriber_id != subscriber.id:
            counts["subscriber_mismatch"] += 1
        else:
            counts["aligned"] += 1
    keys = (
        "total",
        "without_sales_order",
        "with_sales_order",
        "missing_sales_order",
        "subscriber_mismatch",
        "aligned",
    )
    return {key: int(counts[key]) for key in keys}


def _subscription_counts(
    subscriptions: list[Subscription],
    *,
    subscribers: dict[UUID, UUID | None],
) -> dict[str, Any]:
    counts = Counter(total=len(subscriptions))
    statuses: list[str] = []
    for subscription in subscriptions:
        statuses.append(_value(subscription.status))
        subscriber_party_id = subscribers.get(subscription.subscriber_id, "missing")
        if subscriber_party_id == "missing":
            counts["missing_subscriber"] += 1
        elif subscriber_party_id is None:
            counts["subscriber_without_party"] += 1
        else:
            counts["party_linked"] += 1
    result: dict[str, Any] = {
        key: int(counts[key])
        for key in (
            "total",
            "missing_subscriber",
            "subscriber_without_party",
            "party_linked",
        )
    }
    result["by_status"] = _controlled_counts(
        statuses, allowed={item.value for item in SubscriptionStatus}
    )
    return result


def _ticket_counts(
    tickets: list[Ticket],
    *,
    leads: dict[UUID, Lead],
    subscribers: dict[UUID, UUID | None],
) -> dict[str, int]:
    counts = Counter(total=len(tickets))
    for ticket in tickets:
        if ticket.lead_id is None:
            counts["without_lead"] += 1
            continue
        counts["with_lead"] += 1
        lead = leads.get(ticket.lead_id)
        if lead is None:
            counts["missing_lead"] += 1
            continue
        linked_ids = {
            value
            for value in (
                ticket.subscriber_id,
                ticket.customer_account_id,
                ticket.customer_person_id,
            )
            if value is not None
        }
        if not linked_ids:
            counts["lead_only"] += 1
            counts["aligned_with_lead"] += 1
            continue
        counts["customer_linked"] += 1
        issue = False
        for subscriber_id in linked_ids:
            subscriber_party_id = subscribers.get(subscriber_id, "missing")
            if subscriber_party_id == "missing":
                counts["missing_linked_subscriber"] += 1
                issue = True
            elif lead.party_id is not None and subscriber_party_id != lead.party_id:
                counts["party_mismatch"] += 1
                issue = True
            elif lead.party_id is None and subscriber_id != lead.subscriber_id:
                counts["legacy_subscriber_mismatch"] += 1
                issue = True
        if not issue:
            counts["aligned_with_lead"] += 1
    keys = (
        "total",
        "without_lead",
        "with_lead",
        "missing_lead",
        "lead_only",
        "customer_linked",
        "missing_linked_subscriber",
        "party_mismatch",
        "legacy_subscriber_mismatch",
        "aligned_with_lead",
    )
    return {key: int(counts[key]) for key in keys}


def _delivery_counts(
    *,
    orders: list[SalesOrder],
    projects: list[Project],
    installations: list[InstallationProject],
    service_orders: list[ServiceOrder],
    handoffs: list[CustomerExperienceHandoff],
) -> dict[str, int]:
    """PII-free convergence from accepted sale through CX acceptance."""

    counts: Counter[str] = Counter()
    projects_by_id = {row.id: row for row in projects}
    project_by_order = {
        row.sales_order_id: row for row in projects if row.sales_order_id is not None
    }
    installations_by_id = {row.id: row for row in installations}
    installation_by_project = {row.project_id: row for row in installations}
    handoff_by_service_order = {row.service_order_id: row for row in handoffs}
    for order in orders:
        if not order.is_active or order.status == "cancelled":
            continue
        counts["eligible_sales_orders"] += 1
        project = project_by_order.get(order.id)
        if project is None:
            counts["sales_orders_without_project"] += 1
            continue
        if (
            project.subscriber_id != order.subscriber_id
            or project.quote_id != order.quote_id
        ):
            counts["project_context_mismatch"] += 1
        installation = installation_by_project.get(project.id)
        if installation is None:
            counts["projects_without_installation"] += 1
        elif installation.subscriber_id != project.subscriber_id:
            counts["installation_context_mismatch"] += 1

    seen_line_ids: set[UUID] = set()
    for service_order in service_orders:
        if service_order.sales_order_line_id is None:
            continue
        counts["sales_service_orders"] += 1
        if service_order.sales_order_line_id in seen_line_ids:
            counts["duplicate_sales_line_service_orders"] += 1
        seen_line_ids.add(service_order.sales_order_line_id)
        project = (
            projects_by_id.get(service_order.project_id)
            if service_order.project_id is not None
            else None
        )
        installation = (
            installations_by_id.get(service_order.installation_project_id)
            if service_order.installation_project_id is not None
            else None
        )
        if (
            project is None
            or installation is None
            or project.sales_order_id != service_order.sales_order_id
            or installation.project_id != service_order.project_id
            or service_order.subscriber_id != project.subscriber_id
        ):
            counts["service_order_context_mismatch"] += 1
        if (
            installation is not None
            and installation.status == InstallationProjectStatus.verified.value
            and service_order.status == ServiceOrderStatus.draft
        ):
            counts["verified_implementation_not_released"] += 1
        if service_order.status == ServiceOrderStatus.active:
            counts["active_sales_service_orders"] += 1
            handoff = handoff_by_service_order.get(service_order.id)
            if handoff is None:
                counts["active_service_orders_without_cx_handoff"] += 1
            elif (
                handoff.subscriber_id != service_order.subscriber_id
                or handoff.subscription_id != service_order.subscription_id
                or handoff.sales_order_id != service_order.sales_order_id
                or handoff.project_id != service_order.project_id
                or handoff.installation_project_id
                != service_order.installation_project_id
            ):
                counts["cx_handoff_context_mismatch"] += 1

    counts["cx_handoffs"] = len(handoffs)
    counts["cx_handoffs_accepted"] = sum(row.status == "accepted" for row in handoffs)
    counts["cx_handoffs_needing_attention"] = sum(
        row.status == "needs_attention" for row in handoffs
    )
    keys = (
        "eligible_sales_orders",
        "sales_orders_without_project",
        "project_context_mismatch",
        "projects_without_installation",
        "installation_context_mismatch",
        "sales_service_orders",
        "duplicate_sales_line_service_orders",
        "service_order_context_mismatch",
        "verified_implementation_not_released",
        "active_sales_service_orders",
        "active_service_orders_without_cx_handoff",
        "cx_handoff_context_mismatch",
        "cx_handoffs",
        "cx_handoffs_accepted",
        "cx_handoffs_needing_attention",
    )
    return {key: int(counts[key]) for key in keys}


def build_customer_lifecycle_audit(db: Session) -> dict[str, Any]:
    """Return aggregate lifecycle-link coverage without changing database state."""

    inspector = inspect(db.get_bind())
    table_names = set(inspector.get_table_names())
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - table_names)
    if missing_tables:
        return _not_installed(missing_tables=missing_tables)
    missing_columns = {
        table_name: sorted(
            required - {column["name"] for column in inspector.get_columns(table_name)}
        )
        for table_name, required in _REQUIRED_COLUMNS.items()
    }
    missing_columns = {
        table_name: columns
        for table_name, columns in missing_columns.items()
        if columns
    }
    if missing_columns:
        return _not_installed(missing_columns=missing_columns)

    party_rows = db.query(Party.id, Party.status).all()
    subscriber_rows = db.query(Subscriber).all()
    lead_rows = db.query(Lead).all()
    capture_rows = db.query(LeadOriginCapture).all()
    quote_rows = db.query(Quote).all()
    order_rows = db.query(SalesOrder).all()
    subscription_rows = db.query(Subscription).all()
    ticket_rows = db.query(Ticket).all()
    project_rows = db.query(Project).all()
    installation_rows = db.query(InstallationProject).all()
    service_order_rows = db.query(ServiceOrder).all()
    handoff_rows = db.query(CustomerExperienceHandoff).all()
    referral_rows = db.query(Referral).filter(Referral.is_active.is_(True)).all()
    parties = {row.id: _value(row.status) for row in party_rows}
    subscribers = {row.id: row.party_id for row in subscriber_rows}
    leads = {row.id: row for row in lead_rows}
    quotes = {row.id: row for row in quote_rows}
    orders = {row.id: row for row in order_rows}
    campaigns = {
        row.id: row.channel for row in db.query(Campaign.id, Campaign.channel).all()
    }
    recipients = {
        row.id: (row.campaign_id, row.subscriber_id)
        for row in db.query(
            CampaignRecipient.id,
            CampaignRecipient.campaign_id,
            CampaignRecipient.subscriber_id,
        ).all()
    }
    contact_party_ids = {
        row.party_id
        for row in db.query(PartyContactPoint.party_id)
        .filter(PartyContactPoint.is_active.is_(True))
        .distinct()
        .all()
    }
    captures = {row.lead_id: row for row in capture_rows}
    return {
        "status": "installed",
        "lead_identity": _lead_identity_counts(
            lead_rows, parties=parties, subscribers=subscribers
        ),
        "origin_capture": _origin_counts(
            capture_rows,
            leads=leads,
            campaigns=campaigns,
            recipients=recipients,
            subscribers=subscribers,
        ),
        "referrals": _referral_counts(
            referral_rows,
            parties=parties,
            subscribers=subscribers,
            leads=leads,
            captures=captures,
            contact_party_ids=contact_party_ids,
        ),
        "quotes": _quote_counts(quote_rows, leads=leads, subscribers=subscribers),
        "sales_orders": _sales_order_counts(
            order_rows, quotes=quotes, subscribers=subscribers
        ),
        "subscriber_sales_order_links": _subscriber_sales_order_counts(
            subscriber_rows, orders=orders
        ),
        "subscriptions": _subscription_counts(
            subscription_rows, subscribers=subscribers
        ),
        "tickets": _ticket_counts(ticket_rows, leads=leads, subscribers=subscribers),
        "delivery": _delivery_counts(
            orders=order_rows,
            projects=project_rows,
            installations=installation_rows,
            service_orders=service_order_rows,
            handoffs=handoff_rows,
        ),
        "artifact_contract": _artifact_contract(),
    }


def _artifact_contract() -> dict[str, bool]:
    return {
        "read_only": True,
        "contains_identity_values": False,
        "automatic_party_or_account_binding": False,
        "automatic_origin_inference": False,
        "changes_sales_or_support_lifecycle": False,
        "changes_subscription_billing_or_access_state": False,
    }


def _not_installed(**details: Any) -> dict[str, Any]:
    return {
        "status": "not_installed",
        **details,
        "artifact_contract": _artifact_contract(),
    }
