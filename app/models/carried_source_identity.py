"""Reviewed provenance decisions for customers native before the BSS handoff."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CarriedSourceIdentityDisposition(enum.Enum):
    """Closed reviewed outcomes that can replace carried-source ambiguity."""

    native_before_handoff = "native_before_handoff"


class CarriedSourceIdentityAdjudication(Base):
    """Immutable dual-reviewed evidence that one customer was Sub-native.

    The row does not assign a Splynx identifier and does not write money. It
    permits the opening-history resolver to use complete native Sub facts for
    an account that genuinely existed before the fixed financial handoff.
    """

    __tablename__ = "carried_source_identity_adjudications"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            name="uq_carried_source_identity_adjudications_account",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_carried_source_identity_adjudications_idempotency",
        ),
        UniqueConstraint(
            "command_id",
            name="uq_carried_source_identity_adjudications_command",
        ),
        CheckConstraint(
            "reviewed_by_id <> approved_by_id",
            name="ck_carried_source_identity_distinct_reviewers",
        ),
        CheckConstraint(
            "length(preview_fingerprint) = 64 AND "
            "length(evidence_sha256) = 64 AND "
            "length(command_fingerprint) = 64",
            name="ck_carried_source_identity_digest_lengths",
        ),
        CheckConstraint(
            "length(trim(evidence_ref)) > 0 AND length(trim(reason)) > 0",
            name="ck_carried_source_identity_review_evidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscribers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    disposition: Mapped[CarriedSourceIdentityDisposition] = mapped_column(
        Enum(
            CarriedSourceIdentityDisposition,
            name="carriedsourceidentitydisposition",
        ),
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    financial_handoff_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    account_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("system_users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    command_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class CarriedSourceIdentityAdjudicationImmutableError(RuntimeError):
    """Raised when code attempts to rewrite reviewed provenance evidence."""


@event.listens_for(CarriedSourceIdentityAdjudication, "before_update")
def _reject_carried_source_identity_update(*_args: object) -> None:
    raise CarriedSourceIdentityAdjudicationImmutableError(
        "Carried-source identity adjudications are append-only"
    )


@event.listens_for(CarriedSourceIdentityAdjudication, "before_delete")
def _reject_carried_source_identity_delete(*_args: object) -> None:
    raise CarriedSourceIdentityAdjudicationImmutableError(
        "Carried-source identity adjudications are append-only"
    )


__all__ = [
    "CarriedSourceIdentityAdjudication",
    "CarriedSourceIdentityAdjudicationImmutableError",
    "CarriedSourceIdentityDisposition",
]
