"""CRM Network Map point-asset reconciliation against canonical Selfcare assets.

This coordinator owns no canonical map data. It selects immutable CRM staging
evidence, classifies it, and delegates proposal/review/apply transitions to the
existing fiber identity owners.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.fiber_change_request import FiberChangeRequest
from app.models.fiber_topology_identity import (
    FiberTopologyAssetSourceLink,
    FiberTopologyIdentityDecision,
    FiberTopologyIdentityExecutionRun,
)
from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import FdhCabinet, FiberAccessPoint, FiberSpliceClosure
from app.services.network.fiber_topology_identity import (
    CRM_SOURCE_SYSTEM,
    FiberTopologyIdentityError,
    stable_source_external_id,
)
from app.services.network.fiber_topology_review import (
    FiberIdentityExecutionRunResult,
    FiberIdentityProposalBatchPreview,
    FiberIdentityProposalBatchResult,
    FiberTopologyReviewError,
    execute_identity_batch,
    preview_identity_proposal_batch,
    propose_identity_batch,
)

SUPPORTED_CRM_POINT_ASSET_TYPES = frozenset(
    {"fdh_cabinet", "fiber_access_point", "splice_closure"}
)
CRM_POINT_PROFILES = {
    "fdh_cabinet": "crm_fdh_cabinets",
    "fiber_access_point": "crm_access_points",
    "splice_closure": "crm_splice_closures",
}
CLASSIFICATIONS = frozenset(
    {
        "already_linked",
        "exact_match",
        "candidate_match",
        "create_new",
        "unchanged",
        "conflict",
        "invalid",
        "superseded_source",
    }
)
REQUIRED_METADATA = frozenset(
    {
        "source_archive_sha256",
        "snapshot_timestamp",
        "importer_version",
        "source_count",
        "active_source_count",
        "valid_active_source_count",
        "restored_count",
        "staged_count",
        "reconciliation_status",
        "full_manifest_sha256",
    }
)
EXPECTED_RECONCILIATION_STATUS = "source_restore_staged_counts_match"


class CrmNetworkMapPointMigrationError(ValueError):
    """Raised when CRM point migration evidence is incomplete or unsafe."""


@dataclass(frozen=True)
class CrmAuthoritativeBatchSet:
    asset_type: str
    profile: str
    source_archive_sha256: str
    snapshot_timestamp: str
    importer_version: str
    full_manifest_sha256: str
    source_count: int
    active_source_count: int
    valid_active_source_count: int
    restored_count: int
    staged_count: int
    reconciliation_status: str
    batch_ids: tuple[uuid.UUID, ...]
    superseded_batch_ids: tuple[uuid.UUID, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_type": self.asset_type,
            "batch_ids": [str(value) for value in self.batch_ids],
            "full_manifest_sha256": self.full_manifest_sha256,
            "importer_version": self.importer_version,
            "profile": self.profile,
            "reconciliation_status": self.reconciliation_status,
            "restored_count": self.restored_count,
            "snapshot_timestamp": self.snapshot_timestamp,
            "source_archive_sha256": self.source_archive_sha256,
            "source_count": self.source_count,
            "active_source_count": self.active_source_count,
            "valid_active_source_count": self.valid_active_source_count,
            "staged_count": self.staged_count,
            "superseded_batch_ids": [str(value) for value in self.superseded_batch_ids],
        }


@dataclass(frozen=True)
class CrmFeatureReconciliation:
    staged_feature_id: uuid.UUID
    source_identity: str | None
    asset_type: str
    classification: str
    reason_code: str
    canonical_asset_id: uuid.UUID | None = None
    proposal_action: str | None = None
    proposal_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_type": self.asset_type,
            "canonical_asset_id": str(self.canonical_asset_id)
            if self.canonical_asset_id
            else None,
            "classification": self.classification,
            "proposal_action": self.proposal_action,
            "proposal_reason": self.proposal_reason,
            "reason_code": self.reason_code,
            "source_identity": self.source_identity,
            "staged_feature_id": str(self.staged_feature_id),
        }


@dataclass(frozen=True)
class CrmPointMigrationReport:
    selections: tuple[CrmAuthoritativeBatchSet, ...]
    rows: tuple[CrmFeatureReconciliation, ...]
    canonical_before: dict[str, int]
    proposal_status_counts: dict[str, int]
    change_request_status_counts: dict[str, int]

    def to_dict(self, *, include_rows: bool = False) -> dict[str, object]:
        grouped: dict[str, Counter[str]] = defaultdict(Counter)
        for row in self.rows:
            grouped[row.asset_type][row.classification] += 1
        payload: dict[str, object] = {
            "canonical_count_after": dict(self.canonical_before),
            "canonical_count_before": dict(self.canonical_before),
            "change_request_status_counts": dict(self.change_request_status_counts),
            "classifications": {
                asset_type: dict(sorted(counts.items()))
                for asset_type, counts in sorted(grouped.items())
            },
            "proposal_status_counts": dict(self.proposal_status_counts),
            "selections": [selection.to_dict() for selection in self.selections],
        }
        if include_rows:
            payload["rows"] = [row.to_dict() for row in self.rows]
        return payload


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha(value: Any, field: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise CrmNetworkMapPointMigrationError(f"{field} must be a SHA-256 digest")
    return text


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CrmNetworkMapPointMigrationError(f"{field} is required")
    return text


def _required_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CrmNetworkMapPointMigrationError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CrmNetworkMapPointMigrationError(f"{field} must be an integer") from exc
    if result < 0:
        raise CrmNetworkMapPointMigrationError(f"{field} cannot be negative")
    return result


def _batch_metadata(batch: FiberTopologySourceBatch) -> dict[str, Any]:
    metadata = dict(batch.source_metadata or {})
    missing = sorted(REQUIRED_METADATA - set(metadata))
    if missing:
        raise CrmNetworkMapPointMigrationError(
            f"batch {batch.id} is missing authoritative metadata: " + ", ".join(missing)
        )
    if metadata["reconciliation_status"] != EXPECTED_RECONCILIATION_STATUS:
        raise CrmNetworkMapPointMigrationError(
            f"batch {batch.id} has unsafe reconciliation status"
        )
    metadata["source_archive_sha256"] = _sha(
        metadata["source_archive_sha256"], "source_archive_sha256"
    )
    metadata["full_manifest_sha256"] = _sha(
        metadata["full_manifest_sha256"], "full_manifest_sha256"
    )
    metadata["snapshot_timestamp"] = _required_text(
        metadata["snapshot_timestamp"], "snapshot_timestamp"
    )
    metadata["importer_version"] = _required_text(
        metadata["importer_version"], "importer_version"
    )
    for field in (
        "source_count",
        "active_source_count",
        "valid_active_source_count",
        "restored_count",
        "staged_count",
    ):
        metadata[field] = _required_int(metadata[field], field)
    return metadata


def _cohort_key(batch: FiberTopologySourceBatch) -> tuple[str, str, str, str, str]:
    metadata = _batch_metadata(batch)
    return (
        batch.asset_type,
        metadata["source_archive_sha256"],
        metadata["full_manifest_sha256"],
        metadata["snapshot_timestamp"],
        metadata["importer_version"],
    )


def _candidate_cohorts(
    db: Session, *, expected_archive_sha256: str | None = None
) -> dict[
    str, list[tuple[tuple[str, str, str, str, str], list[FiberTopologySourceBatch]]]
]:
    filters = [
        FiberTopologySourceBatch.source_system == CRM_SOURCE_SYSTEM,
        FiberTopologySourceBatch.asset_type.in_(SUPPORTED_CRM_POINT_ASSET_TYPES),
    ]
    # Validate operator input here, but never use it to hide newer cohorts before
    # authority is resolved. Filtering first would let an operator make a stale
    # archive appear authoritative merely by repeating its own digest.
    if expected_archive_sha256 is not None:
        _sha(expected_archive_sha256, "expected_archive_sha256")
    batches = list(
        db.scalars(
            select(FiberTopologySourceBatch)
            .where(*filters)
            .order_by(FiberTopologySourceBatch.created_at.desc())
        )
    )
    grouped: dict[
        str, dict[tuple[str, str, str, str, str], list[FiberTopologySourceBatch]]
    ] = defaultdict(dict)
    for batch in batches:
        if batch.profile != CRM_POINT_PROFILES.get(batch.asset_type):
            continue
        try:
            key = _cohort_key(batch)
        except CrmNetworkMapPointMigrationError:
            # Legacy rehearsal batches predate the v2 authoritative metadata
            # contract. They remain immutable evidence, but cannot participate
            # in authority selection even when an operator supplies the expected
            # fresh archive hash.
            continue
        grouped[batch.asset_type].setdefault(key, []).append(batch)
    return {
        asset_type: list(cohorts.items()) for asset_type, cohorts in grouped.items()
    }


def _selection_from_cohort(
    key: tuple[str, str, str, str, str],
    batches: list[FiberTopologySourceBatch],
    superseded: tuple[uuid.UUID, ...],
) -> CrmAuthoritativeBatchSet:
    (
        asset_type,
        archive_sha256,
        manifest_sha256,
        snapshot_timestamp,
        importer_version,
    ) = key
    metadata = _batch_metadata(batches[0])
    if any(batch.status != "staged" or batch.blocker_count for batch in batches):
        raise CrmNetworkMapPointMigrationError(
            f"{asset_type} authoritative cohort contains blocked batches"
        )
    staged_rows = sum(batch.feature_count for batch in batches)
    if metadata["source_count"] != metadata["restored_count"]:
        raise CrmNetworkMapPointMigrationError(
            f"{asset_type} source/restored counts do not match"
        )
    if metadata["staged_count"] != staged_rows:
        raise CrmNetworkMapPointMigrationError(
            f"{asset_type} staged count does not match selected batches"
        )
    return CrmAuthoritativeBatchSet(
        asset_type=asset_type,
        profile=CRM_POINT_PROFILES[asset_type],
        source_archive_sha256=archive_sha256,
        snapshot_timestamp=snapshot_timestamp,
        importer_version=importer_version,
        full_manifest_sha256=manifest_sha256,
        source_count=metadata["source_count"],
        active_source_count=metadata["active_source_count"],
        valid_active_source_count=metadata["valid_active_source_count"],
        restored_count=metadata["restored_count"],
        staged_count=metadata["staged_count"],
        reconciliation_status=metadata["reconciliation_status"],
        batch_ids=tuple(sorted(batch.id for batch in batches)),
        superseded_batch_ids=superseded,
    )


def select_authoritative_crm_point_batches(
    db: Session, *, expected_archive_sha256: str | None = None
) -> tuple[CrmAuthoritativeBatchSet, ...]:
    """Select one authoritative immutable CRM staging cohort per point asset type."""

    selections: list[CrmAuthoritativeBatchSet] = []
    for asset_type, cohorts in sorted(
        _candidate_cohorts(db, expected_archive_sha256=expected_archive_sha256).items()
    ):
        ordered = sorted(
            cohorts,
            key=lambda item: (
                item[0][3],
                max(batch.created_at for batch in item[1]),
                item[0][1],
            ),
            reverse=True,
        )
        if not ordered:
            continue
        selected_key, selected_batches = ordered[0]
        superseded = tuple(
            sorted(batch.id for _key, rows in ordered[1:] for batch in rows)
        )
        selections.append(
            _selection_from_cohort(selected_key, selected_batches, superseded)
        )
    result = tuple(selections)
    if expected_archive_sha256 is not None:
        expected = _sha(expected_archive_sha256, "expected_archive_sha256")
        mismatched = tuple(
            selection.asset_type
            for selection in result
            if selection.source_archive_sha256 != expected
        )
        if mismatched:
            raise CrmNetworkMapPointMigrationError(
                "expected archive is not authoritative for: " + ", ".join(mismatched)
            )
    return result


def _point(feature: FiberTopologyStagedFeature) -> tuple[float, float] | None:
    geometry = feature.geometry_geojson or {}
    if geometry.get("type") != "Point":
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        return None
    try:
        longitude = float(coordinates[0])
        latitude = float(coordinates[1])
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    return longitude, latitude


def _source_property(feature: FiberTopologyStagedFeature, key: str) -> str | None:
    for candidate, value in (feature.source_properties or {}).items():
        if str(candidate).casefold() == key.casefold() and str(value or "").strip():
            return str(value).strip()
    return None


def _source_active(feature: FiberTopologyStagedFeature) -> bool:
    value = _source_property(feature, "is_active")
    return value is None or value.casefold() in {"1", "true", "yes", "active"}


def _canonical_code_match(
    db: Session, feature: FiberTopologyStagedFeature
) -> tuple[str, uuid.UUID | None]:
    code = _source_property(feature, "code") or feature.external_id
    if not code or feature.asset_type == "splice_closure":
        return "unsupported_identifier", None
    model = FdhCabinet if feature.asset_type == "fdh_cabinet" else FiberAccessPoint
    ids = tuple(
        db.scalars(select(model.id).where(func.lower(model.code) == code.casefold()))
    )
    if len(ids) == 1:
        return "exact_unique_code_match", ids[0]
    if len(ids) > 1:
        return "duplicate_canonical_code", None
    return "no_code_match", None


def _canonical_name_candidates(
    db: Session, feature: FiberTopologyStagedFeature
) -> tuple[uuid.UUID, ...]:
    name = (feature.display_name or "").strip()
    if not name:
        return ()
    model: Any
    if feature.asset_type == "fdh_cabinet":
        model = FdhCabinet
    elif feature.asset_type == "fiber_access_point":
        model = FiberAccessPoint
    else:
        model = FiberSpliceClosure
    return tuple(
        db.scalars(
            select(model.id).where(func.lower(model.name) == name.casefold())
        ).all()
    )


def _coerce_candidate_values(values: list[Any]) -> tuple[uuid.UUID, ...]:
    result: list[uuid.UUID] = []
    for value in values:
        try:
            candidate_id = uuid.UUID(str(value))
        except (TypeError, ValueError):
            continue
        if candidate_id not in result:
            result.append(candidate_id)
    return tuple(result)


def _source_links(
    db: Session,
) -> dict[tuple[str, str, str], FiberTopologyAssetSourceLink]:
    return {
        (link.source_system, link.source_asset_type, link.external_id): link
        for link in db.scalars(
            select(FiberTopologyAssetSourceLink).where(
                FiberTopologyAssetSourceLink.source_system == CRM_SOURCE_SYSTEM,
                FiberTopologyAssetSourceLink.status == "active",
            )
        )
    }


def _active_decisions(
    db: Session,
) -> dict[tuple[str, str, str], FiberTopologyIdentityDecision]:
    rows = db.scalars(
        select(FiberTopologyIdentityDecision).where(
            FiberTopologyIdentityDecision.source_system == CRM_SOURCE_SYSTEM,
            FiberTopologyIdentityDecision.source_asset_type.in_(
                SUPPORTED_CRM_POINT_ASSET_TYPES
            ),
            FiberTopologyIdentityDecision.status.in_(
                ("proposed", "approved", "change_requested")
            ),
        )
    )
    return {
        (row.source_system, row.source_asset_type, row.source_external_id): row
        for row in rows
        if row.source_external_id
    }


def _features_for_selection(
    db: Session, selection: CrmAuthoritativeBatchSet
) -> tuple[FiberTopologyStagedFeature, ...]:
    return tuple(
        db.scalars(
            select(FiberTopologyStagedFeature)
            .options(joinedload(FiberTopologyStagedFeature.batch))
            .where(FiberTopologyStagedFeature.batch_id.in_(selection.batch_ids))
            .order_by(FiberTopologyStagedFeature.row_number)
        )
        .unique()
        .all()
    )


def _classify_feature(
    db: Session,
    feature: FiberTopologyStagedFeature,
    *,
    duplicate_source_identities: set[str],
    links: dict[tuple[str, str, str], FiberTopologyAssetSourceLink],
    active_decisions: dict[tuple[str, str, str], FiberTopologyIdentityDecision],
) -> CrmFeatureReconciliation:
    source_identity = stable_source_external_id(
        feature.batch.source_system, feature.asset_type, feature.external_id
    )
    source_key = (
        feature.batch.source_system,
        feature.asset_type,
        source_identity or "",
    )
    if not source_identity:
        return CrmFeatureReconciliation(
            feature.id, None, feature.asset_type, "invalid", "missing_source_id"
        )
    if source_identity in duplicate_source_identities:
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "invalid",
            "duplicate_source_identity",
        )
    if source_key in links:
        link = links[source_key]
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "already_linked",
            "durable_source_link",
            canonical_asset_id=link.canonical_asset_id,
        )
    if source_key in active_decisions:
        decision = active_decisions[source_key]
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "unchanged",
            f"active_identity_decision_{decision.status}",
            canonical_asset_id=decision.target_asset_id,
            proposal_action=decision.action,
            proposal_reason=decision.reason,
        )
    if feature.match_status == "blocked" or feature.blocker_codes:
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "invalid",
            "staging_blocked",
        )
    if not _source_active(feature):
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "invalid",
            "inactive_source_asset",
        )
    if not (feature.display_name or "").strip():
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "invalid",
            "missing_required_name",
        )
    if feature.asset_type in {"fdh_cabinet", "fiber_access_point"} and not (
        _source_property(feature, "code") or feature.external_id
    ):
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "invalid",
            "missing_required_code",
        )
    if _point(feature) is None:
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "invalid",
            "invalid_coordinates",
        )
    match_reason, canonical_id = _canonical_code_match(db, feature)
    if canonical_id is not None:
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "exact_match",
            match_reason,
            canonical_asset_id=canonical_id,
            proposal_action="link_existing",
        )
    if match_reason == "duplicate_canonical_code":
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "conflict",
            match_reason,
        )
    candidates = tuple(
        {
            *_coerce_candidate_values(list(feature.candidate_asset_ids or [])),
            *_canonical_name_candidates(db, feature),
        }
    )
    if candidates:
        return CrmFeatureReconciliation(
            feature.id,
            source_identity,
            feature.asset_type,
            "candidate_match",
            "name_or_prior_candidate_requires_review",
            canonical_asset_id=candidates[0] if len(candidates) == 1 else None,
        )
    return CrmFeatureReconciliation(
        feature.id,
        source_identity,
        feature.asset_type,
        "create_new",
        "valid_authoritative_source_without_canonical_match",
        proposal_action="create",
    )


def reconcile_authoritative_crm_points(
    db: Session, *, expected_archive_sha256: str | None = None
) -> tuple[CrmFeatureReconciliation, ...]:
    selections = select_authoritative_crm_point_batches(
        db, expected_archive_sha256=expected_archive_sha256
    )
    links = _source_links(db)
    decisions = _active_decisions(db)
    rows: list[CrmFeatureReconciliation] = []
    for selection in selections:
        features = _features_for_selection(db, selection)
        source_counts = Counter(
            stable_source_external_id(
                feature.batch.source_system, feature.asset_type, feature.external_id
            )
            for feature in features
        )
        duplicates = {key for key, count in source_counts.items() if key and count > 1}
        rows.extend(
            _classify_feature(
                db,
                feature,
                duplicate_source_identities=duplicates,
                links=links,
                active_decisions=decisions,
            )
            for feature in features
        )
        superseded_features = tuple(
            db.scalars(
                select(FiberTopologyStagedFeature)
                .options(joinedload(FiberTopologyStagedFeature.batch))
                .where(
                    FiberTopologyStagedFeature.batch_id.in_(
                        selection.superseded_batch_ids
                    )
                )
                .order_by(
                    FiberTopologyStagedFeature.batch_id,
                    FiberTopologyStagedFeature.row_number,
                )
            )
            .unique()
            .all()
        )
        rows.extend(
            CrmFeatureReconciliation(
                staged_feature_id=feature.id,
                source_identity=stable_source_external_id(
                    feature.batch.source_system,
                    feature.asset_type,
                    feature.external_id,
                ),
                asset_type=feature.asset_type,
                classification="superseded_source",
                reason_code="newer_authoritative_source_cohort_selected",
            )
            for feature in superseded_features
        )
    return tuple(rows)


def _proposal_items(rows: tuple[CrmFeatureReconciliation, ...]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for row in rows:
        if (
            row.classification == "exact_match"
            or (
                row.classification == "unchanged"
                and row.proposal_action == "link_existing"
            )
        ) and row.canonical_asset_id:
            items.append(
                {
                    "action": "link_existing",
                    "reason": row.proposal_reason or row.reason_code,
                    "staged_feature_id": str(row.staged_feature_id),
                    "target_asset_id": str(row.canonical_asset_id),
                }
            )
        elif row.classification == "create_new" or (
            row.classification == "unchanged" and row.proposal_action == "create"
        ):
            items.append(
                {
                    "action": "create",
                    "reason": row.proposal_reason or row.reason_code,
                    "staged_feature_id": str(row.staged_feature_id),
                }
            )
    if not items:
        raise CrmNetworkMapPointMigrationError(
            "no eligible CRM point features require proposal generation"
        )
    return items


def _proposal_source_name(
    selections: tuple[CrmAuthoritativeBatchSet, ...], expected_archive_sha256: str
) -> str:
    manifest = _digest([selection.to_dict() for selection in selections])
    return (
        f"crm-network-map-point-assets-{expected_archive_sha256[:12]}-{manifest[:12]}"
    )


def preview_crm_point_identity_proposals(
    db: Session,
    *,
    expected_archive_sha256: str,
    proposed_by: str,
    reason: str,
) -> FiberIdentityProposalBatchPreview:
    selections = select_authoritative_crm_point_batches(
        db, expected_archive_sha256=expected_archive_sha256
    )
    rows = reconcile_authoritative_crm_points(
        db, expected_archive_sha256=expected_archive_sha256
    )
    return preview_identity_proposal_batch(
        db,
        _proposal_items(rows),
        proposed_by=proposed_by,
        reason=reason,
        source_name=_proposal_source_name(selections, expected_archive_sha256),
    )


def propose_crm_point_identity_proposals(
    db: Session,
    *,
    expected_archive_sha256: str,
    proposed_by: str,
    reason: str,
) -> FiberIdentityProposalBatchResult:
    selections = select_authoritative_crm_point_batches(
        db, expected_archive_sha256=expected_archive_sha256
    )
    rows = reconcile_authoritative_crm_points(
        db, expected_archive_sha256=expected_archive_sha256
    )
    return propose_identity_batch(
        db,
        _proposal_items(rows),
        proposed_by=proposed_by,
        reason=reason,
        source_name=_proposal_source_name(selections, expected_archive_sha256),
    )


def _proposal_batch_source_feature_batches(
    db: Session, proposal_batch_id: str | uuid.UUID
) -> tuple[FiberTopologySourceBatch, ...]:
    batch_id = (
        proposal_batch_id
        if isinstance(proposal_batch_id, uuid.UUID)
        else uuid.UUID(str(proposal_batch_id))
    )
    return tuple(
        db.scalars(
            select(FiberTopologySourceBatch)
            .join(
                FiberTopologyStagedFeature,
                FiberTopologyStagedFeature.batch_id == FiberTopologySourceBatch.id,
            )
            .join(
                FiberTopologyIdentityDecision,
                FiberTopologyIdentityDecision.staged_feature_id
                == FiberTopologyStagedFeature.id,
            )
            .where(FiberTopologyIdentityDecision.proposal_batch_id == batch_id)
            .options(joinedload(FiberTopologySourceBatch.features))
        )
        .unique()
        .all()
    )


def assert_crm_identity_batch_authoritative(
    db: Session,
    *,
    proposal_batch_id: str | uuid.UUID,
    expected_archive_sha256: str,
) -> None:
    source_batches = _proposal_batch_source_feature_batches(db, proposal_batch_id)
    if not source_batches:
        raise CrmNetworkMapPointMigrationError(
            "proposal batch has no CRM point-source staging evidence"
        )
    selected_ids = {
        batch_id
        for selection in select_authoritative_crm_point_batches(
            db, expected_archive_sha256=expected_archive_sha256
        )
        for batch_id in selection.batch_ids
    }
    if not selected_ids:
        if any(
            _batch_metadata(batch)["source_archive_sha256"] != expected_archive_sha256
            for batch in source_batches
        ):
            raise CrmNetworkMapPointMigrationError(
                "proposal batch archive hash mismatch"
            )
        raise CrmNetworkMapPointMigrationError(
            "no authoritative CRM point batches found"
        )
    for batch in source_batches:
        if batch.id not in selected_ids:
            raise CrmNetworkMapPointMigrationError(
                "proposal batch contains a superseded or non-authoritative source batch"
            )
        metadata = _batch_metadata(batch)
        if metadata["source_archive_sha256"] != expected_archive_sha256:
            raise CrmNetworkMapPointMigrationError(
                "proposal batch archive hash mismatch"
            )


def dry_run_crm_point_identity_apply(
    db: Session,
    *,
    proposal_batch_id: str | uuid.UUID,
    expected_archive_sha256: str,
) -> dict[str, object]:
    assert_crm_identity_batch_authoritative(
        db,
        proposal_batch_id=proposal_batch_id,
        expected_archive_sha256=expected_archive_sha256,
    )
    batch_id = (
        proposal_batch_id
        if isinstance(proposal_batch_id, uuid.UUID)
        else uuid.UUID(str(proposal_batch_id))
    )
    rows = tuple(
        db.scalars(
            select(FiberTopologyIdentityDecision)
            .where(
                FiberTopologyIdentityDecision.proposal_batch_id == batch_id,
                FiberTopologyIdentityDecision.status == "approved",
            )
            .order_by(FiberTopologyIdentityDecision.proposal_batch_row_number)
        )
    )
    return {
        "approved_decision_count": len(rows),
        "canonical_writes": 0,
        "decisions": [row.id.hex for row in rows],
        "mode": "dry_run",
        "proposal_batch_id": str(batch_id),
    }


def execute_crm_point_identity_apply(
    db: Session,
    *,
    proposal_batch_id: str | uuid.UUID,
    expected_manifest_sha256: str,
    expected_archive_sha256: str,
    executed_by: str,
    limit: int = 50,
) -> FiberIdentityExecutionRunResult:
    assert_crm_identity_batch_authoritative(
        db,
        proposal_batch_id=proposal_batch_id,
        expected_archive_sha256=expected_archive_sha256,
    )
    try:
        return execute_identity_batch(
            db,
            proposal_batch_id,
            expected_manifest_sha256=expected_manifest_sha256,
            executed_by=executed_by,
            limit=limit,
        )
    except (FiberTopologyIdentityError, FiberTopologyReviewError) as exc:
        raise CrmNetworkMapPointMigrationError(str(exc)) from exc


def build_crm_point_migration_report(
    db: Session,
    *,
    expected_archive_sha256: str | None = None,
    include_rows: bool = False,
) -> dict[str, object]:
    selections = select_authoritative_crm_point_batches(
        db, expected_archive_sha256=expected_archive_sha256
    )
    rows = reconcile_authoritative_crm_points(
        db, expected_archive_sha256=expected_archive_sha256
    )
    canonical_before = {
        "fdh_cabinet": int(db.scalar(select(func.count(FdhCabinet.id))) or 0),
        "fiber_access_point": int(
            db.scalar(select(func.count(FiberAccessPoint.id))) or 0
        ),
        "splice_closure": int(
            db.scalar(select(func.count(FiberSpliceClosure.id))) or 0
        ),
    }
    proposal_status_counts = {
        status: count
        for status, count in db.execute(
            select(
                FiberTopologyIdentityDecision.status,
                func.count(FiberTopologyIdentityDecision.id),
            )
            .where(
                FiberTopologyIdentityDecision.source_system == CRM_SOURCE_SYSTEM,
                FiberTopologyIdentityDecision.source_asset_type.in_(
                    SUPPORTED_CRM_POINT_ASSET_TYPES
                ),
            )
            .group_by(FiberTopologyIdentityDecision.status)
        )
    }
    change_request_status_counts = {
        str(getattr(status, "value", status)): count
        for status, count in db.execute(
            select(FiberChangeRequest.status, func.count(FiberChangeRequest.id))
            .join(
                FiberTopologyIdentityDecision,
                FiberTopologyIdentityDecision.change_request_id
                == FiberChangeRequest.id,
            )
            .where(
                FiberTopologyIdentityDecision.source_system == CRM_SOURCE_SYSTEM,
                FiberTopologyIdentityDecision.source_asset_type.in_(
                    SUPPORTED_CRM_POINT_ASSET_TYPES
                ),
            )
            .group_by(FiberChangeRequest.status)
        )
    }
    decision_status_by_asset: dict[str, Counter[str]] = defaultdict(Counter)
    for asset_type, status, count in db.execute(
        select(
            FiberTopologyIdentityDecision.source_asset_type,
            FiberTopologyIdentityDecision.status,
            func.count(FiberTopologyIdentityDecision.id),
        )
        .where(
            FiberTopologyIdentityDecision.source_system == CRM_SOURCE_SYSTEM,
            FiberTopologyIdentityDecision.source_asset_type.in_(
                SUPPORTED_CRM_POINT_ASSET_TYPES
            ),
        )
        .group_by(
            FiberTopologyIdentityDecision.source_asset_type,
            FiberTopologyIdentityDecision.status,
        )
    ):
        decision_status_by_asset[str(asset_type)][str(status)] = int(count)

    decision_asset_types = {
        str(decision_id): asset_type
        for decision_id, asset_type in db.execute(
            select(
                FiberTopologyIdentityDecision.id,
                FiberTopologyIdentityDecision.source_asset_type,
            ).where(
                FiberTopologyIdentityDecision.source_system == CRM_SOURCE_SYSTEM,
                FiberTopologyIdentityDecision.source_asset_type.in_(
                    SUPPORTED_CRM_POINT_ASSET_TYPES
                ),
            )
        )
    }
    failed_applies: Counter[str] = Counter()
    for payload in db.scalars(
        select(FiberTopologyIdentityExecutionRun.result_payload).where(
            FiberTopologyIdentityExecutionRun.error_count > 0
        )
    ):
        for outcome in payload.get("outcomes", []):
            if outcome.get("outcome") != "error":
                continue
            asset_type = decision_asset_types.get(str(outcome.get("decision_id")))
            if asset_type in SUPPORTED_CRM_POINT_ASSET_TYPES:
                failed_applies[asset_type] += 1

    selection_by_asset = {selection.asset_type: selection for selection in selections}
    classifications_by_asset: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        classifications_by_asset[row.asset_type][row.classification] += 1
    per_asset: dict[str, dict[str, object]] = {}
    for asset_type in sorted(SUPPORTED_CRM_POINT_ASSET_TYPES):
        selection = selection_by_asset.get(asset_type)
        classifications = classifications_by_asset[asset_type]
        statuses = decision_status_by_asset[asset_type]
        canonical_after = canonical_before[asset_type]
        applied_creates = int(
            db.scalar(
                select(func.count(FiberTopologyIdentityDecision.id)).where(
                    FiberTopologyIdentityDecision.source_system == CRM_SOURCE_SYSTEM,
                    FiberTopologyIdentityDecision.source_asset_type == asset_type,
                    FiberTopologyIdentityDecision.action == "create",
                    FiberTopologyIdentityDecision.status == "applied",
                )
            )
            or 0
        )
        per_asset[asset_type] = {
            "source_total": selection.source_count if selection else 0,
            "valid_active_source_total": (
                selection.valid_active_source_count if selection else 0
            ),
            "authoritative_batch": (
                [str(batch_id) for batch_id in selection.batch_ids] if selection else []
            ),
            "superseded_batches": (
                [str(batch_id) for batch_id in selection.superseded_batch_ids]
                if selection
                else []
            ),
            "staged_total": selection.staged_count if selection else 0,
            "already_linked": classifications["already_linked"],
            "exact_matches": classifications["exact_match"],
            "candidate_matches": classifications["candidate_match"],
            "proposed_creates": classifications["create_new"],
            "conflicts": classifications["conflict"],
            "invalid_records": classifications["invalid"],
            "approved_proposals": statuses["approved"],
            "rejected_proposals": statuses["declined"],
            "applied_proposals": statuses["applied"],
            "failed_applies": failed_applies[asset_type],
            "canonical_count_before": max(canonical_after - applied_creates, 0),
            "canonical_count_after": canonical_after,
            "hard_reconciliation_failure": bool(
                classifications["conflict"] or classifications["invalid"]
            ),
            "total_mismatch": bool(
                selection and selection.source_count != selection.restored_count
            ),
        }
    report = CrmPointMigrationReport(
        selections=selections,
        rows=rows,
        canonical_before=canonical_before,
        proposal_status_counts=proposal_status_counts,
        change_request_status_counts=change_request_status_counts,
    )
    payload = report.to_dict(include_rows=include_rows)
    payload["per_asset"] = per_asset
    payload["canonical_count_before"] = {
        asset_type: cast(int, summary["canonical_count_before"])
        for asset_type, summary in per_asset.items()
    }
    payload["canonical_count_after"] = {
        asset_type: cast(int, summary["canonical_count_after"])
        for asset_type, summary in per_asset.items()
    }
    payload["hard_reconciliation_failure"] = any(
        bool(summary["hard_reconciliation_failure"]) or bool(summary["total_mismatch"])
        for summary in per_asset.values()
    )
    return payload


__all__ = [
    "CRM_POINT_PROFILES",
    "EXPECTED_RECONCILIATION_STATUS",
    "SUPPORTED_CRM_POINT_ASSET_TYPES",
    "CrmAuthoritativeBatchSet",
    "CrmFeatureReconciliation",
    "CrmNetworkMapPointMigrationError",
    "assert_crm_identity_batch_authoritative",
    "build_crm_point_migration_report",
    "dry_run_crm_point_identity_apply",
    "execute_crm_point_identity_apply",
    "preview_crm_point_identity_proposals",
    "propose_crm_point_identity_proposals",
    "reconcile_authoritative_crm_points",
    "select_authoritative_crm_point_batches",
]
