"""Integration flow tests: whole-module journeys on real PostgreSQL.

These reuse the root conftest's ``engine``/``db_session`` fixtures. The
authoritative integration target is explicit PostgreSQL/PostGIS, created by
the real Alembic chain through ``make test-integration``. SQLite and
``Base.metadata.create_all`` are rejected rather than silently skipped: flow
tests exist to exercise the deployed PG-only surface (JSONB operators, FK
cascades, row locks, migration-owned constraints, indexes and triggers) the
fast unit lane does not claim to represent.

Each test drives a migrated module's NATIVE path end-to-end with its Phase 3
flag ON via ``enable_flags`` — the flag-off write-throughs stay covered by
the unit suite.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models.domain_settings import DomainSetting, SettingDomain, SettingValueType
from app.services import control_registry


@pytest.fixture(autouse=True)
def _require_postgres(engine):
    if engine.dialect.name != "postgresql":
        raise pytest.UsageError(
            "integration flows require migrated PostgreSQL; run "
            "`make test-integration` with TEST_DATABASE_URL pointing at a "
            "disposable test database"
        )


@pytest.fixture
def enable_flags(db_session: Session):
    """Flip Phase 3 controls for one test (rows roll back with the session).

    Controls resolve exclusively from their canonical ``modules.<feature>``
    row (``control_registry._resolve_own_flag`` — retired legacy aliases are
    deliberately ignored), so that is what we write. Accepts either the
    control key ("quotes.native_write") or its legacy setting name
    ("quotes_native_write_enabled") for readability at call sites.
    """

    def _enable(*keys: str) -> None:
        for key in keys:
            control = control_registry._CONTROLS.get(key)
            if control is None:
                # legacy-name convenience: find the control by alias
                dotted = key.removesuffix("_enabled").replace("_native_", ".native_")
                control = control_registry._CONTROLS.get(dotted)
            assert control is not None, f"unknown control for {key!r}"
            db_session.add(
                DomainSetting(
                    domain=SettingDomain.modules,
                    key=control_registry.canonical_setting_key(control),
                    value_type=SettingValueType.boolean,
                    value_text="true",
                    is_active=True,
                )
            )
        db_session.flush()

    return _enable
