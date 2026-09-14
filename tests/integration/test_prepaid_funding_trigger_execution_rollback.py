"""The mismatched-replay review item must survive the transaction it's raised in.

`evaluate_prepaid_service_after_settlement`'s mismatched-replay path writes a
`PrepaidDraftReconciliationException` review item and then raises a permanent,
non-retryable error. When driven through the real entry point
(`execute_prepaid_service_after_settlement`), that raise unwinds
`execute_owner_command`'s whole transaction -- so the review item write must
happen on a genuinely independent connection/transaction
(`_record_review_item_out_of_band`), or it is destroyed by the very rollback
it exists to survive.

This can only be proven with a REAL, separate PostgreSQL connection --
SQLite's `StaticPool` test lane shares one physical connection for the whole
test, so a "second" session there is still inside the same uncommitted
transaction and would falsely appear to roll back the write even if the
production code were correct. Requires `TEST_DATABASE_URL`
(`make test-db-up && make test-integration`).

Assumes the application's own `SessionLocal` (`app.db.get_engine()`) resolves
to the SAME PostgreSQL instance as `TEST_DATABASE_URL` in this run -- the
same assumption every other owner-command integration test in this suite
already depends on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import sessionmaker

from app.models.billing import (
    Payment,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
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
    PrepaidFundingTriggerExecution,
)
from app.models.subscriber import Reseller, Subscriber
from app.services import event_store as event_store_service
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.prepaid_service_renewals import (
    EvaluatePrepaidServiceAfterSettlementCommand,
    PrepaidServiceRenewalError,
    execute_prepaid_service_after_settlement,
)


def test_mismatched_replay_review_item_survives_the_transaction_rollback(engine):
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]

    with session_factory() as setup:
        reseller = Reseller(
            name=f"Trigger Rollback {suffix}",
            code=f"trigger-rollback-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Trigger",
            last_name="Rollback",
            email=f"trigger-rollback-{suffix}@example.com",
            reseller=reseller,
            billing_mode=BillingMode.prepaid,
        )
        offer = CatalogOffer(
            name=f"Trigger Rollback Plan {suffix}",
            service_type=ServiceType.residential,
            access_type=AccessType.fiber,
            price_basis=PriceBasis.flat,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            status=OfferStatus.active,
            is_active=True,
        )
        setup.add_all([reseller, account, offer])
        setup.flush()
        subscription = Subscription(
            subscriber_id=account.id,
            offer_id=offer.id,
            status=SubscriptionStatus.active,
            billing_mode=BillingMode.prepaid,
            billing_cycle=BillingCycle.monthly,
            # `ck_subscriptions_active_billing_anchor` (migration 539)
            # requires an active subscription to carry both `start_at` and
            # `next_billing_at` -- missing here (2026-09, round 9), so this
            # fixture's INSERT was rejected before this test could ever run
            # on PostgreSQL.
            start_at=datetime(2026, 6, 1, tzinfo=UTC),
            next_billing_at=datetime(2026, 7, 1, tzinfo=UTC),
            unit_price=Decimal("50.00"),
        )
        setup.add_all(
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
        payment = Payment(
            account_id=account.id,
            amount=Decimal("50.00"),
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
            is_active=True,
        )
        setup.add(payment)
        setup.flush()
        setup.add(
            PaymentSettlement(
                payment_id=payment.id,
                currency="NGN",
                amount=Decimal("50.00"),
                origin=PaymentSettlementOrigin.system,
            )
        )
        setup.commit()
        account_id = account.id
        payment_id = payment.id

        event = Event(
            event_type=EventType.payment_received,
            payload={"payment_id": str(payment_id)},
            account_id=account_id,
        )
        event_record = event_store_service.create_event_record(setup, event)
        setup.commit()
        event_store_id = event_record.id
        event_id = event_record.event_id

        # Plant a receipt for this exact event_store_id with a fingerprint
        # that cannot match anything the real call below will compute --
        # forcing the mismatched-replay/permanent-conflict path.
        setup.add(
            PrepaidFundingTriggerExecution(
                event_store_id=event_store_id,
                event_id=event_id,
                event_type=event_record.event_type,
                payment_id=payment_id,
                account_id=account_id,
                currency="NGN",
                effective_at=payment.paid_at,
                request_fingerprint="f" * 64,
                outcome_fingerprint="0" * 64,
                disposition="renewal_review_required",
            )
        )
        setup.commit()

    with session_factory() as worker:
        raised = None
        try:
            execute_prepaid_service_after_settlement(
                worker,
                EvaluatePrepaidServiceAfterSettlementCommand(
                    context=CommandContext.system(
                        actor="pytest:trigger-rollback",
                        scope=str(account_id),
                        reason="pytest mismatched replay",
                        idempotency_key=f"pytest-trigger-rollback-{suffix}",
                    ),
                    account_id=account_id,
                    payment_id=payment_id,
                    evidence_ref="pytest:trigger-rollback",
                    event_id=event_id,
                ),
            )
        except PrepaidServiceRenewalError as exc:
            raised = exc
        assert raised is not None
        assert raised.code.endswith("trigger_execution_conflict")
        assert raised.retryable is False

    # A THIRD, brand-new session/connection: proves the review item is
    # actually durable, not merely visible within the same rolled-back
    # transaction.
    with session_factory() as check:
        review_items = (
            check.query(PrepaidDraftReconciliationException)
            .filter(
                PrepaidDraftReconciliationException.account_id == account_id,
                PrepaidDraftReconciliationException.reason
                == "trigger_execution_fingerprint_mismatch",
            )
            .all()
        )
        assert len(review_items) == 1, (
            "the review item must survive execute_owner_command's rollback -- "
            "it is written out of band, on a separate connection, for "
            "exactly this reason"
        )
        # The receipt row itself was part of the OWNER COMMAND's own
        # transaction (planted directly, not through the failing call), so
        # it survives independently of this test's assertion -- confirm it's
        # still there and untouched (the failing call must not have written
        # a second, competing receipt).
        assert (
            check.query(PrepaidFundingTriggerExecution)
            .filter(PrepaidFundingTriggerExecution.event_store_id == event_store_id)
            .count()
            == 1
        )
