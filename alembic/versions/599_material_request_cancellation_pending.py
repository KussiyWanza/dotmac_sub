"""Add cancellation-pending material request state.

Revision ID: 599_material_cancel_pending
Revises: 598_opening_corrections
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "599_material_cancel_pending"
down_revision: str | None = "598_opening_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CURRENT = (
    "status IN ('draft','submitted','approved','rejected','issued','fulfilled',"
    "'canceled','accepted_by_erp','pending_stock','sync_failed')"
)
_WITH_CANCELLATION_PENDING = (
    "status IN ('draft','submitted','approved','rejected','issued','fulfilled',"
    "'canceled','accepted_by_erp','pending_stock','cancellation_pending',"
    "'sync_failed')"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_field_material_requests_status",
        "field_material_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_field_material_requests_status",
        "field_material_requests",
        _WITH_CANCELLATION_PENDING,
    )


def downgrade() -> None:
    op.execute(
        "UPDATE field_material_requests SET status = 'pending_stock' "
        "WHERE status = 'cancellation_pending'"
    )
    op.drop_constraint(
        "ck_field_material_requests_status",
        "field_material_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_field_material_requests_status",
        "field_material_requests",
        _CURRENT,
    )
