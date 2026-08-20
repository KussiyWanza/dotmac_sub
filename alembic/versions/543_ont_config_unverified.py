"""Represent delivered ONT configuration whose exact readback is unavailable.

Revision ID: 543_ont_config_unverified
Revises: 542_subscription_additional_ip_permission
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "543_ont_config_unverified"
down_revision: str | None = "542_subscription_additional_ip_permission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PHASES = (
    "saved",
    "queued",
    "applying",
    "readback_pending",
    "verified",
    "failed",
    "superseded",
    "retired",
)
_NEW_PHASES = (*_OLD_PHASES[:4], "delivered_unverified", *_OLD_PHASES[4:])


def _check(values: tuple[str, ...]) -> str:
    return f"phase IN ({', '.join(repr(value) for value in values)})"


def _drop_check_constraint_if_exists(table_name: str, constraint_name: str) -> None:
    existing = {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
    }
    if constraint_name in existing:
        op.drop_constraint(constraint_name, table_name, type_="check")


def upgrade() -> None:
    _drop_check_constraint_if_exists(
        "ont_service_configuration_heads", "ck_ont_service_config_head_phase"
    )
    op.create_check_constraint(
        "ck_ont_service_config_head_phase",
        "ont_service_configuration_heads",
        _check(_NEW_PHASES),
    )
    _drop_check_constraint_if_exists(
        "ont_service_configuration_revisions",
        "ck_ont_service_config_revision_phase",
    )
    op.create_check_constraint(
        "ck_ont_service_config_revision_phase",
        "ont_service_configuration_revisions",
        _check(_NEW_PHASES),
    )


def downgrade() -> None:
    connection = op.get_bind()
    for table in (
        "ont_service_configuration_heads",
        "ont_service_configuration_revisions",
    ):
        if connection.execute(
            sa.text(
                f"SELECT 1 FROM {table} WHERE phase = 'delivered_unverified' LIMIT 1"
            )
        ).first():
            raise RuntimeError(
                "Cannot downgrade while delivered_unverified ONT configuration "
                "rows exist"
            )
    _drop_check_constraint_if_exists(
        "ont_service_configuration_heads", "ck_ont_service_config_head_phase"
    )
    op.create_check_constraint(
        "ck_ont_service_config_head_phase",
        "ont_service_configuration_heads",
        _check(_OLD_PHASES),
    )
    _drop_check_constraint_if_exists(
        "ont_service_configuration_revisions",
        "ck_ont_service_config_revision_phase",
    )
    op.create_check_constraint(
        "ck_ont_service_config_revision_phase",
        "ont_service_configuration_revisions",
        _check(_OLD_PHASES),
    )
