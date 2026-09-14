"""Add immutable Inbox Customer completion-policy snapshots.

Revision ID: 596_inbox_customer_completion_policy
Revises: 595_active_cpe_identity
Create Date: 2026-09-11
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "596_inbox_customer_completion_policy"
down_revision = "595_active_cpe_identity"
branch_labels = None
depends_on = None

_POLICY_TABLE = "inbox_customer_completion_policy_versions"
_CONVERSATION_TABLE = "inbox_conversations"
_POLICY_COLUMN = "customer_completion_policy_version_id"
_INITIAL_POLICY_ID = uuid.UUID("fe24c672-291a-5f89-96f7-282b8862d06f")


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    return column in {
        item["name"] for item in inspect(op.get_bind()).get_columns(table)
    }


def _has_index(table: str, index_name: str) -> bool:
    return any(
        item["name"] == index_name for item in inspect(op.get_bind()).get_indexes(table)
    )


def _has_foreign_key(
    table: str, *, constrained_column: str, referred_table: str
) -> bool:
    return any(
        item.get("constrained_columns") == [constrained_column]
        and item.get("referred_table") == referred_table
        for item in inspect(op.get_bind()).get_foreign_keys(table)
    )


def _has_named_foreign_key(table: str, constraint_name: str) -> bool:
    return any(
        item.get("name") == constraint_name
        for item in inspect(op.get_bind()).get_foreign_keys(table)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" or not _has_table(_CONVERSATION_TABLE):
        return
    if not _has_table(_POLICY_TABLE):
        op.create_table(
            _POLICY_TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("required_fields", sa.JSON(), nullable=False),
            sa.Column("created_by_person_id", postgresql.UUID(as_uuid=True)),
            sa.Column("decision_source", sa.String(length=80), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "version > 0",
                name="ck_inbox_customer_completion_policy_version_positive",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "version", name="uq_inbox_customer_completion_policy_version"
            ),
        )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION
                reject_inbox_customer_completion_policy_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'Inbox Customer completion policy versions are immutable';
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "trg_inbox_customer_completion_policy_immutable ON "
        f"{_POLICY_TABLE}"
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER trg_inbox_customer_completion_policy_immutable
            BEFORE UPDATE OR DELETE ON {_POLICY_TABLE}
            FOR EACH ROW EXECUTE FUNCTION
                reject_inbox_customer_completion_policy_mutation();
            """
        )
    )
    policy_versions = sa.table(
        _POLICY_TABLE,
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("version", sa.Integer()),
        sa.column("required_fields", sa.JSON()),
        sa.column("decision_source", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    bind.execute(
        postgresql.insert(policy_versions)
        .values(
            id=_INITIAL_POLICY_ID,
            version=1,
            required_fields=["name", "phone", "address"],
            decision_source="migration_initial_policy",
            created_at=sa.func.now(),
        )
        .on_conflict_do_nothing(index_elements=["version"])
    )
    if not _has_column(_CONVERSATION_TABLE, _POLICY_COLUMN):
        op.add_column(
            _CONVERSATION_TABLE,
            sa.Column(_POLICY_COLUMN, postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not _has_foreign_key(
        _CONVERSATION_TABLE,
        constrained_column=_POLICY_COLUMN,
        referred_table=_POLICY_TABLE,
    ):
        op.create_foreign_key(
            "fk_inbox_conversations_customer_completion_policy",
            _CONVERSATION_TABLE,
            _POLICY_TABLE,
            [_POLICY_COLUMN],
            ["id"],
            ondelete="RESTRICT",
        )
    if not _has_index(
        _CONVERSATION_TABLE,
        "ix_inbox_conversations_customer_completion_policy",
    ):
        op.create_index(
            "ix_inbox_conversations_customer_completion_policy",
            _CONVERSATION_TABLE,
            [_POLICY_COLUMN],
        )
    conversations = sa.table(
        _CONVERSATION_TABLE,
        sa.column(_POLICY_COLUMN, postgresql.UUID(as_uuid=True)),
    )
    bind.execute(
        sa.update(conversations)
        .where(conversations.c.customer_completion_policy_version_id.is_(None))
        .values(customer_completion_policy_version_id=_INITIAL_POLICY_ID)
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite" or not _has_table(_CONVERSATION_TABLE):
        return
    if _has_column(_CONVERSATION_TABLE, _POLICY_COLUMN):
        if _has_index(
            _CONVERSATION_TABLE,
            "ix_inbox_conversations_customer_completion_policy",
        ):
            op.drop_index(
                "ix_inbox_conversations_customer_completion_policy",
                table_name=_CONVERSATION_TABLE,
            )
        if _has_named_foreign_key(
            _CONVERSATION_TABLE,
            "fk_inbox_conversations_customer_completion_policy",
        ):
            op.drop_constraint(
                "fk_inbox_conversations_customer_completion_policy",
                _CONVERSATION_TABLE,
                type_="foreignkey",
            )
        op.drop_column(_CONVERSATION_TABLE, _POLICY_COLUMN)
    if _has_table(_POLICY_TABLE):
        op.execute(
            "DROP TRIGGER IF EXISTS "
            "trg_inbox_customer_completion_policy_immutable ON "
            f"{_POLICY_TABLE}"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS reject_inbox_customer_completion_policy_mutation()"
        )
        op.drop_table(_POLICY_TABLE)
