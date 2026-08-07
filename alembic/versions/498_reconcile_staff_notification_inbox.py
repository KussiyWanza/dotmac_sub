"""reconcile historical staff push deliveries into the in-app inbox

Revision ID: 498_reconcile_staff_notification_inbox
Revises: 497_unify_staff_notification_inbox
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "498_reconcile_staff_notification_inbox"
down_revision: str | None = "497_unify_staff_notification_inbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Materialize legacy staff push rows without creating false unread items."""

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))
    op.execute(
        sa.text(
            r"""
            WITH eligible_deliveries AS (
                SELECT
                    n.id AS source_notification_id,
                    su.id AS system_user_id,
                    left(COALESCE(NULLIF(n.subject, ''), 'Notification'), 180)
                        AS title,
                    n.body,
                    CASE
                        WHEN COALESCE(n.metadata->>'target_url', '') = '/admin'
                            OR COALESCE(n.metadata->>'target_url', '') LIKE '/admin/%'
                        THEN n.metadata->>'target_url'
                        ELSE '/admin'
                    END AS target_url,
                    COALESCE(n.sent_at, n.created_at, now()) AS historical_read_at,
                    COALESCE(n.created_at, now()) AS created_at,
                    COALESCE(n.updated_at, n.created_at, now()) AS updated_at
                FROM notifications AS n
                JOIN system_users AS su
                    ON su.is_active
                    AND (
                        su.id = CASE
                            WHEN n.recipient ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN n.recipient::uuid
                            ELSE NULL
                        END
                        OR lower(su.email) = lower(n.recipient)
                    )
                WHERE n.channel = 'push'
                    AND n.is_active
            )
            INSERT INTO admin_notifications (
                id,
                alert_id,
                source_notification_id,
                system_user_id,
                title,
                body,
                target_url,
                read_at,
                created_at,
                updated_at
            )
            SELECT
                gen_random_uuid(),
                NULL,
                eligible.source_notification_id,
                eligible.system_user_id,
                eligible.title,
                eligible.body,
                eligible.target_url,
                eligible.historical_read_at,
                eligible.created_at,
                eligible.updated_at
            FROM eligible_deliveries AS eligible
            ON CONFLICT (source_notification_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    """The reconciliation is intentionally forward-only to preserve inbox history."""
