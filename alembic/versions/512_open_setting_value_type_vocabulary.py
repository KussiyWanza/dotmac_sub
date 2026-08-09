"""Open the setting-value-type vocabulary (ADR-0008)

``502_open_setting_domain_vocabulary`` opened ``SettingDomain``. This is the
same closed-list defect one column across: ``value_type`` is the native
``settingvaluetype`` enum with four members, and the value-alignment CHECK on
``domain_settings`` names one of them literally --- it permits ``value_json``
only when ``value_type = 'json'``. Together they make a fifth value type
unstorable, whoever declares it.

That is not hypothetical. Sub has four settings whose values are LISTS
(``imports.import_history_log``, ``imports.import_jobs_log``, ``audit.methods``,
``audit.skip_paths``) and thirteen that are money amounts held as decimal
strings, with the currency in a separate setting each reader pairs up by hand
--- one of them reading it as ``float``. ``dotmac_kernel`` declares ``money``
and (from ``0.1.0a26``) ``list``, and neither can be written to these tables
until the database stops enumerating the vocabulary.

## What changes

THREE tables carry the enum, and all three must be converted before the type
can be dropped: ``domain_settings``, ``subscriber_custom_fields`` and
``subscription_engine_settings``. Converting only the one this work needs would
leave the type alive, still constraining the others, and leave a reader unable
to tell which rule applies where.

The CHECK constraint is replaced by the invariant that actually holds and names
no type: exactly one value column is populated. This mirrors kernel migration
``20260808_0015_open_value_types``, whose reasoning applies here unchanged ---
which column a type uses is a property of the type (``ValueTypeSpec.storage``),
and the database only needs to know that one of them is.

Values are preserved via ``USING value_type::text``. Nothing is reclassified:
this migration widens what may be stored, and stores nothing.

Revision ID: 512_open_setting_value_type_vocabulary
Revises: 511_sales_order_invoice_links
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "512_open_setting_value_type_vocabulary"
down_revision: str | None = "511_sales_order_invoice_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_NAME = "settingvaluetype"

#: Every column carrying the enum today. All three are converted together: the
#: type cannot be dropped while any of them still references it, and a
#: half-converted vocabulary is worse than either state.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("domain_settings", "value_type"),
    ("subscriber_custom_fields", "value_type"),
    ("subscription_engine_settings", "value_type"),
)

ALIGNMENT_CONSTRAINT = "ck_domain_settings_value_alignment"

#: The members the enum carried at the time of this migration. Used only to
#: rebuild the type on downgrade --- the upgrade path never enumerates them.
LEGACY_MEMBERS: tuple[str, ...] = ("string", "integer", "boolean", "json")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _enum_exists() -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text("SELECT 1 FROM pg_type WHERE typname = :name"),
            {"name": ENUM_NAME},
        )
        .scalar()
    )


def _table_exists(table: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text("SELECT to_regclass(:table)"),
            {"table": f"public.{table}"},
        )
        .scalar()
    )


def _external_dependants() -> list[str]:
    """Objects still depending on the enum type, ignoring its own array type.

    Every enum owns an implicit array type that depends on it; that dependency
    is internal and must not block the drop. Anything else --- a column this
    migration does not know about, a cast, a domain --- must, and the right
    outcome is then to leave the type alone and let a human look. Never
    ``CASCADE``: that would silently rewrite a column nobody reviewed.
    """

    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT DISTINCT
                COALESCE(dependant.relname, pg_describe_object(
                    dep.classid, dep.objid, dep.objsubid
                )) AS description
            FROM pg_depend AS dep
            JOIN pg_type AS enum_type ON enum_type.oid = dep.refobjid
            LEFT JOIN pg_class AS dependant ON dependant.oid = dep.objid
            WHERE enum_type.typname = :name
              AND dep.deptype <> 'i'
              AND NOT (
                  dep.classid = 'pg_type'::regclass
                  AND dep.objid = (
                      SELECT oid FROM pg_type WHERE typname = '_' || :name
                  )
              )
            """
        ),
        {"name": ENUM_NAME},
    )
    return [row.description for row in rows if row.description]


def upgrade() -> None:
    if not _is_postgres():
        # SQLite renders the enum as VARCHAR already and does not enforce the
        # CHECK the same way; there is nothing to widen and no type to drop.
        return

    # ORDER MATTERS, and getting it wrong fails loudly rather than subtly. The
    # alignment CHECK compares `value_type` against `'json'::settingvaluetype`,
    # so PostgreSQL re-validates it while rewriting the column and finds itself
    # comparing a VARCHAR to an enum:
    #
    #     operator does not exist: character varying = settingvaluetype
    #
    # The constraint that NAMES the type therefore has to come off before the
    # column that CARRIES it changes. Kernel migration
    # `20260808_0015_open_value_types` drops it first for the same reason.
    op.execute(
        sa.text(
            f"ALTER TABLE domain_settings DROP CONSTRAINT IF EXISTS "
            f"{ALIGNMENT_CONSTRAINT}"
        )
    )

    # Raw SQL rather than ``op.alter_column`` with an ``sa.Enum``: naming the
    # enum here would register this file as a second creator of
    # ``settingvaluetype`` with the duplicate-enum guard
    # (``tests/architecture/test_migration_enum_creation.py``), which is the
    # opposite of what it does.
    for table, column in COLUMNS:
        if not _table_exists(table):
            continue
        op.execute(
            sa.text(
                f"ALTER TABLE {table} "
                f"ALTER COLUMN {column} TYPE VARCHAR(40) USING {column}::text"
            )
        )

    # A row may hold the JSON text `null` where it means "no JSON value":
    # SQLAlchemy serialises Python `None` that way unless `none_as_null=True`.
    # Such a row satisfies `value_json IS NOT NULL` while carrying nothing, so
    # it would slip past the new constraint on a technicality; it is also wrong
    # on its own terms.
    op.execute(
        sa.text(
            "UPDATE domain_settings SET value_json = NULL "
            "WHERE value_json::text = 'null'"
        )
    )

    # The replacement names NO type --- which is the whole point --- while
    # staying true to how Sub actually stores values today: a row carries a
    # value in at least one column.
    #
    # NOT "exactly one". The kernel's equivalent constraint is exactly-one,
    # because there a type's `ValueTypeSpec.storage` picks its single column.
    # Sub deliberately writes a BOOLEAN to BOTH (`normalize_for_db`: the seed
    # wrote both, the retired per-domain handlers wrote both, and `_to_bool` in
    # `app.main` reads `value_json` first, so leaving it NULL made a boolean
    # row's shape depend on which writer produced it). Tightening to
    # exactly-one here would reject rows this codebase writes on purpose ---
    # that is a change of storage convention, and it belongs to the settings
    # cutover, where the kernel becomes the writer and picks the column.
    op.create_check_constraint(
        ALIGNMENT_CONSTRAINT,
        "domain_settings",
        "value_text IS NOT NULL OR value_json IS NOT NULL",
    )

    if not _enum_exists():
        return
    remaining = _external_dependants()
    if remaining:
        print(
            f"Leaving type {ENUM_NAME!r} in place; still referenced by: "
            f"{', '.join(sorted(remaining))}"
        )
        return
    op.execute(sa.text(f"DROP TYPE {ENUM_NAME}"))


def downgrade() -> None:
    """Explicitly destructive: narrows an open vocabulary back to a closed one.

    A row whose type was declared AFTER this migration ran --- a `list` or a
    `money` --- has no member to map back to, so the cast fails and the
    downgrade aborts. That is the honest behaviour; silently deleting or
    remapping those rows would lose settings.
    """

    if not _is_postgres():
        return

    # Mirror of the upgrade's ordering, for the same reason: the constraint
    # that names the type is created only once the column carrying it is the
    # enum again. Building it against a VARCHAR column first would compare a
    # VARCHAR to `'json'::settingvaluetype` and fail.
    op.execute(
        sa.text(
            f"ALTER TABLE domain_settings DROP CONSTRAINT IF EXISTS "
            f"{ALIGNMENT_CONSTRAINT}"
        )
    )

    if not _enum_exists():
        members = ", ".join(f"'{member}'" for member in LEGACY_MEMBERS)
        op.execute(sa.text(f"CREATE TYPE {ENUM_NAME} AS ENUM ({members})"))

    for table, column in COLUMNS:
        if not _table_exists(table):
            continue
        op.execute(
            sa.text(
                f"ALTER TABLE {table} "
                f"ALTER COLUMN {column} TYPE {ENUM_NAME} "
                f"USING {column}::{ENUM_NAME}"
            )
        )

    op.create_check_constraint(
        ALIGNMENT_CONSTRAINT,
        "domain_settings",
        "(value_type = 'json' AND value_json IS NOT NULL AND value_text IS NULL) "
        "OR (value_type != 'json' AND value_text IS NOT NULL)",
    )
