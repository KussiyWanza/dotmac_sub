"""Behavior coverage for the target collections stack (ADR 0007 Phase 5).

Covers the two read-only mode policies and the one `collections.lifecycle`
owner: the case ladder, exact timers, idempotent consequence requests, and
close/restore evidence. Nothing here touches subscription or RADIUS state.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.billing_contract import (
    AccountingTreatment,
    BillingRecordAuthority,
    CollectionTiming,
    ObligationState,
)
from app.models.collections_case import (
    CollectionsCase,
    CollectionsCaseState,
    CollectionsReason,
)
from app.models.durable_timer import DurableTimer, TimerStatus
from app.models.event_store import EventStore
from app.services.collections.lifecycle import CollectionsLifecycle
from app.services.collections.mode_policies import CollectionsProposal
from app.services.collections.postpaid_policy import plan_postpaid_consequence
from app.services.collections.prepaid_policy import (
    PrepaidPolicyError,
    plan_prepaid_consequence,
)
from app.services.owner_commands import CommandContext

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)


def _context() -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="collections:test",
        reason="pytest collections lifecycle",
        idempotency_key=f"pytest:{command_id}",
    )


def _obligation(**overrides) -> SimpleNamespace:
    base = {
        "id": uuid4(),
        "account_id": uuid4(),
        "subscription_id": uuid4(),
        "currency": "NGN",
        "gross_amount": Decimal("25000.00"),
        "resolved_amount": Decimal("0"),
        "state": ObligationState.open,
        "accounting_treatment": AccountingTreatment.receivable,
        "collection_timing": CollectionTiming.arrears,
        "due_at": NOW - timedelta(days=3),
        "period_start": NOW - timedelta(days=20),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _proposal(**overrides) -> CollectionsProposal:
    base = {
        "reason": CollectionsReason.postpaid_overdue,
        "account_id": uuid4(),
        "subscription_id": uuid4(),
        "obligation_id": uuid4(),
        "currency": "NGN",
        "outstanding_amount": Decimal("25000.00"),
        "due_at": NOW - timedelta(days=3),
    }
    base.update(overrides)
    return CollectionsProposal(**base)


# --- mode policies -----------------------------------------------------------


def test_postpaid_policy_proposes_for_an_exact_overdue_receivable(db_session):
    proposal = plan_postpaid_consequence(db_session, obligation=_obligation(), now=NOW)

    assert proposal is not None
    assert proposal.reason is CollectionsReason.postpaid_overdue
    assert proposal.outstanding_amount == Decimal("25000.00")


def test_postpaid_policy_ignores_settled_and_not_yet_due(db_session):
    settled = _obligation(
        state=ObligationState.resolved, resolved_amount=Decimal("25000.00")
    )
    future = _obligation(due_at=NOW + timedelta(days=3))

    assert plan_postpaid_consequence(db_session, obligation=settled, now=NOW) is None
    assert plan_postpaid_consequence(db_session, obligation=future, now=NOW) is None


def test_postpaid_policy_counts_partial_settlement(db_session):
    partial = _obligation(
        state=ObligationState.partially_resolved,
        resolved_amount=Decimal("10000.00"),
    )

    proposal = plan_postpaid_consequence(db_session, obligation=partial, now=NOW)

    assert proposal.outstanding_amount == Decimal("15000.00")


def test_prepaid_policy_proposes_only_when_underfunded(db_session):
    prepaid = _obligation(
        accounting_treatment=AccountingTreatment.prepaid_consumption,
        collection_timing=CollectionTiming.advance,
    )

    proposal = plan_prepaid_consequence(
        db_session,
        obligation=prepaid,
        now=NOW,
        authority=BillingRecordAuthority.shadow,
    )

    # No funding postings exist for this account, so it is underfunded.
    assert proposal is not None
    assert proposal.reason is CollectionsReason.prepaid_underfunded


def test_prepaid_policy_waits_for_the_service_period(db_session):
    prepaid = _obligation(
        accounting_treatment=AccountingTreatment.prepaid_consumption,
        period_start=NOW + timedelta(days=5),
    )

    assert (
        plan_prepaid_consequence(
            db_session,
            obligation=prepaid,
            now=NOW,
            authority=BillingRecordAuthority.shadow,
        )
        is None
    )


def test_prepaid_policy_never_touches_receivables(db_session):
    receivable = _obligation()

    assert (
        plan_prepaid_consequence(
            db_session,
            obligation=receivable,
            now=NOW,
            authority=BillingRecordAuthority.shadow,
        )
        is None
    )


def test_prepaid_policy_fails_closed_for_cutover_quarantine(db_session, monkeypatch):
    prepaid = _obligation(
        accounting_treatment=AccountingTreatment.prepaid_consumption,
        collection_timing=CollectionTiming.advance,
    )
    monkeypatch.setattr(
        "app.services.collections.prepaid_policy.authority_cutover",
        lambda db: object(),
    )
    monkeypatch.setattr(
        "app.services.prepaid_funding_reconstruction.prepaid_funding_incomplete_source_account_ids",
        lambda db, account_ids, *, currency: {prepaid.account_id},
    )

    with pytest.raises(PrepaidPolicyError) as exc_info:
        plan_prepaid_consequence(db_session, obligation=prepaid, now=NOW)

    assert exc_info.value.code == "collections.prepaid_policy.opening_source_incomplete"
    assert str(prepaid.account_id) in exc_info.value.details["work_item_fingerprint"]


# --- collections.lifecycle ---------------------------------------------------


def _advance(db, proposal, *, next_action_at=None):
    return CollectionsLifecycle.advance(
        db,
        proposal,
        context=_context(),
        now=NOW,
        next_action_at=next_action_at,
    )


def test_the_case_ladder_escalates_one_step_per_proposal(db_session):
    proposal = _proposal()

    opened = _advance(db_session, proposal, next_action_at=NOW + timedelta(days=3))
    warned = _advance(db_session, proposal, next_action_at=NOW + timedelta(days=6))
    escalated = _advance(db_session, proposal, next_action_at=NOW + timedelta(days=9))
    requested = _advance(db_session, proposal)
    replayed = _advance(db_session, proposal)

    assert opened.state is CollectionsCaseState.open
    assert warned.state is CollectionsCaseState.warned
    assert escalated.state is CollectionsCaseState.escalated
    assert requested.state is CollectionsCaseState.consequence_requested
    assert requested.consequence_event_id is not None
    # Terminal replay is a no-op with the same recorded consequence.
    assert replayed.state is CollectionsCaseState.consequence_requested
    assert replayed.consequence_event_id == requested.consequence_event_id

    cases = db_session.execute(select(CollectionsCase)).scalars().all()
    assert len(cases) == 1
    assert cases[0].authority is BillingRecordAuthority.shadow


def test_each_step_replaces_the_exact_next_action_timer(db_session):
    proposal = _proposal()

    _advance(db_session, proposal, next_action_at=NOW + timedelta(days=3))
    _advance(db_session, proposal, next_action_at=NOW + timedelta(days=6))

    timers = db_session.execute(select(DurableTimer)).scalars().all()
    scheduled = [t for t in timers if t.status is TimerStatus.scheduled]
    superseded = [t for t in timers if t.status is TimerStatus.superseded]

    assert len(scheduled) == 1
    assert scheduled[0].generation == 2
    assert len(superseded) == 1


def test_the_consequence_request_is_a_reason_scoped_owner_output(db_session):
    proposal = _proposal()
    for next_at in (NOW + timedelta(days=3), NOW + timedelta(days=6), None):
        result = _advance(db_session, proposal, next_action_at=next_at)
    result = _advance(db_session, proposal)

    event = db_session.execute(
        select(EventStore).where(EventStore.event_id == result.consequence_event_id)
    ).scalar_one()
    payload = event.payload

    assert payload["output"] == "collections.consequence_requested"
    assert payload["reason"] == "postpaid_overdue"
    assert payload["consequence_idempotency_key"].startswith("collections:")
    assert payload["envelope"]["producer_owner"] == "collections.lifecycle"


def test_close_cancels_timers_and_stages_restore_evidence(db_session):
    proposal = _proposal()
    for next_at in (
        NOW + timedelta(days=3),
        NOW + timedelta(days=6),
        NOW + timedelta(days=9),
    ):
        _advance(db_session, proposal, next_action_at=next_at)
    requested = _advance(db_session, proposal)
    assert requested.state is CollectionsCaseState.consequence_requested

    closed_id = CollectionsLifecycle.close(
        db_session,
        account_id=proposal.account_id,
        subscription_id=proposal.subscription_id,
        reason=proposal.reason,
        context=_context(),
        now=NOW + timedelta(days=10),
        close_reason="invoice settled in full",
    )

    assert closed_id == requested.case_id
    case = db_session.get(CollectionsCase, closed_id)
    assert case.state is CollectionsCaseState.closed
    assert case.close_reason == "invoice settled in full"

    live_timers = (
        db_session.execute(
            select(DurableTimer).where(DurableTimer.status == TimerStatus.scheduled)
        )
        .scalars()
        .all()
    )
    assert live_timers == []


def test_closing_without_a_live_case_is_idempotent(db_session):
    closed = CollectionsLifecycle.close(
        db_session,
        account_id=uuid4(),
        subscription_id=uuid4(),
        reason=CollectionsReason.postpaid_overdue,
        context=_context(),
        now=NOW,
        close_reason="nothing open",
    )

    assert closed is None


def test_reopening_after_close_starts_a_fresh_case(db_session):
    proposal = _proposal()
    _advance(db_session, proposal)
    CollectionsLifecycle.close(
        db_session,
        account_id=proposal.account_id,
        subscription_id=proposal.subscription_id,
        reason=proposal.reason,
        context=_context(),
        now=NOW,
        close_reason="settled",
    )

    reopened = _advance(db_session, proposal)

    assert reopened.state is CollectionsCaseState.open
    cases = db_session.execute(select(CollectionsCase)).scalars().all()
    assert len(cases) == 2
