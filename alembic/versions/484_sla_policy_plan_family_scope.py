"""Add the plan_family scope to sla_policy_versions.

A commercial family (unlimited / dedicated / home_flex) can now carry its own
effective-dated SLA terms. The scope sits below ``offer_version`` and above
``internal_measurement`` in the resolver precedence: an offer that negotiates
its own terms still wins, and a family default outranks the internal
measurement statement.

Unlike the other scopes this one is a closed vocabulary rather than a foreign
key, so it is constrained by value in the database — a direct write cannot
introduce a family the resolver has no way to match.

Revision 467's exclusion and derived-key constraints predate the family scope
and COALESCE only the UUID scopes. Left untouched they would collapse every
family onto one 'global' exclusion key — so two families could not both hold an
active policy — and would derive every family key as ``plan_family:global``,
failing the derived-key CHECK on every family insert. Both are Postgres-only
(``EXCLUDE USING gist``), so a SQLite test lane cannot observe either. This
revision replaces both definitions.

Revision ID: 484_sla_policy_plan_family_scope
Revises: 483_invoice_discount_history
Revises: 481_billing_reconciliation_permissions
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "484_sla_policy_plan_family_scope"
down_revision: str | None = "483_invoice_discount_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE_CHECK = "ck_sla_policy_versions_scope_matches_source"
_VOCAB_CHECK = "ck_sla_policy_versions_plan_family_vocab"
_EXCLUSION_CONSTRAINT = "ex_sla_policy_versions_no_overlap"
_KEY_CONSTRAINT = "ck_sla_policy_versions_key_is_derived"

_SCOPE_WITH_FAMILY = (
    "(source = 'subscription_contract' AND subscription_id IS NOT NULL "
    " AND subscriber_id IS NULL AND offer_id IS NULL AND plan_family IS NULL) "
    "OR (source = 'account_contract' AND subscriber_id IS NOT NULL "
    " AND subscription_id IS NULL AND offer_id IS NULL AND plan_family IS NULL) "
    "OR (source = 'offer_version' AND offer_id IS NOT NULL "
    " AND subscription_id IS NULL AND subscriber_id IS NULL "
    " AND plan_family IS NULL) "
    "OR (source = 'plan_family' AND plan_family IS NOT NULL "
    " AND subscription_id IS NULL AND subscriber_id IS NULL AND offer_id IS NULL) "
    "OR (source = 'internal_measurement' AND subscription_id IS NULL "
    " AND subscriber_id IS NULL AND offer_id IS NULL AND plan_family IS NULL)"
)

_SCOPE_WITHOUT_FAMILY = (
    "(source = 'subscription_contract' AND subscription_id IS NOT NULL "
    " AND subscriber_id IS NULL AND offer_id IS NULL) "
    "OR (source = 'account_contract' AND subscriber_id IS NOT NULL "
    " AND subscription_id IS NULL AND offer_id IS NULL) "
    "OR (source = 'offer_version' AND offer_id IS NOT NULL "
    " AND subscription_id IS NULL AND subscriber_id IS NULL) "
    "OR (source = 'internal_measurement' AND subscription_id IS NULL "
    " AND subscriber_id IS NULL AND offer_id IS NULL)"
)

_ADD_FAMILY_EXCLUSION = """
    ALTER TABLE sla_policy_versions
    ADD CONSTRAINT ex_sla_policy_versions_no_overlap
    EXCLUDE USING gist (
        source WITH =,
        (COALESCE(
            subscription_id::text,
            subscriber_id::text,
            offer_id::text,
            plan_family,
            'global'
        )) WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
"""

_ADD_FAMILY_KEY_CHECK = """
    ALTER TABLE sla_policy_versions
    ADD CONSTRAINT ck_sla_policy_versions_key_is_derived
    CHECK (
        policy_key = source || ':' || COALESCE(
            subscription_id::text,
            subscriber_id::text,
            offer_id::text,
            plan_family,
            'global'
        )
    )
"""

_ADD_ORIGINAL_EXCLUSION = """
    ALTER TABLE sla_policy_versions
    ADD CONSTRAINT ex_sla_policy_versions_no_overlap
    EXCLUDE USING gist (
        source WITH =,
        (COALESCE(
            subscription_id,
            subscriber_id,
            offer_id,
            '00000000-0000-0000-0000-000000000000'::uuid
        )) WITH =,
        tstzrange(effective_from, effective_to, '[)') WITH &&
    )
"""

_ADD_ORIGINAL_KEY_CHECK = """
    ALTER TABLE sla_policy_versions
    ADD CONSTRAINT ck_sla_policy_versions_key_is_derived
    CHECK (
        policy_key = source || ':' || COALESCE(
            subscription_id::text,
            subscriber_id::text,
            offer_id::text,
            'global'
        )
    )
"""


def _drop_postgres_identity_constraints() -> None:
    op.execute(
        f"ALTER TABLE sla_policy_versions DROP CONSTRAINT {_EXCLUSION_CONSTRAINT}"
    )
    op.execute(f"ALTER TABLE sla_policy_versions DROP CONSTRAINT {_KEY_CONSTRAINT}")


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "sla_policy_versions",
        sa.Column("plan_family", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_sla_policy_versions_plan_family",
        "sla_policy_versions",
        ["plan_family"],
    )
    # Widen the scope invariant before anything can write the new source.
    op.drop_constraint(_SCOPE_CHECK, "sla_policy_versions", type_="check")
    op.create_check_constraint(_SCOPE_CHECK, "sla_policy_versions", _SCOPE_WITH_FAMILY)
    op.create_check_constraint(
        _VOCAB_CHECK,
        "sla_policy_versions",
        "plan_family IS NULL OR plan_family IN ('unlimited', 'dedicated', 'home_flex')",
    )
    if bind.dialect.name == "postgresql":
        # Revision 467's two identity constraints know only the UUID scopes.
        # Leaving them untouched would collapse every family to one global
        # exclusion key and would derive every family key as
        # ``plan_family:global``. Replace both atomically with family-aware
        # definitions before this revision can accept writes.
        _drop_postgres_identity_constraints()
        op.execute(_ADD_FAMILY_EXCLUSION)
        op.execute(_ADD_FAMILY_KEY_CHECK)


def downgrade() -> None:
    # Family-scoped versions cannot be represented once the column goes, and
    # this table is append-only contractual history — refuse rather than
    # silently discard what a customer was owed.
    bind = op.get_bind()
    remaining = bind.execute(
        sa.text("SELECT count(*) FROM sla_policy_versions WHERE source = 'plan_family'")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{remaining} plan_family SLA policy version(s) exist; "
            "supersede or migrate them before downgrading."
        )

    if bind.dialect.name == "postgresql":
        _drop_postgres_identity_constraints()
    op.drop_constraint(_VOCAB_CHECK, "sla_policy_versions", type_="check")
    op.drop_constraint(_SCOPE_CHECK, "sla_policy_versions", type_="check")
    op.create_check_constraint(
        _SCOPE_CHECK, "sla_policy_versions", _SCOPE_WITHOUT_FAMILY
    )
    op.drop_index("ix_sla_policy_versions_plan_family", "sla_policy_versions")
    op.drop_column("sla_policy_versions", "plan_family")
    if bind.dialect.name == "postgresql":
        op.execute(_ADD_ORIGINAL_EXCLUSION)
        op.execute(_ADD_ORIGINAL_KEY_CHECK)
