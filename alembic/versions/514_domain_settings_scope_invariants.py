"""Make a settings row's scope columns agree, and default new rows to the tenant

Migration 507 added `tenant_id`/`scope_kind`/`scope_id` and defaulted them to
`platform`/NULL, which was correct that day. Migration 509 then moved every row
to the operator tenant (ADR-0009 — the ISP operator IS Sub's one tenant, and
`platform` is the kernel's deployment-wide level BENEATH tenant, not a synonym
for "this deployment"). The DEFAULT did not move with the data.

So every setting created after 509 arrived at `platform` scope while every
setting created before it sat at `tenant` scope. Resolution hides this, because
a platform row is precisely what a tenant read falls back to. What it does not
hide is the shape underneath: `uq_domain_settings_scope_domain_key` includes the
scope columns, so one `(domain, key)` may now legitimately hold two rows, and
`DomainSettings.get_optional_by_key` — which filters on neither scope column —
picks between them by whichever the database returns first.

Two changes, and the second is the one that closes the class rather than this
instance:

1. Any row still at `platform` scope moves to the operator tenant — 509's
   statement, re-run, because the rows it is now catching were written after it
   ran.
2. A CHECK ties the two columns together: `platform` has no tenant, every finer
   kind has one. That is
   `dotmac_kernel.setting_scopes.SettingScope.__post_init__` — the kernel's own
   invariant — enforced in the schema, where every writer passes rather than
   only the callers that build a `SettingScope`.

The COLUMN DEFAULT deliberately stays `platform`. The fix for new rows belongs
in the model, where an application write knows it means the operator's setting;
a raw `INSERT` that names no scope is almost always a migration running before
the operator tenant exists (`tenants` arrives in 508, its row in 509), and a
`tenant` default would have every one of those produce a `tenant` row with a
NULL tenant — a violation of the CHECK this same migration adds.

Without (2), fixing the default alone would trade one inconsistent shape for
another: a raw INSERT omitting `tenant_id` would produce `scope_kind='tenant'`
with a NULL tenant, and the resolver filters on BOTH columns, so that row is
invisible to its own lookup — a setting that exists and can never be read.

Existing data already satisfies the CHECK: 509 left every row `tenant` +
operator, and anything written since is `platform` + NULL. Both shapes pass, so
this adds no data repair.

Revision ID: 514_domain_settings_scope_invariants
Revises: 513_team_inbox_unread_query_indexes
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "514_domain_settings_scope_invariants"
down_revision: str | None = "513_team_inbox_unread_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "domain_settings"
SCOPE_CONSTRAINT = "ck_domain_settings_scope_alignment"
#: Must equal `app.services.operator_tenant.OPERATOR_TENANT_ID`, and equals
#: 509's copy. A migration must not import application code that can change
#: under it; `tests/test_operator_tenant.py` asserts the copies still match.
OPERATOR_TENANT_ID = "8c7ae830-51fc-52ae-9818-d84b2a35e568"
SCOPE_ALIGNMENT = (
    "(scope_kind = 'platform' AND tenant_id IS NULL) "
    "OR (scope_kind <> 'platform' AND tenant_id IS NOT NULL)"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        # SQLite builds this schema from model metadata, which carries both the
        # default and the CHECK. Re-issuing them here would be a second, weaker
        # expression of the same declaration.
        return

    # 509's statement, re-run. It caught every row that existed then; these are
    # the ones written since, by a model default that outlived it. Idempotent
    # for the same reason 509 is: a row already at tenant scope is not touched.
    op.get_bind().execute(
        sa.text(
            f"""
            UPDATE {TABLE}
               SET tenant_id = :tenant_id, scope_kind = 'tenant'
             WHERE scope_kind = 'platform' OR tenant_id IS NULL
            """
        ),
        {"tenant_id": OPERATOR_TENANT_ID},
    )

    op.execute(
        sa.text(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {SCOPE_CONSTRAINT}")
    )
    op.create_check_constraint(SCOPE_CONSTRAINT, TABLE, SCOPE_ALIGNMENT)


def downgrade() -> None:
    """Drops the invariant.

    Deliberately does NOT move rows back to platform scope: that is migration
    509's decision to reverse, and doing it here would silently re-scope
    settings this migration never touched.
    """

    if not _is_postgres():
        return

    op.execute(
        sa.text(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {SCOPE_CONSTRAINT}")
    )
