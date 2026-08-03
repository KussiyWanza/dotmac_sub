"""Atomic Lead -> accepted Quote -> implementation workflow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.models.audit import AuditEvent
from app.models.event_store import EventStore
from app.models.party import PartyRole, PartyRoleStatus, PartyRoleType
from app.models.project import (
    Project,
    ProjectTask,
    ProjectTemplate,
    ProjectTemplateTask,
    ProjectType,
)
from app.models.sales import (
    Lead,
    LeadStatus,
    Quote,
    QuoteStatus,
    SalesOrder,
    SalesOrderLine,
)
from app.models.subscriber import Subscriber
from app.models.work_order import WorkOrder
from app.schemas.dispatch import WorkOrderHeaderCreate
from app.schemas.project import ProjectTaskUpdate
from app.schemas.sales import (
    LeadCapturePartyCreate,
    LeadCaptureRequest,
    LeadContactObservation,
    LeadOriginCaptureCreate,
    QuoteCreate,
    QuoteLineItemCreate,
)
from app.services import projects
from app.services import sales as sales_service
from app.services.db_session_adapter import db_session_adapter
from app.services.owner_commands import CommandContext
from app.services.sales import capture, quote_acceptance
from app.services.work_order_commands import work_order_commands


def _lead(db, marker: str) -> Lead:
    result = capture.capture_lead(
        db,
        LeadCaptureRequest(
            party=LeadCapturePartyCreate(
                display_name="Atomic Customer",
                contacts=[
                    LeadContactObservation(
                        channel_type="email",
                        value=f"atomic-{marker}@example.com",
                        is_primary=True,
                    ),
                    LeadContactObservation(
                        channel_type="phone",
                        value="+2348012345678",
                        is_primary=True,
                    ),
                ],
            ),
            title="Fiber installation enquiry",
            lead_source="Website",
            origin=LeadOriginCaptureCreate(
                capture_method="landing_page",
                source_platform="website",
                source_interaction_id=f"quote-acceptance-{marker}",
                landing_path="/fiber",
                capture_source="pytest",
                capture_reason="Atomic quote acceptance test",
            ),
            address="1 Test Avenue",
            region="Lagos",
        ),
        actor_id="pytest",
    )
    return result.lead


def _template(
    db, project_type: ProjectType = ProjectType.fiber_optics_installation
) -> ProjectTemplate:
    template = ProjectTemplate(
        name=f"Accepted quote {uuid4().hex[:8]}",
        project_type=project_type.value,
        is_active=True,
    )
    db.add(template)
    db.flush()
    db.add_all(
        [
            ProjectTemplateTask(
                template_id=template.id,
                title="Survey site",
                sort_order=0,
            ),
            ProjectTemplateTask(
                template_id=template.id,
                title="Install customer fiber",
                sort_order=1,
                auto_create_work_order=True,
                work_order_requires_as_built_evidence=False,
            ),
        ]
    )
    db.commit()
    return template


def _quote(
    db, lead: Lead, project_type: ProjectType = ProjectType.fiber_optics_installation
) -> Quote:
    quote = sales_service.quotes.create(
        db,
        QuoteCreate(
            lead_id=lead.id,
            project_type=project_type,
            currency="NGN",
        ),
    )
    sales_service.quote_line_items.create(
        db,
        QuoteLineItemCreate(
            quote_id=quote.id,
            description="Fiber installation",
            quantity="1",
            unit_price="150000.00",
        ),
    )
    return quote


def _command(
    quote_id: UUID,
    *,
    deposit: quote_acceptance.QuoteAcceptanceDeposit | None = None,
) -> quote_acceptance.AcceptQuoteCommand:
    return quote_acceptance.AcceptQuoteCommand(
        context=CommandContext.system(
            actor="pytest",
            scope="sales:quote-acceptance",
            reason="Customer accepted the commercial Quote",
            idempotency_key=f"quote-acceptance:{quote_id}",
        ),
        quote_id=quote_id,
        deposit=deposit,
    )


def _accept(
    db,
    quote_id: UUID,
    *,
    deposit: quote_acceptance.QuoteAcceptanceDeposit | None = None,
) -> quote_acceptance.QuoteAcceptanceOutcome:
    db_session_adapter.release_read_transaction(db)
    return quote_acceptance.accept_quote(
        db,
        _command(quote_id, deposit=deposit),
    )


def test_lead_and_draft_quote_create_no_downstream_records(db_session):
    lead = _lead(db_session, "no-downstream")
    quote = _quote(db_session, lead)
    alternate_quote = _quote(db_session, lead)

    assert lead.subscriber_id is None
    assert quote.subscriber_id is None
    assert alternate_quote.subscriber_id is None
    assert alternate_quote.id != quote.id
    assert quote.lead_id == lead.id
    assert db_session.query(Quote).filter_by(lead_id=lead.id).count() == 2
    assert db_session.query(Subscriber).count() == 0
    assert db_session.query(SalesOrder).count() == 0
    assert db_session.query(Project).count() == 0
    assert db_session.query(ProjectTask).count() == 0
    assert db_session.query(WorkOrder).count() == 0


def test_quote_acceptance_converts_every_record_in_one_workflow(db_session):
    template = _template(db_session)
    lead = _lead(db_session, "success")
    quote = _quote(db_session, lead)
    outcome = _accept(db_session, quote.id)

    accepted = db_session.get(Quote, quote.id)
    won_lead = db_session.get(Lead, lead.id)
    subscriber = db_session.get(Subscriber, outcome.subscriber_id)
    order = db_session.get(SalesOrder, outcome.sales_order_id)
    project = db_session.get(Project, outcome.project_id)
    tasks = db_session.query(ProjectTask).filter_by(project_id=project.id).all()
    work_orders = db_session.query(WorkOrder).filter_by(project_id=project.id).all()

    assert outcome.replayed is False
    assert accepted.status == QuoteStatus.accepted.value
    assert accepted.subscriber_id == subscriber.id
    assert won_lead.status == LeadStatus.won.value
    assert won_lead.subscriber_id == subscriber.id
    assert subscriber.party_id == won_lead.party_id
    assert order.quote_id == quote.id
    assert order.subscriber_id == subscriber.id
    assert (
        db_session.query(SalesOrderLine).filter_by(sales_order_id=order.id).count() == 1
    )
    assert project.sales_order_id == order.id
    assert accepted.project_type == ProjectType.fiber_optics_installation.value
    assert project.project_type == accepted.project_type
    assert project.project_template_id == template.id
    assert outcome.project_template_id == template.id
    assert len(tasks) == 2
    assert len(work_orders) == 1
    assert work_orders[0].project_task_id in {task.id for task in tasks}
    assert work_orders[0].requires_as_built_evidence is False
    roles = {
        row.role_type: row.status
        for row in db_session.query(PartyRole).filter_by(party_id=lead.party_id).all()
    }
    assert roles[PartyRoleType.customer.value] == PartyRoleStatus.active.value
    assert roles[PartyRoleType.subscriber.value] == PartyRoleStatus.pending.value
    assert db_session.query(AuditEvent).filter_by(action="quote.accepted").count() == 1
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_type == "quote.accepted")
        .count()
        == 1
    )


def test_expired_quote_cannot_be_accepted(db_session, monkeypatch):
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(quote_acceptance, "_utc_now", lambda: now)
    _template(db_session)
    lead = _lead(db_session, "expired")
    quote = _quote(db_session, lead)
    quote.expires_at = now - timedelta(seconds=1)
    db_session.commit()

    with pytest.raises(quote_acceptance.QuoteAcceptanceError) as exc_info:
        _accept(db_session, quote.id)

    assert exc_info.value.code == "sales.quote_acceptance.quote_expired"
    assert (
        exc_info.value.details["expires_at"] == (now - timedelta(seconds=1)).isoformat()
    )
    db_session.expire_all()
    assert db_session.get(Quote, quote.id).status == QuoteStatus.draft.value
    assert db_session.get(Lead, lead.id).status == LeadStatus.new.value
    assert db_session.query(Subscriber).count() == 0
    assert db_session.query(SalesOrder).count() == 0
    assert db_session.query(Project).count() == 0


@pytest.mark.parametrize("expiry_delta", [None, timedelta(minutes=5)])
def test_non_expired_quote_can_be_accepted(
    db_session,
    monkeypatch,
    expiry_delta,
):
    now = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(quote_acceptance, "_utc_now", lambda: now)
    _template(db_session)
    lead = _lead(db_session, f"non-expired-{expiry_delta is not None}")
    quote = _quote(db_session, lead)
    quote.expires_at = now + expiry_delta if expiry_delta is not None else None
    db_session.commit()

    outcome = _accept(db_session, quote.id)

    assert outcome.replayed is False
    assert db_session.get(Quote, quote.id).status == QuoteStatus.accepted.value


def test_expiry_does_not_break_an_accepted_quote_replay(db_session, monkeypatch):
    accepted_at = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(quote_acceptance, "_utc_now", lambda: accepted_at)
    _template(db_session)
    lead = _lead(db_session, "expiry-replay")
    quote = _quote(db_session, lead)
    quote.expires_at = accepted_at + timedelta(minutes=5)
    db_session.commit()
    first = _accept(db_session, quote.id)

    monkeypatch.setattr(
        quote_acceptance,
        "_utc_now",
        lambda: accepted_at + timedelta(minutes=10),
    )
    replay = _accept(db_session, quote.id)

    assert replay.replayed is True
    assert replay.sales_order_id == first.sales_order_id
    assert replay.project_id == first.project_id


def test_exact_deposit_evidence_replays_after_normalization(db_session):
    _template(db_session)
    lead = _lead(db_session, "deposit-replay")
    quote = _quote(db_session, lead)
    first = _accept(
        db_session,
        quote.id,
        deposit=quote_acceptance.QuoteAcceptanceDeposit(
            reference=" deposit-ref ",
            amount=Decimal("37500.0"),
            provider=" paystack ",
        ),
    )

    replay = _accept(
        db_session,
        quote.id,
        deposit=quote_acceptance.QuoteAcceptanceDeposit(
            reference="deposit-ref",
            amount=Decimal("37500.00"),
            provider="paystack",
        ),
    )

    assert replay.replayed is True
    assert replay.sales_order_id == first.sales_order_id
    assert replay.deposit == quote_acceptance.QuoteAcceptanceDepositEvidence(
        reference="deposit-ref",
        amount=Decimal("37500.00"),
        provider="paystack",
    )
    db_session.refresh(quote)
    assert quote.metadata_["deposit"] == {
        "reference": "deposit-ref",
        "amount": "37500.00",
        "provider": "paystack",
        "paid": True,
    }


@pytest.mark.parametrize(
    "conflicting_deposit",
    [
        quote_acceptance.QuoteAcceptanceDeposit(
            reference="different-ref",
            amount=Decimal("37500.00"),
            provider="paystack",
        ),
        quote_acceptance.QuoteAcceptanceDeposit(
            reference="deposit-ref",
            amount=Decimal("1.00"),
            provider="paystack",
        ),
        quote_acceptance.QuoteAcceptanceDeposit(
            reference="deposit-ref",
            amount=Decimal("37500.00"),
            provider="different-provider",
        ),
    ],
)
def test_changed_deposit_evidence_is_rejected_on_replay(
    db_session,
    conflicting_deposit,
):
    _template(db_session)
    lead = _lead(db_session, f"deposit-conflict-{uuid4().hex[:8]}")
    quote = _quote(db_session, lead)
    original = quote_acceptance.QuoteAcceptanceDeposit(
        reference="deposit-ref",
        amount=Decimal("37500.00"),
        provider="paystack",
    )
    first = _accept(db_session, quote.id, deposit=original)

    with pytest.raises(quote_acceptance.QuoteAcceptanceError) as exc_info:
        _accept(db_session, quote.id, deposit=conflicting_deposit)

    assert exc_info.value.code == "sales.quote_acceptance.deposit_evidence_conflict"
    db_session.expire_all()
    stored = db_session.get(Quote, quote.id)
    assert stored.metadata_["deposit"] == {
        "reference": "deposit-ref",
        "amount": "37500.00",
        "provider": "paystack",
        "paid": True,
    }
    assert (
        db_session.query(SalesOrder).filter_by(quote_id=quote.id).one().id
        == first.sales_order_id
    )


def test_missing_project_template_rolls_back_quote_acceptance(db_session):
    lead = _lead(db_session, "template-missing")
    quote = _quote(db_session, lead)

    with pytest.raises(
        quote_acceptance.QuoteAcceptanceError,
        match="participant rejected Quote acceptance",
    ):
        _accept(db_session, quote.id)

    db_session.expire_all()
    assert db_session.get(Quote, quote.id).status == QuoteStatus.draft.value
    assert db_session.get(Lead, lead.id).status == LeadStatus.new.value
    assert db_session.query(Subscriber).count() == 0
    assert db_session.query(SalesOrder).count() == 0
    assert db_session.query(Project).count() == 0
    assert db_session.query(ProjectTask).count() == 0


def test_selected_project_type_assigns_matching_template_and_tasks(db_session):
    selected_type = ProjectType.air_fiber_installation
    template = _template(db_session, selected_type)
    lead = _lead(db_session, "configured-template")
    quote = _quote(db_session, lead, selected_type)

    outcome = _accept(db_session, quote.id)
    project = db_session.get(Project, outcome.project_id)
    tasks = db_session.query(ProjectTask).filter_by(project_id=project.id).all()

    assert project.project_type == selected_type.value
    assert project.project_template_id == template.id
    assert outcome.project_template_id == template.id
    assert {task.title for task in tasks} == {
        "Survey site",
        "Install customer fiber",
    }
    assert db_session.query(WorkOrder).filter_by(project_id=project.id).count() == 1


def test_quote_acceptance_rolls_back_everything_when_event_staging_fails(
    db_session, monkeypatch
):
    _template(db_session)
    lead = _lead(db_session, "rollback")
    quote = _quote(db_session, lead)
    lead_id = lead.id
    quote_id = quote.id

    def fail_quote_event(*_args, **_kwargs):
        raise RuntimeError("event store unavailable")

    monkeypatch.setattr(quote_acceptance, "emit_event", fail_quote_event)
    with pytest.raises(RuntimeError, match="event store unavailable"):
        _accept(db_session, quote_id)

    db_session.expire_all()
    assert db_session.get(Quote, quote_id).status == QuoteStatus.draft.value
    assert db_session.get(Lead, lead_id).status == LeadStatus.new.value
    assert db_session.get(Lead, lead_id).subscriber_id is None
    assert db_session.query(Subscriber).count() == 0
    assert db_session.query(SalesOrder).count() == 0
    assert db_session.query(Project).count() == 0
    assert db_session.query(ProjectTask).count() == 0
    assert db_session.query(WorkOrder).count() == 0
    assert db_session.query(AuditEvent).filter_by(action="quote.accepted").count() == 0


def test_duplicate_quote_acceptance_returns_existing_records(db_session):
    _template(db_session)
    lead = _lead(db_session, "replay")
    quote = _quote(db_session, lead)
    first = _accept(db_session, quote.id)
    # A later policy change must not cause an acceptance retry to generate new
    # operational work for an already accepted Quote.
    survey_policy = (
        db_session.query(ProjectTemplateTask).filter_by(title="Survey site").one()
    )
    survey_task = (
        db_session.query(ProjectTask)
        .filter_by(project_id=first.project_id, title="Survey site")
        .one()
    )
    projects.project_tasks.update(
        db_session,
        str(survey_task.id),
        ProjectTaskUpdate(metadata_={"operator_note": "Site access confirmed"}),
    )
    survey_policy.auto_create_work_order = True
    db_session.commit()
    second = _accept(db_session, quote.id)

    assert second.replayed is True
    assert second.subscriber_id == first.subscriber_id
    assert second.sales_order_id == first.sales_order_id
    assert second.project_id == first.project_id
    assert second.project_template_id == first.project_template_id
    assert second.project_task_ids == first.project_task_ids
    assert second.work_order_ids == first.work_order_ids
    assert db_session.query(Subscriber).count() == 1
    assert db_session.query(SalesOrder).count() == 1
    assert db_session.query(Project).count() == 1
    assert db_session.query(ProjectTask).count() == 2
    assert db_session.query(WorkOrder).count() == 1
    assert db_session.query(AuditEvent).filter_by(action="quote.accepted").count() == 1


def test_accepted_quote_replay_repairs_only_missing_configured_work_order(
    db_session,
):
    _template(db_session)
    lead = _lead(db_session, "repair-missing-work-order")
    quote = _quote(db_session, lead)
    first = _accept(db_session, quote.id)
    automated = db_session.query(WorkOrder).filter_by(project_id=first.project_id).one()
    manual = work_order_commands.create(
        db_session,
        WorkOrderHeaderCreate(
            subscriber_id=first.subscriber_id,
            project_id=first.project_id,
            title="Manual quality inspection",
        ),
    )
    db_session.delete(automated)
    db_session.commit()

    repaired = _accept(db_session, quote.id)
    replayed = _accept(db_session, quote.id)
    rows = (
        db_session.query(WorkOrder)
        .filter_by(project_id=first.project_id)
        .order_by(WorkOrder.created_at.asc(), WorkOrder.id.asc())
        .all()
    )

    assert repaired.replayed is True
    assert replayed.replayed is True
    assert repaired.work_order_ids == replayed.work_order_ids
    assert len(rows) == 2
    assert manual.id in repaired.work_order_ids
    assert sum(row.project_task_id is not None for row in rows) == 1
    assert sum(row.project_task_id is None for row in rows) == 1
