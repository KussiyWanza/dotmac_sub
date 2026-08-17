"""Require Party identity on usable staff sessions.

The column stays nullable globally: subscriber/reseller sessions do not carry
the staff Party projection, and revoked/expired historical staff rows are kept
without inventing an identity. The ratchet applies only to active, unrevoked
staff sessions and refuses a Party projection on a non-staff session.

Revision ID: 541_staff_session_party_ratchet
Revises: 540_ticket_comment_mentions
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "541_staff_session_party_ratchet"
down_revision = "540_ticket_comment_mentions"
branch_labels = None
depends_on = None

_ACTIVE_STAFF_REQUIRES_PARTY = "ck_sessions_active_staff_requires_party"
_PARTY_REQUIRES_STAFF_CONTEXT = "ck_sessions_party_requires_staff_context"


def upgrade() -> None:
    connection = op.get_bind()
    counts = connection.execute(
        sa.text(
            """
            SELECT
                count(*) FILTER (
                    WHERE s.system_user_id IS NOT NULL
                      AND s.status = 'active'
                      AND s.revoked_at IS NULL
                      AND s.party_id IS NULL
                ) AS usable_staff_without_party,
                count(*) FILTER (
                    WHERE s.system_user_id IS NOT NULL
                      AND s.status = 'active'
                      AND s.revoked_at IS NULL
                      AND (su.is_active IS NOT TRUE OR su.person_party_id IS NULL)
                ) AS usable_staff_unbound,
                count(*) FILTER (
                    WHERE s.system_user_id IS NOT NULL
                      AND s.party_id IS NOT NULL
                      AND (
                          s.party_id IS DISTINCT FROM su.person_party_id
                          OR p.party_type IS DISTINCT FROM 'person'
                      )
                ) AS projection_disagreements,
                count(*) FILTER (
                    WHERE s.party_id IS NOT NULL
                      AND s.system_user_id IS NULL
                ) AS party_without_staff_context
            FROM sessions AS s
            LEFT JOIN system_users AS su ON su.id = s.system_user_id
            LEFT JOIN parties AS p ON p.id = s.party_id
            """
        )
    ).one()
    usable_staff_without_party = int(counts.usable_staff_without_party)
    usable_staff_unbound = int(counts.usable_staff_unbound)
    projection_disagreements = int(counts.projection_disagreements)
    party_without_staff_context = int(counts.party_without_staff_context)
    if (
        usable_staff_without_party
        or usable_staff_unbound
        or projection_disagreements
        or party_without_staff_context
    ):
        raise RuntimeError(
            "staff-session Party ratchet refused: "
            f"usable_staff_without_party={usable_staff_without_party}, "
            f"usable_staff_unbound={usable_staff_unbound}, "
            f"projection_disagreements={projection_disagreements}, "
            f"party_without_staff_context={party_without_staff_context}"
        )

    op.create_check_constraint(
        _ACTIVE_STAFF_REQUIRES_PARTY,
        "sessions",
        "system_user_id IS NULL OR status <> 'active' "
        "OR revoked_at IS NOT NULL OR party_id IS NOT NULL",
    )
    op.create_check_constraint(
        _PARTY_REQUIRES_STAFF_CONTEXT,
        "sessions",
        "party_id IS NULL OR system_user_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        _PARTY_REQUIRES_STAFF_CONTEXT,
        "sessions",
        type_="check",
    )
    op.drop_constraint(
        _ACTIVE_STAFF_REQUIRES_PARTY,
        "sessions",
        type_="check",
    )
