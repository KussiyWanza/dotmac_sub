"""Customer self-service add-on purchase with exact adjustment evidence.

Add-ons available to a subscription come from its offer's ``OfferAddOn`` links.
A paid purchase consumes prepaid account funding through
``financial.account_adjustments``. The add-on owner previews the price,
subscription state, funding, receivables, and exact ledger consequence; locked
confirmation rejects stale previews and links the resulting entitlement to the
exact adjustment. Ownership is enforced against the caller's account.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.billing import LedgerCategory
from app.models.catalog import (
    AddOn,
    AddOnPrice,
    OfferAddOn,
    PriceType,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.idempotency import IdempotencyKey
from app.schemas.audit import AuditEventCreate
from app.schemas.billing import (
    AccountAdjustmentConfirm,
    AccountAdjustmentPreviewRequest,
)
from app.services import catalog as catalog_service
from app.services.audit import AuditEvents
from app.services.billing._common import get_account_credit_balance, lock_account
from app.services.billing.adjustments import (
    ACCOUNT_ADJUSTMENT_SCOPE,
    AccountAdjustmentError,
    AccountAdjustmentOrigin,
    AccountAdjustmentPreview,
    ConfirmAccountAdjustmentCommand,
    PreviewAccountAdjustmentQuery,
    preview_account_adjustment,
    stage_account_adjustment,
)
from app.services.common import coerce_uuid, round_money, to_decimal
from app.services.customer_context import optional_customer_account_id
from app.services.customer_financial_position import get_customer_financial_position
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "financial.addon_purchases"
RECURRING_TERMS_ADDED_OUTPUT = "billing.contract_terms.recurring_addon_added"

_PURCHASE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="exact add-on entitlement-to-adjustment link",
    name="confirm_customer_addon_purchase",
)


class AddonPurchaseError(DomainError):
    """Fail-closed add-on purchase confirmation error."""


def _error(suffix: str, message: str, **details: object) -> AddonPurchaseError:
    return AddonPurchaseError(
        code=f"{OWNER}.{suffix}",
        message=message,
        details=dict(details),
    )


def _adjustment_http_error(exc: AccountAdjustmentError) -> HTTPException:
    status_code = {
        "financial.account_adjustments.account_not_found": 404,
        "financial.account_adjustments.invalid_configuration": 503,
        "financial.account_adjustments.insufficient_funding": 402,
        "financial.account_adjustments.stale_preview": 409,
        "financial.account_adjustments.idempotency_conflict": 409,
        "financial.account_adjustments.incomplete_evidence": 409,
        "financial.account_adjustments.write_conflict": 409,
    }.get(exc.code, 400)
    return HTTPException(status_code=status_code, detail=exc.message)


def _addon_active_price_record(add_on: AddOn) -> AddOnPrice | None:
    """Return the one active recurring price, otherwise one active price.

    Multiple active recurring prices are ambiguous commercial terms and fail
    closed before either money or an entitlement is written.
    """

    prices = [price for price in (add_on.prices or []) if price.is_active]
    recurring = [price for price in prices if price.price_type == PriceType.recurring]
    if len(recurring) > 1:
        raise ValueError("Add-on has multiple active recurring prices")
    if recurring:
        return recurring[0]
    return prices[0] if prices else None


def _addon_active_price(add_on: AddOn) -> tuple[Decimal, str]:
    """Best price for an add-on: prefer a recurring active price, else any
    active price. Returns (amount, currency); (0, NGN) when unpriced."""
    chosen = _addon_active_price_record(add_on)
    if chosen is None:
        return Decimal("0.00"), "NGN"
    return round_money(to_decimal(chosen.amount or 0)), str(chosen.currency or "NGN")


def _owned_subscription(db: Session, customer: dict, subscription_id: str):
    """Return the subscription iff it belongs to the caller, else None."""
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    if not subscription:
        return None
    account_id = optional_customer_account_id(db, customer)
    if not account_id or str(subscription.subscriber_id) != str(account_id):
        return None
    return subscription


def _offer_links(db: Session, offer_id) -> list[tuple[OfferAddOn, AddOn]]:
    """Active add-ons offered for a subscription's offer, with their link row."""
    if not offer_id:
        return []
    rows = (
        db.query(OfferAddOn, AddOn)
        .join(AddOn, AddOn.id == OfferAddOn.add_on_id)
        .filter(OfferAddOn.offer_id == offer_id)
        .filter(AddOn.is_active.is_(True))
        .all()
    )
    return [(link, add_on) for link, add_on in rows]


def _serialize_option(link: OfferAddOn, add_on: AddOn) -> dict:
    amount, currency = _addon_active_price(add_on)
    return {
        "add_on_id": str(add_on.id),
        "name": add_on.name,
        "addon_type": getattr(add_on.addon_type, "value", str(add_on.addon_type)),
        "description": add_on.description,
        "amount": float(amount),
        "currency": currency,
        "min_quantity": int(link.min_quantity or 1),
        "max_quantity": link.max_quantity,
        "is_required": bool(link.is_required),
        # Data top-up: GB granted to the quota bucket on purchase (null otherwise).
        "grant_gb": add_on.grant_gb,
    }


@dataclass(frozen=True)
class AddonPurchasePreview:
    subscription: object
    add_on: AddOn
    add_on_price: AddOnPrice | None
    quantity: int
    unit_amount: Decimal
    charge: Decimal
    currency: str
    subscription_status: str
    prepaid_funding_before: Decimal
    prepaid_funding_after: Decimal
    postpaid_receivables: Decimal
    collection_blocking_balance: Decimal
    shortfall: Decimal
    allowed: bool
    rejection_reason: str | None
    adjustment_preview: AccountAdjustmentPreview | None
    fingerprint: str

    def as_dict(self) -> dict[str, object]:
        return {
            "add_on_id": str(self.add_on.id),
            "quantity": self.quantity,
            "unit_amount": self.unit_amount,
            "charge": self.charge,
            "currency": self.currency,
            "subscription_status": self.subscription_status,
            "prepaid_funding_before": self.prepaid_funding_before,
            "prepaid_funding_after": self.prepaid_funding_after,
            "postpaid_receivables": self.postpaid_receivables,
            "collection_blocking_balance": self.collection_blocking_balance,
            "shortfall": self.shortfall,
            "can_afford": self.shortfall == Decimal("0.00"),
            "allowed": self.allowed,
            "rejection_reason": self.rejection_reason,
            "ledger_entry_type": (
                self.adjustment_preview.ledger_entry_type
                if self.adjustment_preview
                else None
            ),
            "ledger_source": (
                self.adjustment_preview.ledger_source
                if self.adjustment_preview
                else None
            ),
            "ledger_amount": (
                self.adjustment_preview.ledger_amount
                if self.adjustment_preview
                else Decimal("0.00")
            ),
            "access_consequence": "none_addon_purchase_only",
            "preview_fingerprint": self.fingerprint,
        }


def _purchase_fingerprint(**values: object) -> str:
    normalized = {
        key: f"{value:.2f}" if isinstance(value, Decimal) else str(value)
        for key, value in values.items()
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_purchase_preview(
    db: Session,
    subscription,
    add_on_id: str,
    quantity: int,
) -> AddonPurchasePreview:
    add_on, add_on_price, unit_amount, currency = _resolve_purchasable(
        db, subscription, add_on_id, quantity
    )
    charge = round_money(unit_amount * quantity)
    account_id = str(subscription.subscriber_id)
    status_value = str(getattr(subscription.status, "value", subscription.status or ""))
    origin_ref = f"{subscription.id}:{add_on.id}:{quantity}"
    adjustment_preview = None
    if charge > Decimal("0.00"):
        adjustment_preview = preview_account_adjustment(
            db,
            PreviewAccountAdjustmentQuery(
                request=AccountAdjustmentPreviewRequest(
                    account_id=subscription.subscriber_id,
                    category=LedgerCategory.custom_service,
                    amount=charge,
                    currency=currency,
                    memo=f"Add-on purchase: {add_on.name}"
                    + (f" x{quantity}" if quantity > 1 else ""),
                    reason="Customer-confirmed add-on purchase",
                ),
                origin=AccountAdjustmentOrigin.addon_purchase,
                origin_ref=origin_ref,
            ),
        )
        funding_before = adjustment_preview.prepaid_funding_before
        funding_after = adjustment_preview.prepaid_funding_after
        receivables = adjustment_preview.postpaid_receivables
        blocking = adjustment_preview.collection_blocking_balance
        shortfall = adjustment_preview.shortfall
    else:
        position = get_customer_financial_position(db, subscription.subscriber_id)
        funding_before = round_money(
            get_account_credit_balance(db, account_id, currency=currency)
        )
        funding_after = funding_before
        receivables = round_money(position.open_invoice_balance)
        blocking = round_money(position.collection_blocking_balance)
        shortfall = Decimal("0.00")

    active = subscription.status == SubscriptionStatus.active
    affordable = shortfall == Decimal("0.00")
    allowed = active and affordable
    rejection_reason = None
    if not active:
        rejection_reason = "subscription_not_active"
    elif not affordable:
        rejection_reason = "insufficient_prepaid_funding"
    fingerprint = _purchase_fingerprint(
        kind="addon_purchase",
        subscription_id=subscription.id,
        subscription_status=status_value,
        offer_id=subscription.offer_id,
        add_on_id=add_on.id,
        add_on_price_id=add_on_price.id if add_on_price is not None else "unpriced",
        add_on_price_type=(
            add_on_price.price_type.value if add_on_price is not None else "unpriced"
        ),
        add_on_billing_cycle=(
            add_on_price.billing_cycle.value
            if add_on_price is not None and add_on_price.billing_cycle is not None
            else "inherit"
        ),
        quantity=quantity,
        unit_amount=unit_amount,
        charge=charge,
        currency=currency,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_after,
        postpaid_receivables=receivables,
        collection_blocking_balance=blocking,
        adjustment_fingerprint=(
            adjustment_preview.fingerprint if adjustment_preview else "no-ledger-entry"
        ),
        allowed=allowed,
    )
    return AddonPurchasePreview(
        subscription=subscription,
        add_on=add_on,
        add_on_price=add_on_price,
        quantity=quantity,
        unit_amount=unit_amount,
        charge=charge,
        currency=currency,
        subscription_status=status_value,
        prepaid_funding_before=funding_before,
        prepaid_funding_after=funding_after,
        postpaid_receivables=receivables,
        collection_blocking_balance=blocking,
        shortfall=shortfall,
        allowed=allowed,
        rejection_reason=rejection_reason,
        adjustment_preview=adjustment_preview,
        fingerprint=fingerprint,
    )


def list_available_addons(
    db: Session, customer: dict, subscription_id: str
) -> dict | None:
    """Add-ons the customer can buy for this service, plus active ones."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        return None

    options = [
        _serialize_option(link, add_on)
        for link, add_on in _offer_links(db, subscription.offer_id)
    ]

    active_rows = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription.id)
        .all()
    )
    now = datetime.now(UTC)

    def _is_expired(end_at) -> bool:
        if end_at is None:
            return False
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=UTC)
        return end_at < now

    active = [
        {
            "id": str(sa.id),
            "add_on_id": str(sa.add_on_id),
            "name": add_on.name,
            "quantity": int(sa.quantity or 1),
            "addon_type": getattr(add_on.addon_type, "value", str(add_on.addon_type)),
            # Data bundles: GB granted per unit (null for non-data add-ons).
            "grant_gb": add_on.grant_gb,
            "total_grant_gb": (
                add_on.grant_gb * int(sa.quantity or 1)
                if add_on.grant_gb is not None
                else None
            ),
            "starts_at": sa.start_at,
            # Null = lasts until the end of the billing period it was bought in.
            "expires_at": sa.end_at,
            "validity_days": add_on.validity_days,
            "is_expired": _is_expired(sa.end_at),
        }
        for sa, add_on in active_rows
    ]

    return {
        "available": options,
        "active": active,
    }


def _resolve_purchasable(
    db: Session, subscription, add_on_id: str, quantity: int
) -> tuple[AddOn, AddOnPrice | None, Decimal, str]:
    """Validate the add-on is offered for this subscription and the quantity is
    in range; return (add_on, unit_amount, currency). Raises ValueError."""
    links = _offer_links(db, subscription.offer_id)
    match = next(
        ((link, ao) for link, ao in links if str(ao.id) == str(add_on_id)), None
    )
    if match is None:
        raise ValueError("Add-on is not available for this service")
    link, add_on = match
    min_q = int(link.min_quantity or 1)
    if quantity < min_q:
        raise ValueError(f"Minimum quantity is {min_q}")
    if link.max_quantity is not None and quantity > int(link.max_quantity):
        raise ValueError(f"Maximum quantity is {link.max_quantity}")
    price = _addon_active_price_record(add_on)
    amount = (
        round_money(to_decimal(price.amount or 0))
        if price is not None
        else Decimal("0.00")
    )
    currency = str(price.currency or "NGN") if price is not None else "NGN"
    return add_on, price, amount, currency


def get_addon_quote(
    db: Session,
    customer: dict,
    subscription_id: str,
    add_on_id: str,
    quantity: int = 1,
) -> dict | None:
    """Owner preview for one add-on purchase and its exact financial result."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        return None
    try:
        return _build_purchase_preview(db, subscription, add_on_id, quantity).as_dict()
    except AccountAdjustmentError as exc:
        raise _adjustment_http_error(exc) from exc


_IDEMPOTENCY_SCOPE = "addon_purchase"


def _find_key(db: Session, key: str) -> IdempotencyKey | None:
    return db.scalars(
        select(IdempotencyKey).where(
            IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
    ).first()


@dataclass(frozen=True)
class PurchaseAddonCommand:
    """Typed customer confirmation for one exact preview."""

    account_id: UUID
    subscription_id: UUID
    add_on_id: UUID
    quantity: int
    preview_fingerprint: str


@dataclass(frozen=True)
class AddonPurchaseOutcome:
    """Stable purchase outcome returned by the add-on transition owner."""

    success: bool
    reason: str | None = None
    subscription_status: str | None = None
    replayed: bool = False
    subscription_add_on_id: UUID | None = None
    add_on_name: str | None = None
    quantity: int | None = None
    charge: Decimal | None = None
    currency: str = "NGN"
    prepaid_funding_before: Decimal | None = None
    prepaid_funding_after: Decimal | None = None
    postpaid_receivables: Decimal | None = None
    collection_blocking_balance: Decimal | None = None
    shortfall: Decimal | None = None
    account_adjustment_id: UUID | None = None
    ledger_entry_id: UUID | None = None
    preview_fingerprint: str | None = None
    access_consequence: str | None = None
    recurring_terms_event_id: UUID | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize only at the adapter boundary."""

        return {
            "success": self.success,
            "reason": self.reason,
            "subscription_status": self.subscription_status,
            "replayed": self.replayed,
            "subscription_add_on_id": (
                str(self.subscription_add_on_id)
                if self.subscription_add_on_id is not None
                else None
            ),
            "add_on_name": self.add_on_name,
            "quantity": self.quantity,
            "charge": self.charge,
            "currency": self.currency,
            "prepaid_funding_before": self.prepaid_funding_before,
            "prepaid_funding_after": self.prepaid_funding_after,
            "postpaid_receivables": self.postpaid_receivables,
            "collection_blocking_balance": self.collection_blocking_balance,
            "shortfall": self.shortfall,
            "account_adjustment_id": (
                str(self.account_adjustment_id)
                if self.account_adjustment_id is not None
                else None
            ),
            "ledger_entry_id": (
                str(self.ledger_entry_id) if self.ledger_entry_id is not None else None
            ),
            "preview_fingerprint": self.preview_fingerprint,
            "access_consequence": self.access_consequence,
        }


def _replay_addon_result(
    db: Session,
    ref_id: str | None,
    *,
    preview_fingerprint: str,
) -> AddonPurchaseOutcome:
    sub_add_on = db.get(SubscriptionAddOn, coerce_uuid(ref_id)) if ref_id else None
    if sub_add_on is None:
        raise _error(
            "incomplete_idempotency_evidence",
            "Add-on idempotency record has no purchase.",
        )
    if sub_add_on.purchase_preview_fingerprint != preview_fingerprint:
        raise _error(
            "idempotency_conflict",
            "Idempotency key was used for another add-on preview.",
        )
    adjustment = sub_add_on.account_adjustment
    return AddonPurchaseOutcome(
        success=True,
        replayed=True,
        subscription_add_on_id=sub_add_on.id,
        quantity=int(getattr(sub_add_on, "quantity", 1) or 1),
        charge=round_money(adjustment.amount) if adjustment else Decimal("0.00"),
        currency=adjustment.currency if adjustment else "NGN",
        prepaid_funding_before=(
            round_money(adjustment.prepaid_funding_before) if adjustment else None
        ),
        prepaid_funding_after=(
            round_money(adjustment.prepaid_funding_after) if adjustment else None
        ),
        postpaid_receivables=(
            round_money(adjustment.postpaid_receivables) if adjustment else None
        ),
        collection_blocking_balance=(
            round_money(adjustment.collection_blocking_balance) if adjustment else None
        ),
        account_adjustment_id=adjustment.id if adjustment else None,
        ledger_entry_id=adjustment.ledger_entry_id if adjustment else None,
        preview_fingerprint=sub_add_on.purchase_preview_fingerprint,
        access_consequence="none_addon_purchase_only",
    )


def confirm_addon_purchase(
    db: Session,
    command: PurchaseAddonCommand,
    *,
    context: CommandContext,
) -> AddonPurchaseOutcome:
    """Confirm entitlement, debit, evidence, and owner output atomically."""

    return execute_owner_command(
        db,
        definition=_PURCHASE_COMMAND,
        context=context,
        operation=lambda: _confirm_addon_purchase(
            db,
            command=command,
            context=context,
        ),
    )


def _confirm_addon_purchase(
    db: Session,
    *,
    command: PurchaseAddonCommand,
    context: CommandContext,
) -> AddonPurchaseOutcome:
    if not context.idempotency_key or len(context.idempotency_key.strip()) < 16:
        raise _error(
            "missing_idempotency_key",
            "A stable idempotency key is required.",
        )
    if len(command.preview_fingerprint.strip()) != 64:
        raise _error(
            "invalid_preview_fingerprint",
            "A valid add-on preview fingerprint is required.",
        )

    account_id = str(command.account_id)
    lock_account(db, account_id)
    subscription = db.execute(
        select(Subscription)
        .where(
            Subscription.id == command.subscription_id,
            Subscription.subscriber_id == command.account_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if subscription is None:
        raise _error("service_not_found", "Service not found.")

    prior = _find_key(db, context.idempotency_key)
    if prior is not None:
        if str(prior.account_id) != account_id:
            raise _error(
                "idempotency_conflict",
                "Idempotency key is already used by another account.",
            )
        return _replay_addon_result(
            db,
            prior.ref_id,
            preview_fingerprint=command.preview_fingerprint,
        )

    try:
        preview = _build_purchase_preview(
            db,
            subscription,
            str(command.add_on_id),
            command.quantity,
        )
    except ValueError as exc:
        raise _error("addon_not_available", str(exc)) from exc
    if preview.fingerprint != command.preview_fingerprint:
        raise _error(
            "stale_preview",
            "Add-on price, service, or funding changed; preview again.",
        )
    if not preview.allowed:
        return AddonPurchaseOutcome(
            success=False,
            reason=preview.rejection_reason,
            subscription_status=preview.subscription_status,
            charge=preview.charge,
            prepaid_funding_before=preview.prepaid_funding_before,
            prepaid_funding_after=preview.prepaid_funding_after,
            postpaid_receivables=preview.postpaid_receivables,
            collection_blocking_balance=preview.collection_blocking_balance,
            shortfall=preview.shortfall,
            currency=preview.currency,
            preview_fingerprint=preview.fingerprint,
        )

    purchased_at = datetime.now(UTC)
    sub_add_on = SubscriptionAddOn(
        subscription_id=subscription.id,
        add_on_id=coerce_uuid(str(preview.add_on.id)),
        quantity=command.quantity,
        start_at=purchased_at,
        purchase_preview_fingerprint=preview.fingerprint,
        purchase_idempotency_key=context.idempotency_key,
    )
    db.add(sub_add_on)
    db.flush()

    adjustment_result = None
    if preview.adjustment_preview is not None:
        adjustment_preview = preview.adjustment_preview
        try:
            adjustment_result = stage_account_adjustment(
                db,
                ConfirmAccountAdjustmentCommand(
                    context=CommandContext.system(
                        actor=f"user:{account_id}",
                        scope=ACCOUNT_ADJUSTMENT_SCOPE,
                        reason="Customer confirmed an add-on purchase debit",
                        idempotency_key=context.idempotency_key,
                    ),
                    confirmation=AccountAdjustmentConfirm(
                        account_id=adjustment_preview.account_id,
                        category=adjustment_preview.category,
                        amount=adjustment_preview.amount,
                        currency=adjustment_preview.currency,
                        memo=adjustment_preview.memo,
                        reason=adjustment_preview.reason,
                        preview_fingerprint=adjustment_preview.fingerprint,
                        idempotency_key=context.idempotency_key,
                    ),
                    origin=AccountAdjustmentOrigin.addon_purchase,
                    origin_ref=(
                        f"{subscription.id}:{preview.add_on.id}:{command.quantity}"
                    ),
                ),
            )
        except AccountAdjustmentError:
            raise
        sub_add_on.account_adjustment_id = adjustment_result.adjustment.id

    # Data top-up: stamp its validity window and credit the purchased GB to the
    # current period's quota bucket.
    if preview.add_on.grant_gb:
        from app.services.usage import grant_data_topup

        grant_data_topup(db, subscription, sub_add_on, preview.add_on)

    db.add(
        IdempotencyKey(
            scope=_IDEMPOTENCY_SCOPE,
            key=context.idempotency_key,
            account_id=subscription.subscriber_id,
            ref_id=str(sub_add_on.id),
        )
    )
    AuditEvents.stage(
        db,
        AuditEventCreate(
            actor_type=AuditActorType.user,
            actor_id=account_id,
            action="confirm",
            entity_type="subscription_add_on_purchase",
            entity_id=str(sub_add_on.id),
            metadata_={
                "subscription_id": str(subscription.id),
                "add_on_id": str(preview.add_on.id),
                "quantity": command.quantity,
                "charge": str(preview.charge),
                "currency": preview.currency,
                "prepaid_funding_before": str(preview.prepaid_funding_before),
                "prepaid_funding_after": str(preview.prepaid_funding_after),
                "postpaid_receivables": str(preview.postpaid_receivables),
                "preview_fingerprint": preview.fingerprint,
                "account_adjustment_id": (
                    str(adjustment_result.adjustment.id)
                    if adjustment_result is not None
                    else None
                ),
                "ledger_entry_id": (
                    str(adjustment_result.ledger_entry.id)
                    if adjustment_result is not None
                    else None
                ),
                "access_consequence": "none_addon_purchase_only",
            },
        ),
    )

    recurring_terms_event_id = None
    price = preview.add_on_price
    if price is not None and price.price_type is PriceType.recurring:
        recurring_terms_event_id = stage_owner_output(
            db,
            OwnerOutputEnvelope(
                event_type=EventType.custom,
                producer_owner=OWNER,
                source_kind="subscription_add_on",
                source_id=sub_add_on.id,
                schema_version=1,
                occurred_at=purchased_at,
            ),
            {
                "output": RECURRING_TERMS_ADDED_OUTPUT,
                "account_id": account_id,
                "subscription_id": str(subscription.id),
                "subscription_add_on_id": str(sub_add_on.id),
                "add_on_id": str(preview.add_on.id),
                "add_on_price_id": str(price.id),
                "description": str(preview.add_on.name),
                "quantity": str(command.quantity),
                "unit_price": str(preview.unit_amount),
                "currency": preview.currency.strip().upper(),
                "billing_cycle": (
                    price.billing_cycle.value
                    if price.billing_cycle is not None
                    else None
                ),
                "purchased_at": purchased_at.isoformat(),
            },
            context=context,
            account_id=command.account_id,
            subscription_id=command.subscription_id,
        )

    db.flush()
    return AddonPurchaseOutcome(
        success=True,
        subscription_add_on_id=sub_add_on.id,
        add_on_name=preview.add_on.name,
        quantity=command.quantity,
        charge=preview.charge,
        currency=preview.currency,
        prepaid_funding_before=preview.prepaid_funding_before,
        prepaid_funding_after=preview.prepaid_funding_after,
        postpaid_receivables=preview.postpaid_receivables,
        collection_blocking_balance=preview.collection_blocking_balance,
        account_adjustment_id=(
            adjustment_result.adjustment.id if adjustment_result is not None else None
        ),
        ledger_entry_id=(
            adjustment_result.ledger_entry.id if adjustment_result is not None else None
        ),
        preview_fingerprint=preview.fingerprint,
        access_consequence="none_addon_purchase_only",
        recurring_terms_event_id=recurring_terms_event_id,
    )


def cancel_addon(
    db: Session, customer: dict, subscription_id: str, sub_add_on_id: str
) -> bool:
    """End one of the caller's add-ons (stops recurring billing from the next
    cycle). Returns False if the add-on isn't found on the caller's service or is
    already ended. No refund is issued — the customer keeps it for the cycle
    already billed."""
    subscription = _owned_subscription(db, customer, subscription_id)
    if subscription is None:
        return False
    sub_add_on = db.get(SubscriptionAddOn, coerce_uuid(sub_add_on_id))
    if (
        sub_add_on is None
        or str(sub_add_on.subscription_id) != str(subscription.id)
        or sub_add_on.end_at is not None
    ):
        return False
    sub_add_on.end_at = datetime.now(UTC)
    db.commit()
    return True
