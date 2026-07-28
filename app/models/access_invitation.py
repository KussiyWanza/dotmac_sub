"""Access invitation aggregate (docs/designs/IDENTITY_ONBOARDING_CHAIN.md).

An invitation was previously only a stateless signed capability: expiry
existed solely as a redeem-time check, so an expired invite was invisible
to the system. This aggregate records the issued → accepted / expired /
revoked lifecycle; the capability itself (signature, TTL enforcement at
redemption) remains owned by the issuing domain and stays fail-closed —
this record is lifecycle evidence, never an access grant.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AccessInvitationStatus(str, enum.Enum):
    issued = "issued"
    accepted = "accepted"
    expired = "expired"
    revoked = "revoked"


class AccessInvitationPurpose(str, enum.Enum):
    staff_invite = "staff_invite"
    reseller_invite = "reseller_invite"
    user_invite = "user_invite"
    subscriber_invite = "subscriber_invite"


class AccessInvitation(Base):
    __tablename__ = "access_invitations"
    __table_args__ = (
        Index("ix_access_invitations_principal", "principal_type", "principal_id"),
        Index("ix_access_invitations_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    principal_type: Mapped[str] = mapped_column(String(40), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AccessInvitationStatus.issued.value
    )
    email_sha256: Mapped[str | None] = mapped_column(String(64))
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
