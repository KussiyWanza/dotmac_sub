"""Add the typed network-zone -> GeoArea binding.

Zones already anchor every outage-incident location handle (PopSite.zone_id,
FdhCabinet.zone_id, NetworkDevice -> PopSite). Binding a zone to a reviewed
GeoArea makes geo-scoped service-team routing resolvable without geometry
inference. The binding is operator-declared through the network-zone owner
service; nothing is backfilled because no authoritative zone-to-area source
exists yet.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "441_network_zone_geo_area_binding"
down_revision = "440_composable_service_teams"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "network_zones",
        sa.Column("geo_area_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_network_zones_geo_area_id_geo_areas",
        "network_zones",
        "geo_areas",
        ["geo_area_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_network_zones_geo_area_id",
        "network_zones",
        ["geo_area_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_network_zones_geo_area_id", table_name="network_zones")
    op.drop_constraint(
        "fk_network_zones_geo_area_id_geo_areas",
        "network_zones",
        type_="foreignkey",
    )
    op.drop_column("network_zones", "geo_area_id")
