"""Record dual-reviewed pre-handoff Sub-native provenance decisions.

Revision ID: 544_carried_source_adjudication
Revises: 543_ont_config_unverified
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "544_carried_source_adjudication"
down_revision: str | None = "543_ont_config_unverified"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISPOSITION_VALUES = ("native_before_handoff",)
_DISPOSITION_TYPE = postgresql.ENUM(
    *_DISPOSITION_VALUES,
    name="carriedsourceidentitydisposition",
    create_type=False,
)


def _install_append_only_trigger() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE FUNCTION carried_source_identity_adjudications_append_only()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'carried_source_identity_adjudications is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_carried_source_identity_adjudications_append_only
        BEFORE UPDATE OR DELETE ON carried_source_identity_adjudications
        FOR EACH ROW
        EXECUTE FUNCTION carried_source_identity_adjudications_append_only()
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        *_DISPOSITION_VALUES,
        name="carriedsourceidentitydisposition",
    ).create(bind, checkfirst=True)
    op.create_table(
        "carried_source_identity_adjudications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("disposition", _DISPOSITION_TYPE, nullable=False),
        sa.Column("source_system", sa.String(length=40), nullable=False),
        sa.Column("financial_handoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evidence_ref", sa.String(length=240), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reviewed_by_id <> approved_by_id",
            name="ck_carried_source_identity_distinct_reviewers",
        ),
        sa.CheckConstraint(
            "length(preview_fingerprint) = 64 AND "
            "length(evidence_sha256) = 64 AND "
            "length(command_fingerprint) = 64",
            name="ck_carried_source_identity_digest_lengths",
        ),
        sa.CheckConstraint(
            "length(trim(evidence_ref)) > 0 AND length(trim(reason)) > 0",
            name="ck_carried_source_identity_review_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["subscribers.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_id"], ["system_users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_id"], ["system_users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", name="uq_carried_source_identity_adjudications_account"
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_carried_source_identity_adjudications_idempotency",
        ),
        sa.UniqueConstraint(
            "command_id", name="uq_carried_source_identity_adjudications_command"
        ),
    )
    _install_append_only_trigger()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS "
            "trg_carried_source_identity_adjudications_append_only "
            "ON carried_source_identity_adjudications"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "carried_source_identity_adjudications_append_only()"
        )
    op.drop_table("carried_source_identity_adjudications")
    postgresql.ENUM(name="carriedsourceidentitydisposition").drop(
        op.get_bind(), checkfirst=True
    )
