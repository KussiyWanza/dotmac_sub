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
