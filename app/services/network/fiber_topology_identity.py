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
from app.models.fiber_support import FiberSupportStructure
from app.models.fiber_topology_identity import (
    FiberTopologyAssetSourceLink,
    FiberTopologyIdentityDecision,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.gis import ServiceBuilding
from app.models.network import FdhCabinet, FiberAccessPoint, FiberSpliceClosure
from app.services import fiber_change_requests

POINT_ASSET_TYPES = frozenset(
    {
        "fdh_cabinet",
        "fiber_access_point",
        "splice_closure",
        "service_building",
        "support_structure",
    }
)
CREATE_ASSET_TYPES = frozenset(
    {"fdh_cabinet", "fiber_access_point", "splice_closure", "support_structure"}
)
LINK_TARGET_MODELS = {
    "fdh_cabinet": FdhCabinet,
    "fiber_access_point": FiberAccessPoint,
    "splice_closure": FiberSpliceClosure,
    "service_building": ServiceBuilding,
    "support_structure": FiberSupportStructure,
}
FINAL_STATUSES = frozenset({"applied", "closed"})
ACTIVE_STATUSES = ("proposed", "approved", "change_requested")
CRM_SOURCE_SYSTEM = "dotmac_crm_fiber_map"


class FiberTopologyIdentityError(ValueError):
    """Raised when a reviewed source-identity transition is invalid."""


@dataclass(frozen=True)
class IdentityDecisionPreview:
    staged_feature_id: uuid.UUID
    source_system: str
    source_asset_type: str
    source_external_id: str | None
    feature_content_sha256: str
    action: str
    target_asset_type: str | None
    target_asset_id: uuid.UUID | None
    reason: str
    proposed_by: str
    decision_sha256: str
    existing_decision_id: uuid.UUID | None = None

    def to_manifest_dict(self, *, row_number: int | None = None) -> dict:
        payload: dict[str, object] = {
            "action": self.action,
            "decision_sha256": self.decision_sha256,
            "feature_content_sha256": self.feature_content_sha256,
            "proposed_by": self.proposed_by,
            "reason": self.reason,
            "source_asset_type": self.source_asset_type,
            "source_external_id": self.source_external_id,
            "source_system": self.source_system,
            "staged_feature_id": str(self.staged_feature_id),
            "target_asset_id": str(self.target_asset_id)
            if self.target_asset_id
            else None,
            "target_asset_type": self.target_asset_type,
        }
        if row_number is not None:
            payload["row_number"] = row_number
        return payload


def _required_text(value: str | None, field: str, *, limit: int) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise FiberTopologyIdentityError(f"{field} is required")
    if len(normalized) > limit:
        raise FiberTopologyIdentityError(f"{field} must be at most {limit} characters")
    return normalized


def _coerce_uuid(value: str | uuid.UUID, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FiberTopologyIdentityError(f"{field} must be a UUID") from exc


def _load_feature(
    db: Session, staged_feature_id: str | uuid.UUID
) -> FiberTopologyStagedFeature:
    feature_id = _coerce_uuid(staged_feature_id, "staged_feature_id")
    feature = db.scalar(
        select(FiberTopologyStagedFeature)
        .options(joinedload(FiberTopologyStagedFeature.batch))
        .where(FiberTopologyStagedFeature.id == feature_id)
    )
    if not feature:
        raise FiberTopologyIdentityError("staged feature not found")
    return feature


def _load_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    *,
    for_update: bool = False,
) -> FiberTopologyIdentityDecision:
    normalized_id = _coerce_uuid(decision_id, "decision_id")
    statement = (
        select(FiberTopologyIdentityDecision)
        .options(
            joinedload(FiberTopologyIdentityDecision.staged_feature).joinedload(
                FiberTopologyStagedFeature.batch
            )
        )
        .where(FiberTopologyIdentityDecision.id == normalized_id)
    )
    if for_update:
        # Lock only the decision row: the joinedload outer joins make an
        # unqualified FOR UPDATE fail on PostgreSQL (nullable side of join).
        statement = statement.with_for_update(of=FiberTopologyIdentityDecision)
    decision = db.scalar(statement)
    if not decision:
        raise FiberTopologyIdentityError("identity decision not found")
    return decision


def _decision_digest(
    feature: FiberTopologyStagedFeature,
    action: str,
    target_asset_type: str | None,
    target_asset_id: uuid.UUID | None,
    reason: str,
    proposed_by: str,
) -> str:
    payload = {
        "action": action,
        "feature_content_sha256": feature.content_sha256,
        "proposed_by": proposed_by,
        "reason": reason,
        "staged_feature_id": str(feature.id),
        "target_asset_id": str(target_asset_id) if target_asset_id else None,
        "target_asset_type": target_asset_type,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def stable_source_external_id(
    source_system: str, source_asset_type: str, external_id: str | None
) -> str | None:
    """Return the durable external identity stored on identity decisions/links."""

    normalized_external_id = (external_id or "").strip()
    if not normalized_external_id:
        return None
    if source_system == CRM_SOURCE_SYSTEM:
        return f"crm_network_map:{source_asset_type}:{normalized_external_id}"
    return normalized_external_id


def _assert_feature_unchanged(
    decision: FiberTopologyIdentityDecision,
) -> FiberTopologyStagedFeature:
    feature = decision.staged_feature
    if feature.content_sha256 != decision.feature_content_sha256:
        raise FiberTopologyIdentityError(
            "staged feature content changed after the identity decision was proposed"
        )
    return feature


def _assert_source_feature_current(
    db: Session, feature: FiberTopologyStagedFeature
) -> FiberTopologyStagedFeature:
    if not feature.external_id:
        return feature
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
        raise FiberTopologyIdentityError(
            "a newer staged version changed this source identity; review the latest "
            "feature before continuing"
        )
    return feature


def _latest_source_feature(
    db: Session, feature: FiberTopologyStagedFeature
) -> FiberTopologyStagedFeature:
    if not feature.external_id:
        return feature
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
    return latest or feature


def _assert_decision_source_current(
    db: Session, decision: FiberTopologyIdentityDecision
) -> FiberTopologyStagedFeature:
    return _assert_source_feature_current(db, _assert_feature_unchanged(decision))


def _target_model(asset_type: str):
    model = LINK_TARGET_MODELS.get(asset_type)
    if model is None:
        raise FiberTopologyIdentityError(
            f"{asset_type} does not have an approved canonical identity target"
        )
    return model


def _assert_target_exists(db: Session, asset_type: str, asset_id: uuid.UUID) -> Any:
    target = db.get(_target_model(asset_type), asset_id)
    if target is None:
        raise FiberTopologyIdentityError("canonical target asset not found")
    if hasattr(target, "is_active") and target.is_active is False:
        raise FiberTopologyIdentityError("canonical target asset is inactive")
    return target


def _existing_source_link(
    db: Session, feature: FiberTopologyStagedFeature
) -> FiberTopologyAssetSourceLink | None:
    source_external_id = stable_source_external_id(
        feature.batch.source_system, feature.asset_type, feature.external_id
    )
    if not source_external_id:
        return None
    return db.scalar(
        select(FiberTopologyAssetSourceLink).where(
            FiberTopologyAssetSourceLink.source_system == feature.batch.source_system,
            FiberTopologyAssetSourceLink.source_asset_type == feature.asset_type,
            FiberTopologyAssetSourceLink.external_id == source_external_id,
        )
    )


def _validate_proposal(
    db: Session,
    feature: FiberTopologyStagedFeature,
    action: str,
    target_asset_id: uuid.UUID | None,
) -> tuple[str | None, uuid.UUID | None]:
    if feature.asset_type not in POINT_ASSET_TYPES:
        raise FiberTopologyIdentityError(
            "only reviewed point assets are eligible for identity decisions"
        )
    if action not in {"create", "link_existing", "reject"}:
        raise FiberTopologyIdentityError("unsupported identity decision action")
    if feature.match_status == "blocked" and action != "reject":
        raise FiberTopologyIdentityError("blocked staged features can only be rejected")
    if action in {"create", "link_existing"} and not feature.external_id:
        raise FiberTopologyIdentityError(
            "a stable external_id is required before canonical identity can be assigned"
        )
    if action == "create":
        if feature.asset_type not in CREATE_ASSET_TYPES:
            raise FiberTopologyIdentityError(
                f"canonical creation is not enabled for {feature.asset_type}"
            )
        if target_asset_id is not None:
            raise FiberTopologyIdentityError(
                "target_asset_id is only valid for link_existing"
            )
        if _existing_source_link(db, feature):
            raise FiberTopologyIdentityError(
                "this source identity already has a canonical link"
            )
        return None, None
    if action == "link_existing":
        if target_asset_id is None:
            raise FiberTopologyIdentityError(
                "target_asset_id is required for link_existing"
            )
        _assert_target_exists(db, feature.asset_type, target_asset_id)
        existing_link = _existing_source_link(db, feature)
        if existing_link:
            raise FiberTopologyIdentityError(
                "this source identity already has a canonical link"
            )
        return feature.asset_type, target_asset_id
    if target_asset_id is not None:
        raise FiberTopologyIdentityError(
            "target_asset_id is only valid for link_existing"
        )
    return None, None


def preview_identity_decision(
    db: Session,
    staged_feature_id: str | uuid.UUID,
    action: str,
    proposed_by: str,
    reason: str,
    target_asset_id: str | uuid.UUID | None = None,
    *,
    require_new: bool = False,
) -> IdentityDecisionPreview:
    actor = _required_text(proposed_by, "proposed_by", limit=160)
    normalized_reason = _required_text(reason, "reason", limit=4000)
    normalized_action = (action or "").strip().lower()
    normalized_target_id = (
        _coerce_uuid(target_asset_id, "target_asset_id")
        if target_asset_id is not None
        else None
    )
    feature = _load_feature(db, staged_feature_id)
    _assert_source_feature_current(db, feature)
    target_type, normalized_target_id = _validate_proposal(
        db, feature, normalized_action, normalized_target_id
    )
    digest = _decision_digest(
        feature,
        normalized_action,
        target_type,
        normalized_target_id,
        normalized_reason,
        actor,
    )
    active_conditions = [
        FiberTopologyIdentityDecision.status.in_(ACTIVE_STATUSES),
        FiberTopologyIdentityDecision.source_system == feature.batch.source_system,
        FiberTopologyIdentityDecision.source_asset_type == feature.asset_type,
    ]
    source_external_id = stable_source_external_id(
        feature.batch.source_system, feature.asset_type, feature.external_id
    )
    if source_external_id:
        active_conditions.append(
            FiberTopologyIdentityDecision.source_external_id == source_external_id
        )
    else:
        active_conditions.append(
            FiberTopologyIdentityDecision.staged_feature_id == feature.id
        )
    existing = db.scalar(
        select(FiberTopologyIdentityDecision).where(*active_conditions)
    )
    if existing:
        if existing.decision_sha256 == digest and not require_new:
            return IdentityDecisionPreview(
                staged_feature_id=feature.id,
                source_system=feature.batch.source_system,
                source_asset_type=feature.asset_type,
                source_external_id=source_external_id,
                feature_content_sha256=feature.content_sha256,
                action=normalized_action,
                target_asset_type=target_type,
                target_asset_id=normalized_target_id,
                reason=normalized_reason,
                proposed_by=actor,
                decision_sha256=digest,
                existing_decision_id=existing.id,
            )
        raise FiberTopologyIdentityError(
            "this source identity already has a different active identity decision"
            if existing.decision_sha256 != digest
            else "this source identity already has an active identity decision"
        )
    prior_same_decision = db.scalar(
        select(FiberTopologyIdentityDecision.id).where(
            FiberTopologyIdentityDecision.decision_sha256 == digest
        )
    )
    if prior_same_decision:
        raise FiberTopologyIdentityError(
            "this exact identity decision is already terminal; provide updated "
            "reason or evidence before proposing it again"
        )
    return IdentityDecisionPreview(
        staged_feature_id=feature.id,
        source_system=feature.batch.source_system,
        source_asset_type=feature.asset_type,
        source_external_id=stable_source_external_id(
            feature.batch.source_system, feature.asset_type, feature.external_id
        ),
        feature_content_sha256=feature.content_sha256,
        action=normalized_action,
        target_asset_type=target_type,
        target_asset_id=normalized_target_id,
        reason=normalized_reason,
        proposed_by=actor,
        decision_sha256=digest,
    )


def propose_identity_decision(
    db: Session,
    staged_feature_id: str | uuid.UUID,
    action: str,
    proposed_by: str,
    reason: str,
    target_asset_id: str | uuid.UUID | None = None,
    *,
    proposal_batch_id: uuid.UUID | None = None,
    proposal_batch_row_number: int | None = None,
    commit: bool = True,
) -> FiberTopologyIdentityDecision:
    if (proposal_batch_id is None) != (proposal_batch_row_number is None):
        raise FiberTopologyIdentityError(
            "proposal batch ID and row number must be provided together"
        )
    if proposal_batch_row_number is not None and proposal_batch_row_number < 1:
        raise FiberTopologyIdentityError("proposal batch row number must be positive")
    preview = preview_identity_decision(
        db,
        staged_feature_id,
        action,
        proposed_by,
        reason,
        target_asset_id,
        require_new=proposal_batch_id is not None,
    )
    if preview.existing_decision_id is not None:
        existing = db.get(FiberTopologyIdentityDecision, preview.existing_decision_id)
        if existing is None:
            raise FiberTopologyIdentityError("active identity decision not found")
        return existing
    decision = FiberTopologyIdentityDecision(
        staged_feature_id=preview.staged_feature_id,
        source_system=preview.source_system,
        source_asset_type=preview.source_asset_type,
        source_external_id=preview.source_external_id,
        feature_content_sha256=preview.feature_content_sha256,
        action=preview.action,
        status="proposed",
        target_asset_type=preview.target_asset_type,
        target_asset_id=preview.target_asset_id,
        reason=preview.reason,
        decision_sha256=preview.decision_sha256,
        proposed_by=preview.proposed_by,
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


def approve_identity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    reviewed_by: str,
    review_notes: str,
    *,
    commit: bool = True,
) -> FiberTopologyIdentityDecision:
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
        raise FiberTopologyIdentityError("identity decision is not awaiting review")
    if decision.proposed_by == actor:
        raise FiberTopologyIdentityError(
            "the proposer cannot review the same identity decision"
        )
    validate_identity_decision_for_review(db, decision.id)
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


def validate_identity_decision_for_review(
    db: Session,
    decision_id: str | uuid.UUID,
) -> FiberTopologyIdentityDecision:
    """Validate an awaiting-review decision without changing review state."""

    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status != "proposed":
        raise FiberTopologyIdentityError("identity decision is not awaiting review")
    feature = _assert_decision_source_current(db, decision)
    if decision.action == "link_existing":
        if decision.target_asset_id is None:
            raise FiberTopologyIdentityError("link decision is missing a target")
        _assert_target_exists(db, feature.asset_type, decision.target_asset_id)
    return decision


def decline_identity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    reviewed_by: str,
    review_notes: str,
    *,
    commit: bool = True,
) -> FiberTopologyIdentityDecision:
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
        raise FiberTopologyIdentityError("identity decision is not awaiting review")
    if decision.proposed_by == actor:
        raise FiberTopologyIdentityError(
            "the proposer cannot review the same identity decision"
        )
    decision.status = "declined"
    decision.reviewed_by = actor
    decision.review_notes = notes
    decision.reviewed_at = datetime.now(UTC)
    decision.closed_reason = "identity_decision_declined"
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def supersede_approved_identity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    superseded_by: str,
    reason: str,
    *,
    commit: bool = True,
) -> FiberTopologyIdentityDecision:
    """Close an approved, unexecuted decision when newer source evidence wins."""

    actor = _required_text(superseded_by, "superseded_by", limit=160)
    normalized_reason = _required_text(reason, "reason", limit=4000)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status == "declined" and (
        decision.closed_reason == "authoritative_source_superseded"
    ):
        return decision
    if decision.status != "approved":
        raise FiberTopologyIdentityError(
            "only approved, unexecuted identity decisions can be superseded"
        )
    if decision.change_request_id is not None:
        raise FiberTopologyIdentityError(
            "identity decisions with change requests must be finalized or rejected"
        )
    if _existing_source_link(db, decision.staged_feature):
        raise FiberTopologyIdentityError(
            "linked source identities cannot be superseded"
        )
    latest = _latest_source_feature(db, decision.staged_feature)
    if latest.id == decision.staged_feature_id:
        raise FiberTopologyIdentityError(
            "no newer staged source evidence supersedes this identity decision"
        )
    decision.status = "declined"
    decision.closed_reason = "authoritative_source_superseded"
    decision.review_notes = (
        f"{decision.review_notes}\n\nSuperseded by {actor}: {normalized_reason}"
    )
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def _polygon_centroid(ring: list[list[float]]) -> tuple[float, float]:
    if len(ring) < 3:
        raise FiberTopologyIdentityError("polygon geometry has too few coordinates")
    area_twice = 0.0
    longitude_sum = 0.0
    latitude_sum = 0.0
    for current, following in zip(ring, ring[1:] + ring[:1], strict=False):
        longitude, latitude = float(current[0]), float(current[1])
        next_longitude, next_latitude = float(following[0]), float(following[1])
        cross = longitude * next_latitude - next_longitude * latitude
        area_twice += cross
        longitude_sum += (longitude + next_longitude) * cross
        latitude_sum += (latitude + next_latitude) * cross
    if abs(area_twice) < 1e-12:
        longitude = sum(float(point[0]) for point in ring) / len(ring)
        latitude = sum(float(point[1]) for point in ring) / len(ring)
        return longitude, latitude
    return (
        longitude_sum / (3.0 * area_twice),
        latitude_sum / (3.0 * area_twice),
    )


def representative_point(geometry: dict) -> tuple[float, float]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        raise FiberTopologyIdentityError("invalid staged point geometry")
    try:
        if geometry_type == "Point":
            return float(coordinates[0]), float(coordinates[1])
        if geometry_type == "Polygon":
            return _polygon_centroid(coordinates[0])
        if geometry_type == "MultiPolygon":
            return _polygon_centroid(coordinates[0][0])
    except (IndexError, TypeError, ValueError) as exc:
        raise FiberTopologyIdentityError("invalid staged point geometry") from exc
    raise FiberTopologyIdentityError(
        f"unsupported point-asset geometry: {geometry_type}"
    )


def _source_property(feature: FiberTopologyStagedFeature, *keys: str) -> str | None:
    normalized = {
        str(key).strip().lower(): value
        for key, value in (feature.source_properties or {}).items()
    }
    for key in keys:
        value = normalized.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _bounded_value(value: str | None, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) > limit:
        raise FiberTopologyIdentityError(
            f"staged {field} exceeds the canonical {limit}-character limit"
        )
    return value


def _create_payload(
    decision: FiberTopologyIdentityDecision,
    feature: FiberTopologyStagedFeature,
) -> dict:
    if not feature.external_id:
        raise FiberTopologyIdentityError("staged feature is missing external_id")
    longitude, latitude = representative_point(feature.geometry_geojson)
    name = _bounded_value(
        feature.display_name or feature.external_id, "display_name", 160
    )
    notes = json.dumps(
        {
            "fiber_topology_identity_decision_id": str(decision.id),
            "source_content_sha256": feature.content_sha256,
            "source_external_id": feature.external_id,
            "source_profile": feature.batch.profile,
            "source_system": feature.batch.source_system,
            "staged_feature_id": str(feature.id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    common = {
        "name": name,
        "latitude": latitude,
        "longitude": longitude,
        "geojson": {"type": "Point", "coordinates": [longitude, latitude]},
        "notes": notes,
    }
    if feature.asset_type == "fdh_cabinet":
        return {
            **common,
            "code": _bounded_value(feature.external_id, "external_id", 80),
        }
    if feature.asset_type == "fiber_access_point":
        return {
            **common,
            "code": _bounded_value(feature.external_id, "external_id", 60),
            "access_point_type": _bounded_value(
                _source_property(feature, "type", "access_point_type"),
                "access_point_type",
                60,
            ),
            "placement": _bounded_value(
                _source_property(feature, "placement"), "placement", 60
            ),
            "street": _bounded_value(
                _source_property(feature, "street", "address"), "street", 200
            ),
            "city": _bounded_value(_source_property(feature, "city"), "city", 100),
            "county": _bounded_value(
                _source_property(feature, "county", "lga"), "county", 100
            ),
            "state": _bounded_value(_source_property(feature, "state"), "state", 60),
        }
    if feature.asset_type == "splice_closure":
        return common
    if feature.asset_type == "support_structure":
        raw_support_type = (
            _source_property(feature, "support_type", "type") or "pole"
        ).lower()
        support_type = (
            raw_support_type
            if raw_support_type in {"pole", "tower", "building_attachment", "other"}
            else "other"
        )
        return {
            **common,
            "code": _bounded_value(feature.external_id, "external_id", 80),
            "inspection_status": "uninspected",
            "lease_status": "unknown",
            "lifecycle_status": "active",
            "ownership_status": "unknown",
            "support_type": support_type,
        }
    raise FiberTopologyIdentityError(
        f"canonical creation is not enabled for {feature.asset_type}"
    )


def _create_source_link(
    db: Session,
    decision: FiberTopologyIdentityDecision,
    canonical_asset_type: str,
    canonical_asset_id: uuid.UUID,
    actor: str,
) -> FiberTopologyAssetSourceLink:
    feature = _assert_feature_unchanged(decision)
    source_external_id = stable_source_external_id(
        feature.batch.source_system, feature.asset_type, feature.external_id
    )
    if not source_external_id:
        raise FiberTopologyIdentityError("staged feature is missing external_id")
    existing = _existing_source_link(db, feature)
    if existing:
        if (
            existing.decision_id == decision.id
            and existing.status == "active"
            and existing.canonical_asset_type == canonical_asset_type
            and existing.canonical_asset_id == canonical_asset_id
        ):
            return existing
        raise FiberTopologyIdentityError(
            "source identity is already linked to a different canonical asset"
        )
    source_link = FiberTopologyAssetSourceLink(
        decision_id=decision.id,
        staged_feature_id=feature.id,
        source_system=feature.batch.source_system,
        source_profile=feature.batch.profile,
        source_asset_type=feature.asset_type,
        external_id=source_external_id,
        content_sha256=feature.content_sha256,
        canonical_asset_type=canonical_asset_type,
        canonical_asset_id=canonical_asset_id,
        status="active",
        linked_by=actor,
    )
    db.add(source_link)
    db.flush()
    return source_link


def execute_identity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    executed_by: str,
    *,
    commit: bool = True,
) -> FiberTopologyIdentityDecision:
    actor = _required_text(executed_by, "executed_by", limit=160)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status in FINAL_STATUSES or decision.status == "change_requested":
        return decision
    if decision.status != "approved":
        raise FiberTopologyIdentityError("identity decision is not approved")
    feature = _assert_decision_source_current(db, decision)
    now = datetime.now(UTC)
    if decision.action == "create":
        change_request = fiber_change_requests.create_request(
            db,
            asset_type=feature.asset_type,
            asset_id=None,
            operation=FiberChangeRequestOperation.create,
            payload=_create_payload(decision, feature),
            requested_by_person_id=None,
            requested_by_vendor_id=None,
            commit=False,
        )
        decision.change_request_id = change_request.id
        decision.status = "change_requested"
    elif decision.action == "link_existing":
        if decision.target_asset_id is None:
            raise FiberTopologyIdentityError("link decision is missing a target")
        _assert_target_exists(db, feature.asset_type, decision.target_asset_id)
        _create_source_link(
            db, decision, feature.asset_type, decision.target_asset_id, actor
        )
        decision.status = "applied"
        decision.finalized_by = actor
        decision.finalized_at = now
    else:
        decision.status = "closed"
        decision.closed_reason = "source_identity_rejected"
        decision.finalized_by = actor
        decision.finalized_at = now
    decision.executed_by = actor
    decision.executed_at = now
    if commit:
        db.commit()
        db.refresh(decision)
    else:
        db.flush()
    return decision


def finalize_identity_decision(
    db: Session,
    decision_id: str | uuid.UUID,
    finalized_by: str,
) -> FiberTopologyIdentityDecision:
    actor = _required_text(finalized_by, "finalized_by", limit=160)
    decision = _load_decision(db, decision_id, for_update=True)
    if decision.status in FINAL_STATUSES:
        return decision
    if decision.status != "change_requested" or decision.change_request_id is None:
        raise FiberTopologyIdentityError(
            "identity decision has no applied change request to finalize"
        )
    change_request = db.get(FiberChangeRequest, decision.change_request_id)
    if change_request is None:
        raise FiberTopologyIdentityError("fiber change request not found")
    if change_request.status == FiberChangeRequestStatus.pending:
        return decision
    now = datetime.now(UTC)
    if change_request.status == FiberChangeRequestStatus.rejected:
        decision.status = "closed"
        decision.closed_reason = "fiber_change_request_rejected"
    elif change_request.status == FiberChangeRequestStatus.applied:
        if change_request.asset_id is None:
            raise FiberTopologyIdentityError(
                "applied fiber change request has no canonical asset_id"
            )
        feature = _assert_feature_unchanged(decision)
        if change_request.asset_type != feature.asset_type:
            raise FiberTopologyIdentityError(
                "fiber change request asset type does not match staged identity"
            )
        _assert_target_exists(db, feature.asset_type, change_request.asset_id)
        _create_source_link(
            db,
            decision,
            feature.asset_type,
            change_request.asset_id,
            actor,
        )
        decision.status = "applied"
    else:
        raise FiberTopologyIdentityError("unsupported fiber change request status")
    decision.finalized_by = actor
    decision.finalized_at = now
    db.commit()
    db.refresh(decision)
    return decision


def decision_to_dict(decision: FiberTopologyIdentityDecision) -> dict:
    return {
        "action": decision.action,
        "change_request_id": str(decision.change_request_id)
        if decision.change_request_id
        else None,
        "closed_reason": decision.closed_reason,
        "decision_id": str(decision.id),
        "decision_sha256": decision.decision_sha256,
        "feature_content_sha256": decision.feature_content_sha256,
        "finalized_at": decision.finalized_at.isoformat()
        if decision.finalized_at
        else None,
        "finalized_by": decision.finalized_by,
        "proposed_by": decision.proposed_by,
        "proposal_batch_id": str(decision.proposal_batch_id)
        if decision.proposal_batch_id
        else None,
        "proposal_batch_row_number": decision.proposal_batch_row_number,
        "reviewed_by": decision.reviewed_by,
        "source_asset_type": decision.source_asset_type,
        "source_external_id": decision.source_external_id,
        "source_system": decision.source_system,
        "staged_feature_id": str(decision.staged_feature_id),
        "status": decision.status,
        "target_asset_id": str(decision.target_asset_id)
        if decision.target_asset_id
        else None,
        "target_asset_type": decision.target_asset_type,
    }


__all__ = [
    "ACTIVE_STATUSES",
    "CREATE_ASSET_TYPES",
    "FiberTopologyIdentityError",
    "IdentityDecisionPreview",
    "LINK_TARGET_MODELS",
    "POINT_ASSET_TYPES",
    "CRM_SOURCE_SYSTEM",
    "approve_identity_decision",
    "decision_to_dict",
    "decline_identity_decision",
    "execute_identity_decision",
    "finalize_identity_decision",
    "preview_identity_decision",
    "propose_identity_decision",
    "representative_point",
    "stable_source_external_id",
    "supersede_approved_identity_decision",
]
