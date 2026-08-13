"""Backfill NAS-to-RADIUS-pool links from active service evidence.

Revision ID: 521_backfill_nas_radius_pool_links
Revises: 520_domain_setting_history
Create Date: 2026-08-11

``IpPool.nas_device_id`` is a legacy one-to-one shortcut, while NAS
``radius_pool:`` tags are the existing many-to-many configuration used by
provisioning. Public pools are served by several access routers, so this data
repair records every link proven by an active subscription/IP assignment.
The explicitly non-production ``Demo`` pool is excluded.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa

from alembic import op

revision: str = "521_backfill_nas_radius_pool_links"
down_revision: str | None = "520_domain_setting_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inferred: defaultdict[UUID, set[UUID]] = defaultdict(set)
    rows = bind.execute(
        sa.text(
            """
            SELECT DISTINCT s.provisioning_nas_device_id, v.pool_id
            FROM ip_assignments AS a
            JOIN subscriptions AS s ON s.id = a.subscription_id
            JOIN ipv4_addresses AS v ON v.id = a.ipv4_address_id
            JOIN ip_pools AS p ON p.id = v.pool_id
            WHERE a.is_active IS TRUE
              AND a.ip_version::text = 'ipv4'
              AND s.status::text = 'active'
              AND s.provisioning_nas_device_id IS NOT NULL
              AND p.is_active IS TRUE
              AND p.ip_version::text = 'ipv4'
              AND lower(trim(p.name)) <> 'demo'
            """
        )
    )
    for nas_id, pool_id in rows:
        inferred[nas_id].add(pool_id)

    for nas_id, pool_ids in inferred.items():
        tags = bind.execute(
            sa.text("SELECT tags FROM nas_devices WHERE id = :nas_id FOR UPDATE"),
            {"nas_id": nas_id},
        ).scalar_one_or_none()
        existing = [tag for tag in (tags or []) if isinstance(tag, str)]
        additions = [
            f"radius_pool:{pool_id}"
            for pool_id in sorted(pool_ids, key=str)
            if f"radius_pool:{pool_id}" not in existing
        ]
        if additions:
            bind.execute(
                sa.text(
                    "UPDATE nas_devices SET tags = :tags, updated_at = now() "
                    "WHERE id = :nas_id"
                ).bindparams(sa.bindparam("tags", type_=sa.JSON())),
                {"nas_id": nas_id, "tags": existing + additions},
            )


def downgrade() -> None:
    # Forward-only data repair: removing a now-authoritative operational link
    # after later assignments may have changed would be unsafe.
    pass
