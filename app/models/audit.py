import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuditActorType(enum.Enum):
    system = "system"
    user = "user"
    api_key = "api_key"
    service = "service"


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    actor_type: Mapped[AuditActorType] = mapped_column(
        Enum(AuditActorType), default=AuditActorType.system
    )
    actor_id: Mapped[str | None] = mapped_column(String(120))
    # Human label resolved at write time (person name, API-key label). Stored,
    # not derived on read, so it survives deletion of the referenced actor
    # (actor_id is not a foreign key) and can be searched without a join.
    actor_label: Mapped[str | None] = mapped_column(String(160), index=True)
    # Optional accountability enrichment. Deliberately not a foreign key: the
    # forensic row must survive deletion of the Party it once named.
    actor_party_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), index=True
    )
    action: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(160))
    entity_id: Mapped[str | None] = mapped_column(String(120))
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    is_success: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    request_id: Mapped[str | None] = mapped_column(String(120))
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON())
    )
    # R1 expansion target. Existing rows remain NULL; every sanctioned writer
    # dual-populates this from metadata plus the forensic columns below.
    details: Mapped[dict[str, object] | None] = mapped_column(
        MutableDict.as_mutable(JSON().with_variant(JSONB(), "postgresql"))
    )
    # Persistence time, distinct from caller-supplyable domain time above.
    # Migration 524 adds this without a default first, then sets the default in
    # a second DDL statement so historical rows remain honestly unknown.
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now()
    )
