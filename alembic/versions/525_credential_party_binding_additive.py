"""Let a credential name the Party it authenticates, and the binding that proves it

R1 of the Party/principal adoption slice. **Additive only.** It adds columns and
one table, removes no legacy column, binds no row and touches no reader. The
legacy `subscriber_id`/`system_user_id`/`reseller_user_id` principal FKs and
their exactly-one CHECK are untouched and remain authoritative.

## Why a binding table rather than a `provider` column

ADR-0019 (starter) rules that a credential authenticates a **Party**, and may
repeat only per **authentication mechanism** — never per principal kind, portal,
account or membership. The discriminator is therefore "which mechanism proved
you are this party", and it must be the installed **binding**, not the mechanism
*code*: two OIDC issuers, or two RADIUS verifiers, are two bindings of one code,
and a code-keyed constraint would forbid a party holding a credential against
each.

Measured on production 2026-08-12: Sub has exactly one binding per mechanism
today — `local` (4,273 of 4,273 credentials), one `radius_servers` row with zero
credentials pointing at it, and `AuthProvider.sso` implemented nowhere. So a
code-keyed constraint would work *today*, by accident of configuration rather
than by structure. This table costs two rows now and is expensive to retrofit
after a uniqueness constraint ships.

`mechanism_code` is a plain string on purpose (ADR-0008): the vocabulary is open
and declared, so a provider module can add a mechanism without changing a host
enum or database CHECK. `binding_key` is the immutable, deployment-global
configuration identity; `name` is only an operator-facing label. The typed
contract lives behind the binding, in the provider owner — which is why nothing
here has a `radius_server_id` column.

## The complete projection

Party, authentication binding, tenant and the evidence quartet are all present
or all absent. A partial projection cannot be persisted. The nullable unique
key on `(tenant_id, party_id, authentication_binding_id)` is safe over the
legacy population because all three columns are NULL, and makes the first
future collision fail closed rather than admitting two credentials for the
same Party and verifier.

## What this deliberately does NOT do

- no NOT NULL, no RLS, no legacy-column removal;
- no backfill. Production has 4,102 unbound credential-bearing principals; they
  are bound by the reviewed command, in approved capped batches, never by DDL.

Revision ID: 525_credential_party_binding_additive
Revises: 524_audit_events_kernel_r1
Create Date: 2026-08-12
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa

from alembic import op

revision: str = "525_credential_party_binding_additive"
down_revision: str | None = "524_audit_events_kernel_r1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BINDINGS = "authentication_bindings"
CREDENTIALS = "user_credentials"
IDENTITY_FUNCTION = "prevent_authentication_binding_identity_change"
IDENTITY_TRIGGER = "trg_authentication_binding_identity_immutable"
_SEEDED_AT = datetime(2026, 8, 12, tzinfo=UTC)

#: Seeded from the owner declarations. ``sso`` is deliberately absent: the
#: legacy compatibility enum contains it but no SOT owner declares or implements
#: it. The deterministic ids make a restored/rehearsed seed byte-identical.
_SEED = (
    (
        UUID("73271924-4749-5762-b53c-0bfff4e914ff"),
        "local.default",
        "local",
        "Local password",
        "Password verified against the stored hash.",
    ),
    (
        UUID("f31a94fb-2409-5c9c-8776-c6a7bb6fee15"),
        "radius.default",
        "radius",
        "RADIUS",
        "Password verified by the configured RADIUS authority.",
    ),
)

_PROJECTION = (
    "(party_id IS NULL AND authentication_binding_id IS NULL AND "
    "tenant_id IS NULL AND party_bound_at IS NULL AND "
    "party_binding_source IS NULL AND party_binding_reason IS NULL) OR "
    "(party_id IS NOT NULL AND authentication_binding_id IS NOT NULL AND "
    "tenant_id IS NOT NULL AND party_bound_at IS NOT NULL AND "
    "party_binding_source IS NOT NULL AND party_binding_reason IS NOT NULL AND "
    "length(trim(party_binding_source)) > 0 AND "
    "length(trim(party_binding_reason)) > 0)"
)


def _matching_fk_name(
    foreign_keys: list[dict[str, object]],
    *,
    name: str,
    columns: list[str],
    referred_table: str,
    ondelete: str,
) -> str | None:
    """Return an exact semantic FK's real name; reject name/shape conflicts."""

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
    tables = set(inspector.get_table_names())
    if BINDINGS not in tables:
        op.create_table(
            BINDINGS,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("binding_key", sa.String(length=80), nullable=False),
            sa.Column("mechanism_code", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("binding_key", name="uq_auth_bindings_binding_key"),
            sa.CheckConstraint(
                "length(trim(binding_key)) > 0",
                name="ck_auth_bindings_binding_key_nonempty",
            ),
            sa.CheckConstraint(
                "length(trim(mechanism_code)) > 0",
                name="ck_auth_bindings_code_nonempty",
            ),
        )
        op.create_index(
            "ix_auth_bindings_code_active",
            BINDINGS,
            ["mechanism_code", "is_active"],
        )

    bindings = sa.table(
        BINDINGS,
        sa.column("id", sa.Uuid()),
        sa.column("binding_key", sa.String()),
        sa.column("mechanism_code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        # Listed and supplied explicitly below. `op.bulk_insert` emits an INSERT
        # naming only the columns it is handed, so the server default never
        # fires: omit `is_active` and it inserts NULL against a NOT NULL column.
        # PostgreSQL is the only place that surfaces this.
        sa.column("is_active", sa.Boolean()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    seed_rows = [
        {
            "id": binding_id,
            "binding_key": binding_key,
            "mechanism_code": code,
            "name": name,
            "description": description,
            "is_active": True,
            "created_at": _SEEDED_AT,
            "updated_at": _SEEDED_AT,
        }
        for binding_id, binding_key, code, name, description in _SEED
    ]
    for seed in seed_rows:
        exists = bind.scalar(
            sa.select(sa.literal(1))
            .select_from(bindings)
            .where(bindings.c.binding_key == seed["binding_key"])
        )
        if exists is None:
            op.bulk_insert(bindings, [seed])

    if bind.dialect.name == "postgresql":
        op.execute(
            f"""
            CREATE OR REPLACE FUNCTION {IDENTITY_FUNCTION}()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.binding_key IS DISTINCT FROM OLD.binding_key
                   OR NEW.mechanism_code IS DISTINCT FROM OLD.mechanism_code THEN
                    RAISE EXCEPTION
                        'authentication binding identity is immutable'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(f"DROP TRIGGER IF EXISTS {IDENTITY_TRIGGER} ON {BINDINGS}")
        op.execute(
            f"""
            CREATE TRIGGER {IDENTITY_TRIGGER}
            BEFORE UPDATE OF binding_key, mechanism_code ON {BINDINGS}
            FOR EACH ROW EXECUTE FUNCTION {IDENTITY_FUNCTION}()
            """
        )

    credential_columns = {
        column["name"] for column in inspector.get_columns(CREDENTIALS)
    }
    additions = (
        sa.Column("party_id", sa.Uuid(), nullable=True),
        sa.Column("party_bound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("party_binding_source", sa.String(length=80), nullable=True),
        sa.Column("party_binding_reason", sa.Text(), nullable=True),
        sa.Column("authentication_binding_id", sa.Uuid(), nullable=True),
        # Nullable until the later RLS/GUC cutover hardens the table.
        sa.Column("tenant_id", sa.Uuid(), nullable=True),
    )
    for column in additions:
        if column.name not in credential_columns:
            op.add_column(CREDENTIALS, column)

    # Refresh after DDL: a squashed fresh install already has today's model
    # shape, while a deployed 522 database has none of these objects.
    inspector = sa.inspect(bind)
    foreign_keys = inspector.get_foreign_keys(CREDENTIALS)
    if (
        _matching_fk_name(
            foreign_keys,
            name="fk_user_credentials_party",
            columns=["party_id"],
            referred_table="parties",
            ondelete="RESTRICT",
        )
        is None
    ):
        op.create_foreign_key(
            "fk_user_credentials_party",
            CREDENTIALS,
            "parties",
            ["party_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if (
        _matching_fk_name(
            foreign_keys,
            name="fk_user_credentials_auth_binding",
            columns=["authentication_binding_id"],
            referred_table=BINDINGS,
            ondelete="RESTRICT",
        )
        is None
    ):
        op.create_foreign_key(
            "fk_user_credentials_auth_binding",
            CREDENTIALS,
            BINDINGS,
            ["authentication_binding_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    if (
        _matching_fk_name(
            foreign_keys,
            name="fk_user_credentials_tenant",
            columns=["tenant_id"],
            referred_table="tenants",
            ondelete="RESTRICT",
        )
        is None
    ):
        op.create_foreign_key(
            "fk_user_credentials_tenant",
            CREDENTIALS,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    constraints = {
        item["name"] for item in inspector.get_check_constraints(CREDENTIALS)
    }
    uniques = {item["name"] for item in inspector.get_unique_constraints(CREDENTIALS)}
    indexes = {item["name"] for item in inspector.get_indexes(CREDENTIALS)}
    if "ck_user_credentials_party_binding_projection" not in constraints:
        op.create_check_constraint(
            "ck_user_credentials_party_binding_projection", CREDENTIALS, _PROJECTION
        )
    if "uq_user_credentials_tenant_party_auth_binding" not in uniques:
        op.create_unique_constraint(
            "uq_user_credentials_tenant_party_auth_binding",
            CREDENTIALS,
            ["tenant_id", "party_id", "authentication_binding_id"],
        )
    if "ix_user_credentials_party_id" not in indexes:
        op.create_index("ix_user_credentials_party_id", CREDENTIALS, ["party_id"])
    if "ix_user_credentials_auth_binding" not in indexes:
        op.create_index(
            "ix_user_credentials_auth_binding",
            CREDENTIALS,
            ["authentication_binding_id"],
        )


def downgrade() -> None:
    op.drop_index("ix_user_credentials_auth_binding", table_name=CREDENTIALS)
    op.drop_index("ix_user_credentials_party_id", table_name=CREDENTIALS)
    op.drop_constraint(
        "uq_user_credentials_tenant_party_auth_binding",
        CREDENTIALS,
        type_="unique",
    )
    op.drop_constraint(
        "ck_user_credentials_party_binding_projection", CREDENTIALS, type_="check"
    )
    # A squashed fresh install creates equivalent model FKs before this
    # incremental revision runs, and PostgreSQL assigns those constraints its
    # own names. Upgrade deliberately adopts their semantics. Downgrade must
    # therefore drop the names actually present, not assume only the names
    # created on a deployed-523 upgrade can exist.
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys(CREDENTIALS)
    for canonical_name, columns, referred_table in (
        ("fk_user_credentials_tenant", ["tenant_id"], "tenants"),
        (
            "fk_user_credentials_auth_binding",
            ["authentication_binding_id"],
            BINDINGS,
        ),
        ("fk_user_credentials_party", ["party_id"], "parties"),
    ):
        actual_name = _matching_fk_name(
            foreign_keys,
            name=canonical_name,
            columns=columns,
            referred_table=referred_table,
            ondelete="RESTRICT",
        )
        if actual_name is not None:
            op.drop_constraint(actual_name, CREDENTIALS, type_="foreignkey")
    for column in (
        "tenant_id",
        "authentication_binding_id",
        "party_binding_reason",
        "party_binding_source",
        "party_bound_at",
        "party_id",
    ):
        op.drop_column(CREDENTIALS, column)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP TRIGGER IF EXISTS {IDENTITY_TRIGGER} ON {BINDINGS}")
        op.execute(f"DROP FUNCTION IF EXISTS {IDENTITY_FUNCTION}()")
    op.drop_index("ix_auth_bindings_code_active", table_name=BINDINGS)
    op.drop_table(BINDINGS)
