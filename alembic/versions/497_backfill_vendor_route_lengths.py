"""Backfill authoritative geodesic lengths for vendor routes.

Revision ID: 496_vendor_route_lengths
Revises: 495_plan_family_catalogues
"""

from alembic import op

revision: str = "496_vendor_route_lengths"
down_revision: str | None = "495_plan_family_catalogues"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE proposed_route_revisions
        SET length_meters = ST_Length(route_geom::geography)
        WHERE route_geom IS NOT NULL AND length_meters IS NULL
        """
    )
    op.execute(
        """
        UPDATE as_built_routes
        SET actual_length_meters = ST_Length(route_geom::geography)
        WHERE route_geom IS NOT NULL AND actual_length_meters IS NULL
        """
    )


def downgrade() -> None:
    # The backfill repairs missing evidence; restored values must not be erased.
    pass
