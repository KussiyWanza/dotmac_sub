from __future__ import annotations

from uuid import uuid4

from app.models.network import FiberSegment, FiberSegmentType, FiberTerminationPoint
from app.services.network_map import build_network_map_plant_projection
from app.services.network_map_contracts import (
    NetworkMapFeatureType,
    NetworkMapLineGeometry,
)


def test_dispatch_plant_segment_uses_migrated_postgis_route_geometry(db_session):
    suffix = uuid4().hex[:12]
    start = FiberTerminationPoint(
        name=f"Map parity start {suffix}",
        latitude=9.08,
        longitude=7.40,
        is_active=True,
    )
    end = FiberTerminationPoint(
        name=f"Map parity end {suffix}",
        latitude=9.11,
        longitude=7.43,
        is_active=True,
    )
    db_session.add_all([start, end])
    db_session.flush()
    segment = FiberSegment(
        name=f"Map parity feeder {suffix}",
        segment_type=FiberSegmentType.feeder,
        from_point_id=start.id,
        to_point_id=end.id,
        route_geom="LINESTRING(7.40 9.08, 7.43 9.11)",
        fiber_count=96,
        is_active=True,
    )
    db_session.add(segment)
    db_session.flush()

    projection = build_network_map_plant_projection(db=db_session)

    feature = next(
        item
        for item in projection.features
        if item.properties.feature_type is NetworkMapFeatureType.fiber_segment
        and item.properties.id == segment.id
    )
    assert isinstance(feature.geometry, NetworkMapLineGeometry)
    assert feature.geometry.coordinates == ((7.4, 9.08), (7.43, 9.11))
    assert feature.properties.segment_type is FiberSegmentType.feeder
    assert feature.properties.fiber_count == 96
