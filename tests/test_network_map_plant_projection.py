from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import event

from app.models.gis import ServiceBuilding
from app.models.network import (
    FdhCabinet,
    FiberAccessPoint,
    FiberCableType,
    FiberSegment,
    FiberSegmentType,
    FiberSplice,
    FiberSpliceClosure,
    FiberSpliceTray,
    OLTDevice,
    Splitter,
)
from app.models.network_monitoring import NetworkDevice, PopSite
from app.services import network_map
from app.services.network_map_contracts import NetworkMapFeatureType


def test_dispatch_plant_projection_covers_every_approved_asset_with_crm_metadata(
    db_session, monkeypatch
):
    site = PopSite(
        name="Parity PoP",
        code="POP-PARITY",
        city="Abuja",
        latitude=9.0765,
        longitude=7.3986,
        notes="Core site",
        is_active=True,
    )
    matched_olt = OLTDevice(
        name="Parity OLT",
        vendor="Huawei",
        model="MA5800",
        notes="Rack A",
        is_active=True,
    )
    unmatched_olt = OLTDevice(name="Unmatched OLT", is_active=True)
    db_session.add_all([site, matched_olt, unmatched_olt])
    db_session.flush()
    device = NetworkDevice(
        name="OLT monitoring node",
        pop_site_id=site.id,
        matched_device_type="olt",
        matched_device_id=matched_olt.id,
        live_status="up",
        is_active=True,
    )
    fdh = FdhCabinet(
        name="FDH One",
        code="FDH-1",
        latitude=9.08,
        longitude=7.40,
        notes="Cabinet note",
        is_active=True,
    )
    closure = FiberSpliceClosure(
        name="Closure One",
        latitude=9.09,
        longitude=7.41,
        notes="Closure note",
        is_active=True,
    )
    access_point = FiberAccessPoint(
        name="FAP One",
        code="FAP-1",
        access_point_type="fat",
        placement="pole",
        latitude=9.10,
        longitude=7.42,
        is_active=True,
    )
    building = ServiceBuilding(
        name="Service Building One",
        code="BLDG-1",
        street="42 Test Avenue",
        city="Abuja",
        latitude=9.11,
        longitude=7.43,
        notes="Building note",
        is_active=True,
    )
    db_session.add_all([device, fdh, closure, access_point, building])
    db_session.flush()
    db_session.add_all(
        [
            Splitter(
                fdh_id=fdh.id,
                name="Splitter One",
                splitter_ratio="1:8",
                input_ports=1,
                output_ports=8,
                is_active=True,
            ),
            Splitter(
                fdh_id=fdh.id,
                name="Historical Splitter",
                input_ports=1,
                output_ports=8,
                is_active=False,
            ),
            FiberSpliceTray(closure_id=closure.id, tray_number=1),
            FiberSpliceTray(closure_id=closure.id, tray_number=2),
            FiberSplice(closure_id=closure.id, position=1),
            FiberSplice(closure_id=closure.id, position=2),
            FiberSplice(closure_id=closure.id, position=3),
        ]
    )
    db_session.flush()

    segment = FiberSegment(
        id=uuid4(),
        name="Feeder One",
        segment_type=FiberSegmentType.feeder,
        cable_type=FiberCableType.armored,
        fiber_count=96,
        length_m=1250.0,
        notes="Segment note",
        is_active=True,
    )
    monkeypatch.setattr(
        network_map,
        "_plant_segment_rows",
        lambda db: [
            (
                segment,
                json.dumps(
                    {
                        "type": "LineString",
                        "coordinates": [[7.40, 9.08], [7.43, 9.11]],
                    }
                ),
            )
        ],
    )

    db_session.expunge_all()
    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(db_session.bind, "before_cursor_execute", record_statement)
    try:
        projection = network_map.build_network_map_plant_projection(db=db_session)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", record_statement)

    by_type = {
        feature.properties.feature_type: feature
        for feature in projection.features
        if feature.properties.feature_type
        not in {NetworkMapFeatureType.pop_site, NetworkMapFeatureType.network_device}
    }
    assert {
        NetworkMapFeatureType.olt_device,
        NetworkMapFeatureType.fdh_cabinet,
        NetworkMapFeatureType.splice_closure,
        NetworkMapFeatureType.access_point,
        NetworkMapFeatureType.service_building,
        NetworkMapFeatureType.fiber_segment,
    } <= set(by_type)

    assert by_type[NetworkMapFeatureType.olt_device].properties.notes == "Rack A"
    assert by_type[NetworkMapFeatureType.fdh_cabinet].properties.splitter_count == 2
    assert by_type[NetworkMapFeatureType.fdh_cabinet].properties.notes == "Cabinet note"
    assert by_type[NetworkMapFeatureType.splice_closure].properties.splice_count == 3
    assert by_type[NetworkMapFeatureType.splice_closure].properties.tray_count == 2
    assert (
        by_type[NetworkMapFeatureType.access_point].properties.access_point_type
        == "fat"
    )
    assert by_type[NetworkMapFeatureType.access_point].properties.placement == "pole"
    assert (
        by_type[NetworkMapFeatureType.service_building].properties.street
        == "42 Test Avenue"
    )
    assert by_type[NetworkMapFeatureType.service_building].properties.city == "Abuja"
    assert by_type[NetworkMapFeatureType.fiber_segment].properties.fiber_count == 96
    assert (
        by_type[NetworkMapFeatureType.fiber_segment].properties.notes == "Segment note"
    )
    assert projection.unmatched_olt_count == 1

    assert sum("from fiber_splices" in statement for statement in statements) == 1
    assert sum("from fiber_splice_trays" in statement for statement in statements) == 1
    assert sum("from splitters" in statement for statement in statements) == 1
    assert sum("from olt_devices" in statement for statement in statements) == 2


def test_dispatch_plant_projection_omits_olt_without_authoritative_site_match(
    db_session,
):
    db_session.add(OLTDevice(name="No mapped monitoring identity", is_active=True))
    db_session.flush()

    projection = network_map.build_network_map_plant_projection(db=db_session)

    assert not any(
        feature.properties.feature_type is NetworkMapFeatureType.olt_device
        for feature in projection.features
    )
    assert projection.unmatched_olt_count == 1


def test_active_segment_contract_requires_validated_route_geometry():
    constraints = {constraint.name for constraint in FiberSegment.__table__.constraints}

    assert "ck_fiber_segments_active_operational_shape" in constraints
    contract = next(
        constraint
        for constraint in FiberSegment.__table__.constraints
        if constraint.name == "ck_fiber_segments_active_operational_shape"
    )
    sql = str(contract.sqltext)
    assert "route_geom IS NOT NULL" in sql
    assert "from_point_id IS NOT NULL" in sql
    assert "to_point_id IS NOT NULL" in sql
