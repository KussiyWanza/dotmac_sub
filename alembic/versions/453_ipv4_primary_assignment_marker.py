"""mark which active IPv4 assignment RADIUS serves

`IPAssignment` recorded that a service HOLDS an address. Nothing recorded which
held address is served as `Framed-IP-Address`. A service may legitimately hold
several -- the admin subscription form allocates one per selected block -- so a
consumer facing two had no answer, and `radius_population` answered by unordered
query position.

A plain unique index on `subscription_id` cannot express the invariant: it would
forbid holding several addresses, which is a supported shape. The missing piece
was a concept, not a constraint. This adds it, backfills it from evidence, and
only then constrains it.

Backfill precedence, deliberately narrow:

1. the active assignment whose address equals `subscriptions.ipv4_address` --
   the served projection is the best available evidence of which address is
   actually in use; then
2. the sole active assignment when a service holds exactly one.

Anything else is left unmarked. A service holding several with no served column,
or whose column matches none of them, is an ownership question this migration
refuses to answer -- consumers already fail closed on exactly that state.

Revision ID: 453_ipv4_primary_assignment_marker
Revises: 452_add_nas_local_secret_retire_operation_type
Create Date: 2026-08-01
"""

import sqlalchemy as sa

from alembic import op

revision = "453_ipv4_primary_assignment_marker"
down_revision = "452_add_nas_local_secret_retire_operation_type"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_ip_assignments_primary_ipv4_active"
_PREDICATE = (
    "is_active AND is_primary AND ipv4_address_id IS NOT NULL "
    "AND subscription_id IS NOT NULL"
)


def upgrade() -> None:
    op.add_column(
        "ip_assignments",
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    conn = op.get_bind()

    # 1. Served column wins. Scoped so it can never mark two rows for one
    #    service: an address appears at most once per subscription.
    conn.execute(
        sa.text(
            """
            UPDATE ip_assignments a
            SET is_primary = true
            FROM subscriptions s, ipv4_addresses v
            WHERE a.subscription_id = s.id
              AND a.ipv4_address_id = v.id
              AND a.is_active
              AND a.ipv4_address_id IS NOT NULL
              AND s.ipv4_address IS NOT NULL
              AND v.address = s.ipv4_address
            """
        )
    )

    # 2. Sole active assignment, only where step 1 marked nothing for that
    #    service. Never promotes one of several.
    conn.execute(
        sa.text(
            """
            UPDATE ip_assignments a
            SET is_primary = true
            WHERE a.is_active
              AND a.ipv4_address_id IS NOT NULL
              AND a.subscription_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM ip_assignments p
                  WHERE p.subscription_id = a.subscription_id
                    AND p.is_active AND p.is_primary
                    AND p.ipv4_address_id IS NOT NULL
              )
              AND (
                  SELECT count(*) FROM ip_assignments c
                  WHERE c.subscription_id = a.subscription_id
                    AND c.is_active AND c.ipv4_address_id IS NOT NULL
              ) = 1
            """
        )
    )

    unmarked = conn.execute(
        sa.text(
            """
            SELECT count(*) FROM (
                SELECT a.subscription_id FROM ip_assignments a
                WHERE a.is_active AND a.ipv4_address_id IS NOT NULL
                  AND a.subscription_id IS NOT NULL
                GROUP BY a.subscription_id
                HAVING count(*) FILTER (WHERE a.is_primary) = 0
            ) x
            """
        )
    ).scalar()
    if unmarked:
        # Reported, not resolved. These are the services whose primary address
        # is genuinely undecidable from stored evidence; consumers fail closed
        # on them and the assignment owner adjudicates.
        print(
            f"[453] {unmarked} service(s) hold an active IPv4 assignment with no "
            "primary marked; radius_population will preserve their existing "
            "RADIUS rows until the assignment owner adjudicates."
        )

    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON ip_assignments (subscription_id) WHERE {_PREDICATE}"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    op.drop_column("ip_assignments", "is_primary")
