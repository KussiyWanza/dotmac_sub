"""Reason-scoped collections cases (ADR 0007 Phase 5, section 8).

One case tracks one account/subscription/reason as policy requires: warning
and escalation states, exact durable timers (owned via ``runtime.durable
timers``), consequence-request idempotency, and close/reopen evidence.

The case owner never mutates subscription or RADIUS state. It emits a
reason-scoped consequence request that only ``access.subscription_lifecycle``
may apply.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.billing_contract import BillingRecordAuthority


class CollectionsReason(enum.Enum):
    """Why a case exists. Prepaid and postpaid keep distinct policy inputs."""

    postpaid_overdue = "postpaid_overdue"
    prepaid_underfunded = "prepaid_underfunded"


class CollectionsCaseState(enum.Enum):
    """Case workflow states."""

    open = "open"
    warned = "warned"
    escalated = "escalated"
    consequence_requested = "consequence_requested"
    closed = "closed"


_reason_enum = Enum(CollectionsReason, name="collectionsreason")
_case_state_enum = Enum(CollectionsCaseState, name="collectionscasestate")
_authority_enum = Enum(BillingRecordAuthority, name="billingrecordauthority")


class CollectionsCase(Base):
    """One reason-scoped collections workflow for one account/subscription."""

    __tablename__ = "collections_cases"
    __table_args__ = (
        # One live case per (account, subscription, reason).
        Index(
            "uq_collections_case_live",
            "account_id",
            "subscription_id",
            "reason",
            unique=True,
            postgresql_where=text("state != 'closed'"),
            sqlite_where=text("state != 'closed'"),
        ),
        Index("ix_collections_case_account", "account_id", "state"),
        UniqueConstraint(
            "consequence_idempotency_key",
            name="uq_collections_case_consequence_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    reason: Mapped[CollectionsReason] = mapped_column(_reason_enum, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    authority: Mapped[BillingRecordAuthority] = mapped_column(
        _authority_enum, nullable=False, default=BillingRecordAuthority.shadow
    )

    state: Mapped[CollectionsCaseState] = mapped_column(
        _case_state_enum, nullable=False, default=CollectionsCaseState.open
    )

    # The exact financial fact that opened/advanced the case.
    source_kind: Mapped[str] = mapped_column(String(80), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consequence_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    # Consequence requests are idempotent; access applies at most one per key.
    consequence_idempotency_key: Mapped[str | None] = mapped_column(String(200))
    consequence_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)

    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
