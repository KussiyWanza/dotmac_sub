"""Fiber review decision loads lock under PostgreSQL.

Regression for the production incident where every identity review lifecycle
step failed with ``FOR UPDATE cannot be applied to the nullable side of an
outer join``: ``_load_decision`` combines relationship ``joinedload`` outer
joins with a row lock, so the lock must be qualified to the decision table
(``FOR UPDATE OF``). SQLite ignores row locks entirely, which is why only
PostgreSQL can exercise this.
"""

from __future__ import annotations

import zipfile

from app.models.fiber_topology_staging import FiberTopologyStagedFeature
from app.services.network.fiber_topology_connectivity import (
    approve_connectivity_decision,
    propose_connectivity_decision,
)
from app.services.network.fiber_topology_identity import (
    approve_identity_decision,
    propose_identity_decision,
    validate_identity_decision_for_review,
)
from app.services.network.fiber_topology_staging import stage_fiber_source


def _write_kmz(tmp_path, filename: str, placemark: str):
    path = tmp_path / filename
    kml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"{placemark}"
        "</Document></kml>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", kml)
    return path


def _placemark(name: str, external_key: str, external_id: str, geometry: str) -> str:
    return (
        "<Placemark>"
        f"<name>{name}</name>"
        "<ExtendedData><SchemaData>"
        f'<SimpleData name="{external_key}">{external_id}</SimpleData>'
        "</SchemaData></ExtendedData>"
        f"{geometry}"
        "</Placemark>"
    )


def _staged_feature(db, tmp_path, profile: str, filename: str, placemark: str):
    result = stage_fiber_source(
        db,
        _write_kmz(tmp_path, filename, placemark),
        profile,
        created_by="integration-test",
    )
    return (
        db.query(FiberTopologyStagedFeature)
        .filter(FiberTopologyStagedFeature.batch_id == result.batch_id)
        .one()
    )


def test_identity_decision_review_locks_under_postgres(db_session, tmp_path):
    feature = _staged_feature(
        db_session,
        tmp_path,
        "osp_cabinets",
        "cabinets.kmz",
        _placemark(
            "Lock Cabinet",
            "fibermngrid",
            "LOCK-CAB-1",
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            "7.51,9.02 7.51,9.03 7.52,9.03 7.51,9.02"
            "</coordinates></LinearRing></outerBoundaryIs></Polygon>",
        ),
    )
    decision = propose_identity_decision(
        db_session,
        staged_feature_id=feature.id,
        action="reject",
        proposed_by="integration-proposer",
        reason="row-lock regression coverage",
        commit=False,
    )
    db_session.flush()

    validated = validate_identity_decision_for_review(db_session, decision.id)
    assert validated.id == decision.id

    approved = approve_identity_decision(
        db_session,
        decision.id,
        "integration-reviewer",
        "row-lock regression coverage",
        commit=False,
    )
    assert approved.status == "approved"


def test_connectivity_decision_review_locks_under_postgres(db_session, tmp_path):
    feature = _staged_feature(
        db_session,
        tmp_path,
        "osp_paths",
        "paths.kmz",
        _placemark(
            "Lock Span",
            "spanid",
            "LOCK-SPAN-1",
            "<LineString><coordinates>7.51,9.02 7.53,9.04</coordinates></LineString>",
        ),
    )
    decision = propose_connectivity_decision(
        db_session,
        feature.id,
        "reject",
        proposed_by="integration-proposer",
        reason="row-lock regression coverage",
        commit=False,
    )
    db_session.flush()

    approved = approve_connectivity_decision(
        db_session,
        decision.id,
        reviewed_by="integration-reviewer",
        review_notes="row-lock regression coverage",
        commit=False,
    )
    assert approved.status == "approved"
