from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.fiber_topology_identity import (
    FiberTopologyAssetSourceLink,
    FiberTopologyIdentityBatchReview,
    FiberTopologyIdentityDecision,
    FiberTopologyIdentityExecutionRun,
    FiberTopologyIdentityProposalBatch,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.services.network.fiber_topology_identity import (
    ACTIVE_STATUSES,
    CREATE_ASSET_TYPES,
    LINK_TARGET_MODELS,
    POINT_ASSET_TYPES,
    FiberTopologyIdentityError,
    approve_identity_decision,
    decision_to_dict,
    decline_identity_decision,
    execute_identity_decision,
    finalize_identity_decision,
    preview_identity_decision,
    propose_identity_decision,
    representative_point,
    stable_source_external_id,
    validate_identity_decision_for_review,
)

MAX_BATCH_ITEMS = 500
MAX_EXECUTION_LIMIT = 100
MAX_QUEUE_LIMIT = 500


class FiberTopologyReviewError(ValueError):
    """Raised when an operator-scale identity review request is invalid."""


class FiberTopologyProposalBatchBlocked(FiberTopologyReviewError):
    def __init__(self, preview: FiberIdentityProposalBatchPreview):
        self.preview = preview
        super().__init__(
            f"proposal batch has {len(preview.blockers)} blocker(s); nothing was written"
        )


@dataclass(frozen=True)
class FiberIdentityProposalBatchPreview:
    request_sha256: str
    manifest_sha256: str
    source_name: str
    proposed_by: str
    reason: str
    items: tuple[dict, ...]
    blockers: tuple[dict, ...]
    existing_batch_id: uuid.UUID | None = None

    @property
    def ready(self) -> bool:
        return not self.blockers

    def to_dict(self, *, include_items: bool = True) -> dict:
        payload = {
            "blocker_count": len(self.blockers),
            "blockers": list(self.blockers),
            "existing_batch_id": str(self.existing_batch_id)
            if self.existing_batch_id
            else None,
            "item_count": len(self.items),
            "manifest_sha256": self.manifest_sha256,
            "proposed_by": self.proposed_by,
            "ready": self.ready,
            "reason": self.reason,
            "request_sha256": self.request_sha256,
            "source_name": self.source_name,
        }
        if include_items:
            payload["items"] = list(self.items)
        return payload


@dataclass(frozen=True)
class FiberIdentityProposalBatchResult:
    batch_id: uuid.UUID
    request_sha256: str
    manifest_sha256: str
    decision_ids: tuple[uuid.UUID, ...]
    created: bool

    def to_dict(self) -> dict:
        return {
            "batch_id": str(self.batch_id),
            "created": self.created,
            "decision_ids": [str(value) for value in self.decision_ids],
            "item_count": len(self.decision_ids),
            "manifest_sha256": self.manifest_sha256,
            "request_sha256": self.request_sha256,
        }


@dataclass(frozen=True)
class FiberIdentityBatchReviewResult:
    review_id: uuid.UUID
    batch_id: uuid.UUID
    batch_manifest_sha256: str
    action: str
    attestation_sha256: str
    decision_ids: tuple[uuid.UUID, ...]
    created: bool

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "attestation_sha256": self.attestation_sha256,
            "batch_id": str(self.batch_id),
            "batch_manifest_sha256": self.batch_manifest_sha256,
            "created": self.created,
            "decision_ids": [str(value) for value in self.decision_ids],
            "item_count": len(self.decision_ids),
            "review_id": str(self.review_id),
        }


@dataclass(frozen=True)
class FiberIdentityExecutionRunResult:
    run_id: uuid.UUID | None
    batch_id: uuid.UUID
    batch_manifest_sha256: str
    requested_limit: int
    outcomes: tuple[dict, ...]
    remaining_approved_count: int
    result_sha256: str | None
    created: bool

    @property
    def counts(self) -> dict[str, int]:
        counts = {"applied": 0, "change_requested": 0, "closed": 0, "error": 0}
        for outcome in self.outcomes:
            status = str(outcome["outcome"])
            counts[status] += 1
        return counts

    def to_dict(self) -> dict:
        return {
            "batch_id": str(self.batch_id),
            "batch_manifest_sha256": self.batch_manifest_sha256,
            "counts": self.counts,
            "created": self.created,
            "outcomes": list(self.outcomes),
            "remaining_approved_count": self.remaining_approved_count,
            "requested_limit": self.requested_limit,
            "result_sha256": self.result_sha256,
            "run_id": str(self.run_id) if self.run_id else None,
            "scanned_count": len(self.outcomes),
        }


@dataclass(frozen=True)
class FiberIdentityReviewQueuePage:
    items: tuple[dict, ...]
    total: int
    limit: int
    offset: int
    state: str
    counts: dict[str, int]

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "items": list(self.items),
            "limit": self.limit,
            "offset": self.offset,
            "state": self.state,
            "total": self.total,
        }


@dataclass(frozen=True)
class FiberIdentityReconcileResult:
    scanned: int
    applied: int
    closed: int
    pending: int
    errors: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "closed": self.closed,
            "error_count": len(self.errors),
            "errors": list(self.errors),
            "pending": self.pending,
            "scanned": self.scanned,
        }


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise FiberTopologyReviewError(f"{field} is required")
    if len(normalized) > limit:
        raise FiberTopologyReviewError(f"{field} must be at most {limit} characters")
    return normalized


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalize_batch_request(
    items: Sequence[Mapping[str, Any]],
    *,
    proposed_by: str,
    reason: str,
    source_name: str,
) -> tuple[dict, str, str, str]:
    actor = _required_text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _required_text(reason, "reason", limit=4000)
    normalized_source = _required_text(source_name, "source_name", limit=255)
    if not items:
        raise FiberTopologyReviewError("proposal batch requires at least one item")
    if len(items) > MAX_BATCH_ITEMS:
        raise FiberTopologyReviewError(
            f"proposal batch cannot exceed {MAX_BATCH_ITEMS} items"
        )
    normalized_items: list[dict] = []
    for row_number, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise FiberTopologyReviewError(
                f"proposal batch row {row_number} must be an object"
            )
        item_reason = str(item.get("reason") or normalized_reason).strip()
        normalized_items.append(
            {
                "action": str(item.get("action") or "").strip().lower(),
                "reason": item_reason,
                "row_number": row_number,
                "staged_feature_id": str(item.get("staged_feature_id") or "").strip(),
                "target_asset_id": str(item.get("target_asset_id")).strip()
                if item.get("target_asset_id") is not None
                else None,
            }
        )
    request_payload = {
        "items": normalized_items,
        "proposed_by": actor,
        "reason": normalized_reason,
        "schema_version": 1,
        "source_name": normalized_source,
    }
    return request_payload, actor, normalized_reason, normalized_source


def _preview_from_existing_batch(
    batch: FiberTopologyIdentityProposalBatch,
) -> FiberIdentityProposalBatchPreview:
    items = tuple(batch.manifest_payload.get("items") or ())
    return FiberIdentityProposalBatchPreview(
        request_sha256=batch.request_sha256,
        manifest_sha256=batch.manifest_sha256,
        source_name=batch.source_name,
        proposed_by=batch.proposed_by,
        reason=batch.reason,
        items=items,
        blockers=(),
        existing_batch_id=batch.id,
    )


def preview_identity_proposal_batch(
    db: Session,
    items: Sequence[Mapping[str, Any]],
    *,
    proposed_by: str,
    reason: str,
    source_name: str = "operator-manifest",
) -> FiberIdentityProposalBatchPreview:
    request_payload, actor, normalized_reason, normalized_source = (
        _normalize_batch_request(
            items,
            proposed_by=proposed_by,
            reason=reason,
            source_name=source_name,
        )
    )
    request_sha256 = _digest(request_payload)
    existing_batch = db.scalar(
        select(FiberTopologyIdentityProposalBatch).where(
            FiberTopologyIdentityProposalBatch.request_sha256 == request_sha256
        )
    )
    if existing_batch:
        return _preview_from_existing_batch(existing_batch)

    resolved_items: list[dict] = []
    blockers: list[dict] = []
    seen_features: set[str] = set()
    for item in request_payload["items"]:
        row_number = int(item["row_number"])
        feature_id = str(item["staged_feature_id"])
        if feature_id in seen_features and feature_id:
            blockers.append(
                {
                    "code": "duplicate_staged_feature",
                    "message": "staged feature appears more than once in the batch",
                    "row_number": row_number,
                    "staged_feature_id": feature_id,
                }
            )
            resolved_items.append(dict(item))
            continue
        seen_features.add(feature_id)
        try:
            preview = preview_identity_decision(
                db,
                staged_feature_id=feature_id,
                action=str(item["action"]),
                target_asset_id=item["target_asset_id"],
                proposed_by=actor,
                reason=str(item["reason"]),
                require_new=True,
            )
        except FiberTopologyIdentityError as exc:
            blockers.append(
                {
                    "code": "identity_decision_blocked",
                    "message": str(exc),
                    "row_number": row_number,
                    "staged_feature_id": feature_id or None,
                }
            )
            resolved_items.append(dict(item))
            continue
        resolved_items.append(preview.to_manifest_dict(row_number=row_number))

    manifest_payload = {
        "items": resolved_items,
        "proposed_by": actor,
        "reason": normalized_reason,
        "request_sha256": request_sha256,
        "schema_version": 1,
        "source_name": normalized_source,
    }
    return FiberIdentityProposalBatchPreview(
        request_sha256=request_sha256,
        manifest_sha256=_digest(manifest_payload),
        source_name=normalized_source,
        proposed_by=actor,
        reason=normalized_reason,
        items=tuple(resolved_items),
        blockers=tuple(blockers),
    )


def propose_identity_batch(
    db: Session,
    items: Sequence[Mapping[str, Any]],
    *,
    proposed_by: str,
    reason: str,
    source_name: str = "operator-manifest",
) -> FiberIdentityProposalBatchResult:
    preview = preview_identity_proposal_batch(
        db,
        items,
        proposed_by=proposed_by,
        reason=reason,
        source_name=source_name,
    )
    if preview.existing_batch_id:
        decision_ids = tuple(
            db.scalars(
                select(FiberTopologyIdentityDecision.id)
                .where(
                    FiberTopologyIdentityDecision.proposal_batch_id
                    == preview.existing_batch_id
                )
                .order_by(FiberTopologyIdentityDecision.proposal_batch_row_number)
            ).all()
        )
        return FiberIdentityProposalBatchResult(
            batch_id=preview.existing_batch_id,
            request_sha256=preview.request_sha256,
            manifest_sha256=preview.manifest_sha256,
            decision_ids=decision_ids,
            created=False,
        )
    if not preview.ready:
        raise FiberTopologyProposalBatchBlocked(preview)

    manifest_payload = {
        "items": list(preview.items),
        "proposed_by": preview.proposed_by,
        "reason": preview.reason,
        "request_sha256": preview.request_sha256,
        "schema_version": 1,
        "source_name": preview.source_name,
    }
    batch = FiberTopologyIdentityProposalBatch(
        manifest_sha256=preview.manifest_sha256,
        request_sha256=preview.request_sha256,
        manifest_payload=manifest_payload,
        item_count=len(preview.items),
        source_name=preview.source_name,
        proposed_by=preview.proposed_by,
        reason=preview.reason,
    )
    db.add(batch)
    try:
        db.flush()
        decisions: list[FiberTopologyIdentityDecision] = []
        for item in preview.items:
            decisions.append(
                propose_identity_decision(
                    db,
                    staged_feature_id=str(item["staged_feature_id"]),
                    action=str(item["action"]),
                    target_asset_id=item.get("target_asset_id"),
                    proposed_by=preview.proposed_by,
                    reason=str(item["reason"]),
                    proposal_batch_id=batch.id,
                    proposal_batch_row_number=int(item["row_number"]),
                    commit=False,
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return FiberIdentityProposalBatchResult(
        batch_id=batch.id,
        request_sha256=preview.request_sha256,
        manifest_sha256=preview.manifest_sha256,
        decision_ids=tuple(decision.id for decision in decisions),
        created=True,
    )


def _coerce_batch_id(value: str | uuid.UUID) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberTopologyReviewError("batch_id must be a UUID") from exc


def _load_proposal_batch(
    db: Session,
    batch_id: str | uuid.UUID,
    *,
    for_update: bool = False,
) -> FiberTopologyIdentityProposalBatch:
    statement = select(FiberTopologyIdentityProposalBatch).where(
        FiberTopologyIdentityProposalBatch.id == _coerce_batch_id(batch_id)
    )
    if for_update:
        statement = statement.with_for_update()
    batch = db.scalar(statement)
    if batch is None:
        raise FiberTopologyReviewError("identity proposal batch not found")
    return batch


def _expected_manifest(value: str) -> str:
    digest = _required_text(value, "expected_manifest_sha256", limit=64)
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise FiberTopologyReviewError(
            "expected_manifest_sha256 must be a lowercase SHA-256 digest"
        )
    return digest


def _batch_decisions(
    db: Session,
    batch: FiberTopologyIdentityProposalBatch,
    *,
    for_update: bool = False,
) -> list[FiberTopologyIdentityDecision]:
    statement = (
        select(FiberTopologyIdentityDecision)
        .where(FiberTopologyIdentityDecision.proposal_batch_id == batch.id)
        .order_by(FiberTopologyIdentityDecision.proposal_batch_row_number)
    )
    if for_update:
        statement = statement.with_for_update()
    decisions = list(db.scalars(statement).all())
    manifest_items = list(batch.manifest_payload.get("items") or ())
    if len(decisions) != batch.item_count or len(manifest_items) != batch.item_count:
        raise FiberTopologyReviewError(
            "proposal batch decision count does not match its immutable manifest"
        )
    for row_number, (decision, item) in enumerate(
        zip(decisions, manifest_items, strict=True), start=1
    ):
        evidence = {
            "action": decision.action,
            "decision_sha256": decision.decision_sha256,
            "feature_content_sha256": decision.feature_content_sha256,
            "proposed_by": decision.proposed_by,
            "reason": decision.reason,
            "row_number": row_number,
            "source_asset_type": decision.source_asset_type,
            "source_external_id": decision.source_external_id,
            "source_system": decision.source_system,
            "staged_feature_id": str(decision.staged_feature_id),
            "target_asset_id": str(decision.target_asset_id)
            if decision.target_asset_id
            else None,
            "target_asset_type": decision.target_asset_type,
        }
        if item != evidence or decision.proposal_batch_row_number != row_number:
            raise FiberTopologyReviewError(
                f"proposal batch row {row_number} does not match its manifest"
            )
    return decisions


def attest_identity_batch(
    db: Session,
    batch_id: str | uuid.UUID,
    *,
    expected_manifest_sha256: str,
    action: str,
    reviewed_by: str,
    review_notes: str,
) -> FiberIdentityBatchReviewResult:
    """Atomically attest and transition every proposed decision in a batch."""

    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"approve", "decline"}:
        raise FiberTopologyReviewError("action must be approve or decline")
    expected_manifest = _expected_manifest(expected_manifest_sha256)
    batch = _load_proposal_batch(db, batch_id, for_update=True)
    if batch.manifest_sha256 != expected_manifest:
        raise FiberTopologyReviewError(
            "expected manifest does not match the proposal batch"
        )
    attestation_payload = {
        "action": normalized_action,
        "batch_id": str(batch.id),
        "batch_manifest_sha256": batch.manifest_sha256,
        "item_count": batch.item_count,
        "proposed_by": batch.proposed_by,
        "review_notes": notes,
        "reviewed_by": actor,
        "schema_version": 1,
    }
    attestation_sha256 = _digest(attestation_payload)
    existing = db.scalar(
        select(FiberTopologyIdentityBatchReview).where(
            FiberTopologyIdentityBatchReview.proposal_batch_id == batch.id
        )
    )
    if existing:
        if existing.attestation_sha256 != attestation_sha256:
            raise FiberTopologyReviewError(
                "proposal batch already has a different review attestation"
            )
        decision_ids = tuple(decision.id for decision in _batch_decisions(db, batch))
        return FiberIdentityBatchReviewResult(
            review_id=existing.id,
            batch_id=batch.id,
            batch_manifest_sha256=batch.manifest_sha256,
            action=existing.action,
            attestation_sha256=existing.attestation_sha256,
            decision_ids=decision_ids,
            created=False,
        )
    if batch.proposed_by == actor:
        raise FiberTopologyReviewError(
            "the batch proposer cannot attest the same proposal batch"
        )

    decisions = _batch_decisions(db, batch, for_update=True)
    if any(decision.status != "proposed" for decision in decisions):
        raise FiberTopologyReviewError(
            "every decision must still be proposed; batch review wrote nothing"
        )
    try:
        if normalized_action == "approve":
            for decision in decisions:
                validate_identity_decision_for_review(db, decision.id)
            for decision in decisions:
                approve_identity_decision(
                    db,
                    decision.id,
                    actor,
                    notes,
                    commit=False,
                )
        else:
            for decision in decisions:
                decline_identity_decision(
                    db,
                    decision.id,
                    actor,
                    notes,
                    commit=False,
                )
        review = FiberTopologyIdentityBatchReview(
            proposal_batch_id=batch.id,
            batch_manifest_sha256=batch.manifest_sha256,
            action=normalized_action,
            proposed_by=batch.proposed_by,
            reviewed_by=actor,
            review_notes=notes,
            item_count=batch.item_count,
            attestation_sha256=attestation_sha256,
        )
        db.add(review)
        db.commit()
        db.refresh(review)
    except Exception:
        db.rollback()
        raise
    return FiberIdentityBatchReviewResult(
        review_id=review.id,
        batch_id=batch.id,
        batch_manifest_sha256=batch.manifest_sha256,
        action=normalized_action,
        attestation_sha256=attestation_sha256,
        decision_ids=tuple(decision.id for decision in decisions),
        created=True,
    )


def inspect_identity_batch(
    db: Session,
    batch_id: str | uuid.UUID,
) -> dict:
    """Return the immutable manifest and its control-plane evidence."""

    batch = _load_proposal_batch(db, batch_id)
    decisions = _batch_decisions(db, batch)
    status_counts: dict[str, int] = {}
    for decision in decisions:
        status_counts[decision.status] = status_counts.get(decision.status, 0) + 1
    review = db.scalar(
        select(FiberTopologyIdentityBatchReview).where(
            FiberTopologyIdentityBatchReview.proposal_batch_id == batch.id
        )
    )
    execution_runs = db.scalars(
        select(FiberTopologyIdentityExecutionRun)
        .where(FiberTopologyIdentityExecutionRun.proposal_batch_id == batch.id)
        .order_by(FiberTopologyIdentityExecutionRun.executed_at)
    ).all()
    return {
        "batch_id": str(batch.id),
        "created_at": batch.created_at.isoformat(),
        "decision_status_counts": status_counts,
        "execution_runs": [
            {
                "counts": {
                    "applied": run.applied_count,
                    "change_requested": run.change_requested_count,
                    "closed": run.closed_count,
                    "error": run.error_count,
                },
                "executed_at": run.executed_at.isoformat(),
                "executed_by": run.executed_by,
                "remaining_approved_count": run.remaining_approved_count,
                "requested_limit": run.requested_limit,
                "result_sha256": run.result_sha256,
                "run_id": str(run.id),
                "scanned_count": run.scanned_count,
            }
            for run in execution_runs
        ],
        "item_count": batch.item_count,
        "manifest_payload": batch.manifest_payload,
        "manifest_sha256": batch.manifest_sha256,
        "proposed_by": batch.proposed_by,
        "review": {
            "action": review.action,
            "attestation_sha256": review.attestation_sha256,
            "review_id": str(review.id),
            "review_notes": review.review_notes,
            "reviewed_at": review.reviewed_at.isoformat(),
            "reviewed_by": review.reviewed_by,
        }
        if review
        else None,
        "source_name": batch.source_name,
    }


def execute_identity_batch(
    db: Session,
    batch_id: str | uuid.UUID,
    *,
    expected_manifest_sha256: str,
    executed_by: str,
    limit: int = 50,
) -> FiberIdentityExecutionRunResult:
    """Execute a bounded set of approved decisions and persist exact outcomes."""

    actor = _required_text(executed_by, "executed_by", limit=160)
    expected_manifest = _expected_manifest(expected_manifest_sha256)
    if limit < 1 or limit > MAX_EXECUTION_LIMIT:
        raise FiberTopologyReviewError(
            f"limit must be between 1 and {MAX_EXECUTION_LIMIT}"
        )
    batch = _load_proposal_batch(db, batch_id)
    if batch.manifest_sha256 != expected_manifest:
        raise FiberTopologyReviewError(
            "expected manifest does not match the proposal batch"
        )
    review = db.scalar(
        select(FiberTopologyIdentityBatchReview)
        .where(FiberTopologyIdentityBatchReview.proposal_batch_id == batch.id)
        .with_for_update()
    )
    if review is None or review.action != "approve":
        raise FiberTopologyReviewError(
            "proposal batch requires an approving review attestation before execution"
        )
    if review.batch_manifest_sha256 != batch.manifest_sha256:
        raise FiberTopologyReviewError(
            "review attestation does not match the proposal-batch manifest"
        )
    _batch_decisions(db, batch)
    decision_ids = tuple(
        db.scalars(
            select(FiberTopologyIdentityDecision.id)
            .where(
                FiberTopologyIdentityDecision.proposal_batch_id == batch.id,
                FiberTopologyIdentityDecision.status == "approved",
            )
            .order_by(FiberTopologyIdentityDecision.proposal_batch_row_number)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
    )
    if not decision_ids:
        remaining = int(
            db.scalar(
                select(func.count())
                .select_from(FiberTopologyIdentityDecision)
                .where(
                    FiberTopologyIdentityDecision.proposal_batch_id == batch.id,
                    FiberTopologyIdentityDecision.status == "approved",
                )
            )
            or 0
        )
        return FiberIdentityExecutionRunResult(
            run_id=None,
            batch_id=batch.id,
            batch_manifest_sha256=batch.manifest_sha256,
            requested_limit=limit,
            outcomes=(),
            remaining_approved_count=remaining,
            result_sha256=None,
            created=False,
        )

    outcomes: list[dict] = []
    try:
        for decision_id in decision_ids:
            try:
                with db.begin_nested():
                    decision = execute_identity_decision(
                        db,
                        decision_id,
                        actor,
                        commit=False,
                    )
                outcomes.append(
                    {
                        "decision_id": str(decision_id),
                        "outcome": decision.status,
                    }
                )
            except FiberTopologyIdentityError as exc:
                outcomes.append(
                    {
                        "decision_id": str(decision_id),
                        "message": str(exc),
                        "outcome": "error",
                    }
                )
        remaining = int(
            db.scalar(
                select(func.count())
                .select_from(FiberTopologyIdentityDecision)
                .where(
                    FiberTopologyIdentityDecision.proposal_batch_id == batch.id,
                    FiberTopologyIdentityDecision.status == "approved",
                )
            )
            or 0
        )
        run_id = uuid.uuid4()
        result_payload = {
            "batch_id": str(batch.id),
            "batch_manifest_sha256": batch.manifest_sha256,
            "executed_by": actor,
            "execution_run_id": str(run_id),
            "outcomes": outcomes,
            "remaining_approved_count": remaining,
            "requested_limit": limit,
            "schema_version": 1,
        }
        result_sha256 = _digest(result_payload)
        counts = {"applied": 0, "change_requested": 0, "closed": 0, "error": 0}
        for outcome in outcomes:
            counts[str(outcome["outcome"])] += 1
        run = FiberTopologyIdentityExecutionRun(
            id=run_id,
            proposal_batch_id=batch.id,
            batch_review_id=review.id,
            batch_manifest_sha256=batch.manifest_sha256,
            executed_by=actor,
            requested_limit=limit,
            scanned_count=len(outcomes),
            change_requested_count=counts["change_requested"],
            applied_count=counts["applied"],
            closed_count=counts["closed"],
            error_count=counts["error"],
            remaining_approved_count=remaining,
            result_payload=result_payload,
            result_sha256=result_sha256,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
    except Exception:
        db.rollback()
        raise
    return FiberIdentityExecutionRunResult(
        run_id=run.id,
        batch_id=batch.id,
        batch_manifest_sha256=batch.manifest_sha256,
        requested_limit=limit,
        outcomes=tuple(outcomes),
        remaining_approved_count=remaining,
        result_sha256=result_sha256,
        created=True,
    )


def _chunks(values: list[uuid.UUID], size: int = 500) -> list[list[uuid.UUID]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _latest_features(
    db: Session, profile: str | None
) -> list[FiberTopologyStagedFeature]:
    statement = (
        select(FiberTopologyStagedFeature)
        .join(FiberTopologyStagedFeature.batch)
        .options(joinedload(FiberTopologyStagedFeature.batch))
        .where(FiberTopologyStagedFeature.asset_type.in_(POINT_ASSET_TYPES))
        .order_by(
            FiberTopologySourceBatch.created_at.desc(),
            FiberTopologyStagedFeature.created_at.desc(),
            FiberTopologyStagedFeature.id.desc(),
        )
    )
    if profile:
        statement = statement.where(FiberTopologySourceBatch.profile == profile)
    features = list(db.scalars(statement).unique().all())
    latest: list[FiberTopologyStagedFeature] = []
    seen: set[tuple[str, str, str]] = set()
    for feature in features:
        identity = feature.external_id or f"feature:{feature.id}"
        source_key = (feature.batch.source_system, feature.asset_type, identity)
        if source_key in seen:
            continue
        seen.add(source_key)
        latest.append(feature)
    return latest


def _feature_source_key(feature: FiberTopologyStagedFeature) -> tuple[str, str, str]:
    identity = (
        stable_source_external_id(
            feature.batch.source_system, feature.asset_type, feature.external_id
        )
        or f"feature:{feature.id}"
    )
    return feature.batch.source_system, feature.asset_type, identity


def _decision_source_key(
    decision: FiberTopologyIdentityDecision,
) -> tuple[str, str, str]:
    identity = decision.source_external_id or f"feature:{decision.staged_feature_id}"
    return decision.source_system, decision.source_asset_type, identity


def _decisions_by_source(
    db: Session,
) -> dict[tuple[str, str, str], list[FiberTopologyIdentityDecision]]:
    result: dict[tuple[str, str, str], list[FiberTopologyIdentityDecision]] = {}
    decisions = db.scalars(
        select(FiberTopologyIdentityDecision)
        .where(FiberTopologyIdentityDecision.source_asset_type.in_(POINT_ASSET_TYPES))
        .order_by(
            FiberTopologyIdentityDecision.proposed_at.desc(),
            FiberTopologyIdentityDecision.id.desc(),
        )
    ).all()
    for decision in decisions:
        result.setdefault(_decision_source_key(decision), []).append(decision)
    return result


def _source_links(
    db: Session,
) -> dict[tuple[str, str, str], FiberTopologyAssetSourceLink]:
    return {
        (link.source_system, link.source_asset_type, link.external_id): link
        for link in db.scalars(select(FiberTopologyAssetSourceLink)).all()
    }


def _review_state(
    feature: FiberTopologyStagedFeature,
    decisions: list[FiberTopologyIdentityDecision],
    link: FiberTopologyAssetSourceLink | None,
) -> tuple[str, FiberTopologyIdentityDecision | None]:
    if link:
        return "linked", decisions[0] if decisions else None
    active = next(
        (decision for decision in decisions if decision.status in ACTIVE_STATUSES),
        None,
    )
    if active:
        return "active", active
    latest = decisions[0] if decisions else None
    return (latest.status if latest else "unreviewed"), latest


def _is_actionable(state: str, decision: FiberTopologyIdentityDecision | None) -> bool:
    if state in {"unreviewed", "declined"}:
        return True
    return bool(
        state == "closed"
        and decision
        and decision.closed_reason == "fiber_change_request_rejected"
    )


def _eligible_actions(feature: FiberTopologyStagedFeature) -> list[str]:
    if feature.match_status == "blocked":
        return ["reject"]
    if feature.asset_type in CREATE_ASSET_TYPES:
        return ["create", "link_existing", "reject"]
    if feature.asset_type == "service_building":
        return ["link_existing", "reject"]
    return ["reject"]


def _coerce_candidate_ids(feature: FiberTopologyStagedFeature) -> list[uuid.UUID]:
    values = list(feature.candidate_asset_ids or [])
    if feature.canonical_asset_id:
        values.insert(0, feature.canonical_asset_id)
    result: list[uuid.UUID] = []
    for value in values:
        try:
            candidate_id = (
                value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
            )
        except (TypeError, ValueError):
            continue
        if candidate_id not in result:
            result.append(candidate_id)
    return result


def _candidate_assets(
    db: Session, features: list[FiberTopologyStagedFeature]
) -> dict[tuple[str, uuid.UUID], object]:
    ids_by_type: dict[str, set[uuid.UUID]] = {}
    for feature in features:
        if feature.asset_type not in LINK_TARGET_MODELS:
            continue
        ids_by_type.setdefault(feature.asset_type, set()).update(
            _coerce_candidate_ids(feature)
        )
    result: dict[tuple[str, uuid.UUID], object] = {}
    for asset_type, candidate_ids in ids_by_type.items():
        model: Any = LINK_TARGET_MODELS[asset_type]
        for chunk in _chunks(list(candidate_ids)):
            for asset in db.scalars(select(model).where(model.id.in_(chunk))).all():
                result[(asset_type, asset.id)] = asset
    return result


def _distance_meters(
    source: tuple[float, float] | None,
    latitude: object,
    longitude: object,
) -> float | None:
    if source is None or latitude is None or longitude is None:
        return None
    try:
        source_longitude, source_latitude = source
        target_latitude = float(str(latitude))
        target_longitude = float(str(longitude))
    except (TypeError, ValueError):
        return None
    lat1 = math.radians(source_latitude)
    lat2 = math.radians(target_latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(target_longitude - source_longitude)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    haversine = max(0.0, min(1.0, haversine))
    return round(
        6_371_000 * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine)), 1
    )


def _feature_point(feature: FiberTopologyStagedFeature) -> tuple[float, float] | None:
    try:
        return representative_point(feature.geometry_geojson)
    except FiberTopologyIdentityError:
        return None


def _candidate_summary(
    feature: FiberTopologyStagedFeature,
    candidate_id: uuid.UUID,
    asset: object,
    source_point: tuple[float, float] | None,
) -> dict:
    return {
        "code": getattr(asset, "code", None),
        "distance_meters": _distance_meters(
            source_point,
            getattr(asset, "latitude", None),
            getattr(asset, "longitude", None),
        ),
        "id": str(candidate_id),
        "is_active": getattr(asset, "is_active", None),
        "is_suggested": candidate_id == feature.canonical_asset_id,
        "latitude": getattr(asset, "latitude", None),
        "longitude": getattr(asset, "longitude", None),
        "name": getattr(asset, "name", None),
    }


def list_identity_review_queue(
    db: Session,
    *,
    profile: str | None = None,
    state: str = "actionable",
    limit: int = 100,
    offset: int = 0,
    include_source_properties: bool = False,
) -> FiberIdentityReviewQueuePage:
    normalized_state = (state or "actionable").strip().lower()
    allowed_states = {
        "actionable",
        "active",
        "all",
        "applied",
        "closed",
        "declined",
        "linked",
        "unreviewed",
    }
    if normalized_state not in allowed_states:
        raise FiberTopologyReviewError("unsupported review queue state")
    if limit < 1 or limit > MAX_QUEUE_LIMIT:
        raise FiberTopologyReviewError(f"limit must be between 1 and {MAX_QUEUE_LIMIT}")
    if offset < 0:
        raise FiberTopologyReviewError("offset cannot be negative")

    features = _latest_features(db, profile)
    decisions_by_source = _decisions_by_source(db)
    source_links = _source_links(db)
    rows: list[
        tuple[
            FiberTopologyStagedFeature,
            str,
            FiberTopologyIdentityDecision | None,
            FiberTopologyAssetSourceLink | None,
        ]
    ] = []
    counts: dict[str, int] = {}
    for feature in features:
        source_key = _feature_source_key(feature)
        link = source_links.get(source_key) if feature.external_id else None
        review_state, decision = _review_state(
            feature, decisions_by_source.get(source_key, []), link
        )
        counts[review_state] = counts.get(review_state, 0) + 1
        if normalized_state == "actionable":
            include = _is_actionable(review_state, decision)
        else:
            include = normalized_state == "all" or review_state == normalized_state
        if include:
            rows.append((feature, review_state, decision, link))

    paginated = rows[offset : offset + limit]
    page_features = [row[0] for row in paginated]
    candidates = _candidate_assets(db, page_features)
    items: list[dict] = []
    for feature, review_state, decision, link in paginated:
        source_point = _feature_point(feature)
        candidate_summaries = [
            _candidate_summary(
                feature,
                candidate_id,
                candidates[(feature.asset_type, candidate_id)],
                source_point,
            )
            for candidate_id in _coerce_candidate_ids(feature)
            if (feature.asset_type, candidate_id) in candidates
        ]
        item = {
            "asset_type": feature.asset_type,
            "batch_id": str(feature.batch_id),
            "blocker_codes": list(feature.blocker_codes or []),
            "candidate_assets": candidate_summaries,
            "content_changed_since_link": bool(
                link and link.content_sha256 != feature.content_sha256
            ),
            "decision_content_is_current": bool(
                not decision
                or decision.feature_content_sha256 == feature.content_sha256
            ),
            "display_name": feature.display_name,
            "eligible_actions": _eligible_actions(feature),
            "external_id": feature.external_id,
            "geometry_type": feature.geometry_type,
            "latest_decision": decision_to_dict(decision) if decision else None,
            "match_reasons": list(feature.match_reasons or []),
            "match_status": feature.match_status,
            "profile": feature.batch.profile,
            "review_state": review_state,
            "source_link": {
                "canonical_asset_id": str(link.canonical_asset_id),
                "canonical_asset_type": link.canonical_asset_type,
                "content_sha256": link.content_sha256,
                "id": str(link.id),
                "status": link.status,
            }
            if link
            else None,
            "source_point": {
                "latitude": source_point[1],
                "longitude": source_point[0],
            }
            if source_point
            else None,
            "staged_feature_id": str(feature.id),
        }
        if include_source_properties:
            item["source_properties"] = feature.source_properties
        items.append(item)
    return FiberIdentityReviewQueuePage(
        items=tuple(items),
        total=len(rows),
        limit=limit,
        offset=offset,
        state=normalized_state,
        counts=counts,
    )


def reconcile_identity_change_requests(
    db: Session,
    *,
    finalized_by: str,
    limit: int = 100,
) -> FiberIdentityReconcileResult:
    actor = _required_text(finalized_by, "finalized_by", limit=160)
    if limit < 1 or limit > 1000:
        raise FiberTopologyReviewError("limit must be between 1 and 1000")
    decision_ids = tuple(
        db.scalars(
            select(FiberTopologyIdentityDecision.id)
            .where(FiberTopologyIdentityDecision.status == "change_requested")
            .order_by(FiberTopologyIdentityDecision.proposed_at)
            .limit(limit)
        ).all()
    )
    applied = 0
    closed = 0
    pending = 0
    errors: list[dict] = []
    for decision_id in decision_ids:
        try:
            decision = finalize_identity_decision(db, decision_id, actor)
        except FiberTopologyIdentityError as exc:
            db.rollback()
            errors.append({"decision_id": str(decision_id), "message": str(exc)})
            continue
        if decision.status == "applied":
            applied += 1
        elif decision.status == "closed":
            closed += 1
        else:
            pending += 1
    return FiberIdentityReconcileResult(
        scanned=len(decision_ids),
        applied=applied,
        closed=closed,
        pending=pending,
        errors=tuple(errors),
    )


__all__ = [
    "FiberIdentityBatchReviewResult",
    "FiberIdentityExecutionRunResult",
    "FiberIdentityProposalBatchPreview",
    "FiberIdentityProposalBatchResult",
    "FiberIdentityReconcileResult",
    "FiberIdentityReviewQueuePage",
    "FiberTopologyProposalBatchBlocked",
    "FiberTopologyReviewError",
    "attest_identity_batch",
    "execute_identity_batch",
    "inspect_identity_batch",
    "list_identity_review_queue",
    "preview_identity_proposal_batch",
    "propose_identity_batch",
    "reconcile_identity_change_requests",
]
