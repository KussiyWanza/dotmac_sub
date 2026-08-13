"""Let a role carry its kernel identity — tenant and slug — without owning it yet

R1 of the roles adoption slice. **Expand only.** It adds two nullable columns,
one FK, two nullable unique keys, one projection CHECK and one index; widens
`name` to the kernel's 120 characters; and gives the existing timestamps the
kernel's server defaults. It removes no legacy column, renames nothing,
backfills nothing, and touches no reader.
`roles.name` and `uq_roles_normalized_name` remain authoritative.

## Why slug is a second column rather than a rename

The kernel's `Role` is keyed `(tenant_id, slug)` with `slug VARCHAR(63)`; before
R1 Sub is keyed on a single global `name VARCHAR(80)` normalized by
`uq_roles_normalized_name`. Those are different identities, not different
spellings of one, so the adoption cannot be a rename:

- Sub's established command contract permits 80 characters, which is greater
  than the slug's 63 even after storage is widened to kernel parity;
- global uniqueness is not per-tenant uniqueness, so two future tenants that may
  each hold `admin` are legal in the kernel and illegal in Sub today.

Carrying both lets `auth.rbac_catalog` — which stays the sole writer — populate
the kernel identity on the same row, in the same statement, while the legacy key
keeps answering every existing reader. The deterministic derivation and its
collision surface are reported by `scripts/roles_slug_collision_report.py`; this
migration deliberately computes nothing.

## The complete projection

`tenant_id` and `slug` are both present or both absent. A role identified in one
half of the kernel key and not the other is not a partial adoption, it is an
unaddressable row: `(NULL, 'admin')` matches no tenant lookup and
`(tenant, NULL)` matches no slug lookup. The nullable unique keys over
`(tenant_id, slug)` and `(tenant_id, id)` are safe across the entire legacy
population because `tenant_id` is NULL there. The first makes a real slug
collision fail closed; the second is the exact parent key kernel
`PartyRoleGrant` needs for its tenant-safe composite foreign key.

## What this deliberately does NOT do

- no NOT NULL, no RLS, no FORCE RLS, no grants;
- no backfill — every existing role keeps both columns NULL until the canonical
  writer sets them or a reviewed, capped command does;
- no lineage-ratchet movement. Kernel revision `0001` remains atomic over roles,
  parties, party_roles, user_credentials and audit_events, and this revision
  moves none of it.

Revision ID: 528_roles_kernel_r1_additive
Revises: 527_credential_party_binding_additive
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "528_roles_kernel_r1_additive"
down_revision: str | None = "527_credential_party_binding_additive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = "roles"

#: Both halves of the kernel identity, or neither. `lower(slug) = slug` is the
#: dialect-neutral half of the kernel's slug rule that SQLite can also enforce;
#: the character-class rule lives in the canonical writer, which is the only
#: thing that may set this column.
_PROJECTION = (
    "(tenant_id IS NULL AND slug IS NULL) OR "
    "(tenant_id IS NOT NULL AND slug IS NOT NULL AND "
    "length(trim(slug)) > 0 AND lower(slug) = slug)"
)


def _matching_fk_name(
    foreign_keys: list[dict[str, object]],
    *,
    name: str,
    columns: list[str],
    referred_table: str,
    ondelete: str,
) -> str | None:
    """Return an exact semantic FK's real name; reject name/shape conflicts.

    Same semantic adoption rule as 527: an earlier repair or composed schema
    may already have installed the exact FK under PostgreSQL's generated name.
    Adopt equivalent semantics, but refuse a familiar name with a different
    shape rather than silently replacing it.
    """

    expected = (columns, referred_table, ["id"], ondelete)
    for foreign_key in foreign_keys:
        options = foreign_key.get("options") or {}
        assert isinstance(options, dict)
        actual = (
            foreign_key.get("constrained_columns"),
            foreign_key.get("referred_table"),
            foreign_key.get("referred_columns"),
            str(options.get("ondelete", "")).upper(),
        )
        if actual == expected:
            actual_name = foreign_key.get("name")
            if not isinstance(actual_name, str) or not actual_name:
                raise RuntimeError(
                    f"Exact {name} semantic FK has no usable database name; "
                    "refusing an unaddressable adoption"
                )
            return actual_name
        if foreign_key.get("name") == name:
            raise RuntimeError(
                f"{name} exists with unexpected definition {actual!r}; refusing "
                "to replace it implicitly"
            )
    return None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(ROLES)}
    for column in (
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=63), nullable=True),
    ):
        if column.name not in columns:
            op.add_column(ROLES, column)

    # These are metadata-only expansions on PostgreSQL: widening VARCHAR does
    # not rewrite values, and adding a `now()` default changes future inserts
    # without touching the existing populated rows. The catalog command keeps
    # its established 80-character validation policy during R1.
    op.alter_column(
        ROLES,
        "name",
        existing_type=sa.String(length=80),
        type_=sa.String(length=120),
        existing_nullable=False,
    )
    for timestamp_column in ("created_at", "updated_at"):
        op.alter_column(
            ROLES,
            timestamp_column,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=sa.func.now(),
        )

    inspector = sa.inspect(bind)
    if (
        _matching_fk_name(
            inspector.get_foreign_keys(ROLES),
            name="fk_roles_tenant",
            columns=["tenant_id"],
            referred_table="tenants",
            ondelete="CASCADE",
        )
        is None
    ):
        op.create_foreign_key(
            "fk_roles_tenant",
            ROLES,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )

    constraints = {item["name"] for item in inspector.get_check_constraints(ROLES)}
    uniques = {item["name"] for item in inspector.get_unique_constraints(ROLES)}
    indexes = {item["name"] for item in inspector.get_indexes(ROLES)}
    if "ck_roles_kernel_identity_projection" not in constraints:
        op.create_check_constraint(
            "ck_roles_kernel_identity_projection", ROLES, _PROJECTION
        )
    if "uq_roles_tenant_slug" not in uniques:
        op.create_unique_constraint(
            "uq_roles_tenant_slug", ROLES, ["tenant_id", "slug"]
        )
    if "uq_roles_tenant_id_id" not in uniques:
        op.create_unique_constraint("uq_roles_tenant_id_id", ROLES, ["tenant_id", "id"])
    if "ix_roles_tenant_id" not in indexes:
        op.create_index("ix_roles_tenant_id", ROLES, ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_roles_tenant_id", table_name=ROLES)
    op.drop_constraint("uq_roles_tenant_id_id", ROLES, type_="unique")
    op.drop_constraint("uq_roles_tenant_slug", ROLES, type_="unique")
    op.drop_constraint("ck_roles_kernel_identity_projection", ROLES, type_="check")
    actual_name = _matching_fk_name(
        sa.inspect(op.get_bind()).get_foreign_keys(ROLES),
        name="fk_roles_tenant",
        columns=["tenant_id"],
        referred_table="tenants",
        ondelete="CASCADE",
    )
    if actual_name is not None:
        op.drop_constraint(actual_name, ROLES, type_="foreignkey")
    for column in ("slug", "tenant_id"):
        op.drop_column(ROLES, column)
    for timestamp_column in ("updated_at", "created_at"):
        op.alter_column(
            ROLES,
            timestamp_column,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=None,
        )
    op.alter_column(
        ROLES,
        "name",
        existing_type=sa.String(length=120),
        type_=sa.String(length=80),
        existing_nullable=False,
    )
