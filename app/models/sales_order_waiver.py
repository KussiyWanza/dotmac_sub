"""Order waiver evidence — a commercial decision, never a payment.

A waiver says Dotmac will not pursue what this order is worth. It used to be
expressed as ``SalesOrder.payment_status = waived`` through the generic order
edit, which is the same field that let an operator manufacture funding: the
decision carried no actor, no grounds, no identity, and it sat in the field
that means "money arrived".

This table is the decision. It deliberately does **not** touch any payment
field, does not create settlement evidence, and never stages
``sales_order.funding_satisfied`` — a waived order was not paid, and nothing
downstream may treat it as though it were.

Historical ``payment_status = waived`` rows stay readable as the record of what
the old path did. Nothing writes that value any more.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class WaiverState(enum.Enum):
    """A waiver is granted, and may later be revoked. Both are decisions."""

    active = "active"
    revoked = "revoked"


class SalesOrderWaiver(Base):
    """One grant of an order waiver, with its revocation if it happened."""

    __tablename__ = "sales_order_waivers"
    __table_args__ = (
        # Idempotency identity for the grant. A replayed grant returns the
        # original row rather than waiving the order twice.
        UniqueConstraint(
            "sales_order_id",
            "grant_idempotency_key",
            name="uq_sales_order_waivers_grant_idempotency",
        ),
        Index("ix_sales_order_waivers_order", "sales_order_id"),
        Index("ix_sales_order_waivers_state", "sales_order_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sales_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_orders.id"), nullable=False
    )
    state: Mapped[WaiverState] = mapped_column(
        Enum(WaiverState, name="salesorderwaiverstate"),
        nullable=False,
        default=WaiverState.active,
    )

    # What was waived, captured at the decision. Exact money, and deliberately
    # a snapshot: the order's total can change later, and the decision must
    # still say what was actually waived at the time it was taken.
    waived_amount: Mapped[object] = mapped_column(Numeric(20, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)

    #: Open registered vocabulary (ADR-0008): a product names its own grounds
    #: without a migration. Validated by the service against the registry.
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_text: Mapped[str | None] = mapped_column(Text)

    #: WHO decided. A waiver with no accountable actor is not evidence, which
    #: is exactly what the old payment_status edit produced.
    granted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    grant_idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Digest of the grant inputs. A replayed key carrying different inputs is
    #: a conflict, not a second waiver and not a silent no-op.
    grant_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    revoked_by: Mapped[str | None] = mapped_column(String(255))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_reason_code: Mapped[str | None] = mapped_column(String(80))
    revoke_reason_text: Mapped[str | None] = mapped_column(Text)
    revoke_idempotency_key: Mapped[str | None] = mapped_column(String(255))

    command_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
