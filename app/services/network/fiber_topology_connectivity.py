"""Reviewed source-to-termination connectivity decisions.

Staged route geometry is evidence only. This owner decides explicit endpoint
identity and delegates every canonical termination/segment mutation to the
reviewed fiber-change-request owner.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.fiber_change_request import (
    FiberChangeRequest,
    FiberChangeRequestOperation,
    FiberChangeRequestStatus,
)
from app.models.fiber_topology_connectivity import (
    FiberTopologyConnectivityDecision,
    FiberTopologySegmentSourceLink,
    FiberTopologyTerminationResolution,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberCableType,
    FiberSegment,
    FiberSegmentType,
    FiberSpliceClosure,
    FiberTerminationPoint,
    ODNEndpointType,
    OntUnit,
    PonPort,
    SplitterPort,
)
from app.services import fiber_change_requests

ACTIVE_STATUSES = (
    "proposed",
    "approved",
    "endpoint_change_requested",
    "segment_change_requested",
)
ENDPOINT_MODELS: dict[str, type] = {
    ODNEndpointType.fdh.value: FdhCabinet,
    ODNEndpointType.fiber_access_point.value: FiberAccessPoint,
    ODNEndpointType.ont.value: OntUnit,
    ODNEndpointType.pon_port.value: PonPort,
    ODNEndpointType.splice_closure.value: FiberSpliceClosure,
    ODNEndpointType.splitter_port.value: SplitterPort,
}


class FiberTopologyConnectivityError(ValueError):
    """Raised when a reviewed connectivity transition is invalid."""


@dataclass(frozen=True)
class FiberConnectivityDecisionPreview:
    """Validated, write-free evidence for one proposed connectivity decision."""

    item: dict
    existing_decision_id: uuid.UUID | None = None

    def to_manifest_dict(self, *, row_number: int) -> dict:
        return {**self.item, "row_number": row_number}


@dataclass(frozen=True)
class FiberConnectivityReconcileResult:
    scanned: int
    applied: int
    closed: int
    endpoint_pending: int
    segment_pending: int
    errors: tuple[dict, ...]

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "closed": self.closed,
            "endpoint_pending": self.endpoint_pending,
            "error_count": len(self.errors),
            "errors": list(self.errors),
            "scanned": self.scanned,
            "segment_pending": self.segment_pending,
        }


def _required_text(value: object, field: str, *, limit: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise FiberTopologyConnectivityError(f"{field} is required")
    if len(normalized) > limit:
        raise FiberTopologyConnectivityError(
            f"{field} must be at most {limit} characters"
        )
    return normalized


def _coerce_uuid(value: object, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberTopologyConnectivityError(f"{field} must be a UUID") from exc


def _load_feature(
    db: Session, staged_feature_id: str | uuid.UUID
) -> FiberTopologyStagedFeature:
    feature = db.scalar(
        select(FiberTopologyStagedFeature)
        .options(joinedload(FiberTopologyStagedFeature.batch))
        .where(
            FiberTopologyStagedFeature.id
            == _coerce_uuid(staged_feature_id, "staged_feature_id")
        )
    )
    if feature is None:
        raise FiberTopologyConnectivityError("staged feature not found")
    if feature.asset_type != "fiber_segment":
        raise FiberTopologyConnectivityError(
            "connectivity decisions require a staged fiber_segment"
        )
    if feature.match_status == "blocked" or feature.external_id is None:
        raise FiberTopologyConnectivityError("staged path identity is blocked")
    if feature.geometry_type != "LineString":
        raise FiberTopologyConnectivityError(
            "staged path must preserve explicit LineString geometry evidence"
        )
    coordinates = feature.geometry_geojson.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise FiberTopologyConnectivityError("staged path geometry is incomplete")
    return feature


def _assert_source_current(
    db: Session, feature: FiberTopologyStagedFeature, expected_sha256: str | None = None
) -> FiberTopologyStagedFeature:
    if expected_sha256 and feature.content_sha256 != expected_sha256:
        raise FiberTopologyConnectivityError(
            "staged path content changed after the connectivity decision"
        )
    latest = db.scalar(
        select(FiberTopologyStagedFeature)
        .join(FiberTopologyStagedFeature.batch)
        .where(
            FiberTopologySourceBatch.source_system == feature.batch.source_system,
            FiberTopologyStagedFeature.asset_type == feature.asset_type,
            FiberTopologyStagedFeature.external_id == feature.external_id,
        )
        .order_by(
            FiberTopologySourceBatch.created_at.desc(),
            FiberTopologyStagedFeature.created_at.desc(),
            FiberTopologyStagedFeature.id.desc(),
        )
    )
    if latest and latest.content_sha256 != feature.content_sha256:
        raise FiberTopologyConnectivityError(
            "a newer staged version changed this path; review the latest feature"
        )
    return feature


def _load_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    for_update: bool = False,
) -> FiberTopologyConnectivityDecision:
    statement = (
        select(FiberTopologyConnectivityDecision)
        .options(
            joinedload(FiberTopologyConnectivityDecision.staged_feature).joinedload(
                FiberTopologyStagedFeature.batch
            )
        )
        .where(
            FiberTopologyConnectivityDecision.id
            == _coerce_uuid(decision_id, "decision_id")
        )
    )
    if for_update:
        # Lock only the decision row: the joinedload outer joins make an
        # unqualified FOR UPDATE fail on PostgreSQL (nullable side of join).
        statement = statement.with_for_update(of=FiberTopologyConnectivityDecision)
    decision = db.scalar(statement)
    if decision is None:
        raise FiberTopologyConnectivityError("connectivity decision not found")
    return decision


def _normalize_endpoint(
    db: Session,
    endpoint_type: object,
    endpoint_ref_id: object,
    *,
    prefix: str,
) -> tuple[str, uuid.UUID, object]:
    normalized_type = str(endpoint_type or "").strip().lower()
    model = ENDPOINT_MODELS.get(normalized_type)
    if model is None:
        raise FiberTopologyConnectivityError(
            f"{prefix}_endpoint_type is not an approved canonical endpoint"
        )
    ref_id = _coerce_uuid(endpoint_ref_id, f"{prefix}_endpoint_ref_id")
    endpoint = db.get(model, ref_id)
    if endpoint is None:
        raise FiberTopologyConnectivityError(
            f"{prefix} canonical endpoint asset not found"
        )
    if getattr(endpoint, "is_active", True) is False:
        raise FiberTopologyConnectivityError(
            f"{prefix} canonical endpoint asset is inactive"
        )
    return normalized_type, ref_id, endpoint


def _segment_route_is_present(segment: FiberSegment) -> bool:
    return segment.route_geom is not None


def _validate_existing_segment(
    db: Session, segment_id: object
) -> tuple[FiberSegment, FiberTerminationPoint, FiberTerminationPoint]:
    segment = db.get(FiberSegment, _coerce_uuid(segment_id, "target_segment_id"))
    if segment is None or segment.is_active is False:
        raise FiberTopologyConnectivityError("canonical target segment not found")
    if not segment.from_point_id or not segment.to_point_id:
        raise FiberTopologyConnectivityError(
            "canonical target segment lacks two explicit termination IDs"
        )
    if not _segment_route_is_present(segment):
        raise FiberTopologyConnectivityError(
            "canonical target segment lacks approved route geometry"
        )
    start = db.get(FiberTerminationPoint, segment.from_point_id)
    end = db.get(FiberTerminationPoint, segment.to_point_id)
    if (
        start is None
        or end is None
        or start.is_active is False
        or end.is_active is False
        or start.ref_id is None
        or end.ref_id is None
    ):
        raise FiberTopologyConnectivityError(
            "canonical target segment has an invalid termination reference"
        )
    from app.services.network.fiber_plant_integrity import (
        FiberPlantIntegrityError,
        validate_active_segment,
    )

    try:
        validate_active_segment(db, segment)
    except FiberPlantIntegrityError as exc:
        raise FiberTopologyConnectivityError(str(exc)) from exc
    return segment, start, end


def _decision_digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def preview_connectivity_decision(
    db: Session,
    staged_feature_id: str | uuid.UUID,
    action: str,
    *,
    proposed_by: str,
    reason: str,
    start_endpoint_type: str | None = None,
    start_endpoint_ref_id: str | uuid.UUID | None = None,
    end_endpoint_type: str | None = None,
    end_endpoint_ref_id: str | uuid.UUID | None = None,
    segment_type: str = "distribution",
    cable_type: str | None = None,
    fiber_count: int | None = None,
    length_m: float | None = None,
    target_segment_id: str | uuid.UUID | None = None,
    expected_feature_content_sha256: str | None = None,
    require_new: bool = False,
) -> FiberConnectivityDecisionPreview:
    """Validate explicit endpoints and exact source content without writing."""

    actor = _required_text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _required_text(reason, "reason", limit=4000)
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"create", "link_existing", "reject"}:
        raise FiberTopologyConnectivityError("unsupported connectivity action")
    feature = _assert_source_current(db, _load_feature(db, staged_feature_id))
    if expected_feature_content_sha256 is not None:
        expected_content = _required_text(
            expected_feature_content_sha256,
            "expected_feature_content_sha256",
            limit=64,
        )
        if len(expected_content) != 64 or any(
            character not in "0123456789abcdef" for character in expected_content
        ):
            raise FiberTopologyConnectivityError(
                "expected_feature_content_sha256 must be a lowercase SHA-256 digest"
            )
        if feature.content_sha256 != expected_content:
            raise FiberTopologyConnectivityError(
                "expected feature content does not match the staged path"
            )

    start_type: str | None = None
    start_ref: uuid.UUID | None = None
    end_type: str | None = None
    end_ref: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    normalized_segment_type: str | None = None
    normalized_cable_type: str | None = None
    normalized_fiber_count: int | None = None
    normalized_length: float | None = None
    if normalized_action == "create":
        start_type, start_ref, _ = _normalize_endpoint(
            db,
            start_endpoint_type,
            start_endpoint_ref_id,
            prefix="start",
        )
        end_type, end_ref, _ = _normalize_endpoint(
            db, end_endpoint_type, end_endpoint_ref_id, prefix="end"
        )
        try:
            normalized_segment_type = FiberSegmentType(
                str(segment_type or "").strip().lower()
            ).value
        except ValueError as exc:
            raise FiberTopologyConnectivityError("invalid segment_type") from exc
        if cable_type is not None:
            try:
                normalized_cable_type = FiberCableType(
                    str(cable_type).strip().lower()
                ).value
            except ValueError as exc:
                raise FiberTopologyConnectivityError("invalid cable_type") from exc
        if fiber_count is None:
            raise FiberTopologyConnectivityError(
                "fiber_count is required for an operational cable"
            )
        if fiber_count < 1:
            raise FiberTopologyConnectivityError("fiber_count must be positive")
        normalized_fiber_count = fiber_count
        if length_m is not None:
            if length_m <= 0:
                raise FiberTopologyConnectivityError("length_m must be positive")
            normalized_length = float(length_m)
    elif normalized_action == "link_existing":
        segment, start, end = _validate_existing_segment(db, target_segment_id)
        target_id = segment.id
        start_type = str(getattr(start.endpoint_type, "value", start.endpoint_type))
        start_ref = start.ref_id
        end_type = str(getattr(end.endpoint_type, "value", end.endpoint_type))
        end_ref = end.ref_id
        normalized_segment_type = str(
            getattr(segment.segment_type, "value", segment.segment_type)
        )
        normalized_cable_type = (
            str(getattr(segment.cable_type, "value", segment.cable_type))
            if segment.cable_type
            else None
        )
        normalized_fiber_count = segment.fiber_count
        normalized_length = segment.length_m
    elif any(
        value is not None
        for value in (
            start_endpoint_type,
            start_endpoint_ref_id,
            end_endpoint_type,
            end_endpoint_ref_id,
            target_segment_id,
        )
    ):
        raise FiberTopologyConnectivityError(
            "reject decisions cannot specify endpoints or a target segment"
        )
    if start_type == end_type and start_ref is not None and start_ref == end_ref:
        raise FiberTopologyConnectivityError("connectivity endpoints must be distinct")

    digest_payload = {
        "action": normalized_action,
        "cable_type": normalized_cable_type,
        "end_endpoint_ref_id": str(end_ref) if end_ref else None,
        "end_endpoint_type": end_type,
        "feature_content_sha256": feature.content_sha256,
        "fiber_count": normalized_fiber_count,
        "length_m": normalized_length,
        "proposed_by": actor,
        "reason": normalized_reason,
        "segment_type": normalized_segment_type,
        "staged_feature_id": str(feature.id),
        "start_endpoint_ref_id": str(start_ref) if start_ref else None,
        "start_endpoint_type": start_type,
        "target_segment_id": str(target_id) if target_id else None,
    }
    decision_sha256 = _decision_digest(digest_payload)
    existing = db.scalar(
        select(FiberTopologyConnectivityDecision).where(
            FiberTopologyConnectivityDecision.source_system
            == feature.batch.source_system,
            FiberTopologyConnectivityDecision.source_asset_type == feature.asset_type,
            FiberTopologyConnectivityDecision.source_external_id == feature.external_id,
            FiberTopologyConnectivityDecision.status.in_(ACTIVE_STATUSES),
        )
    )
    if existing:
        if existing.decision_sha256 == decision_sha256 and not require_new:
            return FiberConnectivityDecisionPreview(
                item={
                    **digest_payload,
                    "decision_sha256": decision_sha256,
                    "source_asset_type": feature.asset_type,
                    "source_external_id": feature.external_id,
                    "source_system": feature.batch.source_system,
                },
                existing_decision_id=existing.id,
            )
        if existing.decision_sha256 == decision_sha256:
            raise FiberTopologyConnectivityError(
                "this source path already has an active connectivity decision"
            )
        raise FiberTopologyConnectivityError(
            "this source path already has a different active connectivity decision"
        )
    if db.scalar(
        select(FiberTopologyConnectivityDecision.id).where(
            FiberTopologyConnectivityDecision.decision_sha256 == decision_sha256
        )
    ):
        raise FiberTopologyConnectivityError(
            "this exact connectivity decision is already terminal"
        )
    return FiberConnectivityDecisionPreview(
        item={
            **digest_payload,
            "decision_sha256": decision_sha256,
            "source_asset_type": feature.asset_type,
            "source_external_id": feature.external_id,
            "source_system": feature.batch.source_system,
        }
    )


def propose_connectivity_decision(
    db: Session,
    staged_feature_id: str | uuid.UUID,
    action: str,
    *,
    proposed_by: str,
    reason: str,
    start_endpoint_type: str | None = None,
    start_endpoint_ref_id: str | uuid.UUID | None = None,
    end_endpoint_type: str | None = None,
    end_endpoint_ref_id: str | uuid.UUID | None = None,
    segment_type: str = "distribution",
    cable_type: str | None = None,
    fiber_count: int | None = None,
    length_m: float | None = None,
    target_segment_id: str | uuid.UUID | None = None,
    expected_feature_content_sha256: str | None = None,
    proposal_batch_id: uuid.UUID | None = None,
    proposal_batch_row_number: int | None = None,
    commit: bool = True,
) -> FiberTopologyConnectivityDecision:
    preview = preview_connectivity_decision(
        db,
        staged_feature_id,
        action,
        proposed_by=proposed_by,
        reason=reason,
        start_endpoint_type=start_endpoint_type,
        start_endpoint_ref_id=start_endpoint_ref_id,
        end_endpoint_type=end_endpoint_type,
        end_endpoint_ref_id=end_endpoint_ref_id,
        segment_type=segment_type,
        cable_type=cable_type,
        fiber_count=fiber_count,
        length_m=length_m,
        target_segment_id=target_segment_id,
        expected_feature_content_sha256=expected_feature_content_sha256,
        require_new=proposal_batch_id is not None,
    )
    if preview.existing_decision_id:
        return _load_decision(db, preview.existing_decision_id)
    item = preview.item
    decision = FiberTopologyConnectivityDecision(
        staged_feature_id=uuid.UUID(str(item["staged_feature_id"])),
        source_system=item["source_system"],
        source_asset_type=item["source_asset_type"],
        source_external_id=item["source_external_id"],
        feature_content_sha256=item["feature_content_sha256"],
        action=item["action"],
        status="proposed",
        start_endpoint_type=item["start_endpoint_type"],
        start_endpoint_ref_id=(
            _coerce_uuid(item["start_endpoint_ref_id"], "start_endpoint_ref_id")
            if item["start_endpoint_ref_id"]
            else None
        ),
        end_endpoint_type=item["end_endpoint_type"],
        end_endpoint_ref_id=(
            _coerce_uuid(item["end_endpoint_ref_id"], "end_endpoint_ref_id")
            if item["end_endpoint_ref_id"]
            else None
        ),
        segment_type=item["segment_type"],
        cable_type=item["cable_type"],
        fiber_count=item["fiber_count"],
        length_m=item["length_m"],
        target_segment_id=(
            _coerce_uuid(item["target_segment_id"], "target_segment_id")
            if item["target_segment_id"]
            else None
        ),
        reason=item["reason"],
        decision_sha256=item["decision_sha256"],
        proposed_by=item["proposed_by"],
        proposal_batch_id=proposal_batch_id,
        proposal_batch_row_number=proposal_batch_row_number,
    )
    db.add(decision)
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def _validate_decision(
    db: Session, decision: FiberTopologyConnectivityDecision
) -> None:
    feature = _assert_source_current(
        db, decision.staged_feature, decision.feature_content_sha256
    )
    if decision.action == "create":
        _normalize_endpoint(
            db,
            decision.start_endpoint_type,
            decision.start_endpoint_ref_id,
            prefix="start",
        )
        _normalize_endpoint(
            db,
            decision.end_endpoint_type,
            decision.end_endpoint_ref_id,
            prefix="end",
        )
    elif decision.action == "link_existing":
        segment, start, end = _validate_existing_segment(db, decision.target_segment_id)
        if (
            segment.id != decision.target_segment_id
            or str(getattr(start.endpoint_type, "value", start.endpoint_type))
            != decision.start_endpoint_type
            or start.ref_id != decision.start_endpoint_ref_id
            or str(getattr(end.endpoint_type, "value", end.endpoint_type))
            != decision.end_endpoint_type
            or end.ref_id != decision.end_endpoint_ref_id
        ):
            raise FiberTopologyConnectivityError(
                "canonical target segment endpoints changed after proposal"
            )
    if feature.geometry_geojson.get("type") != "LineString":
        raise FiberTopologyConnectivityError("staged path geometry evidence changed")


def validate_connectivity_decision_for_review(
    db: Session, decision_id: str | uuid.UUID
) -> FiberTopologyConnectivityDecision:
    """Revalidate exact source and endpoints without changing decision state."""

    decision = _load_decision(db, decision_id)
    if decision.status != "proposed":
        raise FiberTopologyConnectivityError("connectivity decision is not proposed")
    _validate_decision(db, decision)
    return decision


def approve_connectivity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    reviewed_by: str,
    review_notes: str,
    commit: bool = True,
) -> FiberTopologyConnectivityDecision:
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status != "proposed":
        if (
            decision.status != "declined"
            and decision.reviewed_by == actor
            and decision.review_notes == notes
        ):
            return decision
        raise FiberTopologyConnectivityError("connectivity decision is not proposed")
    if decision.proposed_by == actor:
        raise FiberTopologyConnectivityError(
            "the proposer cannot review the same connectivity decision"
        )
    _validate_decision(db, decision)
    decision.status = "approved"
    decision.reviewed_by = actor
    decision.review_notes = notes
    decision.reviewed_at = datetime.now(UTC)
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def decline_connectivity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    reviewed_by: str,
    review_notes: str,
    commit: bool = True,
) -> FiberTopologyConnectivityDecision:
    actor = _required_text(reviewed_by, "reviewed_by", limit=160)
    notes = _required_text(review_notes, "review_notes", limit=4000)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status != "proposed":
        if (
            decision.status == "declined"
            and decision.reviewed_by == actor
            and decision.review_notes == notes
        ):
            return decision
        raise FiberTopologyConnectivityError("connectivity decision is not proposed")
    if decision.proposed_by == actor:
        raise FiberTopologyConnectivityError(
            "the proposer cannot review the same connectivity decision"
        )
    decision.status = "declined"
    decision.reviewed_by = actor
    decision.review_notes = notes
    decision.reviewed_at = datetime.now(UTC)
    decision.closed_reason = "connectivity_decision_declined"
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def _endpoint_label(endpoint_type: str, endpoint: object) -> str:
    label = (
        getattr(endpoint, "name", None)
        or getattr(endpoint, "code", None)
        or str(getattr(endpoint, "id", ""))
    )
    value = f"{endpoint_type.replace('_', ' ').title()}: {label}"
    if len(value) > 160:
        return value[:157] + "..."
    return value


def _active_termination(
    db: Session, endpoint_type: str, endpoint_ref_id: uuid.UUID
) -> FiberTerminationPoint | None:
    points = list(
        db.scalars(
            select(FiberTerminationPoint).where(
                FiberTerminationPoint.endpoint_type == ODNEndpointType(endpoint_type),
                FiberTerminationPoint.ref_id == endpoint_ref_id,
                FiberTerminationPoint.is_active.is_(True),
            )
        ).all()
    )
    if len(points) > 1:
        raise FiberTopologyConnectivityError(
            "canonical endpoint has multiple active termination points"
        )
    return points[0] if points else None


def _get_or_create_resolution(
    db: Session,
    decision: FiberTopologyConnectivityDecision,
    *,
    endpoint_type: str,
    endpoint_ref_id: uuid.UUID,
    actor: str,
) -> FiberTopologyTerminationResolution:
    resolution = db.scalar(
        select(FiberTopologyTerminationResolution)
        .where(
            FiberTopologyTerminationResolution.endpoint_type == endpoint_type,
            FiberTopologyTerminationResolution.endpoint_ref_id == endpoint_ref_id,
        )
        .with_for_update()
    )
    if resolution:
        return resolution
    _, _, endpoint = _normalize_endpoint(
        db, endpoint_type, endpoint_ref_id, prefix="termination"
    )
    existing_point = _active_termination(db, endpoint_type, endpoint_ref_id)
    if existing_point:
        resolution = FiberTopologyTerminationResolution(
            endpoint_type=endpoint_type,
            endpoint_ref_id=endpoint_ref_id,
            status="applied",
            source_decision_id=decision.id,
            termination_point_id=existing_point.id,
            requested_by=actor,
            resolved_at=datetime.now(UTC),
        )
        db.add(resolution)
        db.flush()
        return resolution

    resolution_id = uuid.uuid4()
    notes = json.dumps(
        {
            "connectivity_decision_id": str(decision.id),
            "endpoint_ref_id": str(endpoint_ref_id),
            "endpoint_type": endpoint_type,
            "termination_resolution_id": str(resolution_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    change_request = fiber_change_requests.create_request(
        db,
        asset_type="fiber_termination_point",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload={
            "endpoint_type": endpoint_type,
            "is_active": True,
            "name": _endpoint_label(endpoint_type, endpoint),
            "notes": notes,
            "ref_id": str(endpoint_ref_id),
        },
        requested_by_person_id=None,
        requested_by_vendor_id=None,
        commit=False,
    )
    resolution = FiberTopologyTerminationResolution(
        id=resolution_id,
        endpoint_type=endpoint_type,
        endpoint_ref_id=endpoint_ref_id,
        status="pending",
        source_decision_id=decision.id,
        change_request_id=change_request.id,
        requested_by=actor,
    )
    db.add(resolution)
    db.flush()
    return resolution


def _create_source_link(
    db: Session,
    decision: FiberTopologyConnectivityDecision,
    segment: FiberSegment,
    actor: str,
) -> FiberTopologySegmentSourceLink:
    existing = db.scalar(
        select(FiberTopologySegmentSourceLink).where(
            FiberTopologySegmentSourceLink.source_system == decision.source_system,
            FiberTopologySegmentSourceLink.source_asset_type
            == decision.source_asset_type,
            FiberTopologySegmentSourceLink.external_id == decision.source_external_id,
        )
    )
    if existing:
        if existing.decision_id == decision.id and existing.segment_id == segment.id:
            return existing
        raise FiberTopologyConnectivityError(
            "source path is already linked to another canonical segment"
        )
    feature = decision.staged_feature
    link = FiberTopologySegmentSourceLink(
        decision_id=decision.id,
        staged_feature_id=feature.id,
        source_system=decision.source_system,
        source_profile=feature.batch.profile,
        source_asset_type=decision.source_asset_type,
        external_id=decision.source_external_id,
        content_sha256=decision.feature_content_sha256,
        segment_id=segment.id,
        status="active",
        linked_by=actor,
    )
    db.add(link)
    db.flush()
    return link


def _close_decision(
    decision: FiberTopologyConnectivityDecision, actor: str, reason: str
) -> None:
    now = datetime.now(UTC)
    decision.status = "closed"
    decision.closed_reason = reason
    decision.finalized_by = actor
    decision.finalized_at = now


def _emit_segment_request(
    db: Session,
    decision: FiberTopologyConnectivityDecision,
) -> None:
    if decision.segment_change_request_id:
        return
    start = decision.start_resolution
    end = decision.end_resolution
    if (
        start is None
        or end is None
        or start.status != "applied"
        or end.status != "applied"
        or start.termination_point_id is None
        or end.termination_point_id is None
    ):
        raise FiberTopologyConnectivityError(
            "both endpoint terminations must be applied before segment emission"
        )
    feature = _assert_source_current(
        db, decision.staged_feature, decision.feature_content_sha256
    )
    name = feature.display_name or decision.source_external_id
    if len(name) > 160:
        raise FiberTopologyConnectivityError(
            "staged path name exceeds the canonical 160-character limit"
        )
    notes = json.dumps(
        {
            "connectivity_decision_id": str(decision.id),
            "source_content_sha256": decision.feature_content_sha256,
            "source_external_id": decision.source_external_id,
            "source_profile": feature.batch.profile,
            "source_system": decision.source_system,
            "staged_feature_id": str(feature.id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    payload: dict[str, Any] = {
        "from_point_id": str(start.termination_point_id),
        "geojson": feature.geometry_geojson,
        "is_active": True,
        "name": name,
        "notes": notes,
        "segment_type": decision.segment_type,
        "to_point_id": str(end.termination_point_id),
    }
    if decision.cable_type:
        payload["cable_type"] = decision.cable_type
    if decision.fiber_count:
        payload["fiber_count"] = decision.fiber_count
    if decision.length_m:
        payload["length_m"] = decision.length_m
    request = fiber_change_requests.create_request(
        db,
        asset_type="fiber_segment",
        asset_id=None,
        operation=FiberChangeRequestOperation.create,
        payload=payload,
        requested_by_person_id=None,
        requested_by_vendor_id=None,
        commit=False,
    )
    decision.segment_change_request_id = request.id
    decision.status = "segment_change_requested"


def execute_connectivity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    executed_by: str,
    commit: bool = True,
) -> FiberTopologyConnectivityDecision:
    actor = _required_text(executed_by, "executed_by", limit=160)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status in {
        "endpoint_change_requested",
        "segment_change_requested",
        "applied",
        "closed",
    }:
        return decision
    if decision.status != "approved":
        raise FiberTopologyConnectivityError("connectivity decision is not approved")
    try:
        _validate_decision(db, decision)
    except FiberTopologyConnectivityError:
        decision.executed_by = actor
        decision.executed_at = datetime.now(UTC)
        _close_decision(decision, actor, "source_or_endpoint_changed_before_execution")
        if commit:
            db.commit()
            db.refresh(decision)
        else:
            db.flush()
        return decision

    now = datetime.now(UTC)
    if decision.action == "reject":
        _close_decision(decision, actor, "source_path_rejected")
    elif decision.action == "link_existing":
        segment, _start, _end = _validate_existing_segment(
            db, decision.target_segment_id
        )
        _create_source_link(db, decision, segment, actor)
        decision.canonical_segment_id = segment.id
        decision.status = "applied"
        decision.finalized_by = actor
        decision.finalized_at = now
    else:
        if (
            decision.start_endpoint_type is None
            or decision.start_endpoint_ref_id is None
            or decision.end_endpoint_type is None
            or decision.end_endpoint_ref_id is None
        ):
            raise FiberTopologyConnectivityError(
                "create decision is missing endpoint evidence"
            )
        start = _get_or_create_resolution(
            db,
            decision,
            endpoint_type=decision.start_endpoint_type,
            endpoint_ref_id=decision.start_endpoint_ref_id,
            actor=actor,
        )
        end = _get_or_create_resolution(
            db,
            decision,
            endpoint_type=decision.end_endpoint_type,
            endpoint_ref_id=decision.end_endpoint_ref_id,
            actor=actor,
        )
        decision.start_resolution_id = start.id
        decision.end_resolution_id = end.id
        decision.start_resolution = start
        decision.end_resolution = end
        if start.status == "rejected" or end.status == "rejected":
            _close_decision(decision, actor, "endpoint_change_request_rejected")
        elif start.status == "applied" and end.status == "applied":
            _emit_segment_request(db, decision)
        else:
            decision.status = "endpoint_change_requested"
    decision.executed_by = actor
    decision.executed_at = now
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def _sync_resolution(
    db: Session, resolution: FiberTopologyTerminationResolution
) -> FiberTopologyTerminationResolution:
    if resolution.status != "pending":
        return resolution
    if resolution.change_request_id is None:
        raise FiberTopologyConnectivityError(
            "pending termination resolution has no change request"
        )
    request = db.get(FiberChangeRequest, resolution.change_request_id)
    if request is None:
        raise FiberTopologyConnectivityError(
            "termination change request evidence is missing"
        )
    if request.status == FiberChangeRequestStatus.pending:
        return resolution
    if request.status == FiberChangeRequestStatus.rejected:
        resolution.status = "rejected"
        resolution.resolved_at = datetime.now(UTC)
        db.flush()
        return resolution
    if request.asset_id is None:
        raise FiberTopologyConnectivityError(
            "applied termination change request has no asset ID"
        )
    point = db.get(FiberTerminationPoint, request.asset_id)
    if (
        point is None
        or point.is_active is False
        or str(getattr(point.endpoint_type, "value", point.endpoint_type))
        != resolution.endpoint_type
        or point.ref_id != resolution.endpoint_ref_id
    ):
        raise FiberTopologyConnectivityError(
            "applied termination does not match its endpoint resolution"
        )
    resolution.status = "applied"
    resolution.termination_point_id = point.id
    resolution.resolved_at = datetime.now(UTC)
    db.flush()
    return resolution


def finalize_connectivity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    finalized_by: str,
    commit: bool = True,
) -> FiberTopologyConnectivityDecision:
    actor = _required_text(finalized_by, "finalized_by", limit=160)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status in {"applied", "closed"}:
        return decision
    if decision.status not in {
        "endpoint_change_requested",
        "segment_change_requested",
    }:
        raise FiberTopologyConnectivityError(
            "connectivity decision has no change-request outcome to finalize"
        )
    if decision.status == "endpoint_change_requested":
        if decision.start_resolution is None or decision.end_resolution is None:
            raise FiberTopologyConnectivityError(
                "connectivity decision is missing termination resolutions"
            )
        start = _sync_resolution(db, decision.start_resolution)
        end = _sync_resolution(db, decision.end_resolution)
        if start.status == "rejected" or end.status == "rejected":
            _close_decision(decision, actor, "endpoint_change_request_rejected")
        elif start.status == "applied" and end.status == "applied":
            try:
                _emit_segment_request(db, decision)
            except FiberTopologyConnectivityError as exc:
                if "newer staged version" not in str(
                    exc
                ) and "content changed" not in str(exc):
                    raise
                _close_decision(
                    decision, actor, "source_changed_before_segment_request"
                )
        if commit:
            db.commit()
            db.refresh(decision)
        else:
            db.flush()
        return decision

    if decision.segment_change_request_id is None:
        raise FiberTopologyConnectivityError(
            "segment-requested decision has no change request"
        )
    request = db.get(FiberChangeRequest, decision.segment_change_request_id)
    if request is None:
        raise FiberTopologyConnectivityError("segment change request not found")
    if request.status == FiberChangeRequestStatus.pending:
        return decision
    if request.status == FiberChangeRequestStatus.rejected:
        _close_decision(decision, actor, "segment_change_request_rejected")
    else:
        if request.asset_id is None:
            raise FiberTopologyConnectivityError(
                "applied segment change request has no asset ID"
            )
        segment, _start, _end = _validate_existing_segment(db, request.asset_id)
        if (
            decision.start_resolution is None
            or decision.end_resolution is None
            or segment.from_point_id != decision.start_resolution.termination_point_id
            or segment.to_point_id != decision.end_resolution.termination_point_id
        ):
            raise FiberTopologyConnectivityError(
                "applied segment endpoints do not match the reviewed decision"
            )
        _create_source_link(db, decision, segment, actor)
        decision.canonical_segment_id = segment.id
        decision.status = "applied"
        decision.finalized_by = actor
        decision.finalized_at = datetime.now(UTC)
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def reconcile_connectivity_change_requests(
    db: Session,
    *,
    finalized_by: str,
    limit: int = 100,
) -> FiberConnectivityReconcileResult:
    actor = _required_text(finalized_by, "finalized_by", limit=160)
    if limit < 1 or limit > 1000:
        raise FiberTopologyConnectivityError("limit must be between 1 and 1000")
    decision_ids = tuple(
        db.scalars(
            select(FiberTopologyConnectivityDecision.id)
            .where(
                FiberTopologyConnectivityDecision.status.in_(
                    ("endpoint_change_requested", "segment_change_requested")
                )
            )
            .order_by(FiberTopologyConnectivityDecision.proposed_at)
            .limit(limit)
        ).all()
    )
    applied = 0
    closed = 0
    endpoint_pending = 0
    segment_pending = 0
    errors: list[dict] = []
    for decision_id in decision_ids:
        try:
            decision = finalize_connectivity_decision(
                db, decision_id, finalized_by=actor
            )
        except FiberTopologyConnectivityError as exc:
            db.rollback()
            errors.append({"decision_id": str(decision_id), "message": str(exc)})
            continue
        if decision.status == "applied":
            applied += 1
        elif decision.status == "closed":
            closed += 1
        elif decision.status == "endpoint_change_requested":
            endpoint_pending += 1
        else:
            segment_pending += 1
    return FiberConnectivityReconcileResult(
        scanned=len(decision_ids),
        applied=applied,
        closed=closed,
        endpoint_pending=endpoint_pending,
        segment_pending=segment_pending,
        errors=tuple(errors),
    )


def connectivity_decision_to_dict(
    decision: FiberTopologyConnectivityDecision,
) -> dict:
    return {
        "action": decision.action,
        "canonical_segment_id": str(decision.canonical_segment_id)
        if decision.canonical_segment_id
        else None,
        "closed_reason": decision.closed_reason,
        "decision_id": str(decision.id),
        "decision_sha256": decision.decision_sha256,
        "end_endpoint_ref_id": str(decision.end_endpoint_ref_id)
        if decision.end_endpoint_ref_id
        else None,
        "end_endpoint_type": decision.end_endpoint_type,
        "end_resolution_id": str(decision.end_resolution_id)
        if decision.end_resolution_id
        else None,
        "feature_content_sha256": decision.feature_content_sha256,
        "segment_change_request_id": str(decision.segment_change_request_id)
        if decision.segment_change_request_id
        else None,
        "source_external_id": decision.source_external_id,
        "staged_feature_id": str(decision.staged_feature_id),
        "start_endpoint_ref_id": str(decision.start_endpoint_ref_id)
        if decision.start_endpoint_ref_id
        else None,
        "start_endpoint_type": decision.start_endpoint_type,
        "start_resolution_id": str(decision.start_resolution_id)
        if decision.start_resolution_id
        else None,
        "status": decision.status,
        "target_segment_id": str(decision.target_segment_id)
        if decision.target_segment_id
        else None,
    }


__all__ = [
    "FiberConnectivityReconcileResult",
    "FiberTopologyConnectivityError",
    "approve_connectivity_decision",
    "connectivity_decision_to_dict",
    "decline_connectivity_decision",
    "execute_connectivity_decision",
    "finalize_connectivity_decision",
    "propose_connectivity_decision",
    "reconcile_connectivity_change_requests",
]
