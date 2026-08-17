from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine

from app.services.network.crm_network_map_source import (
    CrmMapProfileExtraction,
    CrmMapProfileName,
    CrmMapSourceFeature,
    CrmNetworkMapSourceError,
    build_kml,
    extract_crm_network_map,
    feature_batches,
    validate_sha256,
)
from app.services.network import crm_network_map_source
from app.services.network.fiber_topology_staging import preview_fiber_source


def _feature(source_id: str, name: str) -> CrmMapSourceFeature:
    return CrmMapSourceFeature(
        source_id=source_id,
        display_name=name,
        geometry_type="Point",
        coordinates=(7.1, 9.1),
        properties=(
            ("crm_id", source_id),
            ("id", source_id),
            ("name", name),
            ("type", "fdh_cabinet"),
        ),
    )


def test_crm_kml_is_deterministic_and_uses_crm_source_system(db_session, tmp_path):
    features = (_feature("crm-1", "CRM Cabinet"),)
    first = build_kml(features)
    second = build_kml(features)
    path = tmp_path / "crm_fdh_cabinets.kml"
    path.write_bytes(first)

    preview = preview_fiber_source(db_session, path, "crm_fdh_cabinets")

    assert first == second
    assert hashlib.sha256(first).hexdigest() == preview.file_sha256
    assert preview.source_system == "dotmac_crm_fiber_map"
    assert preview.feature_count == 1
    assert preview.blocker_count == 0
    assert preview.features[0].feature.external_id == "crm-1"


def test_crm_feature_batches_are_bounded_and_stable():
    extraction = CrmMapProfileExtraction(
        profile=CrmMapProfileName.FDH_CABINETS,
        source_table="fdh_cabinets",
        source_count=3,
        inactive_count=0,
        features=(
            _feature("crm-1", "One"),
            _feature("crm-2", "Two"),
            _feature("crm-3", "Three"),
        ),
        blockers=(),
    )

    batches = feature_batches(extraction, batch_size=2)

    assert [[feature.source_id for feature in batch] for batch in batches] == [
        ["crm-1", "crm-2"],
        ["crm-3"],
    ]


def test_crm_source_digest_and_batch_size_fail_closed():
    with pytest.raises(CrmNetworkMapSourceError) as digest_error:
        validate_sha256("not-a-digest")
    assert digest_error.value.code == "network.crm_map_source.invalid_archive_digest"

    extraction = CrmMapProfileExtraction(
        profile=CrmMapProfileName.FDH_CABINETS,
        source_table="fdh_cabinets",
        source_count=1,
        inactive_count=0,
        features=(_feature("crm-1", "One"),),
        blockers=(),
    )
    with pytest.raises(CrmNetworkMapSourceError) as batch_error:
        feature_batches(extraction, batch_size=101)
    assert batch_error.value.code == "network.crm_map_source.invalid_batch_size"


def test_crm_source_refuses_a_nonisolated_database_before_connecting():
    engine = create_engine(
        "postgresql+psycopg://invalid:invalid@127.0.0.1:9/dotmac_omni"
    )
    try:
        with pytest.raises(CrmNetworkMapSourceError) as error:
            extract_crm_network_map(engine, archive_sha256="a" * 64)
    finally:
        engine.dispose()

    assert error.value.code == "network.crm_map_source.unsafe_source_database"


def test_segment_endpoint_evidence_is_preserved_in_source_properties():
    row = {
        "created_at": "2026-08-17T10:00:00+00:00",
        "fiber_count": 12,
        "from_point_id": "11111111-1111-1111-1111-111111111111",
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "is_active": True,
        "name": "Cable A",
        "route_srid": 4326,
        "to_point_id": "22222222-2222-2222-2222-222222222222",
        "updated_at": "2026-08-17T10:01:00+00:00",
    }

    blockers = crm_network_map_source._segment_endpoint_blockers(
        row,
        {
            "11111111-1111-1111-1111-111111111111": {
                "endpoint_type": "fdh",
                "is_active": True,
                "name": "FDH A",
                "ref_id": "33333333-3333-3333-3333-333333333333",
            },
            "22222222-2222-2222-2222-222222222222": {
                "endpoint_type": "splice_closure",
                "is_active": True,
                "name": "Closure B",
                "ref_id": "44444444-4444-4444-4444-444444444444",
            },
        },
    )

    assert blockers == []
    properties = dict(
        crm_network_map_source._properties(
            next(
                spec
                for spec in crm_network_map_source.PROFILE_SPECS
                if spec.profile is CrmMapProfileName.FIBER_SEGMENTS
            ),
            row,
            {"splitter_count": {}, "tray_count": {}, "splice_count": {}},
        )
    )
    assert properties["from_endpoint_type"] == "fdh"
    assert properties["from_endpoint_ref_id"] == "33333333-3333-3333-3333-333333333333"
    assert properties["to_endpoint_type"] == "splice_closure"
    assert properties["to_endpoint_ref_id"] == "44444444-4444-4444-4444-444444444444"
    assert properties["fiber_count"] == "12"
    assert properties["route_srid"] == "4326"
    assert (
        properties["source_evidence_schema_version"]
        == crm_network_map_source.SEGMENT_EVIDENCE_SCHEMA_VERSION
    )


def test_segment_endpoint_and_capacity_changes_alter_kml_hash():
    first = CrmMapSourceFeature(
        source_id="seg-1",
        display_name="Cable A",
        geometry_type="LineString",
        coordinates=((7.1, 9.1), (7.2, 9.2)),
        properties=(
            ("crm_id", "seg-1"),
            ("fiber_count", "12"),
            ("from_endpoint_ref_id", "fdh-1"),
            ("from_endpoint_type", "fdh"),
            ("to_endpoint_ref_id", "closure-1"),
            ("to_endpoint_type", "splice_closure"),
            ("type", "fiber_segment"),
        ),
    )
    changed_capacity = CrmMapSourceFeature(
        source_id="seg-1",
        display_name="Cable A",
        geometry_type="LineString",
        coordinates=((7.1, 9.1), (7.2, 9.2)),
        properties=(
            ("crm_id", "seg-1"),
            ("fiber_count", "24"),
            ("from_endpoint_ref_id", "fdh-1"),
            ("from_endpoint_type", "fdh"),
            ("to_endpoint_ref_id", "closure-1"),
            ("to_endpoint_type", "splice_closure"),
            ("type", "fiber_segment"),
        ),
    )

    assert hashlib.sha256(build_kml((first,))).hexdigest() != hashlib.sha256(
        build_kml((changed_capacity,))
    ).hexdigest()
    assert hashlib.sha256(
        build_kml((first,), archive_sha256="a" * 64)
    ).hexdigest() != hashlib.sha256(
        build_kml((first,), archive_sha256="b" * 64)
    ).hexdigest()


def test_segment_endpoint_blockers_fail_closed():
    row = {
        "fiber_count": 0,
        "from_point_id": "11111111-1111-1111-1111-111111111111",
        "to_point_id": "11111111-1111-1111-1111-111111111111",
    }

    blockers = crm_network_map_source._segment_endpoint_blockers(row, {})

    assert "same_from_to_point_id" in blockers
    assert "from_point_orphaned" in blockers
    assert "to_point_orphaned" in blockers
    assert "missing_or_invalid_fiber_count" in blockers
