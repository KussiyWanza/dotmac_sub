"""Nightly isolation: rollback/atomicity, the poison-pill regression, and the
fatal-abort complement -- proving the split policy produces two genuinely
different, correct behaviors, not just one.

Two real PostgreSQL accounts in one nightly batch:

- Account A is constructed to produce a genuine `legacy_unbacked_funding`
  classification (real divergence, no monkeypatch): a reversed payment
  leaves an unmatched ledger credit that is never payment-backed. This is
  the SAME established, verified fixture pattern this codebase already uses
  for exactly this disposition
  (`tests/test_prepaid_draft_reconciliation.py::test_reversed_payment_is_not_reused_with_opening_funding`).
  `unbacked_credit > 0` unconditionally routes `classify_prospective_prepaid_funding`
  past the `reviewed_opening_fundable` branch (which requires
  `unbacked_credit == 0`) into `insufficient_funding`/`legacy_unbacked_funding`
  -- ambiguous, pre-mutation, `PrepaidRenewalAmbiguousEvidenceError`.
- Account B has EXACT reviewed-opening funding for its own real resolved
  charge (`resolve_prepaid_monthly_charge_detail` -- the same resolver
  `confirm_prepaid_service_renewal` itself calls) and must renew
  successfully in the SAME batch despite A's failure.

A separate, complementary test proves the opposite: an UNCLASSIFIED failure
(not one of the 3 allowlisted types) aborts the WHOLE pass, and an account
sequenced after the failing one is never even reached.

Uses the established two-real-connection PostgreSQL pattern
(`tests/integration/test_account_credit_concurrency.py`,
`tests/integration/test_prepaid_funding_trigger_execution_rollback.py`),
driven through the real production entry point
(`execute_due_prepaid_service_renewals` -> `execute_owner_command`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    Invoice,
    InvoiceLine,
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentAllocation,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
    ServiceEntitlement,
)
from app.models.catalog import (
    AccessType,
    BillingCycle,
    BillingMode,
    CatalogOffer,
    OfferPrice,
    OfferStatus,
    PriceBasis,
    PriceType,
    ServiceType,
    Subscription,
    SubscriptionStatus,
)
from app.models.prepaid_funding import (
    PrepaidDraftReconciliationException,
    PrepaidFundingBaseline,
    PrepaidFundingReconstructionBatch,
)
from app.models.subscriber import Reseller, Subscriber
from app.services.owner_commands import CommandContext
from app.services.prepaid_service_renewals import (
    RunDuePrepaidServiceRenewalsCommand,
    execute_due_prepaid_service_renewals,
    resolve_prepaid_monthly_charge_detail,
)
from tests.prepaid_funding_helpers import materialize_test_prepaid_opening_balance


@pytest.fixture()
def relinquish_nightly_authority_cutover(engine):
    """Relinquish this file's committed authority-cutover marker after each test.

    The nightly proof deliberately uses independently committed sessions so it
    can exercise the production out-of-band review write. Its reviewed
    opening-balance helper therefore commits a real authority-cutover batch,
    unlike the ordinary ``db_session`` fixture whose enclosing transaction is
    rolled back. The shared integration database may subsequently construct
    that fixture, which must be free to install its own empty cutover marker.

    We demote only the reviewed test batch associated with this test's account
    ids; its other test evidence remains available for failure diagnosis, but
    the globally unique authority-cutover slot cannot leak into the next test.
    """

    account_ids: set[object] = set()
    yield account_ids
    if not account_ids:
        return
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as cleanup:
        cleanup.execute(
            update(PrepaidFundingReconstructionBatch)
            .where(
                PrepaidFundingReconstructionBatch.is_authority_cutover.is_(True),
                PrepaidFundingReconstructionBatch.source
                == "pytest-reviewed-opening-balance",
                PrepaidFundingReconstructionBatch.id.in_(
                    select(PrepaidFundingBaseline.batch_id).where(
                        PrepaidFundingBaseline.account_id.in_(account_ids)
                    )
                ),
            )
            .values(is_authority_cutover=False)
        )
        cleanup.commit()


def _account_and_subscription(
    session, *, suffix: str, label: str, next_billing_at: datetime
) -> tuple[Subscriber, Subscription]:
    reseller = Reseller(
        name=f"Nightly Isolation {label} {suffix}",
        code=f"nightly-isolation-{label}-{suffix}",
        is_active=True,
    )
    account = Subscriber(
        first_name="Nightly",
        last_name=label,
        email=f"nightly-isolation-{label}-{suffix}@example.com",
        reseller=reseller,
        billing_mode=BillingMode.prepaid,
    )
    offer = CatalogOffer(
        name=f"Nightly Isolation Plan {label} {suffix}",
        service_type=ServiceType.residential,
        access_type=AccessType.fiber,
        price_basis=PriceBasis.flat,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        status=OfferStatus.active,
        is_active=True,
    )
    session.add_all([reseller, account, offer])
    session.flush()
    subscription = Subscription(
        subscriber_id=account.id,
        offer_id=offer.id,
        status=SubscriptionStatus.active,
        billing_mode=BillingMode.prepaid,
        billing_cycle=BillingCycle.monthly,
        # `ck_subscriptions_active_billing_anchor` (migration 539, enforced
        # on PostgreSQL for every new row even though NOT VALID for
        # pre-existing ones) requires an active subscription to carry BOTH
        # `start_at` and `next_billing_at` -- missing here (2026-09, round
        # 9), so this fixture's INSERT itself was rejected before either
        # flagship isolation test could ever actually run on PostgreSQL.
        start_at=next_billing_at - timedelta(days=30),
        next_billing_at=next_billing_at,
        unit_price=Decimal("50.00"),
    )
    session.add_all(
        [
            subscription,
            OfferPrice(
                offer_id=offer.id,
                price_type=PriceType.recurring,
                amount=Decimal("50.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            ),
        ]
    )
    session.commit()
    return account, subscription


def _stage_reversed_unbacked_payment(
    session, account: Subscriber, amount: Decimal
) -> None:
    """The established fixture for a genuine `legacy_unbacked_funding` case.

    Mirrors `tests/test_prepaid_draft_reconciliation.py`'s `_payment` helper
    plus its `test_reversed_payment_is_not_reused_with_opening_funding`
    scenario exactly: a settled payment's ledger credit survives after the
    payment itself is marked reversed, leaving unmatched ("unbacked")
    account credit that is not backed by any active payment source.
    """
    paid_at = datetime(2026, 6, 25, 10, tzinfo=UTC)
    payment = Payment(
        account_id=account.id,
        amount=amount,
        provider_fee=Decimal("0.00"),
        refunded_amount=Decimal("0.00"),
        currency="NGN",
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
        is_active=True,
        created_at=paid_at,
    )
    session.add(payment)
    session.flush()
    entry = LedgerEntry(
        account_id=account.id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=amount,
        currency="NGN",
        memo="Reviewed test payment",
        is_active=True,
        affects_customer_position=False,
        effective_date=paid_at,
        created_at=paid_at,
    )
    session.add(entry)
    session.flush()
    session.add(
        PaymentSettlement(
            payment_id=payment.id,
            unallocated_ledger_entry_id=entry.id,
            amount=amount,
            unallocated_amount=amount,
            prepaid_amount=Decimal("0.00"),
            currency="NGN",
            origin=PaymentSettlementOrigin.system,
            idempotency_key=f"pytest-nightly-isolation-unbacked-{payment.id}",
            created_at=paid_at,
        )
    )
    session.commit()
    payment.status = PaymentStatus.reversed
    session.commit()


def _assert_zero_mutation_for_subscription(session, subscription_id) -> None:
    assert (
        session.query(Invoice)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .filter(InvoiceLine.subscription_id == subscription_id)
        .count()
        == 0
    )
    assert (
        session.query(InvoiceLine)
        .filter(InvoiceLine.subscription_id == subscription_id)
        .count()
        == 0
    )
    assert (
        session.query(ServiceEntitlement)
        .filter(ServiceEntitlement.subscription_id == subscription_id)
        .count()
        == 0
    )
    assert (
        session.query(PaymentAllocation)
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
        .filter(InvoiceLine.subscription_id == subscription_id)
        .count()
        == 0
    )


def test_ambiguous_account_is_isolated_while_a_second_account_still_renews(
    engine, relinquish_nightly_authority_cutover
):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]

    with session_factory() as setup:
        account_a, subscription_a = _account_and_subscription(
            setup,
            suffix=suffix,
            label="A",
            next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        account_b, subscription_b = _account_and_subscription(
            setup,
            suffix=suffix,
            label="B",
            next_billing_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )

        # Derive the real charge from the actual resolver
        # `confirm_prepaid_service_renewal` itself calls -- not a guessed
        # round number.
        charge_a = resolve_prepaid_monthly_charge_detail(
            setup, subscription_a, datetime(2026, 7, 1, 12, tzinfo=UTC)
        )
        charge_b = resolve_prepaid_monthly_charge_detail(
            setup, subscription_b, datetime(2026, 7, 1, 12, tzinfo=UTC)
        )
        assert charge_a is not None and charge_b is not None
        relinquish_nightly_authority_cutover.update({account_a.id, account_b.id})

        # Account A: an active baseline covering the real charge exactly
        # (so the ledger-based affordability gate passes and classification
        # is actually reached), PLUS an unmatched, reversed-payment ledger
        # credit -- `unbacked_credit > 0` deterministically routes
        # classification to `legacy_unbacked_funding` (ambiguous),
        # regardless of the baseline being otherwise sufficient.
        materialize_test_prepaid_opening_balance(
            setup,
            account_a.id,
            charge_a.total,
            position_at=datetime(2026, 6, 20, tzinfo=UTC),
        )
        _stage_reversed_unbacked_payment(setup, account_a, charge_a.total)

        # Account B: exact reviewed-opening funding for its own real
        # resolved charge -- deterministically `reviewed_opening_fundable`.
        materialize_test_prepaid_opening_balance(
            setup,
            account_b.id,
            charge_b.total,
            position_at=datetime(2026, 6, 20, tzinfo=UTC),
        )

        subscription_a_id = subscription_a.id
        subscription_b_id = subscription_b.id

    with session_factory() as worker:
        summary = execute_due_prepaid_service_renewals(
            worker,
            RunDuePrepaidServiceRenewalsCommand(
                context=CommandContext.system(
                    actor="pytest:nightly-isolation",
                    scope="prepaid_service_renewals",
                    reason="pytest nightly isolation poison-pill regression",
                    idempotency_key=f"pytest-nightly-isolation-{suffix}",
                ),
                run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
            ),
        )
        worker.commit()

    # All assertions via a FRESH session, never the one that ran the pass.
    with session_factory() as check:
        _assert_zero_mutation_for_subscription(check, subscription_a_id)
        assert (
            check.query(ServiceEntitlement)
            .filter(ServiceEntitlement.subscription_id == subscription_b_id)
            .count()
            == 1
        )
        invoice_b = (
            check.query(Invoice)
            .join(InvoiceLine, InvoiceLine.invoice_id == Invoice.id)
            .filter(InvoiceLine.subscription_id == subscription_b_id)
            .one()
        )
        assert invoice_b.status.value == "paid"
        assert invoice_b.balance_due == Decimal("0.00")

        assert summary["prepaid_renewals_status"] == "partial_failure"
        isolated = summary["prepaid_renewals_isolated"]
        assert isinstance(isolated, list)
        assert len(isolated) == 1
        # Account-level isolation (2026-09, round 7): the isolated entry now
        # names the account and every due subscription that account had in
        # this pass (comma-joined) -- account A has exactly one due
        # subscription here, so the value is unchanged even though the key
        # is now plural.
        assert isolated[0]["subscription_ids"] == str(subscription_a_id)
        assert isolated[0]["error_type"] == "PrepaidRenewalAmbiguousEvidenceError"

        review_items = (
            check.query(PrepaidDraftReconciliationException)
            .filter(
                PrepaidDraftReconciliationException.subscription_id == subscription_a_id
            )
            .all()
        )
        assert len(review_items) == 1

    # Replay: the exact same pass again deduplicates -- no second review
    # item, no mutation for A, B untouched (already funded, so
    # `already_covered`, not re-funded).
    with session_factory() as worker2:
        second_summary = execute_due_prepaid_service_renewals(
            worker2,
            RunDuePrepaidServiceRenewalsCommand(
                context=CommandContext.system(
                    actor="pytest:nightly-isolation",
                    scope="prepaid_service_renewals",
                    reason="pytest nightly isolation poison-pill regression replay",
                    idempotency_key=f"pytest-nightly-isolation-replay-{suffix}",
                ),
                run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
            ),
        )
        worker2.commit()

    with session_factory() as check2:
        assert second_summary["prepaid_renewals_status"] == "partial_failure"
        _assert_zero_mutation_for_subscription(check2, subscription_a_id)
        assert (
            check2.query(PrepaidDraftReconciliationException)
            .filter(
                PrepaidDraftReconciliationException.subscription_id == subscription_a_id
            )
            .count()
            == 1
        ), "replay must update the existing review item, not create a duplicate"
        assert (
            check2.query(ServiceEntitlement)
            .filter(ServiceEntitlement.subscription_id == subscription_b_id)
            .count()
            == 1
        ), "B must not be double-funded on replay"


def test_unclassified_failure_aborts_the_whole_pass_before_later_accounts(
    engine, monkeypatch, relinquish_nightly_authority_cutover
):
    """The direct complement: a failure NOT in the 3-type allowlist (a
    posting-owner failure, matching the shape of the preserved
    `test_scheduled_posting_failure_rolls_back_renewal_business_result`) is
    never isolated -- it aborts the entire pass, and an account sequenced
    AFTER the failing one is never even reached.
    """
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]

    with session_factory() as setup:
        account_first, subscription_first = _account_and_subscription(
            setup,
            suffix=suffix,
            label="First",
            next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        account_second, subscription_second = _account_and_subscription(
            setup,
            suffix=suffix,
            label="Second",
            next_billing_at=datetime(2026, 7, 1, 1, tzinfo=UTC),
        )
        relinquish_nightly_authority_cutover.update(
            {account_first.id, account_second.id}
        )
        materialize_test_prepaid_opening_balance(
            setup,
            account_first.id,
            Decimal("100.00"),
            position_at=datetime(2026, 6, 20, tzinfo=UTC),
        )
        materialize_test_prepaid_opening_balance(
            setup,
            account_second.id,
            Decimal("100.00"),
            position_at=datetime(2026, 6, 20, tzinfo=UTC),
        )
        subscription_first_id = subscription_first.id
        subscription_second_id = subscription_second.id

    call_count = 0

    def _boom(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("posting owner unavailable (pytest forced failure)")

    monkeypatch.setattr(
        "app.services.billing.customer_subledger.stage_posting_group", _boom
    )

    with session_factory() as worker:
        raised = None
        try:
            execute_due_prepaid_service_renewals(
                worker,
                RunDuePrepaidServiceRenewalsCommand(
                    context=CommandContext.system(
                        actor="pytest:nightly-fatal-abort",
                        scope="prepaid_service_renewals",
                        reason="pytest nightly fatal-abort complement",
                        idempotency_key=f"pytest-nightly-fatal-abort-{suffix}",
                    ),
                    run_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
                ),
            )
        except RuntimeError as exc:
            raised = exc
        assert raised is not None
        assert "posting owner unavailable" in str(raised)

    # Only ONE call: the due-subscription scan is ordered by
    # `next_billing_at`, so `subscription_first` (due earliest) is
    # processed first, fails, and the exception propagates out of the WHOLE
    # pass before `subscription_second`'s iteration is ever reached.
    assert call_count == 1

    with session_factory() as check:
        _assert_zero_mutation_for_subscription(check, subscription_first_id)
        _assert_zero_mutation_for_subscription(check, subscription_second_id)
