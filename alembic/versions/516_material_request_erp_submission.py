"""Prepare contextual material requests and ERP catalogue projection.

Revision ID: 516_material_request_erp_submission
Revises: 515_project_template_vendor_assignment_scope
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "516_material_request_erp_submission"
down_revision = "515_project_template_vendor_assignment_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "field_inventory_items",
        sa.Column("source_system", sa.String(40), nullable=True),
    )
    op.add_column("field_inventory_items", sa.Column("source_item_id", sa.String(80)))
    op.add_column("field_inventory_items", sa.Column("description", sa.Text()))
    op.add_column("field_inventory_items", sa.Column("category_code", sa.String(120)))
    op.add_column("field_inventory_items", sa.Column("category_name", sa.String(160)))
    op.add_column(
        "field_inventory_items",
        sa.Column(
            "source_is_active", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
    )
    op.add_column(
        "field_inventory_items",
        sa.Column(
            "field_request_eligible",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "field_inventory_items",
        sa.Column(
            "track_serial_numbers",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "field_inventory_items",
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "field_inventory_items", sa.Column("last_synced_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "field_inventory_items", sa.Column("source_payload_hash", sa.String(64))
    )
    op.execute(
        "UPDATE field_inventory_items SET source_system = 'legacy_crm', source_item_id = COALESCE(crm_item_id, id::text)"
    )
    op.alter_column("field_inventory_items", "source_system", nullable=False)
    op.create_unique_constraint(
        "uq_field_inventory_source_item",
        "field_inventory_items",
        ["source_system", "source_item_id"],
    )

    op.create_table(
        "field_inventory_warehouses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_system", sa.String(40), nullable=False),
        sa.Column("source_warehouse_id", sa.String(80), nullable=False),
        sa.Column("code", sa.String(100), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("source_is_active", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_payload_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_system", "source_warehouse_id", name="uq_field_warehouse_source"
        ),
        sa.UniqueConstraint(
            "source_system", "code", name="uq_field_warehouse_source_code"
        ),
    )
    op.create_index(
        "ix_field_inventory_warehouses_name", "field_inventory_warehouses", ["name"]
    )

    for name, target in (
        ("ticket_id", "support_tickets.id"),
        ("project_id", "projects.id"),
        ("project_task_id", "project_tasks.id"),
    ):
        op.add_column(
            "field_material_requests", sa.Column(name, postgresql.UUID(as_uuid=True))
        )
        op.create_foreign_key(
            f"fk_field_material_requests_{name}",
            "field_material_requests",
            target.split(".")[0],
            [name],
            ["id"],
            ondelete="RESTRICT",
        )
    op.add_column(
        "field_material_requests",
        sa.Column(
            "fulfillment_channel",
            sa.String(20),
            server_default="manual",
            nullable=False,
        ),
    )
    op.add_column(
        "field_material_requests", sa.Column("required_by", sa.DateTime(timezone=True))
    )
    op.add_column(
        "field_material_requests",
        sa.Column("sent_to_erp_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "field_material_requests", sa.Column("issued_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "field_material_requests",
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True)),
    )
    op.alter_column("field_material_requests", "work_order_mirror_id", nullable=True)
    op.alter_column(
        "field_material_requests", "requested_by_technician_id", nullable=True
    )
    op.drop_constraint(
        "ck_field_material_requests_status", "field_material_requests", type_="check"
    )
    op.create_check_constraint(
        "ck_field_material_requests_status",
        "field_material_requests",
        "status IN ('draft','submitted','approved','rejected','issued','fulfilled','canceled','accepted_by_erp','pending_stock','sync_failed')",
    )
    op.create_check_constraint(
        "ck_field_material_requests_fulfillment_channel",
        "field_material_requests",
        "fulfillment_channel IN ('manual','erp')",
    )
    op.create_check_constraint(
        "ck_field_material_requests_has_context",
        "field_material_requests",
        "ticket_id IS NOT NULL OR project_id IS NOT NULL OR project_task_id IS NOT NULL OR work_order_mirror_id IS NOT NULL",
    )

    for name, type_ in (
        ("source_item_id_snapshot", sa.String(80)),
        ("sku_snapshot", sa.String(80)),
        ("name_snapshot", sa.String(160)),
        ("unit_snapshot", sa.String(40)),
    ):
        op.add_column("field_material_request_items", sa.Column(name, type_))
    op.execute(
        """UPDATE field_material_request_items AS line SET source_item_id_snapshot=item.source_item_id, sku_snapshot=item.sku, name_snapshot=item.name, unit_snapshot=item.unit FROM field_inventory_items AS item WHERE item.id=line.item_id"""
    )


def downgrade() -> None:
    op.drop_column("field_material_request_items", "unit_snapshot")
    op.drop_column("field_material_request_items", "name_snapshot")
    op.drop_column("field_material_request_items", "sku_snapshot")
    op.drop_column("field_material_request_items", "source_item_id_snapshot")

    op.drop_constraint(
        "ck_field_material_requests_has_context",
        "field_material_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_field_material_requests_fulfillment_channel",
        "field_material_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_field_material_requests_status", "field_material_requests", type_="check"
    )
    op.create_check_constraint(
        "ck_field_material_requests_status",
        "field_material_requests",
        "status IN ('draft','submitted','approved','rejected','issued','fulfilled','canceled')",
    )
    op.alter_column(
        "field_material_requests", "requested_by_technician_id", nullable=False
    )
    op.alter_column("field_material_requests", "work_order_mirror_id", nullable=False)
    op.drop_column("field_material_requests", "last_reconciled_at")
    op.drop_column("field_material_requests", "issued_at")
    op.drop_column("field_material_requests", "sent_to_erp_at")
    op.drop_column("field_material_requests", "required_by")
    op.drop_column("field_material_requests", "fulfillment_channel")
    for name in ("project_task_id", "project_id", "ticket_id"):
        op.drop_constraint(
            f"fk_field_material_requests_{name}",
            "field_material_requests",
            type_="foreignkey",
        )
        op.drop_column("field_material_requests", name)

    op.drop_index(
        "ix_field_inventory_warehouses_name",
        table_name="field_inventory_warehouses",
    )
    op.drop_table("field_inventory_warehouses")
    op.drop_constraint(
        "uq_field_inventory_source_item", "field_inventory_items", type_="unique"
    )
    for name in (
        "source_payload_hash",
        "last_synced_at",
        "source_updated_at",
        "track_serial_numbers",
        "field_request_eligible",
        "source_is_active",
        "category_name",
        "category_code",
        "description",
        "source_item_id",
        "source_system",
    ):
        op.drop_column("field_inventory_items", name)
