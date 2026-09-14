"""PostgreSQL compatibility between account writers and prepaid review evidence."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.models.prepaid_funding import PrepaidDraftReconciliationException
from app.models.subscriber import Reseller, Subscriber
from app.services.billing._common import lock_account


def test_account_writer_lock_allows_prepaid_review_fk_and_serializes_writers(engine):
    """A review write can reference a locked account without admitting a writer race.

    The renewal ambiguity path writes its durable review item through a separate
    session while its owner transaction holds this account lock. PostgreSQL
    needs ``KEY SHARE`` on ``subscribers`` for that foreign key. A second
    account writer must still wait behind the holder, which proves this is not
    a relaxation of the account-credit serialization contract.
    """

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:12]
    with session_factory() as setup:
        reseller = Reseller(
            name=f"Prepaid Lock Compatibility {suffix}",
            code=f"prepaid-lock-{suffix}",
            is_active=True,
        )
        account = Subscriber(
            first_name="Prepaid",
            last_name="LockCompatibility",
            email=f"prepaid-lock-{suffix}@example.com",
            reseller=reseller,
        )
        setup.add_all([reseller, account])
        setup.commit()
        account_id = account.id

    with session_factory() as holder:
        lock_account(holder, str(account_id))

        # This is the real out-of-band review-row shape: its account FK must
        # acquire KEY SHARE while the funding owner holds the account lock.
        with session_factory() as review_writer:
            review_writer.execute(text("SET LOCAL lock_timeout = '200ms'"))
            review_writer.add(
                PrepaidDraftReconciliationException(
                    account_id=account_id,
                    invoice_id=None,
                    subscription_id=None,
                    status="open",
                    reason="renewal_legacy_unbacked_funding",
                    currency="NGN",
                    required_amount=Decimal("1.00"),
                    payment_backed_amount=Decimal("0.00"),
                    opening_funding_amount=Decimal("0.00"),
                    preview_fingerprint="a" * 64,
                    alert_fingerprint=f"pytest-prepaid-lock-{suffix}",
                )
            )
            review_writer.commit()

        # The compatible FK lock above must not turn the account lock into a
        # shared/read lock: another account writer remains serialized until
        # the original holder ends its transaction.
        with session_factory() as competing_writer:
            competing_writer.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(OperationalError):
                lock_account(competing_writer, str(account_id))
            competing_writer.rollback()

        holder.commit()

    with session_factory() as released_writer:
        lock_account(released_writer, str(account_id))
        released_writer.commit()

    with session_factory() as check:
        assert (
            check.query(PrepaidDraftReconciliationException)
            .filter(PrepaidDraftReconciliationException.account_id == account_id)
            .count()
            == 1
        )
