"""Test helpers for the irreversible prepaid opening-balance contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import select

from app.models.billing import (
    LedgerEntry,
    LedgerEntryType,
    LedgerSource,
    Payment,
    PaymentSettlement,
    PaymentSettlementOrigin,
    PaymentStatus,
)
from app.models.catalog import BillingCycle, OfferPrice, PriceType, Subscription
from app.models.prepaid_funding import PrepaidFundingReconstructionBatch
from app.services import prepaid_funding_attestation
from app.services.prepaid_funding_reconstruction import (
    apply_prepaid_funding_reconstruction,
    parse_reconstruction_manifest,
)
from tests.prepaid_funding_test_support import (
    ephemeral_public_signing_key_pem,
    sealed_reconstruction_payload,
)

TEST_PREPAID_POSITION_AT = datetime(2026, 3, 16, tzinfo=UTC)


def ensure_test_prepaid_contract(
    db,
    subscription: Subscription,
    amount: Decimal | str = Decimal("100.00"),
    *,
    currency: str = "NGN",
) -> None:
    """Give a test subscription complete, internally consistent renewal terms.

    Production prepaid enforcement deliberately refuses catalog-only pricing:
    the contracted amount must be frozen on ``Subscription.unit_price``. The
    catalog row remains necessary for currency/cadence metadata, so generic
    fixtures that become prepaid in a test need both pieces of evidence.
    """
    contracted = Decimal(str(amount))
    subscription.unit_price = contracted
    subscription.billing_cycle = BillingCycle.monthly
    price = db.scalar(
        select(OfferPrice).where(
            OfferPrice.offer_id == subscription.offer_id,
            OfferPrice.price_type == PriceType.recurring,
            OfferPrice.is_active.is_(True),
        )
    )
    if price is None:
        db.add(
            OfferPrice(
                offer_id=subscription.offer_id,
                price_type=PriceType.recurring,
                amount=contracted,
                currency=currency,
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
    db.flush()


def materialize_test_prepaid_opening_balance(
    db,
    account_id: object,
    amount: Decimal | str,
    *,
    position_at: datetime = TEST_PREPAID_POSITION_AT,
) -> None:
    """Create the same reviewed authority record production runtime requires."""
    materialize_test_prepaid_opening_balances(
        db,
        {account_id: amount},
        position_at=position_at,
    )


def materialize_test_prepaid_opening_balances(
    db,
    balances: dict[object, Decimal | str],
    *,
    position_at: datetime = TEST_PREPAID_POSITION_AT,
    quarantined: dict[object, str | tuple[str, ...]] | None = None,
) -> None:
    """Materialize a complete test cohort in one reviewed batch."""
    db.query(PrepaidFundingReconstructionBatch).filter(
        PrepaidFundingReconstructionBatch.source
        == "pytest-empty-native-install-cutover",
        PrepaidFundingReconstructionBatch.account_count == 0,
    ).delete(synchronize_session=False)
    db.flush()
    payload = sealed_reconstruction_payload(
        position_at,
        balances,
        quarantined=quarantined,
        source="pytest-reviewed-opening-balance",
    )
    digest = parse_reconstruction_manifest(payload["manifest"]).manifest_sha256
    with patch.object(
        prepaid_funding_attestation,
        "resolve_trusted_public_key_pem",
        return_value=ephemeral_public_signing_key_pem(),
    ):
        apply_prepaid_funding_reconstruction(
            db,
            payload,
            expected_manifest_sha256=digest,
            evidence_ref="pytest:prepaid-opening-balance",
            approved_by="pytest",
            expected_account_ids=set(balances) | set(quarantined or {}),
            now=position_at + timedelta(minutes=1),
        )
    db.commit()


def create_test_settled_payment_credit(
    db,
    account_id: object,
    amount: Decimal | str,
    *,
    currency: str = "NGN",
    paid_at: datetime,
    provider_fee: Decimal | str = Decimal("0.00"),
    origin: PaymentSettlementOrigin = PaymentSettlementOrigin.system,
    idempotency_key: str | None = None,
) -> Payment:
    """Create canonical settled payment credit with allocation evidence."""

    payment_amount = Decimal(str(amount))
    fee = Decimal(str(provider_fee))
    settlement_amount = payment_amount - fee
    payment = Payment(
        account_id=account_id,
        amount=payment_amount,
        provider_fee=fee,
        refunded_amount=Decimal("0.00"),
        currency=currency,
        status=PaymentStatus.succeeded,
        paid_at=paid_at,
        created_at=paid_at,
        is_active=True,
    )
    db.add(payment)
    db.flush()
    entry = LedgerEntry(
        account_id=account_id,
        payment_id=payment.id,
        entry_type=LedgerEntryType.credit,
        source=LedgerSource.payment,
        amount=settlement_amount,
        currency=currency,
        memo="Test settled payment credit",
        effective_date=paid_at,
        created_at=paid_at,
        is_active=True,
        affects_customer_position=False,
    )
    db.add(entry)
    db.flush()
    db.add(
        PaymentSettlement(
            payment_id=payment.id,
            unallocated_ledger_entry_id=entry.id,
            amount=settlement_amount,
            unallocated_amount=settlement_amount,
            prepaid_amount=Decimal("0.00"),
            currency=currency,
            origin=origin,
            idempotency_key=idempotency_key
            or f"pytest:settled-payment-credit:{payment.id}",
            created_at=paid_at,
        )
    )
    db.flush()
    return payment
