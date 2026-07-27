"""Campaigns can target on financial state, using the owner's definition.

Marketing that cannot see service state is the reason a separate CRM was worth
replacing: not upselling a subscriber who is in arrears is the concrete case.

The rule under test is that the segment never restates what "due" or "overdue"
means. It asks ``invoice_collectibility``, which owns those definitions, so a
campaign audience and the customer's own invoice page cannot disagree.

See docs/designs/MARKETING_SALES_SOT.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import Invoice, InvoiceStatus
from app.models.comms_campaign import (
    Campaign,
    CampaignChannel,
    CampaignStatus,
    CampaignType,
)
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services import comms_campaigns
from app.services.invoice_collectibility import (
    accounts_with_due_debt,
    accounts_with_overdue_debt,
    due_invoice_balance,
    overdue_debt_balance,
)


def _subscriber(db_session, **overrides) -> Subscriber:
    subscriber = Subscriber(
        first_name="Seg",
        last_name="Customer",
        email=f"seg-{uuid4().hex[:8]}@example.com",
        status=overrides.pop("status", SubscriberStatus.active),
        is_active=True,
        marketing_opt_in=True,
        **overrides,
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _invoice(db_session, subscriber, *, balance, status, due_at) -> Invoice:
    invoice = Invoice(
        account_id=subscriber.id,
        status=status,
        balance_due=Decimal(balance),
        total=Decimal(balance),
        currency="NGN",
        due_at=due_at,
    )
    db_session.add(invoice)
    db_session.flush()
    return invoice


def _campaign(db_session, segment: dict | None) -> Campaign:
    campaign = Campaign(
        name=f"Segment test {uuid4().hex[:6]}",
        campaign_type=CampaignType.one_time.value,
        channel=CampaignChannel.email.value,
        status=CampaignStatus.draft.value,
        subject="Hello",
        body_text="Body",
        segment_filter=segment,
    )
    db_session.add(campaign)
    db_session.flush()
    return campaign


# --- the cohort predicates agree with the per-account reads ---------------


def test_a_cohort_answer_matches_the_per_account_read(db_session):
    """The whole point: one definition, two shapes.

    If these ever diverge, a campaign targets people whose invoice page says
    something different.
    """
    owing = _subscriber(db_session)
    clear = _subscriber(db_session)
    _invoice(
        db_session,
        owing,
        balance="15000.00",
        status=InvoiceStatus.overdue,
        due_at=datetime.now(UTC) - timedelta(days=10),
    )
    db_session.flush()

    cohort = accounts_with_overdue_debt(db_session, [owing.id, clear.id])

    assert (owing.id in cohort) is (overdue_debt_balance(db_session, owing.id) > 0)
    assert (clear.id in cohort) is (overdue_debt_balance(db_session, clear.id) > 0)
    assert cohort == {owing.id}


def test_a_due_cohort_matches_the_per_account_read(db_session):
    due = _subscriber(db_session)
    not_yet = _subscriber(db_session)
    _invoice(
        db_session,
        due,
        balance="5000.00",
        status=InvoiceStatus.issued,
        due_at=datetime.now(UTC) - timedelta(days=1),
    )
    _invoice(
        db_session,
        not_yet,
        balance="5000.00",
        status=InvoiceStatus.issued,
        due_at=datetime.now(UTC) + timedelta(days=14),
    )
    db_session.flush()

    cohort = accounts_with_due_debt(db_session, [due.id, not_yet.id])

    assert (due.id in cohort) is (due_invoice_balance(db_session, due.id) > 0)
    assert (not_yet.id in cohort) is (due_invoice_balance(db_session, not_yet.id) > 0)
    assert cohort == {due.id}


def test_an_empty_cohort_asks_nothing(db_session):
    assert accounts_with_due_debt(db_session, []) == set()
    assert accounts_with_overdue_debt(db_session, []) == set()


def test_a_settled_invoice_is_not_debt(db_session):
    settled = _subscriber(db_session)
    _invoice(
        db_session,
        settled,
        balance="0.00",
        status=InvoiceStatus.paid,
        due_at=datetime.now(UTC) - timedelta(days=30),
    )
    db_session.flush()

    assert accounts_with_overdue_debt(db_session, [settled.id]) == set()


# --- the segment filters an audience -------------------------------------


def _build(db_session, campaign):
    return comms_campaigns.build_recipient_list(db_session, campaign.id)


def test_targeting_overdue_debt_excludes_everyone_else(db_session):
    owing = _subscriber(db_session)
    clear = _subscriber(db_session)
    _invoice(
        db_session,
        owing,
        balance="9000.00",
        status=InvoiceStatus.overdue,
        due_at=datetime.now(UTC) - timedelta(days=20),
    )
    campaign = _campaign(db_session, {"financial_position": "has_overdue_debt"})
    db_session.commit()

    result = _build(db_session, campaign)

    assert result.created == 1
    assert result.skipped_reasons.get("financial_position") == 1


def test_excluding_debtors_is_the_upsell_case(db_session):
    """The reason this exists: do not sell to someone who owes you money."""
    owing = _subscriber(db_session)
    clear = _subscriber(db_session)
    _invoice(
        db_session,
        owing,
        balance="9000.00",
        status=InvoiceStatus.issued,
        due_at=datetime.now(UTC) - timedelta(days=2),
    )
    campaign = _campaign(db_session, {"financial_position": "no_due_debt"})
    db_session.commit()

    result = _build(db_session, campaign)

    assert result.created == 1
    assert result.skipped_reasons.get("financial_position") == 1


def test_no_financial_segment_targets_everyone(db_session):
    _subscriber(db_session)
    _subscriber(db_session)
    campaign = _campaign(db_session, {})
    db_session.commit()

    result = _build(db_session, campaign)

    assert result.created == 2
    assert "financial_position" not in result.skipped_reasons


def test_an_unknown_segment_value_is_ignored_not_guessed(db_session):
    """A typo must not silently narrow an audience to nobody."""
    _subscriber(db_session)
    campaign = _campaign(db_session, {"financial_position": "in_arrears"})
    db_session.commit()

    result = _build(db_session, campaign)

    assert result.created == 1
    assert "financial_position" not in result.skipped_reasons


@pytest.mark.parametrize("value", sorted(comms_campaigns.FINANCIAL_SEGMENTS))
def test_every_declared_segment_value_resolves(db_session, value):
    _subscriber(db_session)
    campaign = _campaign(db_session, {"financial_position": value})
    db_session.commit()

    result = _build(db_session, campaign)

    assert result.created + result.skipped == 1


def test_the_segment_does_not_restate_the_debt_rule():
    """Guard: the campaign service must ask the collectibility owner.

    Re-deriving 'overdue' here is the parallel derivation path the standard
    forbids, and the drift would be invisible until a customer complained.
    """
    from pathlib import Path

    source = Path("app/services/comms_campaigns.py").read_text()
    assert "accounts_with_overdue_debt" in source
    assert "OVERDUE_DEBT_STATUSES" not in source
    assert "Invoice.balance_due" not in source
