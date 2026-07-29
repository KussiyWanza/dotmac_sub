"""Shadow-only recurring add-on contract migration output (ADR 0007).

This migration owner does not decide prices and does not charge customers. It
captures the exact legacy ``SubscriptionAddOn`` plus active recurring catalog
price facts for one future service period, binds them to a reviewable
fingerprint, and stages a durable owner output. ``billing.contracts`` consumes
that output; callers never write contract lines directly.

The producer is intentionally temporary. Live add-on purchase, cancellation,
route, sales, admin, and remediation writers must eventually emit the same
typed billing-terms output atomically with their own state transition. Until
those writers are migrated, this command is backfill evidence only and cannot
move billing authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_contract import (
    BillingContract,
    BillingContractLine,
    BillingContractSourceKind,
    BillingContractVersion,
    BillingContractVersionStatus,
    ChargeComponent,
)
from app.models.catalog import AddOn, AddOnPrice, PriceType, SubscriptionAddOn
from app.models.event_store import EventStore
from app.models.idempotency import IdempotencyKey
from app.models.sales import SalesOrderLine
from app.services.billing.cadence import Interval, service_period
from app.services.billing.contracts import BillingContracts
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import OwnerOutputEnvelope, stage_owner_output
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "billing.addon_contract_backfill"
OUTPUT = "billing.addon_contract_backfill.captured"
_IDEMPOTENCY_SCOPE = "billing:addon-contract-backfill"

_CAPTURE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="recurring add-on contract migration snapshot",
    name="capture_recurring_addon_contract_snapshot",
)


class AddonContractBackfillError(DomainError):
    """Fail-closed add-on contract backfill error."""


def _error(suffix: str, message: str, **details: object) -> AddonContractBackfillError:
    return AddonContractBackfillError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RecurringAddonTermSnapshot:
    """Exact legacy facts proposed as one recurring contract line."""

    subscription_add_on_id: UUID
    add_on_id: UUID
    add_on_price_id: UUID
    description: str
    quantity: Decimal
    unit_price: Decimal
    currency: str
    source_started_at: datetime | None
    source_ends_at: datetime | None


@dataclass(frozen=True)
class RecurringAddonBackfillPreview:
    """Reviewable source snapshot for one future contract boundary."""

    account_id: UUID
    subscription_id: UUID
    contract_id: UUID
    current_contract_version_id: UUID
    sales_order_id: UUID
    target_period: Interval
    terms: tuple[RecurringAddonTermSnapshot, ...]
    fingerprint: str
    change_required: bool


@dataclass(frozen=True)
class CaptureRecurringAddonBackfillCommand:
    """Confirm one exact preview; a changed source requires a new preview."""

    subscription_id: UUID
    period_index: int
    preview_fingerprint: str


@dataclass(frozen=True)
class RecurringAddonBackfillCaptureResult:
    """Durable output identity for a fresh capture or an exact replay."""

    event_id: UUID
    preview_fingerprint: str
    recurring_addon_count: int
    replayed: bool


def _fingerprint(
    *,
    contract: BillingContract,
    current: BillingContractVersion,
    sales_order_id: UUID,
    target_period: Interval,
    terms: tuple[RecurringAddonTermSnapshot, ...],
) -> str:
    payload = {
        "policy_version": "billing-addon-contract-backfill-v1",
        "account_id": str(contract.account_id),
        "subscription_id": str(contract.subscription_id),
        "contract_id": str(contract.id),
        "current_contract_version_id": str(current.id),
        "current_contract_version": current.version,
        "sales_order_id": str(sales_order_id),
        "target_period_start": target_period.starts_at.isoformat(),
        "target_period_end": target_period.ends_at.isoformat(),
        "terms": [
            {
                **asdict(term),
                "subscription_add_on_id": str(term.subscription_add_on_id),
                "add_on_id": str(term.add_on_id),
                "add_on_price_id": str(term.add_on_price_id),
                "quantity": str(term.quantity),
                "unit_price": str(term.unit_price),
                "source_started_at": (
                    term.source_started_at.isoformat()
                    if term.source_started_at is not None
                    else None
                ),
                "source_ends_at": (
                    term.source_ends_at.isoformat()
                    if term.source_ends_at is not None
                    else None
                ),
            }
            for term in terms
        ],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _sales_order_anchor(db: Session, *, contract_id: UUID) -> UUID:
    sales_order_id = db.execute(
        select(SalesOrderLine.sales_order_id)
        .join(
            BillingContractVersion,
            BillingContractVersion.source_id == SalesOrderLine.id,
        )
        .where(
            BillingContractVersion.contract_id == contract_id,
            BillingContractVersion.source_kind
            == BillingContractSourceKind.sales_order_line,
        )
        .order_by(BillingContractVersion.version.asc())
        .limit(1)
    ).scalar_one_or_none()
    if sales_order_id is None:
        raise _error(
            "missing_sales_order_anchor",
            "The shadow contract has no structural sales-order-line anchor.",
            contract_id=str(contract_id),
        )
    return sales_order_id


def _active_terms(
    db: Session,
    *,
    subscription_id: UUID,
    target_period: Interval,
    currency: str,
) -> tuple[RecurringAddonTermSnapshot, ...]:
    rows = db.execute(
        select(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .where(SubscriptionAddOn.subscription_id == subscription_id)
        .order_by(SubscriptionAddOn.id)
    ).all()
    candidates: list[
        tuple[SubscriptionAddOn, AddOn, datetime | None, datetime | None]
    ] = []
    for subscription_add_on, add_on in rows:
        started_at = _utc(subscription_add_on.start_at)
        ends_at = _utc(subscription_add_on.end_at)
        if (started_at is not None and started_at >= target_period.ends_at) or (
            ends_at is not None and ends_at <= target_period.starts_at
        ):
            continue
        candidates.append((subscription_add_on, add_on, started_at, ends_at))

    prices_by_addon: dict[UUID, list[AddOnPrice]] = {}
    if candidates:
        price_rows = (
            db.execute(
                select(AddOnPrice)
                .where(
                    AddOnPrice.add_on_id.in_(
                        {add_on.id for _, add_on, _, _ in candidates}
                    ),
                    AddOnPrice.price_type == PriceType.recurring,
                    AddOnPrice.is_active.is_(True),
                )
                .order_by(AddOnPrice.add_on_id, AddOnPrice.id)
            )
            .scalars()
            .all()
        )
        for price in price_rows:
            prices_by_addon.setdefault(price.add_on_id, []).append(price)

    terms: list[RecurringAddonTermSnapshot] = []
    for subscription_add_on, add_on, started_at, ends_at in candidates:
        prices = prices_by_addon.get(add_on.id, [])
        if not prices:
            continue
        if len(prices) != 1:
            raise _error(
                "ambiguous_recurring_price",
                "A recurring add-on must have exactly one active recurring price.",
                subscription_add_on_id=str(subscription_add_on.id),
                active_price_count=len(prices),
            )
        if (started_at is not None and started_at > target_period.starts_at) or (
            ends_at is not None and ends_at < target_period.ends_at
        ):
            raise _error(
                "partial_period_addon",
                "A partial-period add-on needs an explicit proration term version.",
                subscription_add_on_id=str(subscription_add_on.id),
                target_period_start=target_period.starts_at.isoformat(),
                target_period_end=target_period.ends_at.isoformat(),
            )

        price = prices[0]
        price_currency = str(price.currency or "").strip().upper()
        if price_currency != currency:
            raise _error(
                "mixed_currency_addon",
                "The recurring add-on price differs from the contract currency.",
                subscription_add_on_id=str(subscription_add_on.id),
                contract_currency=currency,
                addon_currency=price_currency,
            )
        quantity = Decimal(str(subscription_add_on.quantity or 0))
        if quantity <= 0:
            raise _error(
                "invalid_addon_quantity",
                "A recurring add-on quantity must be positive.",
                subscription_add_on_id=str(subscription_add_on.id),
            )
        unit_price = Decimal(str(price.amount))
        if unit_price < 0:
            raise _error(
                "invalid_addon_price",
                "A recurring add-on price cannot be negative.",
                subscription_add_on_id=str(subscription_add_on.id),
            )
        terms.append(
            RecurringAddonTermSnapshot(
                subscription_add_on_id=subscription_add_on.id,
                add_on_id=add_on.id,
                add_on_price_id=price.id,
                description=str(add_on.name),
                quantity=quantity,
                unit_price=unit_price,
                currency=price_currency,
                source_started_at=started_at,
                source_ends_at=ends_at,
            )
        )
    return tuple(terms)


def _change_required(
    db: Session,
    *,
    current_version_id: UUID,
    terms: tuple[RecurringAddonTermSnapshot, ...],
) -> bool:
    current = list(
        db.execute(
            select(BillingContractLine).where(
                BillingContractLine.contract_version_id == current_version_id,
                BillingContractLine.charge_component == ChargeComponent.addon,
                BillingContractLine.is_finite.is_(False),
            )
        )
        .scalars()
        .all()
    )
    existing = sorted(
        (
            line.component_key,
            line.description,
            Decimal(line.quantity),
            Decimal(line.unit_price),
            line.currency,
        )
        for line in current
    )
    proposed = sorted(
        (
            str(term.subscription_add_on_id),
            term.description,
            term.quantity,
            term.unit_price,
            term.currency,
        )
        for term in terms
    )
    return existing != proposed


class BillingAddonContractBackfill:
    """Preview and emit exact migration snapshots; never charge or repair."""

    @staticmethod
    def preview(
        db: Session,
        *,
        subscription_id: UUID,
        period_index: int,
    ) -> RecurringAddonBackfillPreview:
        return BillingAddonContractBackfill._preview(
            db,
            subscription_id=subscription_id,
            period_index=period_index,
            lock=False,
        )

    @staticmethod
    def capture(
        db: Session,
        command: CaptureRecurringAddonBackfillCommand,
        *,
        context: CommandContext,
    ) -> RecurringAddonBackfillCaptureResult:
        return execute_owner_command(
            db,
            definition=_CAPTURE_COMMAND,
            context=context,
            operation=lambda: BillingAddonContractBackfill._capture(
                db, command=command, context=context
            ),
        )

    @staticmethod
    def _capture(
        db: Session,
        *,
        command: CaptureRecurringAddonBackfillCommand,
        context: CommandContext,
    ) -> RecurringAddonBackfillCaptureResult:
        if not context.idempotency_key:
            raise _error(
                "missing_idempotency_key",
                "Capturing an add-on snapshot requires a business idempotency key.",
            )
        existing = db.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
                IdempotencyKey.key == context.idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None and existing.ref_id:
            try:
                event_id = UUID(existing.ref_id)
            except ValueError as exc:
                raise _error(
                    "incomplete_idempotency_evidence",
                    "The replay key contains no valid add-on capture event id.",
                ) from exc
            event = db.execute(
                select(EventStore).where(EventStore.event_id == event_id)
            ).scalar_one_or_none()
            if event is None:
                raise _error(
                    "incomplete_idempotency_evidence",
                    "The replay key references no durable add-on capture output.",
                    event_id=str(event_id),
                )
            stored_subscription_id = str(event.payload.get("subscription_id") or "")
            stored_fingerprint = str(event.payload.get("preview_fingerprint") or "")
            if (
                stored_subscription_id != str(command.subscription_id)
                or stored_fingerprint != command.preview_fingerprint
            ):
                raise _error(
                    "idempotency_conflict",
                    "The idempotency key belongs to another add-on capture.",
                    event_id=str(event_id),
                )
            stored_terms = event.payload.get("terms")
            recurring_addon_count = (
                len(stored_terms) if isinstance(stored_terms, list) else 0
            )
            return RecurringAddonBackfillCaptureResult(
                event_id=event_id,
                preview_fingerprint=stored_fingerprint,
                recurring_addon_count=recurring_addon_count,
                replayed=True,
            )
        if len(command.preview_fingerprint) != 64:
            raise _error(
                "invalid_preview_fingerprint",
                "A valid recurring add-on preview fingerprint is required.",
            )

        preview = BillingAddonContractBackfill._preview(
            db,
            subscription_id=command.subscription_id,
            period_index=command.period_index,
            lock=True,
        )
        if preview.fingerprint != command.preview_fingerprint:
            raise _error(
                "stale_preview",
                "Recurring add-on terms changed; preview again.",
                expected_fingerprint=command.preview_fingerprint,
                actual_fingerprint=preview.fingerprint,
            )
        if not preview.change_required:
            raise _error(
                "already_captured",
                "The current contract version already contains the exact add-on terms.",
                current_contract_version_id=str(preview.current_contract_version_id),
            )

        event_id = stage_owner_output(
            db,
            OwnerOutputEnvelope(
                event_type=EventType.custom,
                producer_owner=OWNER,
                source_kind="sales_order",
                source_id=preview.sales_order_id,
                schema_version=1,
            ),
            {
                "output": OUTPUT,
                "sales_order_id": str(preview.sales_order_id),
                "account_id": str(preview.account_id),
                "subscription_id": str(preview.subscription_id),
                "contract_id": str(preview.contract_id),
                "current_contract_version_id": str(preview.current_contract_version_id),
                "target_period_start": preview.target_period.starts_at.isoformat(),
                "target_period_end": preview.target_period.ends_at.isoformat(),
                "preview_fingerprint": preview.fingerprint,
                "terms": [
                    {
                        "subscription_add_on_id": str(term.subscription_add_on_id),
                        "add_on_id": str(term.add_on_id),
                        "add_on_price_id": str(term.add_on_price_id),
                        "description": term.description,
                        "quantity": str(term.quantity),
                        "unit_price": str(term.unit_price),
                        "currency": term.currency,
                        "source_started_at": (
                            term.source_started_at.isoformat()
                            if term.source_started_at is not None
                            else None
                        ),
                        "source_ends_at": (
                            term.source_ends_at.isoformat()
                            if term.source_ends_at is not None
                            else None
                        ),
                    }
                    for term in preview.terms
                ],
            },
            context=context,
            account_id=preview.account_id,
            subscription_id=preview.subscription_id,
        )
        db.add(
            IdempotencyKey(
                scope=_IDEMPOTENCY_SCOPE,
                key=context.idempotency_key,
                account_id=preview.account_id,
                ref_id=str(event_id),
            )
        )
        db.flush()
        return RecurringAddonBackfillCaptureResult(
            event_id=event_id,
            preview_fingerprint=preview.fingerprint,
            recurring_addon_count=len(preview.terms),
            replayed=False,
        )

    @staticmethod
    def _preview(
        db: Session,
        *,
        subscription_id: UUID,
        period_index: int,
        lock: bool,
    ) -> RecurringAddonBackfillPreview:
        if period_index < 1:
            raise _error(
                "invalid_period_index",
                "Backfill must begin on a future contract service-period boundary.",
                period_index=period_index,
            )
        contract_query = select(BillingContract).where(
            BillingContract.subscription_id == subscription_id
        )
        if lock:
            contract_query = contract_query.with_for_update()
        contract = db.execute(contract_query).scalar_one_or_none()
        if contract is None:
            raise _error(
                "contract_not_found",
                "The subscription has no shadow billing contract.",
                subscription_id=str(subscription_id),
            )

        version_query = select(BillingContractVersion).where(
            BillingContractVersion.contract_id == contract.id,
            BillingContractVersion.status == BillingContractVersionStatus.effective,
            BillingContractVersion.ends_at.is_(None),
        )
        if lock:
            version_query = version_query.with_for_update()
        current = db.execute(version_query).scalar_one_or_none()
        if current is None:
            raise _error(
                "current_contract_version_not_found",
                "The subscription has no open effective contract version.",
                subscription_id=str(subscription_id),
            )

        cadence = BillingContracts.cadence_of(current)
        starts_at = _utc(current.starts_at)
        assert starts_at is not None
        target_period = service_period(
            cadence=cadence,
            contract_start=starts_at,
            index=period_index,
        )
        sales_order_id = _sales_order_anchor(db, contract_id=contract.id)
        terms = _active_terms(
            db,
            subscription_id=subscription_id,
            target_period=target_period,
            currency=current.currency,
        )
        fingerprint = _fingerprint(
            contract=contract,
            current=current,
            sales_order_id=sales_order_id,
            target_period=target_period,
            terms=terms,
        )
        return RecurringAddonBackfillPreview(
            account_id=contract.account_id,
            subscription_id=subscription_id,
            contract_id=contract.id,
            current_contract_version_id=current.id,
            sales_order_id=sales_order_id,
            target_period=target_period,
            terms=terms,
            fingerprint=fingerprint,
            change_required=_change_required(
                db,
                current_version_id=current.id,
                terms=terms,
            ),
        )


__all__ = [
    "AddonContractBackfillError",
    "BillingAddonContractBackfill",
    "CaptureRecurringAddonBackfillCommand",
    "OUTPUT",
    "RecurringAddonBackfillCaptureResult",
    "RecurringAddonBackfillPreview",
    "RecurringAddonTermSnapshot",
]
