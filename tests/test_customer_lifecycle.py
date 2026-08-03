from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.catalog import (
    AccessType,
    CatalogOffer,
    PriceBasis,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.comms_campaign import Campaign, CampaignRecipient
from app.models.party import PartyType
from app.models.sales import Lead, Quote, SalesOrder
from app.models.subscriber import Subscriber
from app.models.support import Ticket
from app.schemas.sales import (
    LeadCreate,
    LeadOriginCaptureCreate,
    LeadUpdate,
    QuoteCreate,
    QuoteUpdate,
)
from app.schemas.sales_order import SalesOrderCreate, SalesOrderUpdate
from app.schemas.support import TicketCreate, TicketUpdate
from app.services import party as party_service
from app.services import sales as sales_service
from app.services import sales_orders, support
from app.services.customer_lifecycle_audit import build_customer_lifecycle_audit
from app.services.domain_errors import DomainError
from app.services.sales import lifecycle as lead_lifecycle
from scripts.migration.audit_customer_lifecycle import _set_transaction_read_only

_EVIDENCE = {
    "source": "reviewed_customer_lifecycle",
    "reason": "Reviewed Party and account lifecycle context",
}


def _party(db_session, label: str = "Private Prospect"):
    return party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name=label,
    )


def _subscriber(db_session, party, label: str = "Private Customer"):
    subscriber = Subscriber(
        first_name=label,
        last_name="Record",
        email=f"{uuid.uuid4().hex}@example.test",
    )
    db_session.add(subscriber)
    db_session.flush()
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=party.id,
        **_EVIDENCE,
    )
    return subscriber


def _website_origin() -> LeadOriginCaptureCreate:
    return LeadOriginCaptureCreate(
        capture_method="landing_page",
        source_platform="website",
        utm_source="website",
        landing_path="/fiber/abuja",
        capture_source="public_lead_form",
        capture_reason="Captured with the lead creation request",
    )


def test_party_first_lead_does_not_require_fake_subscriber(db_session):
    party = _party(db_session)

    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
            lead_source="Website",
            origin_capture=_website_origin(),
        ),
    )

    assert lead.party_id == party.id
    assert lead.subscriber_id is None
    assert lead.origin_capture.capture_method == "landing_page"
    assert lead.origin_capture.landing_path == "/fiber/abuja"
    assert db_session.query(Subscriber).count() == 0


def test_external_ad_origin_keeps_provider_ids_out_of_native_campaign_columns(
    db_session,
):
    party = _party(db_session)

    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
            lead_source="Facebook Ads",
            origin_capture=LeadOriginCaptureCreate(
                capture_method="ad_lead_form_webhook",
                source_platform="meta",
                external_campaign_id="meta-campaign-123",
                external_ad_set_id="meta-adset-456",
                external_form_id="meta-form-789",
                capture_source="meta_lead_webhook",
                capture_reason="Provider-signed lead form delivery",
            ),
        ),
    )

    assert lead.campaign_id is None
    assert lead.campaign_recipient_id is None
    assert lead.origin_capture.external_campaign_id == "meta-campaign-123"


def test_origin_capture_rejects_method_platform_and_source_conflicts(db_session):
    party = _party(db_session)

    with pytest.raises(HTTPException) as exc:
        sales_service.leads.create(
            db_session,
            LeadCreate(
                party_id=party.id,
                party_binding_source=_EVIDENCE["source"],
                party_binding_reason=_EVIDENCE["reason"],
                lead_source="Google",
                origin_capture=LeadOriginCaptureCreate(
                    capture_method="landing_page",
                    source_platform="meta",
                    external_campaign_id="meta-campaign-123",
                    capture_source="invalid_test",
                    capture_reason="Method and platform deliberately conflict",
                ),
            ),
        )

    assert exc.value.status_code == 400
    assert "requires source_platform='website'" in exc.value.detail
    assert db_session.query(Lead).count() == 0


def test_native_campaign_origin_requires_matching_recipient_party(db_session):
    party = _party(db_session)
    subscriber = _subscriber(db_session, party)
    campaign = Campaign(name="Private campaign")
    db_session.add(campaign)
    db_session.flush()
    recipient = CampaignRecipient(
        campaign_id=campaign.id,
        subscriber_id=subscriber.id,
        address="private@example.test",
    )
    db_session.add(recipient)
    db_session.flush()

    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
            lead_source="Email",
            origin_capture=LeadOriginCaptureCreate(
                capture_method="campaign_response",
                source_platform="sub_campaign",
                campaign_id=campaign.id,
                campaign_recipient_id=recipient.id,
                capture_source="campaign_reply_handler",
                capture_reason="Reply linked to a native recipient",
            ),
        ),
    )

    assert lead.campaign_id == campaign.id
    assert lead.campaign_recipient_id == recipient.id


def test_reviewed_account_attachment_is_idempotent_and_refuses_repoint(db_session):
    party = _party(db_session)
    other_party = _party(db_session, "Other Private Party")
    subscriber = _subscriber(db_session, party)
    other_subscriber = _subscriber(db_session, other_party, "Other Customer")
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
        ),
    )

    attached = lead_lifecycle.attach_lead_subscriber(
        db_session,
        lead_id=lead.id,
        subscriber_id=subscriber.id,
        **_EVIDENCE,
    )
    evidence = (
        attached.subscriber_linked_at,
        attached.subscriber_link_source,
        attached.subscriber_link_reason,
    )
    retried = lead_lifecycle.attach_lead_subscriber(
        db_session,
        lead_id=lead.id,
        subscriber_id=subscriber.id,
        source="ignored_retry",
        reason="Ignored retry evidence",
    )

    assert retried.subscriber_id == subscriber.id
    assert (
        retried.subscriber_linked_at,
        retried.subscriber_link_source,
        retried.subscriber_link_reason,
    ) == evidence
    with pytest.raises(lead_lifecycle.LeadLifecycleError, match="does not match"):
        lead_lifecycle.attach_lead_subscriber(
            db_session,
            lead_id=lead.id,
            subscriber_id=other_subscriber.id,
            **_EVIDENCE,
        )


def test_origin_capture_and_its_lead_source_projection_are_immutable(db_session):
    party = _party(db_session)
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
            lead_source="Website",
            origin_capture=_website_origin(),
        ),
    )

    with pytest.raises(HTTPException) as exc:
        sales_service.leads.update(
            db_session,
            str(lead.id),
            LeadUpdate(lead_source="Google"),
        )

    assert exc.value.status_code == 409
    assert lead.lead_source == "Website"


def test_quote_order_and_ticket_guards_reject_cross_party_links(db_session):
    party = _party(db_session)
    other_party = _party(db_session, "Other Private Party")
    subscriber = _subscriber(db_session, party)
    other_subscriber = _subscriber(db_session, other_party, "Other Customer")
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
        ),
    )

    with pytest.raises(HTTPException) as quote_error:
        sales_service.quotes.create(
            db_session,
            QuoteCreate(
                subscriber_id=other_subscriber.id,
                lead_id=lead.id,
                project_type="fiber_optics_installation",
            ),
        )
    assert quote_error.value.status_code == 409

    quote = sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
        ),
    )
    with pytest.raises(HTTPException) as order_error:
        sales_orders.sales_orders.create(
            db_session,
            SalesOrderCreate(
                subscriber_id=other_subscriber.id,
                quote_id=quote.id,
            ),
        )
    assert order_error.value.status_code == 409

    with pytest.raises(DomainError) as ticket_error:
        support.tickets.create(
            db_session,
            TicketCreate(
                title="Private support request",
                lead_id=lead.id,
                subscriber_id=other_subscriber.id,
            ),
        )
    assert ticket_error.value.code == "lead_subscriber_mismatch"

    lead_only_ticket = support.tickets.create(
        db_session,
        TicketCreate(title="Private prospect question", lead_id=lead.id),
    )
    assert lead_only_ticket.lead_id == lead.id
    assert lead_only_ticket.subscriber_id is None


def test_quote_authoring_does_not_attach_an_account_before_acceptance(
    db_session,
):
    party = _party(db_session)
    subscriber = _subscriber(db_session, party)
    sibling_account = _subscriber(db_session, party, "Sibling Account")
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
        ),
    )

    first_quote = sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
        ),
    )

    sibling_quote = sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=sibling_account.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
        ),
    )

    db_session.refresh(lead)
    assert lead.subscriber_id is None
    assert lead.subscriber_linked_at is None
    assert first_quote.lead_id == sibling_quote.lead_id == lead.id


def test_quote_order_and_ticket_update_guards_validate_prospective_links(db_session):
    party = _party(db_session)
    other_party = _party(db_session, "Other Private Party")
    subscriber = _subscriber(db_session, party)
    other_subscriber = _subscriber(db_session, other_party, "Other Customer")
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
        ),
    )
    quote = Quote(subscriber_id=subscriber.id, lead_id=lead.id)
    db_session.add(quote)
    db_session.flush()
    order = SalesOrder(subscriber_id=subscriber.id, quote_id=quote.id)
    ticket = Ticket(
        title="Private linked ticket",
        lead_id=lead.id,
        subscriber_id=subscriber.id,
    )
    db_session.add_all((order, ticket))
    db_session.flush()

    with pytest.raises(HTTPException) as quote_error:
        sales_service.quotes.update(
            db_session,
            str(quote.id),
            QuoteUpdate(subscriber_id=other_subscriber.id),
        )
    assert quote_error.value.status_code == 409

    with pytest.raises(HTTPException) as order_error:
        sales_orders.sales_orders.update(
            db_session,
            str(order.id),
            SalesOrderUpdate(subscriber_id=other_subscriber.id),
        )
    assert order_error.value.status_code == 409

    with pytest.raises(DomainError) as ticket_error:
        support.tickets.update(
            db_session,
            str(ticket.id),
            TicketUpdate(subscriber_id=other_subscriber.id),
        )
    assert ticket_error.value.code == "lead_subscriber_mismatch"


def test_lifecycle_audit_reports_aggregate_alignment_without_identity_values(
    db_session,
):
    private_name = "Private Lifecycle Name"
    private_email = "private-lifecycle@example.test"
    party = _party(db_session, private_name)
    subscriber = Subscriber(
        first_name=private_name,
        last_name="Record",
        email=private_email,
    )
    db_session.add(subscriber)
    db_session.flush()
    party_service.bind_subscriber_account(
        db_session,
        subscriber_id=subscriber.id,
        party_id=party.id,
        **_EVIDENCE,
    )
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            party_id=party.id,
            subscriber_id=subscriber.id,
            party_binding_source=_EVIDENCE["source"],
            party_binding_reason=_EVIDENCE["reason"],
            lead_source="Website",
            origin_capture=_website_origin(),
        ),
    )
    quote = Quote(subscriber_id=subscriber.id, lead_id=lead.id)
    db_session.add(quote)
    db_session.flush()
    order = SalesOrder(subscriber_id=subscriber.id, quote_id=quote.id)
    db_session.add(order)
    db_session.flush()
    subscriber.sales_order_id = order.id
    offer = CatalogOffer(
        name="Private Offer",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        Subscription(
            subscriber_id=subscriber.id,
            offer_id=offer.id,
            status=SubscriptionStatus.blocked,
        )
    )
    db_session.add(Ticket(title="Private Ticket", lead_id=lead.id))
    db_session.flush()

    audit = build_customer_lifecycle_audit(db_session)
    serialized = json.dumps(audit, sort_keys=True)

    assert audit["status"] == "installed"
    assert audit["lead_identity"]["aligned"] == 1
    assert audit["origin_capture"]["aligned"] == 1
    assert audit["quotes"]["aligned_with_lead"] == 1
    assert audit["sales_orders"]["aligned_with_quote"] == 1
    assert audit["subscriber_sales_order_links"]["aligned"] == 1
    assert audit["subscriptions"]["party_linked"] == 1
    assert audit["subscriptions"]["by_status"]["blocked"] == 1
    assert audit["tickets"]["lead_only"] == 1
    assert audit["artifact_contract"]["read_only"] is True
    assert private_name not in serialized
    assert private_email not in serialized
    assert str(party.id) not in serialized


def test_lifecycle_audit_surfaces_legacy_cross_account_debt(db_session):
    first = Subscriber(
        first_name="First",
        last_name="Record",
        email="first-legacy@example.test",
    )
    second = Subscriber(
        first_name="Second",
        last_name="Record",
        email="second-legacy@example.test",
    )
    db_session.add_all((first, second))
    db_session.flush()
    lead = Lead(subscriber_id=first.id)
    db_session.add(lead)
    db_session.flush()
    quote = Quote(subscriber_id=second.id, lead_id=lead.id)
    db_session.add(quote)
    db_session.flush()
    db_session.add(SalesOrder(subscriber_id=first.id, quote_id=quote.id))
    db_session.add(
        Ticket(
            title="Legacy mismatch",
            lead_id=lead.id,
            customer_account_id=second.id,
        )
    )
    db_session.flush()

    audit = build_customer_lifecycle_audit(db_session)

    assert audit["quotes"]["legacy_subscriber_mismatch"] == 1
    assert audit["sales_orders"]["subscriber_mismatch"] == 1
    assert audit["tickets"]["legacy_subscriber_mismatch"] == 1


def test_operator_lifecycle_audit_uses_read_only_repeatable_read_transaction():
    executed: list[str] = []
    postgresql_db = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
        execute=lambda statement: executed.append(str(statement)),
    )

    _set_transaction_read_only(postgresql_db)

    assert executed == ["SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"]
