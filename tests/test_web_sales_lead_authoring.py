"""Focused contracts for the admin New Lead authoring experience."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.catalog import RegionZone
from app.models.organization import Organization, OrganizationAccountType
from app.models.party import (
    Party,
    PartyContactPoint,
    PartyContactPointType,
    PartyRelationship,
    PartyType,
)
from app.models.sales import Lead, LeadStatus, Pipeline, PipelineStage
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.subscriber import Reseller, Subscriber
from app.models.system_user import SystemUser
from app.models.team_inbox import InboxConversation
from app.services import conversation_lead_relationships, web_sales
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext
from app.services.sales import lead_authoring


def _staff_owner(db_session) -> SystemUser:
    person = Party(party_type=PartyType.person.value, display_name="Sales Owner")
    team = ServiceTeam(name=f"Sales-{uuid4().hex}", is_active=True)
    db_session.add_all([person, team])
    db_session.flush()
    user = SystemUser(
        first_name="Sales",
        last_name="Owner",
        email=f"sales-owner-{uuid4().hex}@example.com",
        is_active=True,
        person_party_id=person.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Lead owner fixture",
    )
    db_session.add(user)
    db_session.flush()
    db_session.add(
        ServiceTeamMember(team_id=team.id, person_id=person.id, is_active=True)
    )
    db_session.commit()
    return user


def _pipeline(db_session) -> tuple[Pipeline, PipelineStage]:
    pipeline = Pipeline(name=f"Pipeline-{uuid4().hex}", is_active=True)
    db_session.add(pipeline)
    db_session.flush()
    stage = PipelineStage(
        pipeline_id=pipeline.id,
        name="Qualified",
        order_index=1,
        is_active=True,
    )
    db_session.add(stage)
    db_session.commit()
    return pipeline, stage


def _region(db_session, *, active: bool = True) -> RegionZone:
    region = RegionZone(name=f"Region-{uuid4().hex[:8]}", is_active=active)
    db_session.add(region)
    db_session.commit()
    return region


def _organization(db_session, *, reseller: bool = False) -> tuple[Organization, Party]:
    party = Party(
        party_type=PartyType.organization.value,
        display_name=f"Organization {uuid4().hex[:8]}",
    )
    db_session.add(party)
    db_session.flush()
    organization = Organization(
        name=party.display_name,
        account_type=(
            OrganizationAccountType.reseller.value
            if reseller
            else OrganizationAccountType.prospect.value
        ),
        party_id=party.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Organization fixture",
        is_active=True,
    )
    db_session.add(organization)
    db_session.commit()
    return organization, party


def _author(db_session, actor: SystemUser, **overrides: object):
    actor_id = str(actor.id)
    # Public owner commands deliberately require a transaction-free adapter
    # session. ORM attribute refreshes in test setup may have autobegun a read.
    db_session.commit()
    fields: dict[str, object] = {
        "actor_system_user_id": actor_id,
        "submission_id": str(uuid4()),
        "display_name": "Amina Bello Yusuf",
        "status": "new",
        "owner_agent_id": actor_id,
        "emails": ["amina@example.com"],
        "primary_email": "0",
        "phones": ["08031234567"],
        "primary_phone": "0",
        "whatsapp_phone_indices": ["0"],
        "address_line1": "1 Marina Road",
        "address_line2": "Suite 2",
        "date_of_birth": "1990-04-03",
        "gender": "female",
        "nin": "12345678901",
        "city": "Lagos",
        "postal_code": "100001",
        "country_code": "ng",
        "organization_id": None,
        "managed_by_reseller": False,
        "reseller_id": None,
        "pipeline_id": None,
        "stage_id": None,
        "lead_source": "Website",
        "region_zone_id": None,
        "estimated_value": "250000.00",
        "currency": "ngn",
        "probability": "65",
        "expected_close_date": "2026-12-31",
        "lost_reason": None,
        "notes": "Site survey requested.",
        "is_active": True,
    }
    fields.update(overrides)
    return web_sales.author_lead_from_form(db_session, **fields)  # type: ignore[arg-type]


def test_new_lead_template_has_exactly_three_primary_cards_in_order():
    template = Path("templates/admin/sales/leads/new_form.html").read_text(
        encoding="utf-8"
    )
    titles = [
        'card("Contact / Lead Information"',
        'card("Pipeline and Value"',
        'card("Additional Information"',
    ]
    assert template.count("{% call card(") == 3
    assert [template.index(title) for title in titles] == sorted(
        template.index(title) for title in titles
    )
    assert "Create and qualify a new sales opportunity" in template
    assert "max-w-3xl" in template
    assert "dark:" in template
    assert "md:grid-cols-2" in template
    assert "md:grid-cols-3" in template


def test_new_lead_template_excludes_legacy_and_forged_identity_fields():
    template = Path("templates/admin/sales/leads/new_form.html").read_text(
        encoding="utf-8"
    )
    assert "Contact Type" not in template
    assert "Customer ID" not in template
    assert template.count('name="notes"') == 1
    assert 'name="address_line1"' in template
    assert 'name="address_line2"' in template
    assert 'name="person_id"' not in template
    assert 'name="party_id"' not in template
    assert "Linked to this Lead" in template
    assert '<select class="{{ input_class }}" id="region_zone_id"' in template


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Amina Bello Yusuf", ("Amina Bello Yusuf", "Amina", "Bello Yusuf")),
        ("Amina", ("Amina", "Amina", "Unknown")),
        ("", ("Unknown", "Unknown", "Unknown")),
    ],
)
def test_display_name_splitting(value, expected):
    assert lead_authoring.split_display_name(value) == expected


def test_display_name_limit_is_120_characters():
    assert lead_authoring.split_display_name("A" * 120)[0] == "A" * 120
    with pytest.raises(DomainError) as exc:
        lead_authoring.split_display_name("A" * 121)
    assert exc.value.code == "sales.lead_authoring.display_name_too_long"


def test_authoring_creates_one_party_lead_contacts_and_relationship(db_session):
    actor = _staff_owner(db_session)
    pipeline, stage = _pipeline(db_session)
    region = _region(db_session)
    organization, organization_party = _organization(db_session)

    outcome = _author(
        db_session,
        actor,
        emails=[" AMINA@example.com ", "amina@example.com", "second@example.com"],
        primary_email="2",
        phones=["08031234567", "+2348031234567", "08035557777"],
        primary_phone="2",
        whatsapp_phone_indices=["0", "2"],
        pipeline_id=str(pipeline.id),
        stage_id=str(stage.id),
        region_zone_id=str(region.id),
        organization_id=str(organization.id),
    )

    lead = db_session.get(Lead, outcome.lead_id)
    party = db_session.get(Party, outcome.party_id)
    assert lead is not None and party is not None
    assert lead.party_id == party.id
    assert lead.pipeline_id == pipeline.id
    assert lead.stage_id == stage.id
    assert lead.region == region.name
    assert lead.address is None
    assert lead.estimated_value == Decimal("250000.00")
    assert lead.currency == "NGN"
    assert party.display_name == "Amina Bello Yusuf"
    assert party.metadata_["first_name"] == "Amina"
    assert party.metadata_["last_name"] == "Bello Yusuf"
    assert party.metadata_["country_code"] == "NG"
    assert party.metadata_["organization_id"] == str(organization.id)

    points = (
        db_session.query(PartyContactPoint)
        .filter(PartyContactPoint.party_id == party.id)
        .order_by(PartyContactPoint.channel_type, PartyContactPoint.created_at)
        .all()
    )
    assert len([p for p in points if p.channel_type == "email"]) == 2
    assert len([p for p in points if p.channel_type == "phone"]) == 2
    assert len([p for p in points if p.channel_type == "whatsapp"]) == 2
    assert (
        next(
            p for p in points if p.channel_type == "email" and p.is_primary
        ).normalized_value
        == "second@example.com"
    )
    assert (
        next(
            p for p in points if p.channel_type == "phone" and p.is_primary
        ).normalized_value
        == "+2348035557777"
    )
    relationship = (
        db_session.query(PartyRelationship)
        .filter_by(
            subject_party_id=party.id,
            object_party_id=organization_party.id,
        )
        .one()
    )
    assert relationship.relationship_type == "contact_for"


def test_edit_lead_updates_complete_record_and_preserves_party_identity(db_session):
    actor = _staff_owner(db_session)
    pipeline, stage = _pipeline(db_session)
    region = _region(db_session)
    organization, organization_party = _organization(db_session)
    created = _author(
        db_session,
        actor,
        emails=["keep@example.com", "remove@example.com"],
        primary_email="0",
        phones=["08031234567"],
        primary_phone="0",
        nin="12345678901",
    )
    lead = db_session.get(Lead, created.lead_id)
    party = db_session.get(Party, created.party_id)
    assert lead is not None and party is not None
    original_party_id = lead.party_id
    original_nin = party.metadata_["nin_encrypted"]
    kept = (
        db_session.query(PartyContactPoint)
        .filter_by(
            party_id=party.id,
            channel_type=PartyContactPointType.email.value,
            normalized_value="keep@example.com",
        )
        .one()
    )
    kept.verification_status = "verified"
    db_session.commit()

    context = web_sales.build_lead_edit_context(db_session, lead_id=str(lead.id))
    assert context["form_title"] == "Edit Lead"
    assert context["lead_form"].emails == (
        "keep@example.com",
        "remove@example.com",
    )
    assert context["lead_form"].display_name == "Amina Bello Yusuf"
    actor_id = str(actor.id)
    lead_id = str(lead.id)
    organization_id = str(organization.id)
    pipeline_id = str(pipeline.id)
    stage_id = str(stage.id)
    region_id = str(region.id)
    db_session.commit()

    outcome = web_sales.update_lead_from_form(
        db_session,
        actor_system_user_id=actor_id,
        submission_id=str(uuid4()),
        lead_id=lead_id,
        title="Enterprise fibre renewal",
        display_name="Amina Yusuf",
        status="contacted",
        owner_agent_id=actor_id,
        emails=["keep@example.com", "new@example.com"],
        primary_email="1",
        phones=["08035557777"],
        primary_phone="0",
        whatsapp_phone_indices=["0"],
        address_line1="12 Marina Road",
        address_line2="Floor 3",
        date_of_birth="1990-04-03",
        gender="female",
        nin="",
        city="Lagos",
        postal_code="100001",
        country_code="ng",
        organization_id=organization_id,
        managed_by_reseller=False,
        reseller_id=None,
        pipeline_id=pipeline_id,
        stage_id=stage_id,
        lead_source="Website",
        region_zone_id=region_id,
        estimated_value="350000.00",
        currency="ngn",
        address="Victoria Island service location",
        probability="75",
        expected_close_date="2026-12-31",
        lost_reason=None,
        notes="Updated after qualification call.",
        is_active=True,
    )

    db_session.expire_all()
    updated_lead = db_session.get(Lead, created.lead_id)
    updated_party = db_session.get(Party, created.party_id)
    assert outcome.replayed is False
    assert updated_lead is not None and updated_party is not None
    assert updated_lead.party_id == original_party_id
    assert updated_lead.title == "Enterprise fibre renewal"
    assert updated_lead.pipeline_id == pipeline.id
    assert updated_lead.stage_id == stage.id
    assert updated_lead.region == region.name
    assert updated_lead.address == "Victoria Island service location"
    assert updated_party.display_name == "Amina Yusuf"
    assert updated_party.metadata_["organization_id"] == str(organization.id)
    assert updated_party.metadata_["nin_encrypted"] == original_nin

    emails = (
        db_session.query(PartyContactPoint)
        .filter_by(
            party_id=updated_party.id,
            channel_type=PartyContactPointType.email.value,
        )
        .all()
    )
    by_email = {point.normalized_value: point for point in emails}
    assert by_email["keep@example.com"].verification_status == "verified"
    assert by_email["keep@example.com"].is_active is True
    assert by_email["remove@example.com"].is_active is False
    assert by_email["new@example.com"].is_primary is True
    relationship = (
        db_session.query(PartyRelationship)
        .filter_by(
            subject_party_id=updated_party.id,
            object_party_id=organization_party.id,
            relationship_type="contact_for",
        )
        .one()
    )
    assert relationship.status == "active"


def test_edit_lead_exact_submission_replays_without_duplicate_contacts(db_session):
    actor = _staff_owner(db_session)
    created = _author(db_session, actor)
    edit_id = str(uuid4())
    fields = {
        "actor_system_user_id": str(actor.id),
        "submission_id": edit_id,
        "lead_id": str(created.lead_id),
        "title": "Amina Bello Yusuf",
        "display_name": "Amina Bello Yusuf",
        "status": "new",
        "owner_agent_id": str(actor.id),
        "emails": ["updated@example.com"],
        "primary_email": "0",
        "phones": ["08031234567"],
        "primary_phone": "0",
        "whatsapp_phone_indices": ["0"],
        "address_line1": None,
        "address_line2": None,
        "date_of_birth": None,
        "gender": "unknown",
        "nin": None,
        "city": None,
        "postal_code": None,
        "country_code": None,
        "organization_id": None,
        "managed_by_reseller": False,
        "reseller_id": None,
        "pipeline_id": None,
        "stage_id": None,
        "lead_source": "Website",
        "region_zone_id": None,
        "estimated_value": None,
        "currency": "NGN",
        "address": None,
        "probability": "65",
        "expected_close_date": "2026-12-31",
        "lost_reason": None,
        "notes": None,
        "is_active": True,
    }
    db_session.commit()
    first = web_sales.update_lead_from_form(db_session, **fields)
    second = web_sales.update_lead_from_form(db_session, **fields)
    assert first.replayed is False
    assert second.replayed is True
    assert (
        db_session.query(PartyContactPoint)
        .filter_by(
            party_id=created.party_id,
            channel_type=PartyContactPointType.email.value,
            normalized_value="updated@example.com",
        )
        .count()
        == 1
    )


def test_edit_canonicalizes_exact_legacy_subscriber_party_binding(db_session):
    actor = _staff_owner(db_session)
    person = Party(
        party_type=PartyType.person.value,
        display_name="Legacy Person",
        status="active",
    )
    db_session.add(person)
    db_session.flush()
    subscriber = Subscriber(
        first_name="Legacy",
        last_name="Person",
        email="legacy@example.com",
        party_id=person.id,
        party_bound_at=datetime.now(UTC),
        party_binding_source="pytest",
        party_binding_reason="Reviewed legacy Subscriber Party",
    )
    db_session.add(subscriber)
    db_session.flush()
    lead = Lead(
        subscriber_id=subscriber.id,
        title="Legacy Lead",
        status="new",
        lead_source="Website",
        is_active=True,
    )
    db_session.add(lead)
    db_session.commit()
    actor_id = actor.id
    person_id = person.id
    lead_id = lead.id
    db_session.commit()

    outcome = lead_authoring.edit_lead(
        db_session,
        lead_authoring.EditLeadCommand(
            context=CommandContext.system(
                actor=str(actor_id),
                scope="sales:lead-maintenance",
                reason="Test exact legacy Lead Party binding",
                idempotency_key=f"test-lead-edit:{lead_id}:{uuid4()}",
            ),
            edit_id=uuid4(),
            lead_id=lead_id,
            actor_system_user_id=actor_id,
            title="Legacy Lead Updated",
            status=LeadStatus.new,
            owner_system_user_id=actor_id,
            pipeline_id=None,
            stage_id=None,
            lead_source="Website",
            region_zone_id=None,
            estimated_value=None,
            currency="NGN",
            address=None,
            probability=25,
            expected_close_date=None,
            lost_reason=None,
            notes=None,
            is_active=True,
            person=lead_authoring.LeadPersonDraft(
                display_name="Legacy Person",
                emails=lead_authoring.LeadContactDraft(
                    values=("legacy.updated@example.com",),
                    primary_index=0,
                ),
                phones=lead_authoring.LeadContactDraft(
                    values=("08031234567",),
                    primary_index=0,
                ),
                whatsapp_phone_indices=(),
                address_line1=None,
                address_line2=None,
                date_of_birth=None,
                gender="unknown",
                nin=None,
                city=None,
                postal_code=None,
                country_code="NG",
                organization_id=None,
                reseller_id=None,
            ),
        ),
    )

    db_session.expire_all()
    updated = db_session.get(Lead, lead_id)
    assert updated is not None
    assert outcome.party_id == person_id
    assert updated.party_id == person_id
    assert updated.party_bound_at is not None
    assert updated.party_binding_source == "admin_sales_lead_edit"


def test_exact_submission_replays_without_duplicates(db_session):
    actor = _staff_owner(db_session)
    submission = str(uuid4())
    first = _author(db_session, actor, submission_id=submission)
    second = _author(db_session, actor, submission_id=submission)
    assert second.replayed is True
    assert second.lead_id == first.lead_id
    assert second.party_id == first.party_id
    assert db_session.query(Lead).filter(Lead.id == first.lead_id).count() == 1
    assert db_session.query(Party).filter(Party.id == first.party_id).count() == 1


def test_inbox_authoring_creates_party_lead_and_origin_link_atomically(db_session):
    actor = _staff_owner(db_session)
    conversation = InboxConversation(
        channel_type="email",
        contact_address=f"new-{uuid4()}@example.com",
        subject="New service enquiry",
        is_active=True,
    )
    db_session.add(conversation)
    db_session.commit()

    outcome = _author(
        db_session,
        actor,
        inbox_conversation_id=str(conversation.id),
        emails=[conversation.contact_address],
    )

    link = conversation_lead_relationships.active_link(db_session, conversation.id)
    assert link is not None
    assert link.lead_id == outcome.lead_id
    assert link.party_id == outcome.party_id
    lead = db_session.get(Lead, outcome.lead_id)
    assert lead is not None
    assert lead.metadata_["origin_conversation_id"] == str(conversation.id)


def test_pipeline_stage_mismatch_rolls_back_person_and_lead(db_session):
    actor = _staff_owner(db_session)
    first_pipeline, _ = _pipeline(db_session)
    _second_pipeline, second_stage = _pipeline(db_session)
    before_parties = db_session.query(Party).count()
    before_leads = db_session.query(Lead).count()
    with pytest.raises(DomainError) as exc:
        _author(
            db_session,
            actor,
            pipeline_id=str(first_pipeline.id),
            stage_id=str(second_stage.id),
        )
    assert exc.value.code == "sales.lead_authoring.stage_pipeline_mismatch"
    assert db_session.query(Party).count() == before_parties
    assert db_session.query(Lead).count() == before_leads


def test_inactive_region_and_owner_are_rejected(db_session):
    actor = _staff_owner(db_session)
    inactive_region = _region(db_session, active=False)
    with pytest.raises(DomainError) as region_exc:
        _author(db_session, actor, region_zone_id=str(inactive_region.id))
    assert region_exc.value.details["field"] == "region_zone_id"

    actor.is_active = False
    db_session.commit()
    with pytest.raises(DomainError) as actor_exc:
        _author(db_session, actor)
    assert actor_exc.value.code == "sales.lead_authoring.actor_not_eligible"


def test_primary_email_can_be_shared_by_multiple_people(db_session):
    actor = _staff_owner(db_session)
    existing = Party(party_type=PartyType.person.value, display_name="Existing")
    db_session.add(existing)
    db_session.flush()
    db_session.add(
        PartyContactPoint(
            party_id=existing.id,
            channel_type=PartyContactPointType.email.value,
            normalized_value="taken@example.com",
            display_value="taken@example.com",
            is_primary=True,
            is_active=True,
        )
    )
    db_session.commit()
    outcome = _author(db_session, actor, emails=["taken@example.com"])
    created = db_session.query(PartyContactPoint).filter_by(
        party_id=outcome.party_id,
        normalized_value="taken@example.com",
        is_primary=True,
    )
    assert created.one().channel_type == PartyContactPointType.email.value


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"emails": ["not-an-email"]}, "emails"),
        ({"phones": ["123"]}, "phones"),
        ({"nin": "1234"}, "nin"),
        ({"country_code": "NGA"}, "country_code"),
        ({"date_of_birth": "2999-01-01"}, "date_of_birth"),
        ({"estimated_value": "-1"}, "estimated_value"),
        ({"currency": "12X"}, "currency"),
    ],
)
def test_private_and_scalar_validation_is_server_authoritative(
    db_session, overrides, field
):
    actor = _staff_owner(db_session)
    with pytest.raises(DomainError) as exc:
        _author(db_session, actor, **overrides)
    assert exc.value.details["field"] == field


def test_reseller_organization_keeps_contacts_for_account_conversion(db_session):
    actor = _staff_owner(db_session)
    organization, _party = _organization(db_session, reseller=True)
    outcome = _author(
        db_session,
        actor,
        organization_id=str(organization.id),
        emails=["direct@example.com"],
        phones=["08031234567"],
        whatsapp_phone_indices=["0"],
    )
    assert outcome.reseller_routed is True
    assert (
        db_session.query(PartyContactPoint)
        .filter(PartyContactPoint.party_id == outcome.party_id)
        .count()
        == 3
    )
    party = db_session.get(Party, outcome.party_id)
    assert party.metadata_["communication_routed_through_reseller"] is True


def test_selected_reseller_is_persisted_on_lead(db_session):
    actor = _staff_owner(db_session)
    reseller = Reseller(
        name=f"Partner {uuid4().hex[:8]}",
        contact_email="partner@example.com",
        is_active=True,
        is_house=False,
    )
    db_session.add(reseller)
    db_session.commit()

    outcome = _author(
        db_session,
        actor,
        managed_by_reseller=True,
        reseller_id=str(reseller.id),
        emails=["shared@example.com"],
    )

    lead = db_session.get(Lead, outcome.lead_id)
    assert lead.reseller_id == reseller.id
    assert outcome.reseller_routed is True


def test_new_context_defaults_owner_and_loads_configured_regions(db_session):
    actor = _staff_owner(db_session)
    active = _region(db_session)
    _inactive = _region(db_session, active=False)
    context = web_sales.build_lead_new_context(
        db_session, actor_system_user_id=str(actor.id)
    )
    assert context["lead_form"].owner_agent_id == str(actor.id)
    assert context["lead_form"].status == "new"
    assert context["lead_form"].emails == ("",)
    assert context["lead_form"].phones == ("",)
    assert [item.id for item in context["regions"]] == [active.id]
