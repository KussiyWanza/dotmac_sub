"""Leads / pipeline / quotes service tests (Phase 3 sales-vertical port)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException

from app.models.party import Party
from app.models.project import ProjectTemplate
from app.models.sales import (
    LeadStatus,
    QuoteStatus,
    SalesOrder,
    SalesOrderStatus,
)
from app.models.subscriber import PartyStatus, Subscriber
from app.schemas.sales import (
    LeadCreate,
    LeadUpdate,
    PipelineCreate,
    PipelineStageCreate,
    PipelineUpdate,
    QuoteCreate,
    QuoteLineItemCreate,
    QuoteLineItemUpdate,
    QuoteUpdate,
)
from app.services import sales as sales_service


def _make_subscriber(db, **overrides) -> Subscriber:
    data = {
        "first_name": "Ada",
        "last_name": "Obi",
        "email": f"ada-{uuid.uuid4().hex}@example.com",
    }
    data.update(overrides)
    if data.get("party_id") is None:
        party = Party(
            display_name=(
                f"{data.get('first_name') or ''} {data.get('last_name') or ''}".strip()
            ),
            party_type="person",
            status="active",
        )
        db.add(party)
        db.flush()
        data["party_id"] = party.id
        data["party_bound_at"] = datetime.now(UTC)
        data["party_binding_source"] = "pytest"
        data["party_binding_reason"] = "Sales service fixture Party binding"
    subscriber = Subscriber(**data)
    db.add(subscriber)
    db.commit()
    db.refresh(subscriber)
    return subscriber


def _make_pipeline(db, name="Sales"):
    return sales_service.pipelines.create(db, PipelineCreate(name=name))


def _make_stage(db, pipeline, name="New", order_index=0, default_probability=25):
    return sales_service.pipeline_stages.create(
        db,
        PipelineStageCreate(
            pipeline_id=pipeline.id,
            name=name,
            order_index=order_index,
            default_probability=default_probability,
        ),
    )


def _make_quote(db, subscriber: Subscriber, **overrides):
    lead = sales_service.leads.create(db, LeadCreate(subscriber_id=subscriber.id))
    overrides.setdefault("project_type", "fiber_optics_installation")
    return sales_service.quotes.create(
        db,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            **overrides,
        ),
    )


# ---------------------------------------------------------------------------
# Pipelines / stages
# ---------------------------------------------------------------------------


def test_pipeline_crud_and_soft_delete(db_session):
    pipeline = _make_pipeline(db_session, name="Fiber Sales")
    assert pipeline.id is not None

    fetched = sales_service.pipelines.get(db_session, str(pipeline.id))
    assert fetched.name == "Fiber Sales"

    sales_service.pipelines.update(
        db_session, str(pipeline.id), PipelineUpdate(name="Fiber & AirFiber")
    )
    assert sales_service.pipelines.get(db_session, str(pipeline.id)).name == (
        "Fiber & AirFiber"
    )

    sales_service.pipelines.delete(db_session, str(pipeline.id))
    active = sales_service.pipelines.list(db_session, None, "created_at", "desc", 50, 0)
    assert all(p.id != pipeline.id for p in active)


def test_pipeline_stage_create_and_ordering(db_session):
    pipeline = _make_pipeline(db_session)
    _make_stage(db_session, pipeline, name="Qualify", order_index=1)
    _make_stage(db_session, pipeline, name="New", order_index=0)

    stages = sales_service.pipeline_stages.list(
        db_session, str(pipeline.id), None, "order_index", "asc", 50, 0
    )
    assert [s.name for s in stages] == ["New", "Qualify"]


def test_pipeline_stage_requires_pipeline(db_session):
    with pytest.raises(HTTPException) as exc:
        sales_service.pipeline_stages.create(
            db_session,
            PipelineStageCreate(pipeline_id=uuid.uuid4(), name="Orphan"),
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def test_lead_create_title_falls_back_to_subscriber_name(db_session):
    subscriber = _make_subscriber(db_session)
    lead = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    assert lead.title == "Ada Obi"
    assert lead.status == LeadStatus.new.value  # stored as plain string


def test_lead_create_missing_subscriber_404(db_session):
    with pytest.raises(HTTPException) as exc:
        sales_service.leads.create(db_session, LeadCreate(subscriber_id=uuid.uuid4()))
    assert exc.value.status_code == 404


def test_lead_source_portal_is_valid(db_session):
    """The crm#233 fix: 'portal' normalizes to the new Portal member instead
    of 400ing (the self-serve quote request was broken end-to-end)."""
    subscriber = _make_subscriber(db_session)
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(subscriber_id=subscriber.id, lead_source="portal"),
    )
    assert lead.lead_source == "Portal"
    assert "Portal" in sales_service.LEAD_SOURCE_OPTIONS


def test_lead_source_alias_normalization(db_session):
    subscriber = _make_subscriber(db_session)
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(subscriber_id=subscriber.id, lead_source="fb ads"),
    )
    assert lead.lead_source == "Facebook Ads"


def test_lead_source_invalid_400(db_session):
    subscriber = _make_subscriber(db_session)
    with pytest.raises(HTTPException) as exc:
        sales_service.leads.create(
            db_session,
            LeadCreate(subscriber_id=subscriber.id, lead_source="carrier pigeon"),
        )
    assert exc.value.status_code == 400


def test_lead_source_inferred_from_attribution_metadata(db_session):
    subscriber = _make_subscriber(db_session)
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            subscriber_id=subscriber.id,
            metadata_={"attribution": {"utm_source": "google", "gclid": "x"}},
        ),
    )
    assert lead.lead_source == "Google"


def test_lead_dedup_returns_existing_open_lead(db_session):
    subscriber = _make_subscriber(db_session)
    first = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    second = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    assert second.id == first.id
    assert second.dedup_returned_existing is True
    assert (second.metadata_ or {}).get("dedup_hits") == 1


def test_lead_dedup_scoped_per_pipeline(db_session):
    subscriber = _make_subscriber(db_session)
    pipeline = _make_pipeline(db_session)
    no_pipeline = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    in_pipeline = sales_service.leads.create(
        db_session,
        LeadCreate(subscriber_id=subscriber.id, pipeline_id=pipeline.id),
    )
    assert in_pipeline.id != no_pipeline.id


def test_lead_dedup_disabled_creates_duplicates(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.sales.service._lead_dedup_enabled", lambda db: False
    )
    subscriber = _make_subscriber(db_session)
    first = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    second = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    assert second.id != first.id


def test_lead_create_does_not_upgrade_legacy_party_status(db_session):
    subscriber = _make_subscriber(db_session, party_status=PartyStatus.lead.value)
    sales_service.leads.create(db_session, LeadCreate(subscriber_id=subscriber.id))
    assert subscriber.party_status == PartyStatus.lead.value


def test_lead_cannot_be_marked_won_outside_quote_acceptance(db_session):
    subscriber = _make_subscriber(db_session, party_status=PartyStatus.contact.value)
    lead = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    assert lead.closed_at is None

    with pytest.raises(HTTPException) as exc:
        sales_service.leads.update(
            db_session, str(lead.id), LeadUpdate(status=LeadStatus.won)
        )
    assert exc.value.status_code == 409
    db_session.refresh(lead)
    assert lead.status == LeadStatus.new.value
    assert lead.closed_at is None
    assert subscriber.party_status == PartyStatus.contact.value


def test_lead_list_search_by_subscriber_fields(db_session):
    subscriber = _make_subscriber(db_session, first_name="Ngozi", last_name="Eze")
    sales_service.leads.create(db_session, LeadCreate(subscriber_id=subscriber.id))
    rows = sales_service.leads.list(
        db_session,
        None,
        None,
        None,
        None,
        None,
        "created_at",
        "desc",
        50,
        0,
        search="ngozi",
    )
    assert len(rows) == 1
    assert rows[0].subscriber_id == subscriber.id


def test_kanban_view_and_update_stage(db_session):
    subscriber = _make_subscriber(db_session)
    pipeline = _make_pipeline(db_session)
    stage_new = _make_stage(db_session, pipeline, name="New", order_index=0)
    stage_hot = _make_stage(
        db_session, pipeline, name="Hot", order_index=1, default_probability=80
    )
    lead = sales_service.leads.create(
        db_session,
        LeadCreate(
            subscriber_id=subscriber.id,
            pipeline_id=pipeline.id,
            stage_id=stage_new.id,
        ),
    )

    board = sales_service.leads.kanban_view(db_session, str(pipeline.id))
    assert [c["title"] for c in board["columns"]] == ["New", "Hot"]
    assert len(board["records"]) == 1
    record = board["records"][0]
    assert record["id"] == str(lead.id)
    assert record["contact_name"] == "Ada Obi"
    assert record["status"] == LeadStatus.new.value

    moved = sales_service.leads.update_stage(
        db_session, str(lead.id), str(stage_hot.id)
    )
    assert moved["stage_id"] == str(stage_hot.id)
    assert moved["probability"] == 80  # defaulted from the stage


def test_bulk_assign_pipeline_unassigned_scope(db_session):
    subscriber_a = _make_subscriber(db_session)
    subscriber_b = _make_subscriber(db_session)
    pipeline = _make_pipeline(db_session)
    other_pipeline = _make_pipeline(db_session, name="Other")
    stage = _make_stage(db_session, pipeline)

    unassigned = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber_a.id)
    )
    assigned = sales_service.leads.create(
        db_session,
        LeadCreate(subscriber_id=subscriber_b.id, pipeline_id=other_pipeline.id),
    )

    count = sales_service.leads.bulk_assign_pipeline(
        db_session, str(pipeline.id), str(stage.id), scope="unassigned"
    )
    assert count == 1
    db_session.refresh(unassigned)
    db_session.refresh(assigned)
    assert unassigned.pipeline_id == pipeline.id
    assert unassigned.stage_id == stage.id
    assert assigned.pipeline_id == other_pipeline.id


# ---------------------------------------------------------------------------
# Quotes + line items
# ---------------------------------------------------------------------------


def test_quote_create_defaults_and_quote_name_metadata(db_session):
    subscriber = _make_subscriber(db_session)
    quote = _make_quote(db_session, subscriber)
    assert quote.status == QuoteStatus.draft.value
    assert (quote.metadata_ or {}).get("quote_name") == "Ada Obi"


def test_quote_owner_from_metadata(db_session):
    subscriber = _make_subscriber(db_session)
    staff_uuid = uuid.uuid4()
    quote = _make_quote(
        db_session,
        subscriber,
        metadata_={"owner_person_id": str(staff_uuid)},
    )
    assert quote.owner_person_id == staff_uuid


def test_quote_sent_stamps_sent_at(db_session):
    subscriber = _make_subscriber(db_session)
    quote = _make_quote(db_session, subscriber)
    # A quote must have at least one line before it can be sent -- an empty
    # quote is worth nothing and must not reach a customer.
    sales_service.quote_line_items.create(
        db_session,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Installation",
            quantity=Decimal("1"),
            unit_price=Decimal("25000.00"),
        ),
    )
    quote = sales_service.quotes.update(
        db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.sent)
    )
    assert quote.sent_at is not None


def test_quote_line_amount_and_totals_are_gross(db_session):
    subscriber = _make_subscriber(db_session)
    quote = _make_quote(db_session, subscriber, tax_rate=Decimal("7.5"))
    item = sales_service.quote_line_items.create(
        db_session,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Installation",
            quantity=Decimal("2"),
            unit_price=Decimal("100.00"),
        ),
    )
    assert item.discount_percent == Decimal("0.00")
    assert item.amount == Decimal("200.00")

    db_session.refresh(quote)
    assert quote.subtotal == Decimal("200.00")
    assert quote.tax_total == Decimal("15.00")
    assert quote.total == Decimal("215.00")

    sales_service.quote_line_items.update(
        db_session,
        str(item.id),
        QuoteLineItemUpdate(quantity=Decimal("3")),
    )
    db_session.refresh(quote)
    assert quote.subtotal == Decimal("300.00")
    assert quote.total == Decimal("322.50")


def test_quote_count_by_status(db_session):
    subscriber = _make_subscriber(db_session)
    _make_quote(db_session, subscriber)
    counts = sales_service.quotes.count_by_status(db_session)
    assert counts[QuoteStatus.draft.value] == 1
    assert counts["total"] == 1


def test_quote_accept_creates_sales_order_and_wins_lead(db_session):
    subscriber = _make_subscriber(db_session, party_status=PartyStatus.contact.value)
    lead = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id, lead_source="portal")
    )
    quote = sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
        ),
    )
    sales_service.quote_line_items.create(
        db_session,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Installation cost",
            quantity=Decimal("1"),
            unit_price=Decimal("500.00"),
            metadata_={"note": "one-off"},
        ),
    )
    db_session.add(
        ProjectTemplate(
            name="Sales service installation",
            project_type="fiber_optics_installation",
            is_active=True,
        )
    )
    db_session.commit()

    quote = sales_service.quotes.update(
        db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.accepted)
    )

    sales_order = (
        db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).one()
    )
    assert sales_order.status == SalesOrderStatus.confirmed.value
    assert sales_order.subscriber_id == subscriber.id
    assert sales_order.order_number.startswith("SO-")
    assert sales_order.total == Decimal("500.00")
    assert sales_order.source == "Portal"  # carried from the lead
    lines = sales_order.lines
    assert len(lines) == 1
    assert lines[0].description == "Installation cost"

    db_session.refresh(lead)
    assert lead.status == LeadStatus.won.value
    assert lead.closed_at is not None
    assert subscriber.party_status == PartyStatus.contact.value

    # Idempotent: re-accepting must not mint a second sales order.
    sales_service.quotes.update(
        db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.accepted)
    )
    count = db_session.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).count()
    assert count == 1


def test_quote_rejection_does_not_close_the_lead(db_session):
    subscriber = _make_subscriber(db_session)
    lead = sales_service.leads.create(
        db_session, LeadCreate(subscriber_id=subscriber.id)
    )
    quote = sales_service.quotes.create(
        db_session,
        QuoteCreate(
            subscriber_id=subscriber.id,
            lead_id=lead.id,
            project_type="fiber_optics_installation",
        ),
    )
    sales_service.quotes.update(
        db_session, str(quote.id), QuoteUpdate(status=QuoteStatus.rejected)
    )
    db_session.refresh(lead)
    assert lead.status == LeadStatus.new.value
