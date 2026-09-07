"""Add optional structural project infrastructure targets.

Revision ID: 578_project_infrastructure
Revises: 577_migrated_opening_consumption_source
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "578_project_infrastructure"
down_revision: str | None = "577_migrated_opening_consumption_source"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TARGETS = (
    ("location", "pop_sites"),
    ("nas", "nas_devices"),
    ("access_point", "network_devices"),
    ("base_station", "pop_sites"),
    ("olt", "olt_devices"),
    ("pon_port", "pon_ports"),
    ("cabinet", "fdh_cabinets"),
)


def upgrade() -> None:
    op.create_table(
        "project_infrastructure",
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        *(
            sa.Column(
                f"{kind}_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{table}.id", ondelete="RESTRICT"),
                nullable=True,
            )
            for kind, table in TARGETS
        ),
        sa.CheckConstraint(
            " + ".join(
                f"CASE WHEN {kind}_id IS NOT NULL THEN 1 ELSE 0 END"
                for kind, _ in TARGETS
            )
            + " = 1",
            name="ck_project_infrastructure_one_target",
        ),
    )


def downgrade() -> None:
    # Refuse to discard reviewed scope evidence during an application rollback.
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM project_infrastructure)")
    ):
        raise RuntimeError(
            "Project infrastructure exists; retain the additive schema and roll forward"
        )
    op.drop_table("project_infrastructure")
