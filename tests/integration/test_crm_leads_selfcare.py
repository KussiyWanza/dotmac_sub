"""PostgreSQL journey for the Selfcare CRM-compatible Leads interface."""

from __future__ import annotations

from uuid import uuid4

from app.models.party import Party, PartyContactPoint
from app.models.subscriber import Subscriber
from app.schemas.sales import PipelineCreate, PipelineStageCreate
from app.services import sales, web_sales


def test_selfcare_lead_create_update_summary_and_delete(db_session):
    suffix = uuid4().hex[:10]
    email = f"crm-lead-{suffix}@example.com"
    party = Party(
        display_name="CRM Lead",
        party_type="person",
        status="active",
    )
    db_session.add(party)
    db_session.flush()
    db_session.add(
        PartyContactPoint(
            party_id=party.id,
            channel_type="email",
            normalized_value=email,
            display_value=email,
            is_primary=True,
            is_active=True,
        )
    )
    db_session.flush()
    pipeline = sales.pipelines.create(
        db_session, PipelineCreate(name=f"CRM Leads {suffix}")
    )
    stage = sales.pipeline_stages.create(
        db_session,
        PipelineStageCreate(
            pipeline_id=pipeline.id,
            name="Qualified",
            default_probability=60,
        ),
    )
    subscriber_count = db_session.query(Subscriber).count()

    lead_id, existing = web_sales.create_lead_from_form(
        db_session,
        title=f"Enterprise opportunity {suffix}",
        status="qualified",
        party_id=str(party.id),
        owner_agent_id=None,
        pipeline_id=str(pipeline.id),
        stage_id=str(stage.id),
        lead_source="Website",
        region="Lagos",
        estimated_value="1000000",
        currency="NGN",
        address=None,
        probability="60",
        expected_close_date="2026-09-30",
        lost_reason=None,
        notes="Integration journey",
        is_active=True,
    )
    assert existing is False

    open_summary = sales.leads.summary(db_session)
    web_sales.set_lead_status(db_session, lead_id=lead_id, status="contacted")
    detail = web_sales.build_lead_detail_context(db_session, lead_id=lead_id)
    summary = sales.leads.summary(db_session)
    assert detail["lead"].status == "contacted"
    assert detail["contact"].email == email
    assert summary.open_leads == open_summary.open_leads
    assert summary.pipeline_value == open_summary.pipeline_value
    assert db_session.query(Subscriber).count() == subscriber_count

    web_sales.deactivate_lead(db_session, lead_id=lead_id)
    assert sales.leads.get(db_session, lead_id).is_active is False
