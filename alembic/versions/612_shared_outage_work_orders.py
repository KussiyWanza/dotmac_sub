"""Add shared-outage work-order kind and provenance.

Customer work orders keep their subscriber. Infrastructure work orders use the
same dispatch lifecycle but are linked to an outage scope revision instead of
one customer.

Revision ID: 612_shared_outage_work_orders
Revises: 611_offer_versions_unique_version_number
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "612_shared_outage_work_orders"
down_revision: str | None = "611_offer_versions_unique_version_number"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_order",
        sa.Column("work_order_kind", sa.String(length=20), nullable=True),
    )
    op.execute(
        "UPDATE work_order SET work_order_kind = 'customer' "
        "WHERE work_order_kind IS NULL"
    )
    op.alter_column(
        "work_order",
        "work_order_kind",
        existing_type=sa.String(length=20),
        nullable=False,
    )
    op.alter_column(
        "work_order",
        "subscriber_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_work_order_kind_subscriber",
        "work_order",
        "(work_order_kind = 'customer' AND subscriber_id IS NOT NULL) "
        "OR (work_order_kind = 'infrastructure' AND subscriber_id IS NULL)",
    )
    op.create_table(
        "outage_incident_work_order_links",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_revision_sequence", sa.Integer(), nullable=False),
        sa.Column("membership_token", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=20), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["incident_id"], ["outage_incidents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["work_order_id"], ["work_order.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["scope_revision_id"], ["outage_scope_revisions.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "incident_id",
            "work_order_id",
            name="uq_outage_incident_work_order_links_pair",
        ),
        sa.UniqueConstraint(
            "incident_id",
            "idempotency_key",
            name="uq_outage_incident_work_order_links_idempotency",
        ),
    )
    op.create_index(
        "ix_outage_incident_work_order_links_incident",
        "outage_incident_work_order_links",
        ["incident_id"],
    )
    op.create_index(
        "ix_outage_incident_work_order_links_work_order",
        "outage_incident_work_order_links",
        ["work_order_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM outage_incident_work_order_links LIMIT 1")
    ).first():
        raise RuntimeError("Cannot downgrade while shared-outage work orders exist")
    op.drop_index(
        "ix_outage_incident_work_order_links_work_order",
        table_name="outage_incident_work_order_links",
    )
    op.drop_index(
        "ix_outage_incident_work_order_links_incident",
        table_name="outage_incident_work_order_links",
    )
    op.drop_table("outage_incident_work_order_links")
    op.drop_constraint("ck_work_order_kind_subscriber", "work_order", type_="check")
    op.alter_column(
        "work_order",
        "subscriber_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column("work_order", "work_order_kind")
