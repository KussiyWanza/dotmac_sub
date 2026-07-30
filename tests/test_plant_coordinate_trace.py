"""Field plant moves must be traceable, and POP projections spatially findable.

Two defects with the same shape: the data was written correctly but into a key
or column the reading surface does not use, so it existed and was unreachable.
"""

from __future__ import annotations

from uuid import uuid4

from app.models.audit import AuditEvent
from app.models.gis import GeoLocation
from app.models.network import FdhCabinet
from app.models.network_monitoring import PopSite
from app.models.subscriber import UserType
from app.models.system_user import SystemUser
from app.services.field.map_assets import field_map_assets
from app.services.gis_sync import GeoSync


def _user(db_session) -> SystemUser:
    user = SystemUser(
        first_name="Field",
        last_name="Tech",
        display_name="Field Tech",
        email=f"trace-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def test_a_field_move_is_recorded_under_the_key_detail_pages_query(db_session):
    """Plant detail pages filter activity on the snake_case asset key.

    Writing the ORM class name instead left every activity feed empty, so an
    operator could not answer "who moved this cabinet and when" from the UI.
    """

    user = _user(db_session)
    cabinet = FdhCabinet(
        name="FDH Trace",
        code=f"FDH-{uuid4().hex[:6]}",
        latitude=9.071,
        longitude=7.451,
        is_active=True,
    )
    db_session.add(cabinet)
    db_session.flush()

    field_map_assets.update_location(
        db_session,
        asset_type="fdh_cabinet",
        asset_id=str(cabinet.id),
        latitude=9.081,
        longitude=7.462,
        actor_id=str(user.id),
        source="gps",
    )

    event = (
        db_session.query(AuditEvent)
        .filter(AuditEvent.action == "field:map_asset:update_location")
        .one()
    )
    assert event.entity_type == "fdh_cabinet"
    assert event.entity_id == str(cabinet.id)


def test_a_projected_pop_carries_geometry_so_spatial_reads_find_it(db_session):
    """Nearby and in-area queries filter geom IS NOT NULL.

    A projection with only latitude and longitude is invisible to them, which
    is why POPs never appeared in coverage or radius results.
    """

    pop = PopSite(
        name="Garki POP",
        code=f"POP-{uuid4().hex[:6]}",
        latitude=9.0579,
        longitude=7.4951,
        is_active=True,
    )
    db_session.add(pop)
    db_session.flush()

    GeoSync.sync_pop_sites(db_session)
    db_session.flush()

    projected = (
        db_session.query(GeoLocation).filter(GeoLocation.pop_site_id == pop.id).one()
    )
    assert projected.geom is not None

    # A later move of the POP refreshes the projected geometry rather than
    # leaving a stale point behind.
    pop.latitude = 9.0600
    pop.longitude = 7.5000
    db_session.flush()
    GeoSync.sync_pop_sites(db_session)
    db_session.flush()

    refreshed = (
        db_session.query(GeoLocation).filter(GeoLocation.pop_site_id == pop.id).one()
    )
    assert refreshed.latitude == 9.0600
    assert refreshed.geom is not None
