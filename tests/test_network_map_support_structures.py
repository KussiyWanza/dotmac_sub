"""Network-map fiber support-structure canvas layer."""

from app.services.network_map import build_network_map_projection
from app.services.network_map_contracts import NetworkMapFeatureType


def test_map_context_includes_support_structures_stat(db_session):
    projection = build_network_map_projection(db=db_session)
    # the fiber support-structure layer contributes a stat + a feature type
    assert projection.stats.support_structures == 0
    types = {feature.properties.feature_type for feature in projection.features}
    assert NetworkMapFeatureType.support_structure not in types  # none seeded


def test_map_context_stats_has_all_fiber_layers(db_session):
    stats = build_network_map_projection(db=db_session).stats
    assert stats.fdh_cabinets == 0
    assert stats.splice_closures == 0
    assert stats.access_points == 0
    assert stats.support_structures == 0
