from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from app.models.fiber_topology_staging import (
    FiberTopologySourceBatch,
    FiberTopologyStagedFeature,
)
from app.models.network import FdhCabinet, FiberAccessPoint
from app.services.network.fiber_topology_staging import (
    SOURCE_PROFILES,
    preview_fiber_source,
    stage_fiber_preview_batch,
    stage_fiber_source,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _geometry_xml(geometry_type: str, coordinates: str) -> str:
    if geometry_type == "Polygon":
        return (
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            f"{coordinates}"
            "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        )
    return (
        f"<{geometry_type}><coordinates>{coordinates}</coordinates></{geometry_type}>"
    )


def _placemark(
    *,
    name: str,
    properties: dict[str, str],
    geometry_type: str,
    coordinates: str,
) -> str:
    simple_data = "".join(
        f'<SimpleData name="{escape(key)}">{escape(value)}</SimpleData>'
        for key, value in properties.items()
    )
    return (
        "<Placemark>"
        f"<name>{escape(name)}</name>"
        "<ExtendedData><SchemaData>"
        f"{simple_data}"
        "</SchemaData></ExtendedData>"
        f"{_geometry_xml(geometry_type, coordinates)}"
        "</Placemark>"
    )


def _write_kmz(
    tmp_path: Path,
    filename: str,
    placemarks: list[str],
    *,
    extra_archive_entry: bool = False,
) -> Path:
    path = tmp_path / filename
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"{''.join(placemarks)}"
        "</Document></kml>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", kml)
        if extra_archive_entry:
            archive.writestr("files/source-note.txt", "same normalized source")
    return path


def _polygon(seed: float) -> str:
    return f"7.{seed:.0f},9.0 7.{seed:.0f},9.1 7.{seed + 1:.0f},9.1 7.{seed:.0f},9.0"


def test_preview_is_deterministic_and_flags_source_collisions(db_session, tmp_path):
    path = _write_kmz(
        tmp_path,
        "cabinets.kmz",
        [
            _placemark(
                name="Cabinet A",
                properties={"fibermngrid": "CAB-1", "name": "Same cabinet"},
                geometry_type="Polygon",
                coordinates=_polygon(1),
            ),
            _placemark(
                name="Cabinet B",
                properties={"fibermngrid": "CAB-2", "name": "Same cabinet"},
                geometry_type="Polygon",
                coordinates=_polygon(1),
            ),
            _placemark(
                name="Missing identity",
                properties={"name": "Missing identity"},
                geometry_type="Polygon",
                coordinates=_polygon(3),
            ),
        ],
    )

    first = preview_fiber_source(db_session, path, "osp_cabinets")
    second = preview_fiber_source(db_session, path, "osp_cabinets")

    assert first.file_sha256 == second.file_sha256
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.status_counts == {"candidate": 2, "blocked": 1}
    assert first.blocker_count == 1
    assert "duplicate_source_name" in first.features[0].match_reasons
    assert "duplicate_source_geometry" in first.features[0].match_reasons
    assert "missing_external_id" in first.features[2].feature.blocker_codes


def test_preview_suggests_external_code_before_normalized_name(db_session, tmp_path):
    canonical = FdhCabinet(
        name="Existing Cabinet",
        code="CAB-1",
        latitude=9.0,
        longitude=7.1,
    )
    db_session.add(canonical)
    db_session.commit()

    path = _write_kmz(
        tmp_path,
        "cabinets.kmz",
        [
            _placemark(
                name="Renamed source cabinet",
                properties={"fibermngrid": "CAB-1", "name": "Renamed cabinet"},
                geometry_type="Polygon",
                coordinates=_polygon(1),
            ),
            _placemark(
                name="Existing Cabinet",
                properties={"fibermngrid": "CAB-2", "name": "Existing Cabinet"},
                geometry_type="Polygon",
                coordinates=_polygon(3),
            ),
        ],
    )

    preview = preview_fiber_source(db_session, path, "osp_cabinets")

    assert preview.features[0].match_status == "exact_external"
    assert preview.features[0].canonical_asset_id == canonical.id
    assert preview.features[1].match_status == "candidate"
    assert preview.features[1].canonical_asset_id == canonical.id
    assert preview.features[1].match_reasons == ("canonical_normalized_name_match",)


def test_stage_is_idempotent_and_never_creates_canonical_assets(db_session, tmp_path):
    path = _write_kmz(
        tmp_path,
        "access-points.kmz",
        [
            _placemark(
                name="FAT-1",
                properties={"access_pointid": "FAT-1", "Name": "FAT-1"},
                geometry_type="Polygon",
                coordinates=_polygon(1),
            )
        ],
    )

    first = stage_fiber_source(
        db_session, path, "osp_access_points", created_by="pytest"
    )
    second = stage_fiber_source(
        db_session, path, "osp_access_points", created_by="pytest-replay"
    )

    assert first.created is True
    assert second.created is False
    assert second.batch_id == first.batch_id
    assert db_session.query(FiberTopologySourceBatch).count() == 1
    assert db_session.query(FiberTopologyStagedFeature).count() == 1
    assert db_session.query(FiberAccessPoint).count() == 0


def test_repackaged_identical_manifest_is_idempotent(db_session, tmp_path):
    feature = _placemark(
        name="FAT-1",
        properties={"access_pointid": "FAT-1", "Name": "FAT-1"},
        geometry_type="Polygon",
        coordinates=_polygon(1),
    )
    first_path = _write_kmz(tmp_path, "first.kmz", [feature])
    repackaged_path = _write_kmz(
        tmp_path,
        "repackaged.kmz",
        [feature],
        extra_archive_entry=True,
    )

    first_preview = preview_fiber_source(db_session, first_path, "osp_access_points")
    repackaged_preview = preview_fiber_source(
        db_session, repackaged_path, "osp_access_points"
    )
    first = stage_fiber_source(
        db_session, first_path, "osp_access_points", created_by="pytest"
    )
    repackaged = stage_fiber_source(
        db_session,
        repackaged_path,
        "osp_access_points",
        created_by="pytest",
    )

    assert first_preview.file_sha256 != repackaged_preview.file_sha256
    assert first_preview.manifest_sha256 == repackaged_preview.manifest_sha256
    assert first.created is True
    assert repackaged.created is False
    assert repackaged.batch_id == first.batch_id


def test_changed_stable_identity_requires_review_and_preserves_lineage(
    db_session, tmp_path
):
    first_path = _write_kmz(
        tmp_path,
        "paths-v1.kmz",
        [
            _placemark(
                name="SPAN-1",
                properties={"spanid": "SPAN-1"},
                geometry_type="LineString",
                coordinates="7.1,9.0 7.2,9.1",
            )
        ],
    )
    first = stage_fiber_source(db_session, first_path, "osp_paths", created_by="pytest")
    first_feature = db_session.query(FiberTopologyStagedFeature).one()

    second_path = _write_kmz(
        tmp_path,
        "paths-v2.kmz",
        [
            _placemark(
                name="SPAN-1",
                properties={"spanid": "SPAN-1"},
                geometry_type="LineString",
                coordinates="7.1,9.0 7.3,9.2",
            )
        ],
    )
    preview = preview_fiber_source(db_session, second_path, "osp_paths")

    assert first.created is True
    assert preview.features[0].match_status == "candidate"
    assert "changed_source_identity" in preview.features[0].match_reasons
    assert preview.features[0].prior_feature_id == first_feature.id

    second = stage_fiber_source(
        db_session, second_path, "osp_paths", created_by="pytest"
    )
    assert second.created is True
    assert db_session.query(FiberTopologySourceBatch).count() == 2
    staged = (
        db_session.query(FiberTopologyStagedFeature)
        .order_by(FiberTopologyStagedFeature.created_at.desc())
        .first()
    )
    assert staged.prior_feature_id == first_feature.id


def test_bounded_stage_preserves_full_source_duplicate_classification(
    db_session, tmp_path
):
    path = _write_kmz(
        tmp_path,
        "crm-cabinets.kmz",
        [
            _placemark(
                name="Shared name",
                properties={"crm_id": "CRM-1", "name": "Shared name"},
                geometry_type="Point",
                coordinates="7.1,9.0",
            ),
            _placemark(
                name="Shared name",
                properties={"crm_id": "CRM-2", "name": "Shared name"},
                geometry_type="Point",
                coordinates="7.2,9.1",
            ),
        ],
    )
    preview = preview_fiber_source(db_session, path, "crm_fdh_cabinets")
    assert preview.status_counts == {"candidate": 2}

    first = stage_fiber_preview_batch(
        db_session,
        preview,
        start=0,
        stop=1,
        source_name="crm_fdh_cabinets-00001.kml",
        created_by="pytest",
        source_metadata={"source_archive_sha256": "a" * 64},
    )
    second = stage_fiber_preview_batch(
        db_session,
        preview,
        start=1,
        stop=2,
        source_name="crm_fdh_cabinets-00002.kml",
        created_by="pytest",
        source_metadata={"source_archive_sha256": "a" * 64},
    )

    assert first.created is True
    assert second.created is True
    assert first.candidate_count == second.candidate_count == 1
    assert {
        feature.match_status
        for feature in db_session.query(FiberTopologyStagedFeature).all()
    } == {"candidate"}
    full_manifest_hashes = {
        batch.source_metadata["full_manifest_sha256"]
        for batch in db_session.query(FiberTopologySourceBatch).all()
    }
    assert len(full_manifest_hashes) == 1
    assert full_manifest_hashes != {preview.manifest_sha256}


def test_crm_bounded_stage_idempotency_is_archive_aware(db_session, tmp_path):
    path = _write_kmz(
        tmp_path,
        "crm-cabinet.kmz",
        [
            _placemark(
                name="FDH 1",
                properties={"crm_id": "CRM-1", "name": "FDH 1"},
                geometry_type="Point",
                coordinates="7.1,9.0",
            ),
        ],
    )
    preview = preview_fiber_source(db_session, path, "crm_fdh_cabinets")
    first_metadata = {
        "importer_version": "stage_crm_network_map:v2",
        "source_archive_sha256": "a" * 64,
    }
    second_metadata = {
        "importer_version": "stage_crm_network_map:v2",
        "source_archive_sha256": "b" * 64,
    }

    first = stage_fiber_preview_batch(
        db_session,
        preview,
        start=0,
        stop=1,
        source_name="crm_fdh_cabinets-00001-a.kml",
        created_by="pytest",
        source_metadata=first_metadata,
    )
    same_archive = stage_fiber_preview_batch(
        db_session,
        preview,
        start=0,
        stop=1,
        source_name="crm_fdh_cabinets-00001-a-rerun.kml",
        created_by="pytest",
        source_metadata=first_metadata,
    )
    new_archive = stage_fiber_preview_batch(
        db_session,
        preview,
        start=0,
        stop=1,
        source_name="crm_fdh_cabinets-00001-b.kml",
        created_by="pytest",
        source_metadata=second_metadata,
    )

    assert first.created is True
    assert same_archive.created is False
    assert same_archive.batch_id == first.batch_id
    assert new_archive.created is True
    assert new_archive.batch_id != first.batch_id

    batches = db_session.query(FiberTopologySourceBatch).all()
    assert len(batches) == 2
    assert {batch.source_metadata["source_archive_sha256"] for batch in batches} == {
        "a" * 64,
        "b" * 64,
    }
    assert db_session.query(FiberTopologyStagedFeature).count() == 2


@pytest.mark.parametrize(
    ("profile_name", "expected_count"),
    [
        ("osp_paths", 1600),
        ("osp_access_points", 286),
        ("osp_cabinets", 113),
        ("osp_splice_info", 1021),
        ("osp_buildings", 1146),
        ("osp_air_fiber", 515),
    ],
)
def test_checked_in_osp_sources_have_stable_ids_and_valid_geometry(
    db_session, profile_name, expected_count
):
    profile = SOURCE_PROFILES[profile_name]
    path = PROJECT_ROOT / "docs" / profile.default_filename

    preview = preview_fiber_source(db_session, path, profile_name)

    assert preview.feature_count == expected_count
    assert preview.blocker_count == 0
    assert all(plan.feature.external_id for plan in preview.features)
