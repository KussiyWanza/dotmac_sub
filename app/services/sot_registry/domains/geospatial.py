"""Canonical SOT declarations for the geospatial domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="geospatial",
    services=(
        SOTService(
            name="gis.geocoding",
            module="app.services.geocoding",
            owns=(
                "address and coordinate resolution",
                "geocode lookup and result caching",
            ),
        ),
        SOTService(
            name="gis.spatial_sync",
            module="app.services.gis_sync",
            owns=(
                "GIS/spatial data synchronization",
                "spatial feature import and projection",
            ),
        ),
    ),
    entrypoints=(
        "app.api.geocoding",
        "app.api.gis",
        "app.tasks.gis",
        "app.services.web_system_geocode_tool",
        "app.services.web_gis",
    ),
    rule="Address/coordinate resolution and spatial data synchronization "
    "resolve through these owners. API, web, and task callers request a "
    "geocode or a sync outcome; they do not embed their own geocode "
    "lookups or spatial write logic.",
)
