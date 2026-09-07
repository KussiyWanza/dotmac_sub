"""Native Sub → DotMac ERP delivery substrate.

Two tables:

* ``field_erp_sync_events`` — the OUTBOX. Field-service money-path actions
  (expense claims, material requests, purchase orders, purchase invoices)
  enqueue a row here with a stable ``idempotency_key``; a background worker
  posts each pending row to ERP's neutral ``/sync/sub/*`` API and records the
  terminal outcome. The idempotency key is stored and sent, so re-delivery of a
  row ERP already saw is safe.

* ``sync_flow_ownership`` — the SINGLE-WRITER GUARD (Michael's addition). One
  row per flow naming the app that currently owns the ERP write path for it.
  Seeded to ``crm`` for every flow: CRM keeps writing until a flow is explicitly
  cut over to sub. The outbox REFUSES to deliver any flow sub does not own, so
  the double-push-across-id-spaces hazard (ERP idempotency keys are
  per-originator-id, and CRM and sub use different UUID spaces) cannot fire
  during a mis-sequenced cutover.

Status/flow/owner columns are stored as ``String`` (app-level enums, not PG
enums) — the same idiom as the maps §A vendor-route domain (migration 248), so
the schema builds cleanly on both Postgres and the sqlite test harness.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class FieldErpSyncFlow(enum.Enum):
    """Money-path flows sub can push to ERP. One outbox row belongs to one flow."""

    expense_claim = "expense_claim"
    material_request = "material_request"
    purchase_order = "purchase_order"
    purchase_invoice = "purchase_invoice"


class FieldErpSyncStatus(enum.Enum):
    """Lifecycle of a single outbox row.

    * ``pending`` — enqueued or awaiting (re)delivery; a transient ERP error
      leaves the row here so the next worker pass retries.
    * ``sent`` — ERP accepted the POST (2xx) but has not returned a terminal
      accept/reject decision yet (awaits a later status poll).
    * ``accepted`` — ERP acknowledged and created the record.
    * ``rejected`` — ERP rejected the record (terminal, business decision).
    * ``dead`` — permanent transport/validation error, or the retry budget was
      exhausted; needs operator attention (dead-letter).
    """

    pending = "pending"
    sent = "sent"
    accepted = "accepted"
    rejected = "rejected"
    dead = "dead"


class SyncFlowOwner(enum.Enum):
    """Which app currently owns the ERP write path for a flow."""

    crm = "crm"
    sub = "sub"


# Explicit handoff commands must start from the seeded/pre-cutover owner.
PRE_CUTOVER_SYNC_FLOW_OWNER: str = SyncFlowOwner.crm.value


# Terminal statuses never re-delivered.
TERMINAL_SYNC_STATUSES = frozenset(
    {
        FieldErpSyncStatus.accepted.value,
        FieldErpSyncStatus.rejected.value,
        FieldErpSyncStatus.dead.value,
    }
)


class FieldErpSyncEvent(Base):
    """Outbox row: one intent to push a sub entity to ERP."""

    __tablename__ = "field_erp_sync_events"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_field_erp_sync_events_idempotency_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # App-level enum stored as text (see module docstring).
    flow: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    # The sub-side entity UUID this row pushes (kept as a plain UUID; the source
    # tables live in several domains).
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=FieldErpSyncStatus.pending.value,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    erp_response: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncFlowOwnership(Base):
    """Single-writer guard: which app owns the ERP write path for each flow."""

    __tablename__ = "sync_flow_ownership"
    __table_args__ = (UniqueConstraint("flow", name="uq_sync_flow_ownership_flow"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    flow: Mapped[str] = mapped_column(String(40), nullable=False)
    owner: Mapped[str] = mapped_column(
        String(10), nullable=False, default=PRE_CUTOVER_SYNC_FLOW_OWNER
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    updated_by: Mapped[str | None] = mapped_column(String(120))


def flow_owned_by_sub(db, flow: FieldErpSyncFlow | str) -> bool:
    """Return True only if sub is the recorded owner of ``flow``.

    Absent a row, ownership defaults to CRM (returns False): sub never delivers a
    flow it was not explicitly handed. This is the code-side half of the
    "enforced by code + deploy checks" control.
    """
    flow_value = flow.value if isinstance(flow, FieldErpSyncFlow) else str(flow)
    row = (
        db.query(SyncFlowOwnership).filter(SyncFlowOwnership.flow == flow_value).first()
    )
    if row is None:
        return False
    return row.owner == SyncFlowOwner.sub.value


def get_flow_ownership(db) -> dict[str, str]:
    """Current owner for every known flow (missing rows default to CRM).

    Feeds the deploy/health ownership surface so operators can see, at a glance,
    which flows sub is authorised to write before/after a cutover.
    """
    rows = {row.flow: row.owner for row in db.query(SyncFlowOwnership).all()}
    return {
        flow.value: rows.get(flow.value, PRE_CUTOVER_SYNC_FLOW_OWNER)
        for flow in FieldErpSyncFlow
    }
