"""Add legacy pre-cutover completion-gate marker and single-use override grants.

Migration 596 backfilled ``customer_completion_policy_version_id`` onto every
existing ``inbox_conversations`` row unconditionally, with no exemption path.
That retroactively enforces the completion gate against roughly 429 legacy
production conversations (113 distinct subscribers) that were never expected
to meet it, with no way to resolve them again. This migration does NOT edit
596 (already applied on staging) -- it is purely additive and forward:

1. A durable ``completion_gate_precutover_at`` marker, stamped exactly once
   here with one captured ``now()`` reused for every backfilled row (that
   shared instant is itself audit evidence). No application code may ever
   write this column -- see
   ``tests/architecture/test_inbox_completion_override_boundary.py``.
2. One frozen ``inbox_customer_completion_cutovers`` census row recording the
   actual counts stamped by this run.
3. ``inbox_completion_override_grants``: a narrowly-scoped, audited,
   single-use resolution override for conversations that carry the marker.
   The marker is eligibility, never authorization -- see
   ``app/services/team_inbox_completion_override.py``.
4. The admin-only ``support:inbox:completion_override`` permission.

The marker backfill only touches conversations 596 already touched
(``customer_completion_policy_version_id = _INITIAL_POLICY_ID``), batched in
keyset pages to bound each individual UPDATE's row/lock footprint given the
real production row count (~21,309 confirmed by a read-only census).

That policy-id predicate alone is time-blind: every conversation-creation
call site snapshots whichever policy version is currently active, and 596
seeded only one version, so a conversation created AFTER 596 ran but BEFORE
this migration runs would carry the identical ``_INITIAL_POLICY_ID`` and
would be wrongly marked pre-cutover -- defeating the "a post-cutover
conversation can never receive a grant" property the whole design depends
on. The backfill therefore also requires
``inbox_conversations.created_at < <the initial policy row's own
created_at>``, which is the one fact that actually distinguishes
"existed before 596's backfill ran" from "merely carries the same policy
id". The batch predicate additionally requires
``completion_gate_precutover_at IS NULL`` and the census-row insert is
guarded by policy_version_id, so re-running this migration (e.g. after an
interruption) never re-stamps an already-marked row with a new timestamp
and never inserts a second census row.

Staging note: staging already ran 596's unconditional backfill with no
marker. This migration is exactly what staging needs too -- a normal
``alembic upgrade head`` picks it up with no special handling.

Revision ID: 602_inbox_completion_legacy_override
Revises: 601_prepaid_draft_exception_no_invoice_identity
Create Date: 2026-09-11
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "602_inbox_completion_legacy_override"
down_revision = "601_prepaid_draft_exception_no_invoice_identity"
branch_labels = None
depends_on = None

_CONVERSATION_TABLE = "inbox_conversations"
_POLICY_TABLE = "inbox_customer_completion_policy_versions"
_GRANTS_TABLE = "inbox_completion_override_grants"
_CUTOVERS_TABLE = "inbox_customer_completion_cutovers"
_MARKER_COLUMN = "completion_gate_precutover_at"
_POLICY_COLUMN = "customer_completion_policy_version_id"
_INITIAL_POLICY_ID = uuid.UUID("fe24c672-291a-5f89-96f7-282b8862d06f")
_BATCH_SIZE = 2000
_OVERRIDE_PERMISSION_KEY = "support:inbox:completion_override"
_OVERRIDE_PERMISSION_DESCRIPTION = (
    "Grant a one-transition legacy customer-completion resolution override"
)


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


def _seed_override_permission() -> None:
    bind = op.get_bind()
    if not {"permissions", "roles", "role_permissions"}.issubset(
        sa.inspect(bind).get_table_names()
    ):
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    roles = sa.Table("roles", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    now = datetime.now(UTC)

    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _OVERRIDE_PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is None:
        permission_id = uuid.uuid4()
        bind.execute(
            permissions.insert().values(
                id=permission_id,
                key=_OVERRIDE_PERMISSION_KEY,
                description=_OVERRIDE_PERMISSION_DESCRIPTION,
                is_active=True,
                is_ui_assignable=True,
                created_at=now,
                updated_at=now,
            )
        )

    admin_id = bind.execute(
        sa.select(roles.c.id).where(
            roles.c.name == "admin",
            roles.c.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if admin_id is None:
        return
    existing = bind.execute(
        sa.select(role_permissions.c.id).where(
            role_permissions.c.role_id == admin_id,
            role_permissions.c.permission_id == permission_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        bind.execute(
            role_permissions.insert().values(
                id=uuid.uuid4(),
                role_id=admin_id,
                permission_id=permission_id,
            )
        )


def _unseed_override_permission() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if not {"permissions", "role_permissions"}.issubset(tables):
        return
    metadata = sa.MetaData()
    permissions = sa.Table("permissions", metadata, autoload_with=bind)
    role_permissions = sa.Table("role_permissions", metadata, autoload_with=bind)
    permission_id = bind.execute(
        sa.select(permissions.c.id).where(permissions.c.key == _OVERRIDE_PERMISSION_KEY)
    ).scalar_one_or_none()
    if permission_id is not None:
        bind.execute(
            role_permissions.delete().where(
                role_permissions.c.permission_id == permission_id
            )
        )
        bind.execute(permissions.delete().where(permissions.c.id == permission_id))


def _create_grants_table() -> None:
    op.create_table(
        _GRANTS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subscriber_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("missing_fields", sa.JSON(), nullable=False),
        sa.Column("canonical_values_digest", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("granted_by", sa.String(255), nullable=False),
        sa.Column("granted_by_system_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("grant_idempotency_key", sa.String(255), nullable=False),
        sa.Column("grant_fingerprint", sa.String(64), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.String(255), nullable=True),
        sa.Column(
            "consumed_transition_event_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("consumed_resolution_reason", sa.String(80), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{_CONVERSATION_TABLE}.id"],
            ondelete="RESTRICT",
            name="fk_inbox_completion_override_grants_conversation",
        ),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            [f"{_POLICY_TABLE}.id"],
            ondelete="RESTRICT",
            name="fk_inbox_completion_override_grants_policy_version",
        ),
        sa.ForeignKeyConstraint(
            ["consumed_transition_event_id"],
            ["inbox_status_transition_events.id"],
            ondelete="RESTRICT",
            name="fk_inbox_completion_override_grants_transition_event",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "grant_idempotency_key",
            name="uq_inbox_completion_override_grants_idempotency",
        ),
        sa.CheckConstraint(
            "(state = 'consumed') = (consumed_at IS NOT NULL)",
            name="ck_inbox_completion_override_grants_consumed_at",
        ),
        sa.CheckConstraint(
            "(consumed_at IS NULL) = (consumed_transition_event_id IS NULL)",
            name="ck_inbox_completion_override_grants_consumed_event",
        ),
        sa.CheckConstraint(
            "expires_at > granted_at",
            name="ck_inbox_completion_override_grants_expiry_after_grant",
        ),
    )
    op.create_index(
        "uq_inbox_completion_override_grants_pending",
        _GRANTS_TABLE,
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index(
        "ix_inbox_completion_override_grants_conversation",
        _GRANTS_TABLE,
        ["conversation_id", "granted_at"],
    )


def _create_cutovers_table() -> None:
    op.create_table(
        _CUTOVERS_TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("marked_conversation_count", sa.Integer(), nullable=False),
        sa.Column("marked_subscriber_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["policy_version_id"],
            [f"{_POLICY_TABLE}.id"],
            ondelete="RESTRICT",
            name="fk_inbox_customer_completion_cutovers_policy_version",
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _initial_policy_created_at(bind: sa.engine.Connection) -> datetime:
    created_at = bind.execute(
        sa.text(f"SELECT created_at FROM {_POLICY_TABLE} WHERE id = :policy_id"),
        {"policy_id": _INITIAL_POLICY_ID},
    ).scalar_one_or_none()
    if created_at is None:
        raise RuntimeError(
            "Cannot backfill the legacy completion-gate marker: the initial "
            f"policy row {_INITIAL_POLICY_ID} is missing from {_POLICY_TABLE}. "
            "Migration 596 must run first."
        )
    return created_at


def _backfill_marker(
    bind: sa.engine.Connection, *, cutover_at: datetime, policy_created_at: datetime
) -> int:
    """Stamp the marker in bounded keyset batches. Returns rows marked.

    Rerun-safe: ``completion_gate_precutover_at IS NULL`` excludes rows a
    prior run already stamped, so a re-run never overwrites an existing
    marker with a new ``cutover_at``. ``created_at < policy_created_at``
    excludes any conversation created after 596 ran (see module docstring).
    """

    conversations = sa.table(
        _CONVERSATION_TABLE,
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column(_POLICY_COLUMN, postgresql.UUID(as_uuid=True)),
        sa.column(_MARKER_COLUMN, sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    marked = 0
    last_id: uuid.UUID | None = None
    while True:
        id_query = (
            sa.select(conversations.c.id)
            .where(
                conversations.c[_POLICY_COLUMN] == _INITIAL_POLICY_ID,
                conversations.c[_MARKER_COLUMN].is_(None),
                conversations.c.created_at < policy_created_at,
            )
            .order_by(conversations.c.id)
            .limit(_BATCH_SIZE)
        )
        if last_id is not None:
            id_query = id_query.where(conversations.c.id > last_id)
        batch_ids = bind.execute(id_query).scalars().all()
        if not batch_ids:
            break
        bind.execute(
            sa.update(conversations)
            .where(conversations.c.id.in_(batch_ids))
            .values(**{_MARKER_COLUMN: cutover_at})
        )
        marked += len(batch_ids)
        last_id = batch_ids[-1]
        if len(batch_ids) < _BATCH_SIZE:
            break
    return marked


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not _has_table(_CONVERSATION_TABLE):
        # No production data to mark on sqlite test databases; mirrors 596's
        # own sqlite short-circuit for the same reason.
        return

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '15min'"))

    if not _has_column(_CONVERSATION_TABLE, _MARKER_COLUMN):
        op.add_column(
            _CONVERSATION_TABLE,
            sa.Column(_MARKER_COLUMN, sa.DateTime(timezone=True), nullable=True),
        )

    if not _has_table(_GRANTS_TABLE):
        _create_grants_table()
    if not _has_table(_CUTOVERS_TABLE):
        _create_cutovers_table()

    _seed_override_permission()

    policy_created_at = _initial_policy_created_at(bind)
    cutover_at = datetime.now(UTC)
    marked_conversation_count = _backfill_marker(
        bind, cutover_at=cutover_at, policy_created_at=policy_created_at
    )
    marked_subscriber_count = bind.execute(
        sa.text(
            f"SELECT COUNT(DISTINCT subscriber_id) FROM {_CONVERSATION_TABLE} "
            f"WHERE {_MARKER_COLUMN} = :cutover_at"
        ),
        {"cutover_at": cutover_at},
    ).scalar_one()

    cutovers = sa.table(
        _CUTOVERS_TABLE,
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("policy_version_id", postgresql.UUID(as_uuid=True)),
        sa.column("cutover_at", sa.DateTime(timezone=True)),
        sa.column("marked_conversation_count", sa.Integer()),
        sa.column("marked_subscriber_count", sa.Integer()),
    )
    # Rerun-safe: exactly one frozen census row per policy version, ever --
    # a re-run after an interruption (or a no-op re-run once everything is
    # already marked) must never insert a second row or overwrite the
    # original counts with a rerun's (likely zero) counts.
    existing_cutover_id = bind.execute(
        sa.select(cutovers.c.id).where(
            cutovers.c.policy_version_id == _INITIAL_POLICY_ID
        )
    ).scalar_one_or_none()
    if existing_cutover_id is None:
        bind.execute(
            cutovers.insert().values(
                id=uuid.uuid4(),
                policy_version_id=_INITIAL_POLICY_ID,
                cutover_at=cutover_at,
                marked_conversation_count=marked_conversation_count,
                marked_subscriber_count=marked_subscriber_count,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not _has_table(_CONVERSATION_TABLE):
        return

    if _has_table(_GRANTS_TABLE):
        consumed_count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {_GRANTS_TABLE} WHERE state = 'consumed'")
        ).scalar_one()
        if consumed_count:
            raise RuntimeError(
                "Refusing downgrade: "
                f"{consumed_count} consumed legacy completion-override "
                "grant(s) exist. A consumed grant is the durable evidence "
                "proving one specific legacy conversation's reviewed, "
                "permissioned resolution; destroying it on downgrade is "
                "worse than a failed downgrade. Resolve or accept this "
                "evidence out-of-band before downgrading."
            )

    _unseed_override_permission()

    if _has_table(_CUTOVERS_TABLE):
        op.drop_table(_CUTOVERS_TABLE)
    if _has_table(_GRANTS_TABLE):
        if _has_index(
            _GRANTS_TABLE, "ix_inbox_completion_override_grants_conversation"
        ):
            op.drop_index(
                "ix_inbox_completion_override_grants_conversation",
                table_name=_GRANTS_TABLE,
            )
        if _has_index(_GRANTS_TABLE, "uq_inbox_completion_override_grants_pending"):
            op.drop_index(
                "uq_inbox_completion_override_grants_pending",
                table_name=_GRANTS_TABLE,
            )
        op.drop_table(_GRANTS_TABLE)
    if _has_column(_CONVERSATION_TABLE, _MARKER_COLUMN):
        op.drop_column(_CONVERSATION_TABLE, _MARKER_COLUMN)
