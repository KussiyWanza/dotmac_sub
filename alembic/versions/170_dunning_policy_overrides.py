"""Configurable dunning policies: account/reseller overrides + general defaults.

Adds nullable ``policy_set_id`` to ``subscribers`` and ``resellers`` so a dunning
policy can be overridden per-account or per-reseller. Resolution order (most
specific first): account -> reseller -> offer/offer_version -> general default
(by billing mode).

Seeds the two general-default policies and wires them via collections settings:
  - prepaid:            suspend on day 0 (due-on-issue, pay-in-advance)
    -> default_prepaid_policy_set_id
  - postpaid/recurring: notify d7, notify d30, suspend d60
    -> default_postpaid_policy_set_id

All seed inserts are idempotent (fixed ids + WHERE NOT EXISTS / ON CONFLICT), so
re-runs are no-ops. Adding two nullable FK columns is non-locking on Postgres.

Revision ID: 170_dunning_policy_overrides
Revises: 169_drop_splynx_sync_state_tables
Create Date: 2026-06-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "170_dunning_policy_overrides"
down_revision = "169_drop_splynx_sync_state_tables"
branch_labels = None
depends_on = None

PREPAID_POLICY_ID = "0d000000-0000-4000-8000-00000000d001"
POSTPAID_POLICY_ID = "0d000000-0000-4000-8000-00000000d002"

# (id, policy_set_id, day_offset, action, note)
_STEPS = [
    (
        "0d000000-0000-4000-8000-0000000d0101",
        PREPAID_POLICY_ID,
        0,
        "suspend",
        "Prepaid is billed in advance (invoice due on issue); suspend if unpaid.",
    ),
    (
        "0d000000-0000-4000-8000-0000000d0201",
        POSTPAID_POLICY_ID,
        7,
        "notify",
        "Payment reminder.",
    ),
    (
        "0d000000-0000-4000-8000-0000000d0202",
        POSTPAID_POLICY_ID,
        30,
        "notify",
        "Final overdue warning.",
    ),
    (
        "0d000000-0000-4000-8000-0000000d0203",
        POSTPAID_POLICY_ID,
        60,
        "suspend",
        "Suspend at 60 days overdue.",
    ),
]


def _has_column(insp, table: str, col: str) -> bool:
    return any(c["name"] == col for c in insp.get_columns(table))


def _seed_policy(policy_id: str, name: str) -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO policy_sets (
                id, name, proration_policy, downgrade_policy,
                suspension_action, refund_policy, trial_card_required,
                is_active, created_at, updated_at
            )
            SELECT CAST(:id AS uuid), :name, 'immediate', 'next_cycle',
                   'suspend', 'none', false, true, now(), now()
            WHERE NOT EXISTS (
                SELECT 1 FROM policy_sets WHERE id = CAST(:id AS uuid)
            )
            """
        ).bindparams(id=policy_id, name=name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if not _has_column(insp, "subscribers", "policy_set_id"):
        op.add_column(
            "subscribers", sa.Column("policy_set_id", UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            "fk_subscribers_policy_set",
            "subscribers",
            "policy_sets",
            ["policy_set_id"],
            ["id"],
        )
    if not _has_column(insp, "resellers", "policy_set_id"):
        op.add_column(
            "resellers", sa.Column("policy_set_id", UUID(as_uuid=True), nullable=True)
        )
        op.create_foreign_key(
            "fk_resellers_policy_set",
            "resellers",
            "policy_sets",
            ["policy_set_id"],
            ["id"],
        )

    _seed_policy(PREPAID_POLICY_ID, "Default — Prepaid (immediate suspend)")
    _seed_policy(POSTPAID_POLICY_ID, "Default — Postpaid (suspend at 60 days)")

    for step_id, policy_id, day_offset, action, note in _STEPS:
        op.execute(
            sa.text(
                """
                INSERT INTO policy_dunning_steps
                    (id, policy_set_id, day_offset, action, note)
                SELECT CAST(:sid AS uuid), CAST(:pid AS uuid), :day,
                       CAST(:action AS dunningaction), :note
                WHERE NOT EXISTS (
                    SELECT 1 FROM policy_dunning_steps WHERE id = CAST(:sid AS uuid)
                )
                """
            ).bindparams(
                sid=step_id, pid=policy_id, day=day_offset, action=action, note=note
            )
        )

    for key, value in (
        ("default_prepaid_policy_set_id", PREPAID_POLICY_ID),
        ("default_postpaid_policy_set_id", POSTPAID_POLICY_ID),
    ):
        # UPDATE-then-INSERT rather than `ON CONFLICT (domain, key)`.
        #
        # A conflict TARGET has to match a unique index that exists when this
        # runs, and `(domain, key)` no longer does: migration 507 replaced it
        # with `uq_domain_settings_scope_domain_key`, which includes the scope
        # columns. That matters here because `001_squashed_initial_schema`
        # builds the baseline from the CURRENT model metadata — so the model is
        # this chain's history, and a constraint dropped from the model is
        # absent at every earlier point too, including this one.
        #
        # The same two statements the `policy_dunning_steps` insert above
        # already uses, and they name no index at all, so no future change to
        # the uniqueness shape can reach back and break this migration again.
        op.execute(
            sa.text(
                """
                UPDATE domain_settings
                   SET value_text = :val, is_active = true
                 WHERE domain = 'collections' AND key = :key
                """
            ).bindparams(key=key, val=value)
        )
        op.execute(
            sa.text(
                """
                INSERT INTO domain_settings (
                    id, domain, key, value_type, value_text,
                    is_secret, is_active, created_at, updated_at
                )
                SELECT
                    gen_random_uuid(), 'collections', :key, 'string', :val,
                    false, true, now(), now()
                WHERE NOT EXISTS (
                    SELECT 1 FROM domain_settings
                     WHERE domain = 'collections' AND key = :key
                )
                """
            ).bindparams(key=key, val=value)
        )


def downgrade() -> None:
    op.execute(
        "DELETE FROM domain_settings WHERE domain = 'collections' AND key IN "
        "('default_prepaid_policy_set_id', 'default_postpaid_policy_set_id')"
    )
    op.execute(
        sa.text(
            "DELETE FROM policy_dunning_steps WHERE policy_set_id IN "
            "(CAST(:a AS uuid), CAST(:b AS uuid))"
        ).bindparams(a=PREPAID_POLICY_ID, b=POSTPAID_POLICY_ID)
    )
    bind = op.get_bind()
    insp = inspect(bind)
    if _has_column(insp, "subscribers", "policy_set_id"):
        op.drop_constraint(
            "fk_subscribers_policy_set", "subscribers", type_="foreignkey"
        )
        op.drop_column("subscribers", "policy_set_id")
    if _has_column(insp, "resellers", "policy_set_id"):
        op.drop_constraint("fk_resellers_policy_set", "resellers", type_="foreignkey")
        op.drop_column("resellers", "policy_set_id")
    op.execute(
        sa.text(
            "DELETE FROM policy_sets WHERE id IN (CAST(:a AS uuid), CAST(:b AS uuid))"
        ).bindparams(a=PREPAID_POLICY_ID, b=POSTPAID_POLICY_ID)
    )
