"""Read-only coverage reconciliation for the complete staged point-asset cohort.

This owner derives source coverage, decision lifecycle, canonical-mutation, and
provenance evidence from immutable staged facts and reviewed identity lineage.
It never infers identity from geometry or names, creates proposals, advances
decisions, approves change requests, or mutates canonical fiber assets.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.fiber_support import FiberSupportStructure
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
from app.models.gis import ServiceBuilding
from app.models.network import FdhCabinet, FiberAccessPoint, FiberSpliceClosure
from app.services.network.fiber_topology_field_observations import (
    field_verification_state_counts,
    project_field_verification_evidence,
)
from app.services.network.fiber_topology_identity import stable_source_external_id

SUPPORTED_CREATE_OR_LINK_TYPES = frozenset(
    {"fdh_cabinet", "fiber_access_point", "splice_closure", "support_structure"}
)
SUPPORTED_LINK_ONLY_TYPES = frozenset({"service_building"})
POINT_ASSET_TYPES = (
    *sorted(SUPPORTED_CREATE_OR_LINK_TYPES),
    *sorted(SUPPORTED_LINK_ONLY_TYPES),
)
CANONICAL_MODELS: dict[str, Any] = {
    "fdh_cabinet": FdhCabinet,
    "fiber_access_point": FiberAccessPoint,
    "service_building": ServiceBuilding,
    "splice_closure": FiberSpliceClosure,
    "support_structure": FiberSupportStructure,
}

COVERAGE_STATES = (
    "source_blocked",
    "unassigned",
    "superseded_evidence",
    "ambiguous_overlapping_coverage",
    "exact",
)
LIFECYCLE_STATES = (
    "source_identity_blocked",
    "missing_identity_decision",
    "stale_source_evidence",
    "overlapping_decision_evidence",
    "missing_batch_evidence",
    "batch_evidence_drift",
    "pending_review",
    "declined",
    "pending_execution",
    "execution_failed",
    "execution_evidence_drift",
    "pending_canonical_mutation",
    "pending_result_reconciliation",
    "applied_current",
    "provenance_drift",
    "rejected_current",
    "change_rejected",
    "other_closed",
)
ACTIVE_DECISION_STATUSES = {"proposed", "approved", "change_requested"}
EXECUTED_DECISION_STATUSES = {"change_requested", "applied", "closed"}
RUN_BLOCKER_CODES = {"run_outcomes_invalid", "run_result_evidence_mismatch"}


class FiberTopologyIdentityCoverageError(ValueError):
    """Raised when identity coverage cannot be derived consistently."""


@dataclass(frozen=True)
class FiberTopologyIdentityCoverageReport:
    coverage_report_sha256: str
    staged_point_count: int
    supported_point_count: int
    source_batch_count: int
    assets: tuple[dict[str, object], ...]
    lineages: tuple[dict[str, object], ...]
    asset_type_counts: dict[str, int]
    coverage_counts: dict[str, int]
    lifecycle_counts: dict[str, int]
    decision_counts: dict[str, int]
    field_verification_counts: dict[str, int]
    batch_evidence_blockers: tuple[dict[str, object], ...]
    gates: tuple[dict[str, object], ...]
    ready_for_point_identity_cutover_review: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_type_counts": self.asset_type_counts,
            "assets": list(self.assets),
            "batch_evidence_blockers": list(self.batch_evidence_blockers),
            "coverage_counts": self.coverage_counts,
            "coverage_report_sha256": self.coverage_report_sha256,
            "decision_counts": self.decision_counts,
            "field_verification_counts": self.field_verification_counts,
            "gates": list(self.gates),
            "lifecycle_counts": self.lifecycle_counts,
            "lineages": list(self.lineages),
            "ready_for_point_identity_cutover_review": (
                self.ready_for_point_identity_cutover_review
            ),
            "schema_version": 2,
            "source_batch_count": self.source_batch_count,
            "staged_point_count": self.staged_point_count,
            "supported_point_count": self.supported_point_count,
        }


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value) -> str | None:
    return value.isoformat() if value else None


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _counts(values: list[str], known: tuple[str, ...]) -> dict[str, int]:
    counter = Counter(values)
    return {name: counter[name] for name in known}


def _gate(code: str, ready: bool, detail: str) -> dict[str, object]:
    return {"code": code, "detail": detail, "ready": ready}


def ensure_identity_coverage_repeatable_snapshot(db: Session) -> None:
    """Require one consistent PostgreSQL snapshot for every exhaustive read."""

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not db.in_transaction():
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return
    isolation_level = db.connection().get_isolation_level().upper()
    if isolation_level not in {"REPEATABLE READ", "SERIALIZABLE"}:
        raise FiberTopologyIdentityCoverageError(
            "point-identity coverage requires a fresh REPEATABLE READ transaction"
        )


def _source_key_from_feature(
    feature: FiberTopologyStagedFeature,
) -> tuple[str, str, str]:
    identity = feature.external_id or f"feature:{feature.id}"
    return feature.batch.source_system, feature.asset_type, identity


def _source_key_from_decision(
    decision: FiberTopologyIdentityDecision,
) -> tuple[str, str, str]:
    identity = decision.source_external_id or f"feature:{decision.staged_feature_id}"
    return decision.source_system, decision.source_asset_type, identity


def _latest_staged_points(db: Session) -> list[FiberTopologyStagedFeature]:
    rows = list(
        db.scalars(
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
        .unique()
        .all()
    )
    latest: list[FiberTopologyStagedFeature] = []
    seen: set[tuple[str, str, str]] = set()
    for feature in rows:
        source_key = _source_key_from_feature(feature)
        if source_key in seen:
            continue
        seen.add(source_key)
        latest.append(feature)
    return sorted(latest, key=_source_key_from_feature)


def _decision_digest(decision: FiberTopologyIdentityDecision) -> str:
    return _digest(
        {
            "action": decision.action,
            "feature_content_sha256": decision.feature_content_sha256,
            "proposed_by": decision.proposed_by,
            "reason": decision.reason,
            "staged_feature_id": str(decision.staged_feature_id),
            "target_asset_id": (
                str(decision.target_asset_id) if decision.target_asset_id else None
            ),
            "target_asset_type": decision.target_asset_type,
        }
    )


def _decision_manifest_item(
    decision: FiberTopologyIdentityDecision, row_number: int
) -> dict[str, object]:
    return {
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
        "target_asset_id": (
            str(decision.target_asset_id) if decision.target_asset_id else None
        ),
        "target_asset_type": decision.target_asset_type,
    }


def _manifest_items(batch: FiberTopologyIdentityProposalBatch) -> list[dict]:
    items = batch.manifest_payload.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise FiberTopologyIdentityCoverageError(
            "stored point-identity batch manifest items are invalid"
        )
    return cast(list[dict], items)


def _review_attestation_sha256(
    batch: FiberTopologyIdentityProposalBatch,
    review: FiberTopologyIdentityBatchReview,
) -> str:
    return _digest(
        {
            "action": review.action,
            "batch_id": str(batch.id),
            "batch_manifest_sha256": review.batch_manifest_sha256,
            "item_count": review.item_count,
            "proposed_by": review.proposed_by,
            "review_notes": review.review_notes,
            "reviewed_by": review.reviewed_by,
            "schema_version": 1,
        }
    )


def _batch_blockers(
    batch: FiberTopologyIdentityProposalBatch,
    decisions: list[FiberTopologyIdentityDecision],
    review: FiberTopologyIdentityBatchReview | None,
    runs: list[FiberTopologyIdentityExecutionRun],
) -> list[dict[str, object]]:
    blockers: list[dict[str, object]] = []
    try:
        items = _manifest_items(batch)
    except FiberTopologyIdentityCoverageError as exc:
        return [{"code": "invalid_manifest_items", "message": str(exc)}]
    if _digest(batch.manifest_payload) != batch.manifest_sha256:
        blockers.append(
            {
                "code": "manifest_digest_mismatch",
                "message": "stored manifest payload does not match its SHA-256",
            }
        )
    if batch.manifest_payload.get("request_sha256") != batch.request_sha256:
        blockers.append(
            {
                "code": "request_digest_reference_mismatch",
                "message": "manifest request digest does not match the batch",
            }
        )
    if len(items) != batch.item_count or len(decisions) != batch.item_count:
        blockers.append(
            {
                "code": "batch_item_count_mismatch",
                "message": "manifest and delegated decision counts differ",
            }
        )
    else:
        for row_number, (item, decision) in enumerate(
            zip(items, decisions, strict=True), start=1
        ):
            if (
                decision.proposal_batch_row_number != row_number
                or item != _decision_manifest_item(decision, row_number)
                or decision.decision_sha256 != _decision_digest(decision)
            ):
                blockers.append(
                    {
                        "code": "batch_row_evidence_mismatch",
                        "decision_id": str(decision.id),
                        "message": f"batch row {row_number} differs from its decision",
                        "row_number": row_number,
                    }
                )
    if review:
        if (
            review.batch_manifest_sha256 != batch.manifest_sha256
            or review.proposal_batch_id != batch.id
            or review.proposed_by != batch.proposed_by
            or review.item_count != batch.item_count
            or _review_attestation_sha256(batch, review) != review.attestation_sha256
        ):
            blockers.append(
                {
                    "code": "batch_review_attestation_mismatch",
                    "message": "stored batch review does not match its exact manifest",
                }
            )
        expected_statuses = (
            {"declined"}
            if review.action == "decline"
            else {"approved", "change_requested", "applied", "closed"}
        )
        if any(
            decision.status not in expected_statuses
            or decision.reviewed_by != review.reviewed_by
            or decision.review_notes != review.review_notes
            for decision in decisions
        ):
            blockers.append(
                {
                    "code": "delegated_review_evidence_mismatch",
                    "message": "delegated decision review evidence differs from the batch attestation",
                }
            )
    elif any(decision.status != "proposed" for decision in decisions):
        blockers.append(
            {
                "code": "missing_batch_review_attestation",
                "message": "delegated decisions advanced without a batch review attestation",
            }
        )
    decision_ids = {str(decision.id) for decision in decisions}
    for run in runs:
        outcomes = run.result_payload.get("outcomes")
        if not isinstance(outcomes, list):
            blockers.append(
                {
                    "code": "run_outcomes_invalid",
                    "message": "stored run outcomes are not a list",
                    "run_id": str(run.id),
                }
            )
            continue
        outcome_rows = [item for item in outcomes if isinstance(item, dict)]
        outcome_ids = [str(item.get("decision_id")) for item in outcome_rows]
        outcome_counts = Counter(str(item.get("outcome")) for item in outcome_rows)
        payload_matches_columns = bool(
            run.result_payload.get("batch_id") == str(batch.id)
            and run.result_payload.get("batch_manifest_sha256") == batch.manifest_sha256
            and run.result_payload.get("executed_by") == run.executed_by
            and run.result_payload.get("execution_run_id") == str(run.id)
            and run.result_payload.get("remaining_approved_count")
            == run.remaining_approved_count
            and run.result_payload.get("requested_limit") == run.requested_limit
            and run.result_payload.get("schema_version") == 1
        )
        if (
            _digest(run.result_payload) != run.result_sha256
            or run.proposal_batch_id != batch.id
            or review is None
            or run.batch_review_id != review.id
            or run.batch_manifest_sha256 != batch.manifest_sha256
            or not payload_matches_columns
            or len(outcome_rows) != len(outcomes)
            or len(outcome_ids) != len(set(outcome_ids))
            or any(decision_id not in decision_ids for decision_id in outcome_ids)
            or run.scanned_count != len(outcomes)
            or run.change_requested_count != outcome_counts["change_requested"]
            or run.applied_count != outcome_counts["applied"]
            or run.closed_count != outcome_counts["closed"]
            or run.error_count != outcome_counts["error"]
        ):
            blockers.append(
                {
                    "code": "run_result_evidence_mismatch",
                    "message": "stored run evidence does not match its exact outcomes",
                    "run_id": str(run.id),
                }
            )
    return blockers


def _decision_run_evidence(
    decision: FiberTopologyIdentityDecision,
    runs: list[FiberTopologyIdentityExecutionRun],
    invalid_run_ids: set[str],
) -> tuple[str, list[dict[str, object]]]:
    if decision.status in {"proposed", "declined"}:
        return "not_required", []
    evidence: list[dict[str, object]] = []
    for run in runs:
        outcomes = run.result_payload.get("outcomes")
        if not isinstance(outcomes, list):
            continue
        for outcome in outcomes:
            if not isinstance(outcome, dict) or outcome.get("decision_id") != str(
                decision.id
            ):
                continue
            evidence.append(
                {
                    "outcome": str(outcome.get("outcome")),
                    "result_sha256": run.result_sha256,
                    "run_id": str(run.id),
                }
            )
    if not evidence:
        return (
            "missing"
            if decision.status in EXECUTED_DECISION_STATUSES
            else "not_required"
        ), evidence
    latest = evidence[-1]
    if str(latest["run_id"]) in invalid_run_ids:
        return "invalid", evidence
    outcome = str(latest["outcome"])
    if decision.status == "approved" and outcome == "error":
        return "failed", evidence
    if outcome == decision.status:
        return "current", evidence
    if (
        decision.action == "create"
        and outcome == "change_requested"
        and decision.status in {"applied", "closed"}
        and decision.finalized_at is not None
        and decision.finalized_by
    ):
        return "current", evidence
    return "stale", evidence


def _change_request_evidence(
    decision: FiberTopologyIdentityDecision,
    feature: FiberTopologyStagedFeature,
) -> tuple[bool, dict[str, object] | None]:
    request = decision.change_request
    if decision.action != "create":
        return request is None, None
    if request is None:
        return decision.status in {"proposed", "approved", "declined"}, None
    notes_evidence: dict[str, object] = {}
    raw_notes = (
        request.payload.get("notes") if isinstance(request.payload, dict) else None
    )
    if isinstance(raw_notes, str):
        try:
            parsed = json.loads(raw_notes)
            if isinstance(parsed, dict):
                notes_evidence = parsed
        except json.JSONDecodeError:
            notes_evidence = {}
    notes_current = notes_evidence == {
        "fiber_topology_identity_decision_id": str(decision.id),
        "source_content_sha256": feature.content_sha256,
        "source_external_id": feature.external_id,
        "source_profile": feature.batch.profile,
        "source_system": feature.batch.source_system,
        "staged_feature_id": str(feature.id),
    }
    current = bool(
        request.asset_type == feature.asset_type
        and _enum_value(request.operation) == "create"
        and notes_current
    )
    return current, {
        "asset_id": str(request.asset_id) if request.asset_id else None,
        "asset_type": request.asset_type,
        "change_request_id": str(request.id),
        "notes_evidence_current": notes_current,
        "operation": _enum_value(request.operation),
        "payload_sha256": _digest(request.payload),
        "status": _enum_value(request.status),
    }


def _provenance_evidence(
    decision: FiberTopologyIdentityDecision,
    feature: FiberTopologyStagedFeature,
    link: FiberTopologyAssetSourceLink | None,
    canonical_assets: dict[tuple[str, str], object],
    change_request_current: bool,
) -> tuple[bool, dict[str, object]]:
    request = decision.change_request
    expected_asset_id = (
        decision.target_asset_id
        if decision.action == "link_existing"
        else request.asset_id
        if request
        else None
    )
    asset = (
        canonical_assets.get((feature.asset_type, str(expected_asset_id)))
        if expected_asset_id
        else None
    )
    canonical_current = bool(asset and getattr(asset, "is_active", False))
    expected_external_id = stable_source_external_id(
        feature.batch.source_system, feature.asset_type, feature.external_id
    )
    link_current = bool(
        link
        and link.status == "active"
        and link.decision_id == decision.id
        and link.staged_feature_id == feature.id
        and link.source_system == feature.batch.source_system
        and link.source_profile == feature.batch.profile
        and link.source_asset_type == feature.asset_type
        and link.external_id == expected_external_id
        and link.content_sha256 == feature.content_sha256
        and link.canonical_asset_type == feature.asset_type
        and link.canonical_asset_id == expected_asset_id
    )
    request_result_current = bool(
        decision.action != "create"
        or (
            request
            and _enum_value(request.status) == "applied"
            and request.asset_id == expected_asset_id
            and change_request_current
        )
    )
    return canonical_current and link_current and request_result_current, {
        "canonical_asset_active": canonical_current,
        "canonical_asset_id": str(expected_asset_id) if expected_asset_id else None,
        "canonical_asset_type": feature.asset_type,
        "change_request_result_current": request_result_current,
        "source_link_content_sha256": link.content_sha256 if link else None,
        "source_link_id": str(link.id) if link else None,
        "source_link_status": link.status if link else None,
        "source_link_valid": link_current,
    }


def _decision_summary(
    decision: FiberTopologyIdentityDecision,
    *,
    batch: FiberTopologyIdentityProposalBatch | None,
    review: FiberTopologyIdentityBatchReview | None,
    batch_blockers: list[dict[str, object]],
    run_state: str,
    run_evidence: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "action": decision.action,
        "batch_evidence_state": (
            "missing" if batch is None else "invalid" if batch_blockers else "current"
        ),
        "batch_id": str(batch.id) if batch else None,
        "batch_manifest_sha256": batch.manifest_sha256 if batch else None,
        "change_request_id": (
            str(decision.change_request_id) if decision.change_request_id else None
        ),
        "closed_reason": decision.closed_reason,
        "decision_digest_current": (
            decision.decision_sha256 == _decision_digest(decision)
        ),
        "decision_id": str(decision.id),
        "decision_sha256": decision.decision_sha256,
        "execution_evidence": run_evidence,
        "execution_evidence_state": run_state,
        "feature_content_sha256": decision.feature_content_sha256,
        "finalized_at": _timestamp(decision.finalized_at),
        "proposed_at": _timestamp(decision.proposed_at),
        "review_action": review.action if review else None,
        "review_attestation_sha256": review.attestation_sha256 if review else None,
        "status": decision.status,
        "target_asset_id": (
            str(decision.target_asset_id) if decision.target_asset_id else None
        ),
        "target_asset_type": decision.target_asset_type,
    }


def _selected_decision(
    exact: list[FiberTopologyIdentityDecision],
) -> tuple[FiberTopologyIdentityDecision | None, bool]:
    applicable = [
        decision
        for decision in exact
        if decision.status in ACTIVE_DECISION_STATUSES
        or decision.status == "applied"
        or (
            decision.status == "closed"
            and decision.action == "reject"
            and decision.closed_reason == "source_identity_rejected"
        )
    ]
    if len(applicable) > 1:
        return None, True
    if applicable:
        return applicable[0], False
    return (exact[0], False) if exact else (None, False)


def _lifecycle_state(
    decision: FiberTopologyIdentityDecision,
    *,
    batch: FiberTopologyIdentityProposalBatch | None,
    review: FiberTopologyIdentityBatchReview | None,
    batch_blockers: list[dict[str, object]],
    execution_evidence_state: str,
    change_request_current: bool,
    change_request_evidence: dict[str, object] | None,
    provenance_current: bool,
) -> str:
    if batch is None:
        return "missing_batch_evidence"
    if batch_blockers or decision.decision_sha256 != _decision_digest(decision):
        return "batch_evidence_drift"
    if decision.status == "proposed":
        return "batch_evidence_drift" if review else "pending_review"
    expected_review = "decline" if decision.status == "declined" else "approve"
    if review is None or review.action != expected_review:
        return "batch_evidence_drift"
    if decision.status == "declined":
        return "declined"
    if decision.status == "approved":
        if execution_evidence_state == "failed":
            return "execution_failed"
        if execution_evidence_state != "not_required":
            return "execution_evidence_drift"
        return "pending_execution"
    if execution_evidence_state != "current" or not change_request_current:
        return "execution_evidence_drift"
    if decision.status == "change_requested":
        request_status = (
            str(change_request_evidence.get("status"))
            if change_request_evidence
            else None
        )
        return (
            "pending_canonical_mutation"
            if request_status == "pending"
            else "pending_result_reconciliation"
        )
    if decision.status == "applied":
        return "applied_current" if provenance_current else "provenance_drift"
    if (
        decision.action == "reject"
        and decision.closed_reason == "source_identity_rejected"
    ):
        return "rejected_current"
    if (
        decision.action == "create"
        and decision.closed_reason == "fiber_change_request_rejected"
        and change_request_evidence
        and change_request_evidence.get("status") == "rejected"
    ):
        return "change_rejected"
    return "other_closed"


def _model_state(asset_type: str) -> str:
    if asset_type in SUPPORTED_CREATE_OR_LINK_TYPES:
        return "supported_create_or_link"
    if asset_type in SUPPORTED_LINK_ONLY_TYPES:
        return "supported_link_only"
    raise FiberTopologyIdentityCoverageError(
        f"unsupported staged point asset type: {asset_type}"
    )


def _source_blocked(feature: FiberTopologyStagedFeature) -> bool:
    geometry = feature.geometry_geojson
    return bool(
        feature.external_id is None
        or feature.match_status == "blocked"
        or feature.geometry_type not in {"Point", "Polygon", "MultiPolygon"}
        or not isinstance(geometry, dict)
        or not isinstance(geometry.get("coordinates"), list)
        or not geometry.get("coordinates")
    )


def reconcile_fiber_identity_coverage(
    db: Session,
) -> FiberTopologyIdentityCoverageReport:
    """Reconcile every latest staged point identity without writing state."""

    ensure_identity_coverage_repeatable_snapshot(db)
    features = _latest_staged_points(db)
    field_verification = project_field_verification_evidence(db, features)
    decisions = list(
        db.scalars(
            select(FiberTopologyIdentityDecision)
            .options(joinedload(FiberTopologyIdentityDecision.change_request))
            .where(
                FiberTopologyIdentityDecision.source_asset_type.in_(POINT_ASSET_TYPES)
            )
            .order_by(
                FiberTopologyIdentityDecision.proposed_at.desc(),
                FiberTopologyIdentityDecision.id.desc(),
            )
        ).all()
    )
    batches = list(
        db.scalars(
            select(FiberTopologyIdentityProposalBatch).order_by(
                FiberTopologyIdentityProposalBatch.created_at,
                FiberTopologyIdentityProposalBatch.id,
            )
        ).all()
    )
    reviews = list(db.scalars(select(FiberTopologyIdentityBatchReview)).all())
    runs = list(
        db.scalars(
            select(FiberTopologyIdentityExecutionRun).order_by(
                FiberTopologyIdentityExecutionRun.executed_at,
                FiberTopologyIdentityExecutionRun.id,
            )
        ).all()
    )
    links = list(db.scalars(select(FiberTopologyAssetSourceLink)).all())

    decisions_by_source: dict[
        tuple[str, str, str], list[FiberTopologyIdentityDecision]
    ] = defaultdict(list)
    decisions_by_batch: dict[str, list[FiberTopologyIdentityDecision]] = defaultdict(
        list
    )
    for decision in decisions:
        decisions_by_source[_source_key_from_decision(decision)].append(decision)
        if decision.proposal_batch_id:
            decisions_by_batch[str(decision.proposal_batch_id)].append(decision)
    for rows in decisions_by_batch.values():
        rows.sort(key=lambda item: item.proposal_batch_row_number or 0)
    batch_by_id = {str(batch.id): batch for batch in batches}
    review_by_batch = {str(review.proposal_batch_id): review for review in reviews}
    runs_by_batch: dict[str, list[FiberTopologyIdentityExecutionRun]] = defaultdict(
        list
    )
    for run in runs:
        runs_by_batch[str(run.proposal_batch_id)].append(run)
    link_by_decision = {str(link.decision_id): link for link in links}

    canonical_ids_by_type: dict[str, set[str]] = defaultdict(set)
    for link in links:
        if link.canonical_asset_type in CANONICAL_MODELS:
            canonical_ids_by_type[link.canonical_asset_type].add(
                str(link.canonical_asset_id)
            )
    canonical_assets: dict[tuple[str, str], object] = {}
    for asset_type, asset_ids in canonical_ids_by_type.items():
        model = CANONICAL_MODELS[asset_type]
        for asset in db.scalars(select(model).where(model.id.in_(asset_ids))).all():
            canonical_assets[(asset_type, str(asset.id))] = asset

    blockers: list[dict[str, object]] = []
    blockers_by_batch: dict[str, list[dict[str, object]]] = {}
    invalid_run_ids_by_batch: dict[str, set[str]] = defaultdict(set)
    for proposal_batch in batches:
        batch_id = str(proposal_batch.id)
        batch_blockers = _batch_blockers(
            proposal_batch,
            decisions_by_batch.get(batch_id, []),
            review_by_batch.get(batch_id),
            runs_by_batch.get(batch_id, []),
        )
        blockers_by_batch[batch_id] = batch_blockers
        for blocker in batch_blockers:
            blockers.append({"batch_id": batch_id, **blocker})
            if blocker["code"] in RUN_BLOCKER_CODES:
                invalid_run_ids_by_batch[batch_id].add(str(blocker.get("run_id")))

    lineage_rows: list[dict[str, object]] = []
    lineage_by_decision: dict[str, dict[str, object]] = {}
    for decision in decisions:
        batch_id = str(decision.proposal_batch_id) if decision.proposal_batch_id else ""
        integrity_blockers = [
            blocker
            for blocker in blockers_by_batch.get(batch_id, [])
            if blocker["code"] not in RUN_BLOCKER_CODES
        ]
        run_state, run_evidence = _decision_run_evidence(
            decision,
            runs_by_batch.get(batch_id, []),
            invalid_run_ids_by_batch.get(batch_id, set()),
        )
        row = _decision_summary(
            decision,
            batch=batch_by_id.get(batch_id),
            review=review_by_batch.get(batch_id),
            batch_blockers=integrity_blockers,
            run_state=run_state,
            run_evidence=run_evidence,
        )
        lineage_rows.append(row)
        lineage_by_decision[str(decision.id)] = row

    coverage_states: list[str] = []
    lifecycle_states: list[str] = []
    selected_decision_statuses: list[str] = []
    asset_rows: list[dict[str, object]] = []
    source_batch_ids: set[str] = set()
    for feature in features:
        source_batch_ids.add(str(feature.batch_id))
        source_key = _source_key_from_feature(feature)
        candidates = decisions_by_source.get(source_key, [])
        exact = [
            decision
            for decision in candidates
            if decision.feature_content_sha256 == feature.content_sha256
        ]
        selected, overlapping = _selected_decision(exact)
        change_evidence: dict[str, object] | None = None
        provenance: dict[str, object] | None = None
        if _source_blocked(feature):
            coverage_state = "source_blocked"
            lifecycle_state = "source_identity_blocked"
        elif overlapping:
            coverage_state = "ambiguous_overlapping_coverage"
            lifecycle_state = "overlapping_decision_evidence"
        elif selected:
            coverage_state = "exact"
            batch_id = (
                str(selected.proposal_batch_id) if selected.proposal_batch_id else ""
            )
            change_current, change_evidence = _change_request_evidence(
                selected, feature
            )
            provenance_current, provenance = _provenance_evidence(
                selected,
                feature,
                link_by_decision.get(str(selected.id)),
                canonical_assets,
                change_current,
            )
            lifecycle_state = _lifecycle_state(
                selected,
                batch=batch_by_id.get(batch_id),
                review=review_by_batch.get(batch_id),
                batch_blockers=[
                    blocker
                    for blocker in blockers_by_batch.get(batch_id, [])
                    if blocker["code"] not in RUN_BLOCKER_CODES
                ],
                execution_evidence_state=str(
                    lineage_by_decision[str(selected.id)]["execution_evidence_state"]
                ),
                change_request_current=change_current,
                change_request_evidence=change_evidence,
                provenance_current=provenance_current,
            )
            selected_decision_statuses.append(selected.status)
        elif candidates:
            coverage_state = "superseded_evidence"
            lifecycle_state = "stale_source_evidence"
        else:
            coverage_state = "unassigned"
            lifecycle_state = "missing_identity_decision"
        coverage_states.append(coverage_state)
        lifecycle_states.append(lifecycle_state)
        selected_id = str(selected.id) if selected else None
        asset_rows.append(
            {
                "asset_type": feature.asset_type,
                "batch_id": str(feature.batch_id),
                "blocker_codes": list(feature.blocker_codes or []),
                "canonical_model_state": _model_state(feature.asset_type),
                "change_request_evidence": change_evidence,
                "content_sha256": feature.content_sha256,
                "coverage_state": coverage_state,
                "display_name": feature.display_name,
                "exact_decision_ids": [str(decision.id) for decision in exact],
                "external_id": feature.external_id,
                "field_verification": field_verification[str(feature.id)],
                "geometry_sha256": feature.geometry_sha256,
                "lifecycle_state": lifecycle_state,
                "match_status": feature.match_status,
                "profile": feature.batch.profile,
                "provenance_evidence": provenance if selected else None,
                "selected_decision": (
                    lineage_by_decision[selected_id] if selected_id else None
                ),
                "source_system": feature.batch.source_system,
                "staged_feature_id": str(feature.id),
                "superseded_decision_ids": [
                    str(decision.id) for decision in candidates if decision not in exact
                ],
            }
        )

    coverage_counts = _counts(coverage_states, COVERAGE_STATES)
    lifecycle_counts = _counts(lifecycle_states, LIFECYCLE_STATES)
    decision_counts = _counts(
        selected_decision_statuses,
        ("proposed", "approved", "declined", "change_requested", "applied", "closed"),
    )
    field_verification_counts = field_verification_state_counts(field_verification)
    asset_type_counts = _counts(
        [feature.asset_type for feature in features], POINT_ASSET_TYPES
    )
    supported_count = sum(
        asset_type_counts[asset_type]
        for asset_type in (*SUPPORTED_CREATE_OR_LINK_TYPES, *SUPPORTED_LINK_ONLY_TYPES)
    )
    cohort_nonempty = bool(features)
    source_reviewable = coverage_counts["source_blocked"] == 0
    exact_once = coverage_counts["exact"] == len(features)
    evidence_current = all(
        lifecycle_counts[state] == 0
        for state in (
            "missing_batch_evidence",
            "batch_evidence_drift",
            "execution_evidence_drift",
        )
    )
    no_pending_review = lifecycle_counts["pending_review"] == 0
    no_pending_execution = (
        lifecycle_counts["pending_execution"] == 0
        and lifecycle_counts["execution_failed"] == 0
    )
    no_pending_mutation = (
        lifecycle_counts["pending_canonical_mutation"] == 0
        and lifecycle_counts["pending_result_reconciliation"] == 0
    )
    supported_terminal = (
        all(
            row["lifecycle_state"] in {"applied_current", "rejected_current"}
            for row in asset_rows
            if row["asset_type"] in POINT_ASSET_TYPES
        )
        and supported_count > 0
    )
    support_structure_terminal = all(
        row["lifecycle_state"] in {"applied_current", "rejected_current"}
        for row in asset_rows
        if row["asset_type"] == "support_structure"
    )
    gates = (
        _gate(
            "complete_latest_staged_point_cohort_nonempty",
            cohort_nonempty,
            "The report scanned every latest staged point-asset source identity and the cohort is non-empty.",
        ),
        _gate(
            "latest_staged_point_sources_reviewable",
            source_reviewable,
            "No latest point has blocked identity or unusable point/polygon evidence.",
        ),
        _gate(
            "latest_point_identities_exactly_covered_once",
            exact_once,
            "Every latest point identity has exactly one applicable decision for its exact current content.",
        ),
        _gate(
            "batch_review_and_run_evidence_current",
            evidence_current and not blockers,
            "Every selected decision has intact manifest, independent review, and required execution evidence.",
        ),
        _gate(
            "no_pending_point_identity_review",
            no_pending_review,
            "No selected point-identity decision remains proposed.",
        ),
        _gate(
            "no_pending_point_identity_execution",
            no_pending_execution,
            "No selected point-identity decision remains approved but unexecuted.",
        ),
        _gate(
            "no_pending_canonical_point_mutation_or_reconciliation",
            no_pending_mutation,
            "No selected identity waits for canonical change-request review or result reconciliation.",
        ),
        _gate(
            "supported_point_identities_terminal_current",
            supported_terminal,
            "Every supported cabinet, FAT, closure, building, or support is applied with current provenance or explicitly reviewed and rejected.",
        ),
        _gate(
            "support_structure_identities_terminal_current",
            support_structure_terminal,
            "Every staged pole/support identity is applied to the canonical support owner with current provenance or explicitly reviewed and rejected.",
        ),
    )
    ready = all(bool(gate["ready"]) for gate in gates)
    report_payload: dict[str, object] = {
        "asset_type_counts": asset_type_counts,
        "assets": asset_rows,
        "batch_evidence_blockers": blockers,
        "coverage_counts": coverage_counts,
        "decision_counts": decision_counts,
        "field_verification_counts": field_verification_counts,
        "gates": list(gates),
        "lifecycle_counts": lifecycle_counts,
        "lineages": lineage_rows,
        "ready_for_point_identity_cutover_review": ready,
        "schema_version": 2,
        "source_batch_count": len(source_batch_ids),
        "staged_point_count": len(features),
        "supported_point_count": supported_count,
    }
    return FiberTopologyIdentityCoverageReport(
        coverage_report_sha256=_digest(report_payload),
        staged_point_count=len(features),
        supported_point_count=supported_count,
        source_batch_count=len(source_batch_ids),
        assets=tuple(asset_rows),
        lineages=tuple(lineage_rows),
        asset_type_counts=asset_type_counts,
        coverage_counts=coverage_counts,
        lifecycle_counts=lifecycle_counts,
        decision_counts=decision_counts,
        field_verification_counts=field_verification_counts,
        batch_evidence_blockers=tuple(blockers),
        gates=gates,
        ready_for_point_identity_cutover_review=ready,
    )


__all__ = [
    "COVERAGE_STATES",
    "LIFECYCLE_STATES",
    "POINT_ASSET_TYPES",
    "SUPPORTED_CREATE_OR_LINK_TYPES",
    "SUPPORTED_LINK_ONLY_TYPES",
    "FiberTopologyIdentityCoverageError",
    "FiberTopologyIdentityCoverageReport",
    "ensure_identity_coverage_repeatable_snapshot",
    "reconcile_fiber_identity_coverage",
]
