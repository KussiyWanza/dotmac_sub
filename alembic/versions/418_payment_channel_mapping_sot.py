"""Retire the duplicate payment-channel collection-account pointer.

Revision ID: 418_payment_channel_mapping_sot
Revises: 417_service_extension_grant_intervals
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa

from alembic import op

revision = "418_payment_channel_mapping_sot"
down_revision = "417_service_extension_grant_intervals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # The squashed schema is generated from the current models, which no longer
    # declare the retired pointer. On a fresh database the column never exists,
    # so there is nothing to migrate or drop — only a real upgrade from a
    # pre-retirement schema carries it.
    columns = {c["name"] for c in sa.inspect(bind).get_columns("payment_channels")}
    if "default_collection_account_id" not in columns:
        return
    rows = bind.execute(
        sa.text(
            "SELECT id, default_collection_account_id FROM payment_channels "
            "WHERE default_collection_account_id IS NOT NULL"
        )
    ).mappings()
    now = datetime.now(UTC)
    for row in rows:
        bind.execute(
            sa.text(
                "UPDATE payment_channel_accounts "
                "SET is_default = false, updated_at = :now "
                "WHERE channel_id = :channel_id "
                "AND currency IS NULL AND is_default = true"
            ),
            {"channel_id": row["id"], "now": now},
        )
        existing = bind.execute(
            sa.text(
                "SELECT id FROM payment_channel_accounts "
                "WHERE channel_id = :channel_id "
                "AND collection_account_id = :account_id "
                "AND currency IS NULL ORDER BY created_at LIMIT 1"
            ),
            {
                "channel_id": row["id"],
                "account_id": row["default_collection_account_id"],
            },
        ).scalar_one_or_none()
        if existing:
            bind.execute(
                sa.text(
                    "UPDATE payment_channel_accounts "
                    "SET is_active = true, is_default = true, updated_at = :now "
                    "WHERE id = :mapping_id"
                ),
                {"mapping_id": existing, "now": now},
            )
        else:
            mapping_id = uuid5(
                NAMESPACE_URL,
                "dotmac:payment-channel-account:"
                f"{row['id']}:{row['default_collection_account_id']}:default",
            )
            bind.execute(
                sa.text(
                    "INSERT INTO payment_channel_accounts "
                    "(id, channel_id, collection_account_id, currency, priority, "
                    "is_default, is_active, created_at, updated_at) VALUES "
                    "(:id, :channel_id, :account_id, NULL, 0, true, true, :now, :now)"
                ),
                {
                    "id": mapping_id,
                    "channel_id": row["id"],
                    "account_id": row["default_collection_account_id"],
                    "now": now,
                },
            )
    op.drop_constraint(
        "payment_channels_default_collection_account_id_fkey",
        "payment_channels",
        type_="foreignkey",
    )
    op.drop_column("payment_channels", "default_collection_account_id")


def downgrade() -> None:
    op.add_column(
        "payment_channels",
        sa.Column("default_collection_account_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "payment_channels_default_collection_account_id_fkey",
        "payment_channels",
        "collection_accounts",
        ["default_collection_account_id"],
        ["id"],
    )
    op.execute(
        sa.text(
            "UPDATE payment_channels pc SET default_collection_account_id = ("
            "SELECT pca.collection_account_id FROM payment_channel_accounts pca "
            "WHERE pca.channel_id = pc.id AND pca.currency IS NULL "
            "AND pca.is_active = true "
            "ORDER BY pca.is_default DESC, pca.priority DESC, pca.created_at LIMIT 1)"
        )
    )
