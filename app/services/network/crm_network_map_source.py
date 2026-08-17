"""Read-only extraction of a restored CRM Network Map archive.

The restored CRM database is an external observation.  This module validates
the selective archive, reads only the approved map tables, and produces
deterministic KML batches for ``network.fiber_source_staging``.  It never
connects to or writes Selfcare's canonical network tables.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

# This module only constructs XML from normalized values; it never parses XML.
from xml.etree import ElementTree as ET  # nosec B405

from sqlalchemy import MetaData, Table, func, inspect, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql.elements import ColumnElement

from app.services.domain_errors import DomainError

SOURCE_SYSTEM = "dotmac_crm_fiber_map"
FORMAT_VERSION = 1
MAX_BATCH_SIZE = 100
SEGMENT_EVIDENCE_SCHEMA_VERSION = "crm_fiber_segments:endpoint_capacity:v1"
NIGERIA_LONGITUDE_RANGE = (2.0, 15.0)
NIGERIA_LATITUDE_RANGE = (4.0, 14.0)
KML_NAMESPACE = "http://www.opengis.net/kml/2.2"

ARCHIVE_TABLES = frozenset(
    {
        "fdh_cabinets",
        "splitters",
        "fiber_splice_closures",
        "fiber_splice_trays",
        "fiber_splices",
        "fiber_strands",
        "fiber_termination_points",
        "fiber_segments",
        "olt_devices",
        "olt_shelves",
        "olt_cards",
        "olt_card_ports",
        "olt_power_units",
        "pon_ports",
        "fiber_access_points",
        "service_buildings",
    }
)


class CrmMapProfileName(StrEnum):
    FDH_CABINETS = "crm_fdh_cabinets"
    ACCESS_POINTS = "crm_access_points"
    SPLICE_CLOSURES = "crm_splice_closures"
    FIBER_SEGMENTS = "crm_fiber_segments"
    SERVICE_BUILDINGS = "crm_service_buildings"


@dataclass(frozen=True)
class CrmMapProfileSpec:
    profile: CrmMapProfileName
    table_name: str
    asset_type: str
    geometry_column: str | None
    geometry_type: str
    property_columns: tuple[str, ...]


PROFILE_SPECS: tuple[CrmMapProfileSpec, ...] = (
    CrmMapProfileSpec(
        profile=CrmMapProfileName.FDH_CABINETS,
        table_name="fdh_cabinets",
        asset_type="fdh_cabinet",
        geometry_column=None,
        geometry_type="Point",
        property_columns=("name", "code", "notes"),
    ),
    CrmMapProfileSpec(
        profile=CrmMapProfileName.ACCESS_POINTS,
        table_name="fiber_access_points",
        asset_type="fiber_access_point",
        geometry_column=None,
        geometry_type="Point",
        property_columns=("code", "name", "access_point_type", "placement"),
    ),
    CrmMapProfileSpec(
        profile=CrmMapProfileName.SPLICE_CLOSURES,
        table_name="fiber_splice_closures",
        asset_type="splice_closure",
        geometry_column=None,
        geometry_type="Point",
        property_columns=("name", "notes"),
    ),
    CrmMapProfileSpec(
        profile=CrmMapProfileName.FIBER_SEGMENTS,
        table_name="fiber_segments",
        asset_type="fiber_segment",
        geometry_column="route_geom",
        geometry_type="LineString",
        property_columns=(
            "name",
            "segment_type",
            "cable_type",
            "fiber_count",
            "length_m",
            "notes",
        ),
    ),
    CrmMapProfileSpec(
        profile=CrmMapProfileName.SERVICE_BUILDINGS,
        table_name="service_buildings",
        asset_type="service_building",
        geometry_column=None,
        geometry_type="Point",
        property_columns=("code", "name", "city", "street", "notes"),
    ),
)


class CrmNetworkMapSourceError(DomainError):
    """Safe, stable failure raised while reading the restored CRM archive."""


@dataclass(frozen=True)
class CrmMapSourceBlocker:
    profile: CrmMapProfileName
    source_id: str
    code: str

    def to_dict(self) -> dict[str, str]:
        return {
            "profile": self.profile.value,
            "source_id": self.source_id,
            "code": self.code,
        }


@dataclass(frozen=True)
class CrmMapSourceFeature:
    source_id: str
    display_name: str | None
    geometry_type: str
    coordinates: tuple[float, float] | tuple[tuple[float, float], ...]
    properties: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class CrmMapProfileExtraction:
    profile: CrmMapProfileName
    source_table: str
    source_count: int
    inactive_count: int
    features: tuple[CrmMapSourceFeature, ...]
    blockers: tuple[CrmMapSourceBlocker, ...]

    @property
    def feature_count(self) -> int:
        return len(self.features)

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.value,
            "source_table": self.source_table,
            "source_count": self.source_count,
            "inactive_count": self.inactive_count,
            "feature_count": self.feature_count,
            "blocker_count": len(self.blockers),
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


@dataclass(frozen=True)
class CrmNetworkMapExtraction:
    archive_sha256: str
    database_name: str
    profiles: tuple[CrmMapProfileExtraction, ...]
    unsupported_active_olt_count: int
    dependency_table_counts: tuple[tuple[str, int], ...]

    @property
    def blocker_count(self) -> int:
        return sum(len(profile.blockers) for profile in self.profiles)

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": FORMAT_VERSION,
            "source_system": SOURCE_SYSTEM,
            "archive_sha256": self.archive_sha256,
            "database_name": self.database_name,
            "blocker_count": self.blocker_count,
            "unsupported_active_olt_count": self.unsupported_active_olt_count,
            "dependency_table_counts": dict(self.dependency_table_counts),
            "profiles": [profile.to_dict() for profile in self.profiles],
        }


def _error(code: str, message: str, **details: object) -> CrmNetworkMapSourceError:
    return CrmNetworkMapSourceError(
        code=f"network.crm_map_source.{code}",
        message=message,
        details=details,
    )


def validate_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise _error("invalid_archive_digest", "Archive SHA-256 is invalid.")
    return normalized


def _json_value(value: object) -> str | None:
    if value is None:
        return None
    enum_value = getattr(value, "value", value)
    if isinstance(enum_value, bool):
        return "true" if enum_value else "false"
    return str(enum_value)


def _finite_coordinate(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = round(float(str(value)), 7)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid_nigeria_coordinate(longitude: float, latitude: float) -> bool:
    return (
        NIGERIA_LONGITUDE_RANGE[0] <= longitude <= NIGERIA_LONGITUDE_RANGE[1]
        and NIGERIA_LATITUDE_RANGE[0] <= latitude <= NIGERIA_LATITUDE_RANGE[1]
    )


def _line_coordinates(raw_geojson: object) -> tuple[tuple[float, float], ...] | None:
    if not raw_geojson:
        return None
    try:
        payload = json.loads(str(raw_geojson))
    except (TypeError, ValueError):
        return None
    if payload.get("type") != "LineString":
        return None
    coordinates: list[tuple[float, float]] = []
    for pair in payload.get("coordinates") or []:
        if not isinstance(pair, list | tuple) or len(pair) < 2:
            return None
        longitude = _finite_coordinate(pair[0])
        latitude = _finite_coordinate(pair[1])
        if longitude is None or latitude is None:
            return None
        if not _valid_nigeria_coordinate(longitude, latitude):
            return None
        coordinates.append((longitude, latitude))
    return tuple(coordinates) if len(coordinates) >= 2 else None


def _line_geometry_payload(raw_geojson: object) -> tuple[str, object] | None:
    if not raw_geojson:
        return None
    try:
        payload = json.loads(str(raw_geojson))
    except (TypeError, ValueError):
        return None
    geometry_type = payload.get("type")
    if geometry_type == "LineString":
        line = _line_coordinates(raw_geojson)
        return (geometry_type, line) if line is not None else None
    if geometry_type != "MultiLineString":
        return None
    lines: list[tuple[tuple[float, float], ...]] = []
    for raw_line in payload.get("coordinates") or []:
        if not isinstance(raw_line, list):
            return None
        line_payload = json.dumps({"type": "LineString", "coordinates": raw_line})
        line = _line_coordinates(line_payload)
        if line is None:
            return None
        lines.append(line)
    return (geometry_type, tuple(lines)) if lines else None


def _required_columns(spec: CrmMapProfileSpec) -> set[str]:
    required = {"id", "is_active", *spec.property_columns}
    if spec.geometry_column:
        required.add(spec.geometry_column)
        if spec.profile is CrmMapProfileName.FIBER_SEGMENTS:
            required.update(
                {
                    "created_at",
                    "from_point_id",
                    "to_point_id",
                    "updated_at",
                }
            )
    else:
        required.update({"latitude", "longitude"})
    return required


def _validated_tables(connection: Connection) -> dict[str, Table]:
    table_names = set(inspect(connection).get_table_names(schema="public"))
    missing_tables = sorted(ARCHIVE_TABLES - table_names)
    if missing_tables:
        raise _error(
            "missing_archive_tables",
            "The restored archive is missing required map tables.",
            missing_tables=missing_tables,
        )
    metadata = MetaData()
    tables = {
        name: Table(name, metadata, schema="public", autoload_with=connection)
        for name in sorted(ARCHIVE_TABLES)
    }
    for spec in PROFILE_SPECS:
        missing_columns = sorted(
            _required_columns(spec) - set(tables[spec.table_name].c.keys())
        )
        if missing_columns:
            raise _error(
                "missing_archive_columns",
                "A restored map table is missing required columns.",
                table=spec.table_name,
                missing_columns=missing_columns,
            )
    return tables


def _related_counts(
    connection: Connection, tables: dict[str, Table]
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {
        "splitter_count": {},
        "tray_count": {},
        "splice_count": {},
    }
    relationships = (
        ("splitter_count", "splitters", "fdh_id"),
        ("tray_count", "fiber_splice_trays", "closure_id"),
        ("splice_count", "fiber_splices", "closure_id"),
    )
    for label, table_name, foreign_key in relationships:
        table = tables[table_name]
        rows = connection.execute(
            select(table.c[foreign_key], func.count())
            .where(table.c[foreign_key].is_not(None))
            .group_by(table.c[foreign_key])
        )
        result[label] = {str(row[0]): int(row[1]) for row in rows}
    return result


def _properties(
    spec: CrmMapProfileSpec,
    row: dict[str, object],
    related: dict[str, dict[str, int]],
) -> tuple[tuple[str, str | None], ...]:
    source_id = str(row["id"])
    values: dict[str, str | None] = {
        "crm_id": source_id,
        "id": source_id,
        "type": spec.asset_type,
    }
    for column in spec.property_columns:
        key = "ap_type" if column == "access_point_type" else column
        values[key] = _json_value(row.get(column))
    if spec.profile is CrmMapProfileName.FDH_CABINETS:
        values["splitter_count"] = str(related["splitter_count"].get(source_id, 0))
    if spec.profile is CrmMapProfileName.SPLICE_CLOSURES:
        values["tray_count"] = str(related["tray_count"].get(source_id, 0))
        values["splice_count"] = str(related["splice_count"].get(source_id, 0))
    if spec.profile is CrmMapProfileName.FIBER_SEGMENTS:
        values["source_evidence_schema_version"] = SEGMENT_EVIDENCE_SCHEMA_VERSION
        for key in (
            "from_point_id",
            "to_point_id",
            "from_endpoint_type",
            "to_endpoint_type",
            "from_endpoint_ref_id",
            "to_endpoint_ref_id",
            "from_endpoint_name",
            "to_endpoint_name",
            "route_srid",
            "created_at",
            "updated_at",
        ):
            values[key] = _json_value(row.get(key))
    return tuple(sorted(values.items()))


def _termination_lookup(
    connection: Connection, tables: dict[str, Table]
) -> dict[str, dict[str, object]]:
    table = tables["fiber_termination_points"]
    rows = connection.execute(
        select(
            table.c.id,
            table.c.endpoint_type,
            table.c.ref_id,
            table.c.name,
            table.c.is_active,
        )
    ).mappings()
    return {str(row["id"]): dict(row) for row in rows}


def _segment_endpoint_blockers(
    row: dict[str, object], terminations: dict[str, dict[str, object]]
) -> list[str]:
    blockers: list[str] = []
    from_point_id = _json_value(row.get("from_point_id"))
    to_point_id = _json_value(row.get("to_point_id"))
    if not from_point_id:
        blockers.append("missing_from_point_id")
    if not to_point_id:
        blockers.append("missing_to_point_id")
    if from_point_id and to_point_id and from_point_id == to_point_id:
        blockers.append("same_from_to_point_id")
    for prefix, point_id in (("from", from_point_id), ("to", to_point_id)):
        if not point_id:
            continue
        termination = terminations.get(point_id)
        if termination is None:
            blockers.append(f"{prefix}_point_orphaned")
            continue
        if not bool(termination.get("is_active")):
            blockers.append(f"{prefix}_point_inactive")
        endpoint_type = _json_value(termination.get("endpoint_type"))
        endpoint_ref_id = _json_value(termination.get("ref_id"))
        if not endpoint_type:
            blockers.append(f"{prefix}_endpoint_type_missing")
        if not endpoint_ref_id:
            blockers.append(f"{prefix}_endpoint_ref_id_missing")
        row[f"{prefix}_endpoint_type"] = endpoint_type
        row[f"{prefix}_endpoint_ref_id"] = endpoint_ref_id
        row[f"{prefix}_endpoint_name"] = _json_value(termination.get("name"))
    try:
        fiber_count = int(str(row.get("fiber_count") or ""))
    except (TypeError, ValueError):
        blockers.append("missing_or_invalid_fiber_count")
    else:
        if fiber_count <= 0:
            blockers.append("missing_or_invalid_fiber_count")
    return blockers


def _extract_profile(
    connection: Connection,
    tables: dict[str, Table],
    spec: CrmMapProfileSpec,
    related: dict[str, dict[str, int]],
    terminations: dict[str, dict[str, object]],
) -> CrmMapProfileExtraction:
    table = tables[spec.table_name]
    selected: list[ColumnElement[Any]] = [
        table.c[column] for column in sorted(_required_columns(spec))
    ]
    if spec.geometry_column:
        selected.append(
            func.ST_AsGeoJSON(table.c[spec.geometry_column], 7).label(
                "_geometry_geojson"
            )
        )
        selected.append(func.ST_SRID(table.c[spec.geometry_column]).label("route_srid"))
    rows = list(connection.execute(select(*selected).order_by(table.c.id)).mappings())
    inactive_count = sum(1 for row in rows if not bool(row["is_active"]))
    features: list[CrmMapSourceFeature] = []
    blockers: list[CrmMapSourceBlocker] = []
    for source_row in rows:
        row = dict(source_row)
        if not bool(row["is_active"]):
            continue
        source_id = str(row["id"])
        if spec.geometry_column:
            line_payload = _line_geometry_payload(row.get("_geometry_geojson"))
            if line_payload is None:
                blockers.append(
                    CrmMapSourceBlocker(
                        profile=spec.profile,
                        source_id=source_id,
                        code="invalid_or_missing_linestring",
                    )
                )
                continue
            geometry_type, line = line_payload
            if geometry_type != spec.geometry_type:
                blockers.append(
                    CrmMapSourceBlocker(
                        profile=spec.profile,
                        source_id=source_id,
                        code="unsupported_geometry_type",
                    )
                )
                continue
            endpoint_blockers = _segment_endpoint_blockers(row, terminations)
            if endpoint_blockers:
                blockers.extend(
                    CrmMapSourceBlocker(
                        profile=spec.profile,
                        source_id=source_id,
                        code=code,
                    )
                    for code in endpoint_blockers
                )
                continue
            coordinates: tuple[float, float] | tuple[tuple[float, float], ...] = (
                tuple(line)  # type: ignore[arg-type]
            )
        else:
            longitude = _finite_coordinate(row.get("longitude"))
            latitude = _finite_coordinate(row.get("latitude"))
            if longitude is None or latitude is None:
                blockers.append(
                    CrmMapSourceBlocker(
                        profile=spec.profile,
                        source_id=source_id,
                        code="missing_point_coordinates",
                    )
                )
                continue
            if not _valid_nigeria_coordinate(longitude, latitude):
                blockers.append(
                    CrmMapSourceBlocker(
                        profile=spec.profile,
                        source_id=source_id,
                        code="coordinate_outside_nigeria",
                    )
                )
                continue
            coordinates = (longitude, latitude)
        features.append(
            CrmMapSourceFeature(
                source_id=source_id,
                display_name=_json_value(row.get("name")),
                geometry_type=spec.geometry_type,
                coordinates=coordinates,
                properties=_properties(spec, row, related),
            )
        )
    return CrmMapProfileExtraction(
        profile=spec.profile,
        source_table=spec.table_name,
        source_count=len(rows),
        inactive_count=inactive_count,
        features=tuple(features),
        blockers=tuple(blockers),
    )


def extract_crm_network_map(
    engine: Engine, *, archive_sha256: str
) -> CrmNetworkMapExtraction:
    """Read one isolated restored archive with a deterministic snapshot."""

    digest = validate_sha256(archive_sha256)
    database_name = str(engine.url.database or "")
    if not any(token in database_name.casefold() for token in ("test", "restore")):
        raise _error(
            "unsafe_source_database",
            "CRM map extraction requires an isolated test/restore database.",
            database_name=database_name,
        )
    with engine.connect().execution_options(
        isolation_level="REPEATABLE READ",
        postgresql_readonly=True,
    ) as connection:
        tables = _validated_tables(connection)
        related = _related_counts(connection, tables)
        terminations = _termination_lookup(connection, tables)
        profiles = tuple(
            _extract_profile(connection, tables, spec, related, terminations)
            for spec in PROFILE_SPECS
        )
        olt_devices = tables["olt_devices"]
        unsupported_active_olt_count = int(
            connection.scalar(
                select(func.count())
                .select_from(olt_devices)
                .where(olt_devices.c.is_active.is_(True))
            )
            or 0
        )
        dependency_table_counts = tuple(
            (
                name,
                int(
                    connection.scalar(select(func.count()).select_from(tables[name]))
                    or 0
                ),
            )
            for name in (
                "splitters",
                "fiber_splice_trays",
                "fiber_splices",
                "fiber_strands",
                "fiber_termination_points",
                "olt_shelves",
                "olt_cards",
                "olt_card_ports",
                "olt_power_units",
                "pon_ports",
            )
        )
    return CrmNetworkMapExtraction(
        archive_sha256=digest,
        database_name=database_name,
        profiles=profiles,
        unsupported_active_olt_count=unsupported_active_olt_count,
        dependency_table_counts=dependency_table_counts,
    )


def feature_batches(
    extraction: CrmMapProfileExtraction, *, batch_size: int
) -> tuple[tuple[CrmMapSourceFeature, ...], ...]:
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise _error(
            "invalid_batch_size",
            f"Batch size must be between 1 and {MAX_BATCH_SIZE}.",
            batch_size=batch_size,
        )
    return tuple(
        extraction.features[index : index + batch_size]
        for index in range(0, len(extraction.features), batch_size)
    )


def _coordinate_text(feature: CrmMapSourceFeature) -> str:
    if feature.geometry_type == "Point":
        longitude, latitude = cast(tuple[float, float], feature.coordinates)
        return f"{longitude:.7f},{latitude:.7f}"
    return " ".join(
        f"{longitude:.7f},{latitude:.7f}"
        for longitude, latitude in cast(
            tuple[tuple[float, float], ...], feature.coordinates
        )
    )


def build_kml(
    features: tuple[CrmMapSourceFeature, ...],
    *,
    archive_sha256: str | None = None,
) -> bytes:
    """Serialize normalized features without changing their source meaning."""

    ET.register_namespace("", KML_NAMESPACE)
    root = ET.Element(f"{{{KML_NAMESPACE}}}kml")
    document = ET.SubElement(root, f"{{{KML_NAMESPACE}}}Document")
    for feature in features:
        placemark = ET.SubElement(document, f"{{{KML_NAMESPACE}}}Placemark")
        name = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}name")
        name.text = feature.display_name or feature.source_id
        extended = ET.SubElement(placemark, f"{{{KML_NAMESPACE}}}ExtendedData")
        schema_data = ET.SubElement(extended, f"{{{KML_NAMESPACE}}}SchemaData")
        properties = dict(feature.properties)
        if archive_sha256 and properties.get("type") == "fiber_segment":
            properties["source_archive_sha256"] = validate_sha256(archive_sha256)
        for key, value in sorted(properties.items()):
            element = ET.SubElement(
                schema_data,
                f"{{{KML_NAMESPACE}}}SimpleData",
                {"name": key},
            )
            element.text = value
        geometry = ET.SubElement(
            placemark, f"{{{KML_NAMESPACE}}}{feature.geometry_type}"
        )
        coordinates = ET.SubElement(geometry, f"{{{KML_NAMESPACE}}}coordinates")
        coordinates.text = _coordinate_text(feature)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def kml_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "CrmMapProfileExtraction",
    "CrmMapProfileName",
    "CrmMapSourceBlocker",
    "CrmMapSourceFeature",
    "CrmNetworkMapExtraction",
    "CrmNetworkMapSourceError",
    "SEGMENT_EVIDENCE_SCHEMA_VERSION",
    "MAX_BATCH_SIZE",
    "SOURCE_SYSTEM",
    "build_kml",
    "extract_crm_network_map",
    "feature_batches",
    "kml_sha256",
    "validate_sha256",
]
