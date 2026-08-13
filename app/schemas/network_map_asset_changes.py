"""Typed contracts for governed Network Map V2 asset changes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.audit import AuditActorType
from app.services.owner_commands import CommandContext


class GovernedNetworkAssetType(StrEnum):
    fdh_cabinet = "fdh_cabinet"
    splice_closure = "splice_closure"
    access_point = "access_point"
    support_structure = "support_structure"


class NetworkAssetChangeOperation(StrEnum):
    create = "create"
    edit = "edit"
    move = "move"


class NetworkAssetProposalStatus(StrEnum):
    pending = "pending"
    applied = "applied"
    rejected = "rejected"


class NetworkAssetReviewDecision(StrEnum):
    approve = "approve"
    reject = "reject"


@dataclass(frozen=True, slots=True)
class NetworkAssetCoordinates:
    latitude: float
    longitude: float

    def to_transport(self) -> dict[str, float]:
        return {"latitude": self.latitude, "longitude": self.longitude}


@dataclass(frozen=True, slots=True)
class NetworkAssetDraft:
    name: str | None = None
    code: str | None = None
    coordinates: NetworkAssetCoordinates | None = None
    notes: str | None = None
    access_point_type: str | None = None
    placement: str | None = None
    street: str | None = None
    city: str | None = None
    support_type: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkAssetSnapshot:
    asset_type: GovernedNetworkAssetType
    asset_id: UUID | None
    name: str
    code: str | None
    coordinates: NetworkAssetCoordinates
    notes: str | None
    access_point_type: str | None = None
    placement: str | None = None
    street: str | None = None
    city: str | None = None
    support_type: str | None = None
    is_active: bool = True

    def to_transport(self) -> dict[str, object]:
        return {
            "asset_type": self.asset_type.value,
            "asset_id": str(self.asset_id) if self.asset_id else None,
            "name": self.name,
            "code": self.code,
            **self.coordinates.to_transport(),
            "notes": self.notes,
            "access_point_type": self.access_point_type,
            "placement": self.placement,
            "street": self.street,
            "city": self.city,
            "support_type": self.support_type,
            "is_active": self.is_active,
        }


@dataclass(frozen=True, slots=True)
class SubmitNetworkAssetProposalCommand:
    context: CommandContext
    actor_id: UUID
    actor_type: AuditActorType
    actor_label: str
    asset_type: GovernedNetworkAssetType
    operation: NetworkAssetChangeOperation
    asset_id: UUID | None
    proposed: NetworkAssetDraft


@dataclass(frozen=True, slots=True)
class ReviewNetworkAssetProposalCommand:
    context: CommandContext
    actor_id: UUID
    actor_type: AuditActorType
    actor_label: str
    proposal_id: UUID
    decision: NetworkAssetReviewDecision
    expected_proposal_sha256: str
    review_notes: str


@dataclass(frozen=True, slots=True)
class ApplyReviewedNetworkAssetChange:
    proposal_id: UUID
    asset_type: GovernedNetworkAssetType
    operation: NetworkAssetChangeOperation
    target_asset_id: UUID | None
    before: NetworkAssetSnapshot | None
    after: NetworkAssetSnapshot


@dataclass(frozen=True, slots=True)
class AppliedNetworkAssetChange:
    asset_id: UUID
    snapshot: NetworkAssetSnapshot


@dataclass(frozen=True, slots=True)
class NetworkAssetProposalAuditEntry:
    action: str
    actor_id: str | None
    actor_label: str | None
    occurred_at: datetime

    def to_transport(self) -> dict[str, object]:
        return {
            "action": self.action,
            "actor_id": self.actor_id,
            "actor_label": self.actor_label,
            "occurred_at": self.occurred_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class NetworkAssetProposalOutcome:
    proposal_id: UUID
    asset_type: GovernedNetworkAssetType
    operation: NetworkAssetChangeOperation
    status: NetworkAssetProposalStatus
    target_asset_id: UUID | None
    result_asset_id: UUID | None
    before: NetworkAssetSnapshot | None
    after: NetworkAssetSnapshot
    proposal_sha256: str
    requested_by_actor_id: UUID
    requested_by_actor_label: str
    request_reason: str
    reviewed_by_actor_id: UUID | None
    reviewed_by_actor_label: str | None
    review_notes: str | None
    created_at: datetime
    reviewed_at: datetime | None
    applied_at: datetime | None
    audit_history: tuple[NetworkAssetProposalAuditEntry, ...] = ()

    def to_transport(self) -> dict[str, object]:
        return {
            "id": str(self.proposal_id),
            "asset_type": self.asset_type.value,
            "operation": self.operation.value,
            "status": self.status.value,
            "target_asset_id": (
                str(self.target_asset_id) if self.target_asset_id else None
            ),
            "result_asset_id": (
                str(self.result_asset_id) if self.result_asset_id else None
            ),
            "before": self.before.to_transport() if self.before else None,
            "after": self.after.to_transport(),
            "proposal_sha256": self.proposal_sha256,
            "requested_by_actor_id": str(self.requested_by_actor_id),
            "requested_by_actor_label": self.requested_by_actor_label,
            "request_reason": self.request_reason,
            "reviewed_by_actor_id": (
                str(self.reviewed_by_actor_id) if self.reviewed_by_actor_id else None
            ),
            "reviewed_by_actor_label": self.reviewed_by_actor_label,
            "review_notes": self.review_notes,
            "created_at": self.created_at.isoformat(),
            "reviewed_at": (self.reviewed_at.isoformat() if self.reviewed_at else None),
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
            "audit_history": [item.to_transport() for item in self.audit_history],
        }


@dataclass(frozen=True, slots=True)
class NetworkAssetProposalList:
    proposals: tuple[NetworkAssetProposalOutcome, ...]
    total: int
    limit: int
    truncated: bool

    def to_transport(self) -> dict[str, object]:
        return {
            "items": [item.to_transport() for item in self.proposals],
            "total": self.total,
            "limit": self.limit,
            "truncated": self.truncated,
        }


class NetworkAssetProposalSubmitRequest(BaseModel):
    asset_type: GovernedNetworkAssetType
    operation: NetworkAssetChangeOperation
    asset_id: UUID | None = None
    name: str | None = Field(default=None, max_length=160)
    code: str | None = Field(default=None, max_length=80)
    latitude: float | None = None
    longitude: float | None = None
    notes: str | None = Field(default=None, max_length=4000)
    access_point_type: str | None = Field(default=None, max_length=60)
    placement: str | None = Field(default=None, max_length=60)
    street: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    support_type: str | None = Field(default=None, max_length=40)
    reason: str = Field(min_length=3, max_length=1000)
    idempotency_key: UUID


class NetworkAssetProposalReviewRequest(BaseModel):
    expected_proposal_sha256: str = Field(min_length=64, max_length=64)
    review_notes: str = Field(min_length=3, max_length=4000)
    idempotency_key: UUID


__all__ = [
    "AppliedNetworkAssetChange",
    "ApplyReviewedNetworkAssetChange",
    "GovernedNetworkAssetType",
    "NetworkAssetChangeOperation",
    "NetworkAssetCoordinates",
    "NetworkAssetDraft",
    "NetworkAssetProposalAuditEntry",
    "NetworkAssetProposalList",
    "NetworkAssetProposalOutcome",
    "NetworkAssetProposalStatus",
    "NetworkAssetProposalSubmitRequest",
    "NetworkAssetProposalReviewRequest",
    "NetworkAssetReviewDecision",
    "NetworkAssetSnapshot",
    "ReviewNetworkAssetProposalCommand",
    "SubmitNetworkAssetProposalCommand",
]
