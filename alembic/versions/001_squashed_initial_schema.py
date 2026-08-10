"""Squashed initial schema — all tables from models.

Creates the complete database schema directly from SQLAlchemy model
definitions. Replaces ~100+ individual migrations.

Because this base builds from CURRENT model metadata, every later revision runs
against today's model shape, not the shape that existed when it was written.
Removing a type from the models therefore reaches BACKWARDS and breaks the
historical chain. `_recreate_retired_types` below is where that coupling is
paid off — see its docstring.

Revision ID: 001_squashed
Revises:
Create Date: 2026-03-29
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "001_squashed"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The `settingdomain` enum as it stood when revision 501 was the head. It is
#: NOT the model's vocabulary any more — `502_open_setting_domain_vocabulary`
#: retired the type — and this tuple must never grow again. `vas` is here and
#: absent from the models on purpose: `144_vas_wallets` added it to the type and
#: a later change dropped it from the models without rebuilding the type.
_LEGACY_SETTING_DOMAIN_MEMBERS: tuple[str, ...] = (
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


def _recreate_retired_types(conn) -> None:
    """Rebuild types the models no longer declare but the CHAIN still needs.

    `Base.metadata.create_all` above only emits what the models declare TODAY.
    Roughly fifteen revisions between here and 501 name `settingdomain`
    directly — `ALTER TYPE ... ADD VALUE` in 144/225/249, and
    `CAST(:domain AS settingdomain)` in a dozen data migrations — so once
    `502_open_setting_domain_vocabulary` removed the enum from the models, a
    fresh chain run died at 144 with `type "settingdomain" does not exist`.

    Editing fifteen historical revisions to tolerate a type that was real when
    they were written would be rewriting history to accommodate the present.
    Restoring the type here is the honest inverse: a fresh database gets the
    shape those revisions were authored against, replays them unchanged, and
    502 converts the column to text and drops the type at exactly the point in
    history where that happened. A database deployed before 502 never re-runs
    this revision and is unaffected.

    Postgres only — SQLite renders enums as VARCHAR, and the chain's SQLite
    path early-returns from the revisions in question anyway.
    """

    if conn.dialect.name != "postgresql":
        return

    members = ", ".join(f"'{member}'" for member in _LEGACY_SETTING_DOMAIN_MEMBERS)
    conn.execute(
        text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'settingdomain') "
            f"THEN CREATE TYPE settingdomain AS ENUM ({members}); "
            "END IF; END $$;"
        )
    )
    # The column is VARCHAR at this point (the models' current shape). Put it
    # back on the enum so the `CAST(... AS settingdomain)` predicates in the
    # revisions that follow resolve.
    conn.execute(
        text(
            "ALTER TABLE domain_settings ALTER COLUMN domain "
            "TYPE settingdomain USING domain::settingdomain"
        )
    )


def _recreate_unmapped_columns(conn) -> None:
    """Rebuild columns the models no longer declare but the CHAIN still needs.

    Same hazard as `_recreate_retired_types`, one level down. `create_all`
    emits only what the models declare TODAY, so a column removed from a model
    simply never exists in a fresh database — and any earlier revision that
    names it dies. `377_repair_sellable_offer_names` issues
    ``UPDATE catalog_offers ... WHERE splynx_tariff_id = :tariff_id``, so
    unmapping these three in the offer model killed a fresh chain run there.

    These columns are unmapped, not dropped: every deployed database still has
    them and their data, because their downgrade path could not restore the
    values (the precedent set when the `throttle_rate_mbps` drop was deferred
    out of 7.135). Recreating them here makes a fresh database match that
    reality instead of diverging from it, and lets the historical revision
    replay exactly as written. When the DROP eventually lands as its own
    reviewed change, it removes them at the correct point in history and this
    helper goes with it.
    """

    if conn.dialect.name != "postgresql":
        return

    for column, ddl_type in (
        ("splynx_tariff_id", "INTEGER"),
        ("splynx_service_name", "VARCHAR(160)"),
        ("splynx_tax_id", "INTEGER"),
    ):
        conn.execute(
            text(
                "ALTER TABLE catalog_offers "
                f"ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            )
        )


def upgrade() -> None:
    conn = op.get_bind()

    # Create required extensions
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis_topology"))
    conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    conn.execute(text("COMMIT"))

    # Import all models so Base.metadata knows about them
    import app.models  # noqa: F401
    from app.db import Base

    # Create all tables from the model definitions
    Base.metadata.create_all(conn.engine)

    _recreate_retired_types(conn)
    _recreate_unmapped_columns(conn)


def downgrade() -> None:
    raise RuntimeError(
        "Cannot downgrade squashed initial migration. Restore from backup instead."
    )
