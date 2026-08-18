"""Immutable KMZ source staging for the fiber-topology owner.

This module writes source facts and match suggestions only.  It never creates,
updates, merges, retires, or deletes canonical network/GIS assets.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path

from defusedxml import ElementTree as ET
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.gis import ServiceBuilding
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberSegment,
    FiberSpliceClosure,
)

KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}
SOURCE_SYSTEM = "dotmac_osp_kmz"
NORMALIZATION_VERSION = 1
MAX_KML_BYTES = 100 * 1024 * 1024
NIGERIA_LONGITUDE_RANGE = (2.0, 15.0)
NIGERIA_LATITUDE_RANGE = (4.0, 14.0)


@dataclass(frozen=True)
class FiberSourceProfile:
    name: str
    default_filename: str
    asset_type: str
    external_id_key: str
    expected_geometry_type: str
    display_name_keys: tuple[str, ...]
    source_system: str = SOURCE_SYSTEM


SOURCE_PROFILES: dict[str, FiberSourceProfile] = {
    "osp_paths": FiberSourceProfile(
        name="osp_paths",
        default_filename="OSP Paths.kmz",
        asset_type="fiber_segment",
        external_id_key="spanid",
        expected_geometry_type="LineString",
        display_name_keys=("name", "spanid"),
    ),
    "osp_access_points": FiberSourceProfile(
        name="osp_access_points",
        default_filename="OSP Access point.kmz",
        asset_type="fiber_access_point",
        external_id_key="access_pointid",
        expected_geometry_type="Polygon",
        display_name_keys=("Name",),
    ),
    "osp_cabinets": FiberSourceProfile(
        name="osp_cabinets",
        default_filename="OSP Cabinet.kmz",
        asset_type="fdh_cabinet",
        external_id_key="fibermngrid",
        expected_geometry_type="Polygon",
        display_name_keys=("name",),
    ),
    "osp_splice_info": FiberSourceProfile(
        name="osp_splice_info",
        default_filename="OSP Splice info.kmz",
        asset_type="splice_closure",
        external_id_key="enclosureid",
        expected_geometry_type="Polygon",
        display_name_keys=("name",),
    ),
    "osp_buildings": FiberSourceProfile(
        name="osp_buildings",
        default_filename="OSP Building.kmz",
        asset_type="service_building",
        external_id_key="buildingid",
        expected_geometry_type="Polygon",
        display_name_keys=("Name",),
    ),
    "osp_air_fiber": FiberSourceProfile(
        name="osp_air_fiber",
        default_filename="OSP Air fiber.kmz",
        asset_type="support_structure",
        external_id_key="poleid",
        expected_geometry_type="Point",
        display_name_keys=("name",),
    ),
    "crm_fdh_cabinets": FiberSourceProfile(
        name="crm_fdh_cabinets",
        default_filename="crm_fdh_cabinets.kml",
        asset_type="fdh_cabinet",
        external_id_key="crm_id",
        expected_geometry_type="Point",
        display_name_keys=("name", "code", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
    "crm_access_points": FiberSourceProfile(
        name="crm_access_points",
        default_filename="crm_access_points.kml",
        asset_type="fiber_access_point",
        external_id_key="crm_id",
        expected_geometry_type="Point",
        display_name_keys=("name", "code", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
    "crm_splice_closures": FiberSourceProfile(
        name="crm_splice_closures",
        default_filename="crm_splice_closures.kml",
        asset_type="splice_closure",
        external_id_key="crm_id",
        expected_geometry_type="Point",
        display_name_keys=("name", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
    "crm_fiber_segments": FiberSourceProfile(
        name="crm_fiber_segments",
        default_filename="crm_fiber_segments.kml",
        asset_type="fiber_segment",
        external_id_key="crm_id",
        expected_geometry_type="LineString",
        display_name_keys=("name", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
    "crm_service_buildings": FiberSourceProfile(
        name="crm_service_buildings",
        default_filename="crm_service_buildings.kml",
        asset_type="service_building",
        external_id_key="crm_id",
        expected_geometry_type="Point",
        display_name_keys=("name", "code", "crm_id"),
        source_system="dotmac_crm_fiber_map",
    ),
}


@dataclass(frozen=True)
class ParsedFiberFeature:
    row_number: int
    asset_type: str
    external_id: str | None
    display_name: str | None
    geometry_type: str
    geometry_geojson: dict
    source_properties: dict
    content_sha256: str
    geometry_sha256: str
    blocker_codes: tuple[str, ...]


@dataclass(frozen=True)
class FiberFeatureMatchPlan:
    feature: ParsedFiberFeature
    match_status: str
    match_reasons: tuple[str, ...]
    candidate_asset_ids: tuple[str, ...]
    canonical_asset_type: str | None
    canonical_asset_id: object | None
    prior_feature_id: object | None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["canonical_asset_id"] = (
            str(self.canonical_asset_id) if self.canonical_asset_id else None
        )
        payload["prior_feature_id"] = (
            str(self.prior_feature_id) if self.prior_feature_id else None
        )
        return payload


@dataclass(frozen=True)
class FiberSourcePreview:
    source_system: str
    profile: FiberSourceProfile
    source_name: str
    file_sha256: str
    manifest_sha256: str
    features: tuple[FiberFeatureMatchPlan, ...]
    status_counts: dict[str, int]
    kml_entry_name: str

    @property
    def feature_count(self) -> int:
        return len(self.features)

    @property
    def blocker_count(self) -> int:
        return self.status_counts.get("blocked", 0)

    @property
    def candidate_count(self) -> int:
        return sum(
            self.status_counts.get(status, 0)
            for status in ("exact_external", "candidate", "ambiguous")
        )

    def to_dict(self, *, include_features: bool = False) -> dict:
        payload = {
            "source_system": self.source_system,
            "profile": self.profile.name,
            "source_name": self.source_name,
            "asset_type": self.profile.asset_type,
            "external_id_key": self.profile.external_id_key,
            "expected_geometry_type": self.profile.expected_geometry_type,
            "normalization_version": NORMALIZATION_VERSION,
            "file_sha256": self.file_sha256,
            "manifest_sha256": self.manifest_sha256,
            "feature_count": self.feature_count,
            "blocker_count": self.blocker_count,
            "candidate_count": self.candidate_count,
            "status_counts": dict(sorted(self.status_counts.items())),
            "kml_entry_name": self.kml_entry_name,
        }
        if include_features:
            payload["features"] = [feature.to_dict() for feature in self.features]
        return payload


@dataclass(frozen=True)
class FiberSourceStageResult:
    batch_id: object
    created: bool
    status: str
    feature_count: int
    blocker_count: int
    candidate_count: int
    new_count: int
    unchanged_count: int
    file_sha256: str
    manifest_sha256: str

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["batch_id"] = str(self.batch_id)
        return payload


@dataclass(frozen=True)
class _CanonicalLookup:
    exact_external: dict[str, tuple[object, ...]]
    by_name: dict[str, tuple[object, ...]]


def source_profile(name: str) -> FiberSourceProfile:
    try:
        return SOURCE_PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported fiber source profile: {name}") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_json(value) -> str:
    return _sha256_bytes(_canonical_json(value))


def _normalized_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _read_kml(path: Path) -> tuple[bytes, bytes, str]:
    raw = path.read_bytes()
    if path.suffix.casefold() == ".kml":
        if len(raw) > MAX_KML_BYTES:
            raise ValueError("KML source exceeds the staging size limit")
        return raw, raw, path.name
    if path.suffix.casefold() != ".kmz":
        raise ValueError("Fiber topology sources must be KMZ or KML files")

    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            entries = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.casefold().endswith(".kml")
            ]
            if len(entries) != 1:
                raise ValueError("KMZ source must contain exactly one KML document")
            entry = entries[0]
            if entry.file_size > MAX_KML_BYTES:
                raise ValueError("KMZ KML document exceeds the staging size limit")
            return raw, archive.read(entry), entry.filename
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid KMZ archive") from exc


def _properties(placemark: ET.Element) -> dict[str, str | None]:
    properties: dict[str, str | None] = {}
    for element in placemark.findall(".//kml:SimpleData", KML_NS):
        key = (element.attrib.get("name") or "").strip()
        if key:
            value = (element.text or "").strip()
            properties[key] = value or None
    for element in placemark.findall(".//kml:Data", KML_NS):
        key = (element.attrib.get("name") or "").strip()
        if key:
            value = element.findtext("kml:value", default="", namespaces=KML_NS).strip()
            properties[key] = value or None
    return dict(sorted(properties.items(), key=lambda item: item[0].casefold()))


def _property(properties: dict[str, str | None], key: str) -> str | None:
    target = key.casefold()
    for candidate, value in properties.items():
        if candidate.casefold() == target:
            return (value or "").strip() or None
    return None


def _coordinates(text: str) -> tuple[list[list[float]], list[str]]:
    coordinates: list[list[float]] = []
    blockers: list[str] = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) < 2:
            blockers.append("invalid_coordinate")
            continue
        try:
            longitude = round(float(parts[0]), 7)
            latitude = round(float(parts[1]), 7)
        except ValueError:
            blockers.append("invalid_coordinate")
            continue
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            blockers.append("invalid_coordinate")
            continue
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            blockers.append("invalid_coordinate")
        if not (
            NIGERIA_LONGITUDE_RANGE[0] <= longitude <= NIGERIA_LONGITUDE_RANGE[1]
            and NIGERIA_LATITUDE_RANGE[0] <= latitude <= NIGERIA_LATITUDE_RANGE[1]
        ):
            blockers.append("coordinate_outside_nigeria")
        coordinates.append([longitude, latitude])
    return coordinates, list(dict.fromkeys(blockers))


def _geometry(placemark: ET.Element) -> tuple[str, dict, tuple[str, ...]]:
    for geometry_type in ("Point", "LineString", "Polygon"):
        element = placemark.find(f".//kml:{geometry_type}", KML_NS)
        if element is None:
            continue
        text = element.findtext(".//kml:coordinates", default="", namespaces=KML_NS)
        coordinates, blockers = _coordinates(text)
        if geometry_type == "Point":
            if len(coordinates) != 1:
                blockers.append("invalid_point_geometry")
            geojson = {
                "type": "Point",
                "coordinates": coordinates[0] if coordinates else [],
            }
        elif geometry_type == "LineString":
            if len(coordinates) < 2:
                blockers.append("invalid_linestring_geometry")
            geojson = {"type": "LineString", "coordinates": coordinates}
        else:
            if coordinates and coordinates[0] != coordinates[-1]:
                coordinates.append(coordinates[0])
            if len(coordinates) < 4:
                blockers.append("invalid_polygon_geometry")
            geojson = {"type": "Polygon", "coordinates": [coordinates]}
        return geometry_type, geojson, tuple(dict.fromkeys(blockers))
    return (
        "Unknown",
        {"type": "GeometryCollection", "geometries": []},
        ("missing_supported_geometry",),
    )


def _parse_features(
    kml: bytes, profile: FiberSourceProfile
) -> list[ParsedFiberFeature]:
    try:
        root = ET.fromstring(kml)
    except ET.ParseError as exc:
        raise ValueError("Invalid KML document") from exc

    parsed: list[ParsedFiberFeature] = []
    for row_number, placemark in enumerate(
        root.findall(".//kml:Placemark", KML_NS), start=1
    ):
        properties = _properties(placemark)
        external_id = _property(properties, profile.external_id_key)
        placemark_name = (
            placemark.findtext("kml:name", default="", namespaces=KML_NS).strip()
            or None
        )
        display_name = next(
            (
                value
                for key in profile.display_name_keys
                if (value := _property(properties, key))
            ),
            placemark_name,
        )
        geometry_type, geojson, geometry_blockers = _geometry(placemark)
        blockers = list(geometry_blockers)
        if not external_id:
            blockers.append("missing_external_id")
        if geometry_type != profile.expected_geometry_type:
            blockers.append("unexpected_geometry_type")
        geometry_sha256 = _sha256_json(geojson)
        normalized = {
            "normalization_version": NORMALIZATION_VERSION,
            "asset_type": profile.asset_type,
            "external_id": external_id,
            "display_name": display_name,
            "geometry": geojson,
            "properties": properties,
        }
        parsed.append(
            ParsedFiberFeature(
                row_number=row_number,
                asset_type=profile.asset_type,
                external_id=external_id,
                display_name=display_name,
                geometry_type=geometry_type,
                geometry_geojson=geojson,
                source_properties=properties,
                content_sha256=_sha256_json(normalized),
                geometry_sha256=geometry_sha256,
                blocker_codes=tuple(dict.fromkeys(blockers)),
            )
        )
    if not parsed:
        raise ValueError("KML source contains no placemarks")
    return parsed


def _ids_by_key(rows, attribute: str) -> dict[str, tuple[object, ...]]:
    grouped: dict[str, list[object]] = defaultdict(list)
    for row in rows:
        key = _normalized_key(getattr(row, attribute, None))
        if key:
            grouped[key].append(row.id)
    return {key: tuple(values) for key, values in grouped.items()}


def _canonical_lookup(db: Session, profile: FiberSourceProfile) -> _CanonicalLookup:
    if profile.asset_type == "fdh_cabinet":
        fdh_rows = db.scalars(select(FdhCabinet)).all()
        return _CanonicalLookup(
            _ids_by_key(fdh_rows, "code"), _ids_by_key(fdh_rows, "name")
        )
    if profile.asset_type == "fiber_access_point":
        access_point_rows = db.scalars(select(FiberAccessPoint)).all()
        return _CanonicalLookup(
            _ids_by_key(access_point_rows, "code"),
            _ids_by_key(access_point_rows, "name"),
        )
    if profile.asset_type == "service_building":
        building_rows = db.scalars(select(ServiceBuilding)).all()
        return _CanonicalLookup(
            _ids_by_key(building_rows, "code"),
            _ids_by_key(building_rows, "name"),
        )
    if profile.asset_type == "fiber_segment":
        segment_rows = db.scalars(select(FiberSegment)).all()
        return _CanonicalLookup({}, _ids_by_key(segment_rows, "name"))
    if profile.asset_type == "splice_closure":
        closure_rows = db.scalars(select(FiberSpliceClosure)).all()
        return _CanonicalLookup({}, _ids_by_key(closure_rows, "name"))
    return _CanonicalLookup({}, {})


def _prior_features(
    db: Session, profile: FiberSourceProfile
) -> dict[str, FiberTopologyStagedFeature]:
    rows = db.scalars(
        select(FiberTopologyStagedFeature)
        .join(
            FiberTopologySourceBatch,
            FiberTopologySourceBatch.id == FiberTopologyStagedFeature.batch_id,
        )
        .where(
            FiberTopologySourceBatch.source_system == profile.source_system,
            FiberTopologySourceBatch.profile == profile.name,
            FiberTopologyStagedFeature.asset_type == profile.asset_type,
            FiberTopologyStagedFeature.external_id.is_not(None),
        )
        .order_by(
            FiberTopologySourceBatch.created_at.desc(),
            FiberTopologyStagedFeature.created_at.desc(),
        )
    ).all()
    result: dict[str, FiberTopologyStagedFeature] = {}
    for row in rows:
        key = _normalized_key(row.external_id)
        if key and key not in result:
            result[key] = row
    return result


def _plan_features(
    db: Session,
    profile: FiberSourceProfile,
    features: list[ParsedFiberFeature],
) -> tuple[FiberFeatureMatchPlan, ...]:
    canonical = _canonical_lookup(db, profile)
    prior = _prior_features(db, profile)
    external_counts = Counter(
        key for feature in features if (key := _normalized_key(feature.external_id))
    )
    name_counts = Counter(
        key for feature in features if (key := _normalized_key(feature.display_name))
    )
    geometry_counts = Counter(feature.geometry_sha256 for feature in features)

    plans: list[FiberFeatureMatchPlan] = []
    for feature in features:
        blockers = list(feature.blocker_codes)
        reasons: list[str] = []
        candidates: set[object] = set()
        canonical_id = None
        prior_id = None
        external_key = _normalized_key(feature.external_id)
        name_key = _normalized_key(feature.display_name)

        if external_key and external_counts[external_key] > 1:
            blockers.append("duplicate_external_id")
        if name_key and name_counts[name_key] > 1:
            reasons.append("duplicate_source_name")
        if geometry_counts[feature.geometry_sha256] > 1:
            reasons.append("duplicate_source_geometry")
        if not feature.display_name:
            reasons.append("missing_display_name")

        prior_feature = prior.get(external_key) if external_key else None
        if prior_feature is not None:
            prior_id = prior_feature.id
            if prior_feature.content_sha256 == feature.content_sha256:
                reasons.append("unchanged_source_identity")
            else:
                reasons.append("changed_source_identity")

        exact = canonical.exact_external.get(external_key, ())
        if exact:
            candidates.update(exact)
            reasons.append("canonical_external_id_match")
        name_matches = canonical.by_name.get(name_key, ()) if name_key else ()
        if name_matches:
            candidates.update(name_matches)
            reasons.append("canonical_normalized_name_match")

        if blockers:
            status = "blocked"
        elif len(exact) > 1 or len(name_matches) > 1:
            status = "ambiguous"
        elif "changed_source_identity" in reasons:
            status = "candidate"
        elif exact:
            status = "exact_external"
            canonical_id = exact[0]
        elif name_matches or any(
            reason
            in {
                "duplicate_source_name",
                "duplicate_source_geometry",
                "missing_display_name",
            }
            for reason in reasons
        ):
            status = "candidate"
            if len(name_matches) == 1:
                canonical_id = name_matches[0]
        elif "unchanged_source_identity" in reasons:
            status = "unchanged"
        else:
            status = "new"

        plans.append(
            FiberFeatureMatchPlan(
                feature=ParsedFiberFeature(
                    **{
                        **asdict(feature),
                        "blocker_codes": tuple(dict.fromkeys(blockers)),
                    }
                ),
                match_status=status,
                match_reasons=tuple(dict.fromkeys(reasons)),
                candidate_asset_ids=tuple(sorted(str(value) for value in candidates)),
                canonical_asset_type=(profile.asset_type if canonical_id else None),
                canonical_asset_id=canonical_id,
                prior_feature_id=prior_id,
            )
        )
    return tuple(plans)


def preview_fiber_source(
    db: Session, path: str | Path, profile_name: str
) -> FiberSourcePreview:
    """Parse and plan one source without persisting anything."""
    profile = source_profile(profile_name)
    source_path = Path(path)
    raw, kml, kml_entry_name = _read_kml(source_path)
    parsed = _parse_features(kml, profile)
    manifest_rows = sorted(
        [
            {
                "external_id": feature.external_id,
                "content_sha256": feature.content_sha256,
            }
            for feature in parsed
        ],
        key=lambda row: (
            _normalized_key(row["external_id"]),
            row["content_sha256"],
        ),
    )
    plans = _plan_features(db, profile, parsed)
    return FiberSourcePreview(
        source_system=profile.source_system,
        profile=profile,
        source_name=source_path.name,
        file_sha256=_sha256_bytes(raw),
        manifest_sha256=_sha256_json(manifest_rows),
        features=plans,
        status_counts=dict(Counter(plan.match_status for plan in plans)),
        kml_entry_name=kml_entry_name,
    )


def _stage_result(batch: FiberTopologySourceBatch, *, created: bool):
    return FiberSourceStageResult(
        batch_id=batch.id,
        created=created,
        status=batch.status,
        feature_count=batch.feature_count,
        blocker_count=batch.blocker_count,
        candidate_count=batch.candidate_count,
        new_count=batch.new_count,
        unchanged_count=batch.unchanged_count,
        file_sha256=batch.file_sha256,
        manifest_sha256=batch.manifest_sha256,
    )


def _manifest_sha256(
    plans: tuple[FiberFeatureMatchPlan, ...],
    *,
    source_metadata: dict | None = None,
) -> str:
    manifest_rows = sorted(
        [
            {
                "external_id": plan.feature.external_id,
                "content_sha256": plan.feature.content_sha256,
            }
            for plan in plans
        ],
        key=lambda row: (
            _normalized_key(row["external_id"]),
            row["content_sha256"],
        ),
    )
    if not source_metadata or not source_metadata.get("source_archive_sha256"):
        return _sha256_json(manifest_rows)
    idempotency = {
        "importer_version": str(source_metadata.get("importer_version", "")),
        "source_archive_sha256": str(source_metadata["source_archive_sha256"]),
    }
    full_manifest_sha256 = source_metadata.get("full_manifest_sha256")
    if full_manifest_sha256:
        idempotency["full_manifest_sha256"] = str(full_manifest_sha256)
    return _sha256_json(
        {
            "features": manifest_rows,
            "idempotency": idempotency,
        }
    )


def _persist_preview(
    db: Session,
    preview: FiberSourcePreview,
    *,
    plans: tuple[FiberFeatureMatchPlan, ...],
    source_name: str,
    created_by: str,
    source_metadata: dict | None = None,
) -> FiberSourceStageResult:
    manifest_sha256 = _manifest_sha256(plans, source_metadata=source_metadata)
    existing = db.scalar(
        select(FiberTopologySourceBatch).where(
            FiberTopologySourceBatch.source_system == preview.source_system,
            FiberTopologySourceBatch.profile == preview.profile.name,
            FiberTopologySourceBatch.manifest_sha256 == manifest_sha256,
        )
    )
    if existing is not None:
        return _stage_result(existing, created=False)

    status_counts = Counter(plan.match_status for plan in plans)
    blocker_count = status_counts.get("blocked", 0)
    candidate_count = sum(
        status_counts.get(status, 0)
        for status in ("exact_external", "candidate", "ambiguous")
    )
    metadata = {
        "normalization_version": NORMALIZATION_VERSION,
        "kml_entry_name": preview.kml_entry_name,
        "expected_geometry_type": preview.profile.expected_geometry_type,
        **(source_metadata or {}),
    }
    batch = FiberTopologySourceBatch(
        source_system=preview.source_system,
        profile=preview.profile.name,
        source_name=source_name,
        asset_type=preview.profile.asset_type,
        external_id_key=preview.profile.external_id_key,
        file_sha256=preview.file_sha256,
        manifest_sha256=manifest_sha256,
        status="blocked" if blocker_count else "staged",
        feature_count=len(plans),
        blocker_count=blocker_count,
        candidate_count=candidate_count,
        unchanged_count=status_counts.get("unchanged", 0),
        new_count=status_counts.get("new", 0),
        source_metadata=metadata,
        created_by=created_by,
    )
    db.add(batch)
    db.flush()
    for plan in plans:
        feature = plan.feature
        db.add(
            FiberTopologyStagedFeature(
                batch_id=batch.id,
                row_number=feature.row_number,
                asset_type=feature.asset_type,
                external_id=feature.external_id,
                display_name=feature.display_name,
                geometry_type=feature.geometry_type,
                geometry_geojson=feature.geometry_geojson,
                source_properties=feature.source_properties,
                content_sha256=feature.content_sha256,
                geometry_sha256=feature.geometry_sha256,
                match_status=plan.match_status,
                blocker_codes=list(feature.blocker_codes),
                match_reasons=list(plan.match_reasons),
                candidate_asset_ids=list(plan.candidate_asset_ids),
                canonical_asset_type=plan.canonical_asset_type,
                canonical_asset_id=plan.canonical_asset_id,
                prior_feature_id=plan.prior_feature_id,
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(
            select(FiberTopologySourceBatch).where(
                FiberTopologySourceBatch.source_system == preview.source_system,
                FiberTopologySourceBatch.profile == preview.profile.name,
                FiberTopologySourceBatch.manifest_sha256 == manifest_sha256,
            )
        )
        if existing is None:
            raise
        return _stage_result(existing, created=False)
    db.refresh(batch)
    return _stage_result(batch, created=True)


def stage_fiber_preview_batch(
    db: Session,
    preview: FiberSourcePreview,
    *,
    start: int,
    stop: int,
    source_name: str,
    created_by: str,
    source_metadata: dict | None = None,
) -> FiberSourceStageResult:
    """Persist one bounded slice using the full-source match classification."""

    actor = created_by.strip()
    if not actor:
        raise ValueError("created_by is required for staged topology evidence")
    if start < 0 or stop <= start or stop > preview.feature_count:
        raise ValueError("preview batch bounds are invalid")
    metadata = {
        "batch_start": start + 1,
        "batch_stop": stop,
        **(source_metadata or {}),
    }
    metadata["full_manifest_sha256"] = _manifest_sha256(
        preview.features,
        source_metadata=metadata,
    )
    return _persist_preview(
        db,
        preview,
        plans=preview.features[start:stop],
        source_name=source_name,
        created_by=actor,
        source_metadata=metadata,
    )


def stage_fiber_source(
    db: Session,
    path: str | Path,
    profile_name: str,
    *,
    created_by: str,
) -> FiberSourceStageResult:
    """Persist an immutable source snapshot; never mutate canonical assets."""
    actor = created_by.strip()
    if not actor:
        raise ValueError("created_by is required for staged topology evidence")
    preview = preview_fiber_source(db, path, profile_name)
    return _persist_preview(
        db,
        preview,
        plans=preview.features,
        source_name=preview.source_name,
        created_by=actor,
    )


__all__ = [
    "FiberFeatureMatchPlan",
    "FiberSourcePreview",
    "FiberSourceProfile",
    "FiberSourceStageResult",
    "SOURCE_PROFILES",
    "preview_fiber_source",
    "source_profile",
    "stage_fiber_preview_batch",
    "stage_fiber_source",
]
