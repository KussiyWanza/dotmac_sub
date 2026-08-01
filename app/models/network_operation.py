"""Network operation tracking model.

Provides durable state tracking for long-running or multi-step network
device operations (OLT sync, ONT authorization, TR-069 bootstrap, etc.).
Operations are composable via parent/child relationships, allowing complex
workflows to be broken into independently trackable sub-operations.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class NetworkOperationStatus(enum.Enum):
    """Lifecycle status for a network operation."""

    pending = "pending"
    running = "running"
    waiting = "waiting"  # Waiting on external system (device inform, reboot)
    succeeded = "succeeded"
    warning = "warning"
    failed = "failed"
    canceled = "canceled"


class NetworkOperationType(enum.Enum):
    """Types of network operations that can be tracked."""

    olt_ont_sync = "olt_ont_sync"
    olt_pon_repair = "olt_pon_repair"
    olt_firmware_upgrade = "olt_firmware_upgrade"
    ont_provision = "ont_provision"
    ont_authorize = "ont_authorize"
    ont_commission = "ont_commission"
    ont_commission_cleanup = "ont_commission_cleanup"
    ont_reboot = "ont_reboot"
    ont_factory_reset = "ont_factory_reset"
    ont_set_pppoe = "ont_set_pppoe"
    ont_set_conn_request_creds = "ont_set_conn_request_creds"
    ont_send_conn_request = "ont_send_conn_request"
    ont_enable_ipv6 = "ont_enable_ipv6"
    ont_firmware_upgrade = "ont_firmware_upgrade"
    ont_return_to_inventory = "ont_return_to_inventory"
    ont_decommission = "ont_decommission"
    cpe_set_conn_request_creds = "cpe_set_conn_request_creds"
    cpe_send_conn_request = "cpe_send_conn_request"
    cpe_reboot = "cpe_reboot"
    cpe_factory_reset = "cpe_factory_reset"
    cpe_tr069_command = "cpe_tr069_command"
    tr069_bootstrap = "tr069_bootstrap"
    wifi_update = "wifi_update"
    pppoe_push = "pppoe_push"
    router_config_push = "router_config_push"
    router_config_backup = "router_config_backup"
    router_reboot = "router_reboot"
    router_firmware_upgrade = "router_firmware_upgrade"
    router_bulk_push = "router_bulk_push"
    nas_vlan_provision = "nas_vlan_provision"
    nas_local_secret_retire = "nas_local_secret_retire"
    autofind_scan = "autofind_scan"


class NetworkOperationTargetType(enum.Enum):
    """Target device type for a network operation."""

    olt = "olt"
    ont = "ont"
    cpe = "cpe"
    router = "router"
    nas = "nas"
    system = "system"  # For operations spanning multiple resources


class NetworkOperationDispatchStatus(enum.Enum):
    """Durable transport state for one operation command."""

    pending = "pending"
    dispatched = "dispatched"
    acknowledged = "acknowledged"
    completed = "completed"
    failed = "failed"
    reconciliation_needed = "reconciliation_needed"
    canceled = "canceled"


class NetworkOperation(Base):
    """A tracked network device operation.

    Records the lifecycle of a network action from initiation through
    completion, including input parameters, results, errors, and retry
    state. Supports parent/child composition for multi-step workflows.

    Invariants enforced at DB level:
    - Only one active operation per correlation_key (partial unique index).
    """

    __tablename__ = "network_operations"
    __table_args__ = (
        Index(
            "ix_netops_target",
            "target_type",
            "target_id",
        ),
        Index(
            "ix_netops_status",
            "status",
        ),
        Index(
            "ix_netops_parent",
            "parent_id",
        ),
        Index(
            "ix_netops_redrive_of",
            "redrive_of_id",
        ),
        Index(
            "uq_netops_redrive_idempotency",
            "redrive_of_id",
            "redrive_idempotency_key",
            unique=True,
        ),
        # Partial unique index on correlation_key is created in the Alembic
        # migration via raw SQL (PostgreSQL-only feature). The service layer
        # enforces the same constraint in application code for portability.
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    operation_type: Mapped[NetworkOperationType] = mapped_column(
        Enum(
            NetworkOperationType,
            name="networkoperationtype",
            create_constraint=False,
        ),
        nullable=False,
    )
    target_type: Mapped[NetworkOperationTargetType] = mapped_column(
        Enum(
            NetworkOperationTargetType,
            name="networkoperationtargettype",
            create_constraint=False,
        ),
        nullable=False,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="CASCADE"),
        nullable=True,
    )
    redrive_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[NetworkOperationStatus] = mapped_column(
        Enum(
            NetworkOperationStatus,
            name="networkoperationstatus",
            create_constraint=False,
        ),
        default=NetworkOperationStatus.pending,
    )
    correlation_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    waiting_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    output_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    initiated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    redrive_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    redrive_reviewed_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    redrive_idempotency_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    parent: Mapped[NetworkOperation | None] = relationship(
        "NetworkOperation",
        remote_side=[id],
        foreign_keys=[parent_id],
        back_populates="children",
    )
    children: Mapped[list[NetworkOperation]] = relationship(
        "NetworkOperation",
        foreign_keys=[parent_id],
        back_populates="parent",
    )
    redrive_source: Mapped[NetworkOperation | None] = relationship(
        "NetworkOperation",
        remote_side=[id],
        foreign_keys=[redrive_of_id],
        back_populates="redrive_attempts",
    )
    redrive_attempts: Mapped[list[NetworkOperation]] = relationship(
        "NetworkOperation",
        foreign_keys=[redrive_of_id],
        back_populates="redrive_source",
    )
    dispatches: Mapped[list[NetworkOperationDispatch]] = relationship(
        "NetworkOperationDispatch",
        back_populates="operation",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class NetworkOperationDispatch(Base):
    """Transactional outbox row for one typed network-operation command."""

    __tablename__ = "network_operation_dispatches"
    __table_args__ = (
        Index(
            "uq_netop_dispatch_operation_key",
            "operation_id",
            "dispatch_key",
            unique=True,
        ),
        Index(
            "ix_netop_dispatch_ready",
            "status",
            "next_attempt_at",
        ),
        CheckConstraint(
            "attempts >= 0 AND max_attempts > 0",
            name="ck_netop_dispatch_attempt_budget",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("network_operations.id", ondelete="CASCADE"),
        nullable=False,
    )
    dispatch_key: Mapped[str] = mapped_column(String(80), nullable=False)
    command_name: Mapped[str] = mapped_column(String(120), nullable=False)
    task_name: Mapped[str] = mapped_column(String(180), nullable=False)
    args_payload: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    kwargs_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    queue: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[NetworkOperationDispatchStatus] = mapped_column(
        Enum(
            NetworkOperationDispatchStatus,
            name="networkoperationdispatchstatus",
            create_constraint=False,
        ),
        nullable=False,
        default=NetworkOperationDispatchStatus.pending,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    task_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    operation: Mapped[NetworkOperation] = relationship(
        "NetworkOperation",
        back_populates="dispatches",
    )
