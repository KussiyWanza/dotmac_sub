import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ErpDomainSyncCursor(Base):
    """Durable keyset cursor for Sub operational context pushed to ERP."""

    __tablename__ = "erp_domain_sync_cursors"

    domain: Mapped[str] = mapped_column(String(40), primary_key=True)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watermark_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ErpOperationalSyncState(Base):
    """Singleton admission and failure evidence, owned by operational domain sync."""

    __tablename__ = "erp_operational_sync_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_erp_sync_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    configuration_fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="ready", nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    diagnostic: Mapped[dict | None] = mapped_column(JSON)
