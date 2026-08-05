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
from app.models.sales import Lead, Pipeline, PipelineStage
from app.models.service_team import ServiceTeam, ServiceTeamMember
from app.models.subscriber import Reseller
from app.models.system_user import SystemUser
from app.services import web_sales
from app.services.domain_errors import DomainError
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
    assert 'name="address"' not in template
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
