from __future__ import annotations

from uuid import uuid4

from app.models.integration_platform import IntegrationInstallation
from app.models.party import (
    PartyContactPointType,
    PartyContactVerificationStatus,
    PartyRoleStatus,
    PartyRoleType,
    PartyType,
)
from app.models.sales import Lead, LeadOriginCapture
from app.services import party as party_service
from app.services.integrations import inbox as integration_inbox
from app.services.integrations import installations
from app.services.integrations.connectors.meta_social_runtime import (
    META_LEAD_CAPTURE_CAPABILITY,
)
from app.services.integrations.meta_social_contracts import (
    MetaLeadField,
    MetaLeadObservation,
)
from app.services.integrations.meta_social_installation import (
    META_SOCIAL_CONFIGURATION_SCOPE,
    ConfigureMetaSocialInstallationCommand,
    configure_meta_social_installation,
)
from app.services.integrations.runtime import ValidationResult
from app.services.owner_commands import CommandContext
from app.services.sales import meta_lead_ads


def _configure_meta_leads(db_session) -> IntegrationInstallation:
    result = configure_meta_social_installation(
        db_session,
        ConfigureMetaSocialInstallationCommand(
            auth_mode="individual",
            app_id="app-1",
            facebook_page_id="page-1",
            instagram_account_id="ig-1",
            graph_version="v21.0",
            webhook_url="https://sub.example.test/api/v1/webhooks/meta",
            meta_oauth_access_token_ref="",
            facebook_page_access_token_ref=(
                "bao://secret/integrations/meta_social#facebook_page_access_token"
            ),
            instagram_login_access_token_ref=(
                "bao://secret/integrations/meta_social#instagram_login_access_token"
            ),
            webhook_signing_secret_ref=(
                "bao://secret/integrations/meta_social#webhook_signing_secret"
            ),
            webhook_verify_token_ref=(
                "bao://secret/integrations/meta_social#webhook_verify_token"
            ),
            environment="test",
        ),
        context=CommandContext.system(
            actor="test",
            scope=META_SOCIAL_CONFIGURATION_SCOPE,
            reason="Configure Meta Lead capture test",
            idempotency_key="test-meta-lead-config",
        ),
    )
    installation = db_session.get(IntegrationInstallation, result.installation_id)
    assert installation is not None
    installations.enable_after_connection_validation(
        db_session,
        installation_id=installation.id,
        connection_result=ValidationResult(valid=True),
    )
    db_session.commit()
    return installation


def test_verified_meta_receipt_captures_party_first_lead(db_session):
    installation = _configure_meta_leads(db_session)
    binding = next(
        item
        for item in installation.capability_bindings
        if item.capability_id == META_LEAD_CAPTURE_CAPABILITY
    )
    receipt, claimed = integration_inbox.receive_and_claim_verified(
        db_session,
        capability_binding_id=binding.id,
        provider_event_id="leadgen-42",
        event_type="meta.leadgen.webhook.v1",
        payload={"leadgen_id": "leadgen-42", "page_id": "page-1"},
    )

    outcome = meta_lead_ads.capture_meta_lead(
        db_session,
        meta_lead_ads.CaptureMetaLeadCommand(
            context=CommandContext.system(
                actor="test",
                scope=meta_lead_ads.META_LEAD_CAPTURE_SCOPE,
                reason="Capture unmatched Meta Lead fixture",
            ),
            receipt_id=receipt.id,
            observation=MetaLeadObservation(
                leadgen_id="leadgen-42",
                created_at="2026-09-01T10:00:00Z",
                page_id="page-1",
                form_id="form-1",
                campaign_id="campaign-1",
                fields=(
                    MetaLeadField(name="full_name", values=("Meta Prospect",)),
                    MetaLeadField(name="email", values=("meta@example.test",)),
                ),
            ),
        ),
    )

    assert claimed is True
    assert outcome.replayed is False
    lead = db_session.get(Lead, outcome.lead_id)
    assert lead is not None
    assert lead.party_id == outcome.party_id
    origin = (
        db_session.query(LeadOriginCapture)
        .filter(LeadOriginCapture.lead_id == lead.id)
        .one()
    )
    assert origin.source_interaction_id == "leadgen-42"


def test_verified_meta_lead_links_existing_customer_without_duplicate_lead(
    db_session, subscriber
):
    installation = _configure_meta_leads(db_session)
    binding = next(
        item
        for item in installation.capability_bindings
        if item.capability_id == META_LEAD_CAPTURE_CAPABILITY
    )
    customer_party = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Existing Meta Customer",
    )
    party_service.add_contact_point(
        db_session,
        party_id=customer_party.id,
        channel_type=PartyContactPointType.email,
        normalized_value="existing-meta@example.test",
        display_value="existing-meta@example.test",
        verification_status=PartyContactVerificationStatus.verified,
    )
    subscriber.party_id = customer_party.id
    subscriber.party_bound_at = subscriber.created_at
    subscriber.party_binding_source = "test"
    subscriber.party_binding_reason = "Reviewed existing Meta Customer"
    receipt, _claimed = integration_inbox.receive_and_claim_verified(
        db_session,
        capability_binding_id=binding.id,
        provider_event_id="leadgen-existing-customer",
        event_type="meta.leadgen.webhook.v1",
        payload={"leadgen_id": "leadgen-existing-customer", "page_id": "page-1"},
    )

    outcome = meta_lead_ads.capture_meta_lead(
        db_session,
        meta_lead_ads.CaptureMetaLeadCommand(
            context=CommandContext.system(
                actor="test",
                scope=meta_lead_ads.META_LEAD_CAPTURE_SCOPE,
                reason="Reject duplicate Meta Lead for existing Customer",
            ),
            receipt_id=receipt.id,
            observation=MetaLeadObservation(
                leadgen_id="leadgen-existing-customer",
                created_at="2026-09-01T10:00:00Z",
                page_id="page-1",
                form_id="form-1",
                fields=(
                    MetaLeadField(name="full_name", values=("Different Profile Name",)),
                    MetaLeadField(name="email", values=("existing-meta@example.test",)),
                ),
            ),
        ),
    )

    assert outcome.kind is meta_lead_ads.MetaLeadCaptureKind.customer_matched
    assert outcome.subscriber_id == subscriber.id
    assert outcome.lead_id is None
    assert db_session.query(Lead).count() == 0
    db_session.refresh(receipt)
    assert receipt.state == "processed"
    assert receipt.consequence_json["subscriber_id"] == str(subscriber.id)


def test_customer_match_is_review_only_and_uses_verified_party_contact(
    db_session, subscriber
):
    customer_party = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Existing Customer",
    )
    party_service.ensure_role(
        db_session,
        party_id=customer_party.id,
        role_type=PartyRoleType.customer,
        status=PartyRoleStatus.active,
        source="test",
    )
    party_service.add_contact_point(
        db_session,
        party_id=customer_party.id,
        channel_type=PartyContactPointType.email,
        normalized_value="same@example.test",
        display_value="same@example.test",
        verification_status=PartyContactVerificationStatus.verified,
    )
    subscriber.party_id = customer_party.id
    subscriber.party_bound_at = subscriber.created_at
    subscriber.party_binding_source = "test"
    subscriber.party_binding_reason = "Reviewed test customer"

    prospect = party_service.create_party(
        db_session,
        party_type=PartyType.person,
        display_name="Meta Prospect",
    )
    party_service.add_contact_point(
        db_session,
        party_id=prospect.id,
        channel_type=PartyContactPointType.email,
        normalized_value="same@example.test",
        display_value="same@example.test",
        verification_status=PartyContactVerificationStatus.unverified,
        provider="meta",
    )
    lead = Lead(
        id=uuid4(),
        party_id=prospect.id,
        party_bound_at=subscriber.created_at,
        party_binding_source="sales.capture",
        party_binding_reason="Captured prospect",
        title="Meta Lead",
        status="new",
        lead_source="Facebook Ads",
        is_active=True,
    )
    db_session.add(lead)
    db_session.commit()
    lead_id = lead.id
    db_session.rollback()

    outcome = meta_lead_ads.reconcile_customer_match(
        db_session,
        meta_lead_ads.ReconcileMetaLeadMatchCommand(
            context=CommandContext.system(
                actor="test",
                scope=meta_lead_ads.META_LEAD_MATCH_SCOPE,
                reason="Test exact candidate",
                idempotency_key=f"test-meta-match:{lead_id}",
            ),
            lead_id=lead_id,
        ),
    )

    db_session.refresh(lead)
    assert outcome.status is meta_lead_ads.MetaLeadMatchStatus.single_candidate
    assert outcome.subscriber_ids == (subscriber.id,)
    assert lead.subscriber_id is None
    assert lead.party_id == prospect.id
    assert lead.metadata_["meta_customer_match"]["status"] == "single_candidate"
