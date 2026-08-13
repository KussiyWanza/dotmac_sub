"""Durable reviewed evidence for Network Map V2 asset proposals."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class NetworkMapAssetChangeProposal(Base):
    __tablename__ = "network_map_asset_change_proposals"
    __table_args__ = (
        CheckConstraint(
            "asset_type IN ('fdh_cabinet', 'splice_closure', "
            "'access_point', 'support_structure')",
            name="ck_network_map_asset_proposals_asset_type",
        ),
        CheckConstraint(
            "operation IN ('create', 'edit', 'move')",
            name="ck_network_map_asset_proposals_operation",
        ),
        CheckConstraint(
            "status IN ('pending', 'applied', 'rejected')",
            name="ck_network_map_asset_proposals_status",
        ),
        CheckConstraint(
            "(operation = 'create' AND target_asset_id IS NULL "
            "AND before_values IS NULL AND source_asset_sha256 IS NULL) OR "
            "(operation IN ('edit', 'move') AND target_asset_id IS NOT NULL "
            "AND before_values IS NOT NULL AND source_asset_sha256 IS NOT NULL)",
            name="ck_network_map_asset_proposals_target_shape",
        ),
        CheckConstraint(
            "(status = 'pending' AND reviewed_by_actor_id IS NULL "
            "AND reviewed_at IS NULL AND review_notes IS NULL "
            "AND applied_at IS NULL AND result_asset_id IS NULL) OR "
            "(status = 'rejected' AND reviewed_by_actor_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_notes IS NOT NULL "
            "AND applied_at IS NULL AND result_asset_id IS NULL) OR "
            "(status = 'applied' AND reviewed_by_actor_id IS NOT NULL "
            "AND reviewed_at IS NOT NULL AND review_notes IS NOT NULL "
            "AND applied_at IS NOT NULL AND result_asset_id IS NOT NULL)",
            name="ck_network_map_asset_proposals_review_shape",
        ),
        CheckConstraint(
            "reviewed_by_actor_id IS NULL "
            "OR reviewed_by_actor_id <> requested_by_actor_id",
            name="ck_network_map_asset_proposals_review_separation",
        ),
        CheckConstraint(
            "length(proposal_sha256) = 64 "
            "AND length(submit_key_sha256) = 64 "
            "AND length(submit_fingerprint_sha256) = 64 "
            "AND (source_asset_sha256 IS NULL "
            "OR length(source_asset_sha256) = 64) "
            "AND (review_key_sha256 IS NULL OR length(review_key_sha256) = 64) "
            "AND (review_fingerprint_sha256 IS NULL "
            "OR length(review_fingerprint_sha256) = 64)",
            name="ck_network_map_asset_proposals_digests",
        ),
        UniqueConstraint(
            "submit_key_sha256",
            name="uq_network_map_asset_proposals_submit_key",
        ),
        UniqueConstraint(
            "review_key_sha256",
            name="uq_network_map_asset_proposals_review_key",
        ),
        Index("ix_network_map_asset_proposals_status", "status", "created_at"),
        Index(
            "ix_network_map_asset_proposals_target",
            "asset_type",
            "target_asset_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_type: Mapped[str] = mapped_column(String(40), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    target_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result_asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before_values: Mapped[dict[str, object] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    after_values: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    source_asset_sha256: Mapped[str | None] = mapped_column(String(64))
    proposal_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    submit_key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    submit_fingerprint_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    review_key_sha256: Mapped[str | None] = mapped_column(String(64))
    review_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64))
    requested_by_actor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    requested_by_actor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    requested_by_actor_label: Mapped[str] = mapped_column(String(160), nullable=False)
    request_reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewed_by_actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_by_actor_type: Mapped[str | None] = mapped_column(String(30))
    reviewed_by_actor_label: Mapped[str | None] = mapped_column(String(160))
    review_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


__all__ = ["NetworkMapAssetChangeProposal"]
