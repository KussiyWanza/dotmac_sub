"""Open the setting-domain vocabulary (ADR-0008)

Turns ``domain_settings.domain`` from the native ``settingdomain`` PostgreSQL
enum into ``VARCHAR(120)``, so that adding a setting domain is a declaration by
its owning SOT domain rather than an ``ALTER TYPE ... ADD VALUE`` migration in
this hosting layer. Three migrations in this repository exist only to add a
member on some module's behalf — ``144_vas_wallets``,
``225_add_field_setting_domain`` and ``249_field_erp_sync_outbox``; this is the
last of that class.

Every existing value is preserved (``USING domain::text``) — including rows
under ``subscription_engine``, the one domain that is deliberately NOT declared
going forward. Those rows keep reading; they simply become unwritable, which is
the intended outcome for a dead domain.

The enum TYPE is dropped only after ``pg_depend`` proves nothing else depends
on it. Never ``CASCADE``: if some column this migration does not know about
still uses the type, the correct outcome is to leave the type in place and let
a human look, not to silently rewrite that column.

Revision ID: 502_open_setting_domain_vocabulary
Revises: 501_retire_allowance_throttle_rate
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "502_open_setting_domain_vocabulary"
down_revision: str | None = "501_retire_allowance_throttle_rate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUM_NAME = "settingdomain"

#: The members the enum carried at the time of this migration. Only used to
#: rebuild the type on downgrade — the upgrade path never enumerates domains.
#:
#: This is the DEPLOYED type's membership, which is not the same as the model's
#: accessors: `vas` was added by `144_vas_wallets` and later dropped from the
#: model without being removed from the type (PostgreSQL cannot remove an enum
#: member without rebuilding). Omitting it here would make a downgrade fail on
#: any surviving `vas` row.
LEGACY_MEMBERS: tuple[str, ...] = (
    "auth",
    "audit",
    "billing",
    "catalog",
    "subscriber",
    "imports",
    "notification",
    "network",
    "network_monitoring",
    "provisioning",
    "geocoding",
    "usage",
    "radius",
    "collections",
    "lifecycle",
    "projects",
    "workflow",
    "modules",
    "inventory",
    "comms",
    "tr069",
    "snmp",
    "bandwidth",
    "subscription_engine",
    "gis",
    "scheduler",
    "field",
    "integration",
    "vas",
)


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


def _external_dependants() -> list[str]:
    """Objects still depending on the enum type, ignoring its own array type.

    Every enum in PostgreSQL owns an implicit array type that depends on it;
    that dependency is internal and must not block the drop. Anything else —
    another table's column, a cast, a domain — must.
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
        # SQLite renders the enum as VARCHAR already; there is nothing to widen
        # and no type to drop.
        return

    # Raw SQL rather than ``op.alter_column`` with an ``sa.Enum`` type: naming
    # the enum in ``upgrade`` would register this file as a second creator of
    # ``settingdomain`` with the duplicate-enum guard
    # (``tests/architecture/test_migration_enum_creation.py``), which is the
    # opposite of what it does.
    op.execute(
        sa.text(
            "ALTER TABLE domain_settings "
            "ALTER COLUMN domain TYPE VARCHAR(120) USING domain::text"
        )
    )

    if not _enum_exists():
        return
    remaining = _external_dependants()
    if remaining:
        # Leave the type in place rather than CASCADE through an unknown
        # dependant. The column change above is the part that matters.
        print(
            f"Leaving type {ENUM_NAME!r} in place; still referenced by: "
            f"{', '.join(sorted(remaining))}"
        )
        return
    op.execute(sa.text(f"DROP TYPE {ENUM_NAME}"))


def downgrade() -> None:
    """Explicitly destructive: narrows an open vocabulary back to a closed one.

    Any row whose domain was declared AFTER this migration ran has no member to
    map back to, so the cast fails and the downgrade aborts. That is the honest
    behaviour — silently deleting or remapping those rows would lose settings.
    """

    if not _is_postgres():
        return

    if not _enum_exists():
        members = ", ".join(f"'{member}'" for member in LEGACY_MEMBERS)
        op.execute(sa.text(f"CREATE TYPE {ENUM_NAME} AS ENUM ({members})"))

    op.execute(
        sa.text(
            "ALTER TABLE domain_settings "
            f"ALTER COLUMN domain TYPE {ENUM_NAME} USING domain::{ENUM_NAME}"
        )
    )
