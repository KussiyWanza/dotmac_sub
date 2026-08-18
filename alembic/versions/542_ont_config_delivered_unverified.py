"""Represent delivered ONT configuration whose exact readback is unavailable.

Revision ID: 542_ont_config_delivered_unverified
Revises: 541_staff_session_party_ratchet
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "542_ont_config_delivered_unverified"
down_revision: str | None = "541_staff_session_party_ratchet"
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


def upgrade() -> None:
    op.drop_constraint(
        "ck_ont_service_config_head_phase",
        "ont_service_configuration_heads",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ont_service_config_head_phase",
        "ont_service_configuration_heads",
        _check(_NEW_PHASES),
    )
    op.drop_constraint(
        "ck_ont_service_config_revision_phase",
        "ont_service_configuration_revisions",
        type_="check",
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
    op.drop_constraint(
        "ck_ont_service_config_head_phase",
        "ont_service_configuration_heads",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ont_service_config_head_phase",
        "ont_service_configuration_heads",
        _check(_OLD_PHASES),
    )
    op.drop_constraint(
        "ck_ont_service_config_revision_phase",
        "ont_service_configuration_revisions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ont_service_config_revision_phase",
        "ont_service_configuration_revisions",
        _check(_OLD_PHASES),
    )
