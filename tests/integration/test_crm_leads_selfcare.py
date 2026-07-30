"""PostgreSQL journey for the Selfcare CRM-compatible Leads interface."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from app.models.subscriber import Subscriber
from app.schemas.sales import PipelineCreate, PipelineStageCreate
from app.services import sales, web_sales
from app.services.subscriber import _default_reseller_id


def test_selfcare_lead_create_update_summary_and_delete(db_session):
    suffix = uuid4().hex[:10]
    subscriber = Subscriber(
        first_name="CRM",
        last_name="Lead",
        email=f"crm-lead-{suffix}@example.com",
        phone=f"080{suffix[:8]}",
        reseller_id=_default_reseller_id(db_session),
    )
    db_session.add(subscriber)
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

    lead_id, existing = web_sales.create_lead_from_form(
        db_session,
        title=f"Enterprise opportunity {suffix}",
        status="qualified",
        subscriber_id=str(subscriber.id),
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
    web_sales.set_lead_status(db_session, lead_id=lead_id, status="won")
    detail = web_sales.build_lead_detail_context(db_session, lead_id=lead_id)
    summary = sales.leads.summary(db_session)
    assert detail["lead"].status == "won"
    assert detail["contact"].email == subscriber.email
    assert summary.won_leads >= 1
    assert summary.open_leads == open_summary.open_leads - 1
    assert summary.pipeline_value == (open_summary.pipeline_value - Decimal("1000000"))

    web_sales.deactivate_lead(db_session, lead_id=lead_id)
    assert sales.leads.get(db_session, lead_id).is_active is False
