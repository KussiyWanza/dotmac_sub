"""Tie every tenant-owned domain setting to its tenant.

Migration 514 already owns the safe scope contract that kernel 0.1.0a40 now
adopts: ``scope_kind`` defaults to ``platform`` for raw writes, and
``ck_domain_settings_scope_alignment`` rejects a scope kind that disagrees with
``tenant_id``.  Neither fact changes here.

The remaining kernel-shape delta is referential integrity.  Migration 508
created ``tenants`` and 509 moved every existing setting to the operator tenant,
but ``domain_settings.tenant_id`` still has no foreign key.  Add only the
kernel's ``ON DELETE CASCADE`` relationship.  A pre-existing exact constraint
is adopted so an integration database that already received the same FK can be
reconciled without duplicate DDL; a same-named constraint with any other shape
fails closed.

The DDL validates all existing rows.  An orphan therefore aborts rather than
being deleted or re-attributed implicitly.  Alembic's configured lock timeout
and the deployment migration retry own lock acquisition.  Downgrade removes
only this FK and changes no rows, defaults, or CHECK constraints.

Revision ID: 523_domain_settings_tenant_fk
Revises: 522_ont_service_configuration_lifecycle
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "523_domain_settings_tenant_fk"
down_revision: str | None = "522_ont_service_configuration_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "domain_settings"
FK_NAME = "fk_domain_settings_tenant"
TENANT_TABLE = "tenants"


def _fk_exists() -> bool:
    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys(TABLE):
        if foreign_key.get("name") != FK_NAME:
            continue

        options = foreign_key.get("options") or {}
        actual = (
            foreign_key.get("constrained_columns"),
            foreign_key.get("referred_table"),
            foreign_key.get("referred_columns"),
            str(options.get("ondelete", "")).upper(),
        )
        expected = (["tenant_id"], TENANT_TABLE, ["id"], "CASCADE")
        if actual != expected:
            raise RuntimeError(
                f"{FK_NAME} exists with an unexpected definition: {actual!r}; "
                "refusing to adopt or replace it implicitly"
            )
        return True
    return False


def upgrade() -> None:
    if not _fk_exists():
        op.create_foreign_key(
            FK_NAME,
            TABLE,
            TENANT_TABLE,
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    if _fk_exists():
        op.drop_constraint(FK_NAME, TABLE, type_="foreignkey")
