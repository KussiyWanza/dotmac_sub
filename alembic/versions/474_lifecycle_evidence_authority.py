"""Make lifecycle evidence authority explicit and prospectively complete.

Revision ID: 474_lifecycle_evidence_authority
Revises: 473_lead_reseller_ownership
Create Date: 2026-08-04

Revision 468 made rows append-only but defaulted every new insert to trusted
``transition_evidence``. It also used handler ``created_at`` as effective time
and gave retries no database identity. This expand migration adds the missing
admission shape without rewriting an immutable historical row:

* legacy rows receive only ``evidence_source=legacy_unattributed`` and remain
  unsupported by the reader, even if 468 over-graded them;
* raw future inserts default to ``unsupported_observation``;
* trusted rows require source identity, effective/recorded times and a
  fingerprint under database constraints;
* one prospective state baseline is appended for every existing subscription.
  It proves state from the cutover instant forward and claims nothing earlier.

The migration is additive. Downgrade is permitted only before any non-cutover
474-format evidence is written; otherwise forward-fix is required.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "474_lifecycle_evidence_authority"
down_revision: str | None = "473_lead_reseller_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY_FUNCTION = "subscription_lifecycle_events_append_only"
_BASELINE_REASON = "Prospective lifecycle evidence cutover baseline"
_SUBSCRIPTION_FK = "subscription_lifecycle_events_subscription_id_fkey"


def _baseline_fingerprint(
    *, subscription_id: uuid.UUID, status: str, effective_at: datetime, source_id: str
) -> str:
    material = "\0".join(
        (
            str(subscription_id),
            "",
            status,
            effective_at.isoformat(),
            "cutover_baseline",
            "state_baseline",
            source_id,
            _BASELINE_REASON,
        )
    )
    return f"sha256:{hashlib.sha256(material.encode()).hexdigest()}"


def _append_cutover_baselines() -> None:
    bind = op.get_bind()
    cutover_at = datetime.now(UTC)
    subscriptions = bind.execute(
        sa.text("SELECT id, status FROM subscriptions ORDER BY id")
    ).all()
    if not subscriptions:
        return

    payloads: list[dict[str, object]] = []
    for raw_id, raw_status in subscriptions:
        subscription_id = (
            raw_id if isinstance(raw_id, uuid.UUID) else uuid.UUID(str(raw_id))
        )
        status = getattr(raw_status, "value", str(raw_status))
        source_id = f"cutover:474:{subscription_id}"
        payloads.append(
            {
                "id": uuid.uuid4(),
                "subscription_id": subscription_id,
                "event_type": "other",
                "to_status": status,
                "reason": _BASELINE_REASON,
                "actor": "migration:474_lifecycle_evidence_authority",
                "evidence_grade": "state_baseline",
                "evidence_source": "cutover_baseline",
                "source_id": source_id,
                "evidence_fingerprint": _baseline_fingerprint(
                    subscription_id=subscription_id,
                    status=status,
                    effective_at=cutover_at,
                    source_id=source_id,
                ),
                "effective_at": cutover_at,
                "recorded_at": cutover_at,
                "created_at": cutover_at,
            }
        )

    bind.execute(
        sa.text(
            """
            INSERT INTO subscription_lifecycle_events
              (id, subscription_id, event_type, to_status, reason, actor,
               evidence_grade, evidence_source, source_id,
               evidence_fingerprint, effective_at, recorded_at, created_at)
            VALUES
              (:id, :subscription_id, :event_type, :to_status, :reason, :actor,
               :evidence_grade, :evidence_source, :source_id,
               :evidence_fingerprint, :effective_at, :recorded_at, :created_at)
            """
        ),
        payloads,
    )


def upgrade() -> None:
    op.drop_constraint(
        _SUBSCRIPTION_FK,
        "subscription_lifecycle_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        _SUBSCRIPTION_FK,
        "subscription_lifecycle_events",
        "subscriptions",
        ["subscription_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "subscription_lifecycle_events",
        sa.Column(
            "evidence_source",
            sa.String(length=40),
            nullable=False,
            server_default="legacy_unattributed",
        ),
    )
    op.add_column(
        "subscription_lifecycle_events",
        sa.Column("source_id", sa.String(length=160), nullable=True),
    )
    op.add_column(
        "subscription_lifecycle_events",
        sa.Column("evidence_fingerprint", sa.String(length=71), nullable=True),
    )
    op.add_column(
        "subscription_lifecycle_events",
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscription_lifecycle_events",
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.drop_constraint(
        "ck_subscription_lifecycle_events_evidence_grade",
        "subscription_lifecycle_events",
        type_="check",
    )
    op.create_check_constraint(
        "ck_subscription_lifecycle_events_evidence_grade",
        "subscription_lifecycle_events",
        "evidence_grade IN ("
        "'transition_evidence', 'state_baseline', "
        "'unsupported_pre_cutover', 'unsupported_observation')",
    )
    op.create_check_constraint(
        "ck_subscription_lifecycle_events_evidence_source",
        "subscription_lifecycle_events",
        "evidence_source IN ("
        "'lifecycle_command', 'subscription_creation', 'cutover_baseline', "
        "'reconciliation_baseline', 'untrusted_observation', "
        "'legacy_unattributed')",
    )
    op.create_check_constraint(
        "ck_subscription_lifecycle_events_trusted_shape",
        "subscription_lifecycle_events",
        "evidence_source = 'legacy_unattributed' OR "
        "evidence_grade IN ("
        "'unsupported_pre_cutover', 'unsupported_observation') OR ("
        "evidence_grade IN ('transition_evidence', 'state_baseline') AND "
        "evidence_source IN ("
        "'lifecycle_command', 'subscription_creation', 'cutover_baseline', "
        "'reconciliation_baseline') AND "
        "source_id IS NOT NULL AND length(source_id) > 0 AND "
        "evidence_fingerprint IS NOT NULL AND "
        "evidence_fingerprint LIKE 'sha256:%' AND "
        "effective_at IS NOT NULL AND recorded_at IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_subscription_lifecycle_events_source_identity",
        "subscription_lifecycle_events",
        ["evidence_source", "source_id"],
    )
    op.create_index(
        "ix_subscription_lifecycle_events_subscription_effective",
        "subscription_lifecycle_events",
        ["subscription_id", "effective_at"],
    )

    op.alter_column(
        "subscription_lifecycle_events",
        "evidence_grade",
        existing_type=sa.String(length=32),
        server_default="unsupported_observation",
    )
    op.alter_column(
        "subscription_lifecycle_events",
        "evidence_source",
        existing_type=sa.String(length=40),
        server_default="untrusted_observation",
    )
    _append_cutover_baselines()


def _drop_append_only_trigger() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"DROP TRIGGER IF EXISTS trg_{_APPEND_ONLY_FUNCTION} "
        "ON subscription_lifecycle_events"
    )


def _restore_append_only_trigger() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"""
        CREATE TRIGGER trg_{_APPEND_ONLY_FUNCTION}
        BEFORE UPDATE OR DELETE ON subscription_lifecycle_events
        FOR EACH ROW EXECUTE FUNCTION {_APPEND_ONLY_FUNCTION}();
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    non_cutover = bind.execute(
        sa.text(
            "SELECT count(*) FROM subscription_lifecycle_events "
            "WHERE evidence_source NOT IN "
            "('legacy_unattributed', 'cutover_baseline')"
        )
    ).scalar_one()
    if non_cutover:
        raise RuntimeError(
            "474 lifecycle evidence has been written; downgrade requires "
            "a reviewed forward fix"
        )

    _drop_append_only_trigger()
    bind.execute(
        sa.text(
            "DELETE FROM subscription_lifecycle_events "
            "WHERE evidence_source = 'cutover_baseline'"
        )
    )
    op.drop_index(
        "ix_subscription_lifecycle_events_subscription_effective",
        table_name="subscription_lifecycle_events",
    )
    op.drop_constraint(
        "uq_subscription_lifecycle_events_source_identity",
        "subscription_lifecycle_events",
        type_="unique",
    )
    op.drop_constraint(
        "ck_subscription_lifecycle_events_trusted_shape",
        "subscription_lifecycle_events",
        type_="check",
    )
    op.drop_constraint(
        "ck_subscription_lifecycle_events_evidence_source",
        "subscription_lifecycle_events",
        type_="check",
    )
    op.drop_constraint(
        "ck_subscription_lifecycle_events_evidence_grade",
        "subscription_lifecycle_events",
        type_="check",
    )
    for column in (
        "recorded_at",
        "effective_at",
        "evidence_fingerprint",
        "source_id",
        "evidence_source",
    ):
        op.drop_column("subscription_lifecycle_events", column)

    op.create_check_constraint(
        "ck_subscription_lifecycle_events_evidence_grade",
        "subscription_lifecycle_events",
        "evidence_grade IN ('transition_evidence', 'unsupported_pre_cutover')",
    )
    op.alter_column(
        "subscription_lifecycle_events",
        "evidence_grade",
        existing_type=sa.String(length=32),
        server_default="transition_evidence",
    )
    op.drop_constraint(
        _SUBSCRIPTION_FK,
        "subscription_lifecycle_events",
        type_="foreignkey",
    )
    op.create_foreign_key(
        _SUBSCRIPTION_FK,
        "subscription_lifecycle_events",
        "subscriptions",
        ["subscription_id"],
        ["id"],
    )
    _restore_append_only_trigger()
