"""Grant and revoke an order waiver. A concern of ``sales.orders``.

Closing the manufacture-funding hole refused ``payment_status`` on the generic
order edit, which also removed the only path to a waiver — waiver travelled on
the same field as settlement. This module is the replacement, and it is
deliberately narrower than the thing it replaces.

What a waiver is: a recorded decision not to pursue what an order is worth,
naming the actor, the grounds, the exact amount and an idempotency identity.

What a waiver is **not**, and what this module therefore never does:

* it never writes ``payment_status``, ``amount_paid`` or ``paid_at`` — a waived
  order was not paid, and it must not be reported as though it were;
* it never stages ``sales_order.funding_satisfied``, so no subscription and no
  provisioning order can follow from a waiver;
* it never creates settlement evidence — no payment row, no ledger entry;
* it never uses the ``funding_authority`` escape hatch. That hatch exists for
  callers asserting money arrived. A waiver asserts the opposite.

Extended credit is deliberately out of scope. Extending credit preserves a
receivable, which is a Billing and Collections concern with its own ownership;
folding it in here would put a live receivable behind a sales-side decision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.sales import SalesOrder
from app.models.sales_order_waiver import SalesOrderWaiver, WaiverState
from app.services.audit_adapter import record_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

OWNER = "sales.orders"

AUDIT_WAIVER_GRANTED = "sales_order.waiver_granted"
AUDIT_WAIVER_REVOKED = "sales_order.waiver_revoked"

_GRANT_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="order waiver decision evidence",
    name="grant_order_waiver",
)
_REVOKE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="order waiver decision evidence",
    name="revoke_order_waiver",
)

#: Open registered vocabulary (ADR-0008). Grounds are declared here rather than
#: in an enum so a product adds one without a migration — but never free text
#: alone, because "why was this order waived" must be answerable later.
WAIVER_REASON_CODES: frozenset[str] = frozenset(
    {
        "goodwill",
        "service_failure",
        "billing_error",
        "contractual_allowance",
        "uncollectible",
        "internal_account",
        "promotional",
    }
)

REVOCATION_REASON_CODES: frozenset[str] = frozenset(
    {
        "granted_in_error",
        "customer_disputed",
        "policy_change",
        "superseded",
    }
)


class OrderWaiverError(DomainError):
    """Fail-closed order-waiver error."""


def _error(suffix: str, message: str, **details: object) -> OrderWaiverError:
    return OrderWaiverError(
        code=f"sales.order_waiver.{suffix}", message=message, details=dict(details)
    )


def _grant_fingerprint(
    *, sales_order_id: UUID, amount: Decimal, currency: str, reason_code: str
) -> str:
    """Digest of the grant inputs.

    Excludes actor and instant on purpose: a retried request is the same
    decision, while a changed amount or grounds must conflict.
    """
    payload = json.dumps(
        {
            "sales_order_id": str(sales_order_id),
            "waived_amount": str(amount),
            "currency": currency.upper(),
            "reason_code": reason_code,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def active_waiver(db: Session, sales_order_id: UUID) -> SalesOrderWaiver | None:
    """The order's active waiver, if it has one.

    Read-only, and safe to call from the sales-order service: it is how the
    commercial-mutation guard knows to refuse.
    """
    return (
        db.query(SalesOrderWaiver)
        .filter(
            SalesOrderWaiver.sales_order_id == sales_order_id,
            SalesOrderWaiver.state == WaiverState.active,
        )
        .one_or_none()
    )


class SalesOrderWaivers:
    """Public command surface for order waivers."""

    @staticmethod
    def grant(
        db: Session,
        *,
        sales_order_id: UUID,
        waived_amount: Decimal,
        reason_code: str,
        reason_text: str | None = None,
        granted_at: datetime | None = None,
        context: CommandContext,
    ) -> SalesOrderWaiver:
        """Record a waiver on one order.

        Idempotent per ``(sales_order_id, context.idempotency_key)``. A replay
        with the same inputs returns the original waiver; a replay with
        different inputs is a conflict, never a second waiver.
        """
        if context.idempotency_key is None:
            raise _error(
                "missing_idempotency_key",
                "A waiver requires an idempotency identity.",
            )
        if reason_code not in WAIVER_REASON_CODES:
            raise _error(
                "unregistered_reason_code",
                "A waiver must state registered grounds.",
                reason_code=reason_code,
                registered=sorted(WAIVER_REASON_CODES),
            )
        if waived_amount <= Decimal("0"):
            raise _error(
                "non_positive_amount",
                "A waiver must waive a positive amount.",
                waived_amount=str(waived_amount),
            )
        if granted_at is not None and granted_at.tzinfo is None:
            raise _error(
                "invalid_decision_instant",
                "Waiver evidence requires a timezone-aware instant.",
            )

        return execute_owner_command(
            db,
            definition=_GRANT_COMMAND,
            context=context,
            operation=lambda: SalesOrderWaivers._grant(
                db,
                sales_order_id=sales_order_id,
                waived_amount=waived_amount,
                reason_code=reason_code,
                reason_text=reason_text,
                granted_at=granted_at or datetime.now(UTC),
                context=context,
            ),
        )

    @staticmethod
    def _grant(
        db: Session,
        *,
        sales_order_id: UUID,
        waived_amount: Decimal,
        reason_code: str,
        reason_text: str | None,
        granted_at: datetime,
        context: CommandContext,
    ) -> SalesOrderWaiver:
        # Lock the order: two concurrent grants must serialise, or both see no
        # active waiver and both create one.
        sales_order = (
            db.query(SalesOrder)
            .filter(SalesOrder.id == sales_order_id)
            .with_for_update()
            .one_or_none()
        )
        if sales_order is None:
            raise _error(
                "sales_order_not_found",
                "No sales order for this waiver.",
                sales_order_id=str(sales_order_id),
            )

        currency = sales_order.currency or "NGN"
        fingerprint = _grant_fingerprint(
            sales_order_id=sales_order_id,
            amount=waived_amount,
            currency=currency,
            reason_code=reason_code,
        )

        existing = (
            db.query(SalesOrderWaiver)
            .filter(
                SalesOrderWaiver.sales_order_id == sales_order_id,
                SalesOrderWaiver.grant_idempotency_key == context.idempotency_key,
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.grant_fingerprint != fingerprint:
                raise _error(
                    "idempotency_conflict",
                    "This idempotency key already granted a different waiver.",
                    idempotency_key=context.idempotency_key,
                )
            return existing

        if active_waiver(db, sales_order_id) is not None:
            raise _error(
                "waiver_already_active",
                "This order already has an active waiver. Revoke it first.",
                sales_order_id=str(sales_order_id),
            )

        waiver = SalesOrderWaiver(
            sales_order_id=sales_order_id,
            state=WaiverState.active,
            waived_amount=waived_amount,
            currency=currency,
            reason_code=reason_code,
            reason_text=reason_text,
            granted_by=context.actor,
            granted_at=granted_at,
            grant_idempotency_key=context.idempotency_key,
            grant_fingerprint=fingerprint,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
        )
        db.add(waiver)
        db.flush()

        # Note what is absent: no payment_status write, no funding event, no
        # settlement row. The waiver is the whole effect.
        record_audit_event(
            db,
            action=AUDIT_WAIVER_GRANTED,
            entity_type="sales_order",
            entity_id=str(sales_order_id),
            actor_type=AuditActorType.user,
            actor_id=context.actor,
            metadata={
                "waiver_id": str(waiver.id),
                "waived_amount": str(waived_amount),
                "currency": currency,
                "reason_code": reason_code,
            },
        )
        return waiver

    @staticmethod
    def revoke(
        db: Session,
        *,
        sales_order_id: UUID,
        reason_code: str,
        reason_text: str | None = None,
        context: CommandContext,
    ) -> SalesOrderWaiver:
        """Withdraw an active waiver. The order becomes pursuable again."""
        if context.idempotency_key is None:
            raise _error(
                "missing_idempotency_key",
                "A revocation requires an idempotency identity.",
            )
        if reason_code not in REVOCATION_REASON_CODES:
            raise _error(
                "unregistered_reason_code",
                "A revocation must state registered grounds.",
                reason_code=reason_code,
                registered=sorted(REVOCATION_REASON_CODES),
            )
        return execute_owner_command(
            db,
            definition=_REVOKE_COMMAND,
            context=context,
            operation=lambda: SalesOrderWaivers._revoke(
                db,
                sales_order_id=sales_order_id,
                reason_code=reason_code,
                reason_text=reason_text,
                context=context,
            ),
        )

    @staticmethod
    def _revoke(
        db: Session,
        *,
        sales_order_id: UUID,
        reason_code: str,
        reason_text: str | None,
        context: CommandContext,
    ) -> SalesOrderWaiver:
        sales_order = (
            db.query(SalesOrder)
            .filter(SalesOrder.id == sales_order_id)
            .with_for_update()
            .one_or_none()
        )
        if sales_order is None:
            raise _error(
                "sales_order_not_found",
                "No sales order for this revocation.",
                sales_order_id=str(sales_order_id),
            )

        waiver = active_waiver(db, sales_order_id)
        if waiver is None:
            # Replay of an already-applied revocation returns the row it
            # revoked, so a retried request is not an error.
            already = (
                db.query(SalesOrderWaiver)
                .filter(
                    SalesOrderWaiver.sales_order_id == sales_order_id,
                    SalesOrderWaiver.revoke_idempotency_key == context.idempotency_key,
                )
                .one_or_none()
            )
            if already is not None:
                return already
            raise _error(
                "no_active_waiver",
                "This order has no active waiver to revoke.",
                sales_order_id=str(sales_order_id),
            )

        waiver.state = WaiverState.revoked
        waiver.revoked_by = context.actor
        waiver.revoked_at = datetime.now(UTC)
        waiver.revoke_reason_code = reason_code
        waiver.revoke_reason_text = reason_text
        waiver.revoke_idempotency_key = context.idempotency_key
        db.flush()

        record_audit_event(
            db,
            action=AUDIT_WAIVER_REVOKED,
            entity_type="sales_order",
            entity_id=str(sales_order_id),
            actor_type=AuditActorType.user,
            actor_id=context.actor,
            metadata={
                "waiver_id": str(waiver.id),
                "reason_code": reason_code,
            },
        )
        return waiver


sales_order_waivers = SalesOrderWaivers()
