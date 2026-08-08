"""The declaration registry is enforced where rows are WRITTEN.

Every assertion here goes through the ORM on a real session, because that is
the only thing that proves the guard. A test that validates a detached registry
object, or that inserts with raw SQL, proves neither half: the listener is
what rejects the write, and raw SQL bypasses it entirely.

The read direction is asserted too, and matters as much: a row stored under a
domain nobody declares any more must still LOAD. Undeclaring a domain freezes
its rows; it must not make them unreadable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.services.setting_domain_registry import (
    SETTING_DOMAIN_OWNERS,
    UndeclaredSettingDomainError,
)

UNDECLARED = "no_module_declares_this"
RETIRED = "subscription_engine"


def _row(domain: str, key: str) -> DomainSetting:
    return DomainSetting(
        id=uuid.uuid4(),
        domain=SettingDomain(domain),
        key=key,
        value_type=SettingValueType.string,
        value_text="x",
        is_secret=False,
        is_active=True,
    )


def test_a_declared_domain_writes(db_session) -> None:
    db_session.add(_row("auth", "write_boundary_declared"))
    db_session.commit()

    stored = (
        db_session.query(DomainSetting)
        .filter(DomainSetting.key == "write_boundary_declared")
        .one()
    )
    assert stored.domain == SettingDomain.auth
    # `.value` is what ~1,300 call sites carried over from the enum use.
    assert stored.domain.value == "auth"
    assert isinstance(stored.domain, SettingDomain)


def test_an_undeclared_domain_is_rejected_on_insert(db_session) -> None:
    db_session.add(_row(UNDECLARED, "write_boundary_undeclared"))
    with pytest.raises(UndeclaredSettingDomainError):
        db_session.commit()
    db_session.rollback()


def test_the_retired_domain_is_rejected_on_insert(db_session) -> None:
    db_session.add(_row(RETIRED, "write_boundary_retired"))
    with pytest.raises(UndeclaredSettingDomainError):
        db_session.commit()
    db_session.rollback()


def test_an_undeclared_domain_is_rejected_on_update(db_session) -> None:
    """Undeclaring a domain must freeze its rows, not leave them editable."""

    row = _row("auth", "write_boundary_update")
    db_session.add(row)
    db_session.commit()

    row.domain = SettingDomain(UNDECLARED)
    with pytest.raises(UndeclaredSettingDomainError):
        db_session.commit()
    db_session.rollback()


def test_rows_under_an_undeclared_domain_still_read(db_session) -> None:
    """The migration preserves every value, including retired ones."""

    # Raw SQL on purpose — the point is to plant a row the ORM listener would
    # now refuse, the way the migration leaves one behind. `created_at` and
    # `updated_at` are NOT NULL with PYTHON-side defaults, so bypassing the ORM
    # means supplying them here.
    now = datetime.now(UTC)
    db_session.execute(
        text(
            "INSERT INTO domain_settings "
            "(id, domain, key, value_type, value_text, is_secret, is_active, "
            "created_at, updated_at) "
            "VALUES (:id, :domain, :key, :value_type, :value_text, :secret, "
            ":active, :created_at, :updated_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "domain": RETIRED,
            "key": "legacy_row",
            "value_type": SettingValueType.string.value,
            "value_text": "kept",
            "secret": False,
            "active": True,
            "created_at": now,
            "updated_at": now,
        },
    )
    db_session.commit()

    stored = (
        db_session.query(DomainSetting).filter(DomainSetting.key == "legacy_row").one()
    )
    assert stored.domain == RETIRED
    assert stored.domain.value == RETIRED
    assert stored.value_text == "kept"


def test_a_newly_declared_domain_writes_without_touching_the_host_module(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the registry, exercised end to end.

    ``invented`` has no accessor on ``SettingDomain`` and no mention anywhere in
    ``app/models/domain_settings.py``. Declaring it is enough to make it
    writable.
    """

    invented = "write_boundary_invented_domain"
    monkeypatch.setattr(
        "app.services.setting_domain_registry.SETTING_DOMAIN_OWNERS",
        {**SETTING_DOMAIN_OWNERS, invented: "some_owning_domain"},
    )

    db_session.add(_row(invented, "write_boundary_new"))
    db_session.commit()

    stored = (
        db_session.query(DomainSetting)
        .filter(DomainSetting.key == "write_boundary_new")
        .one()
    )
    assert stored.domain == invented
    assert not hasattr(SettingDomain, invented)


def test_equality_not_identity(db_session) -> None:
    """Enum members were interned singletons; the open type is not.

    Pinned because exactly one ``is`` comparison existed in the repository and
    silently became always-false — a comparison that reads fine and is wrong.
    """

    assert SettingDomain("auth") == SettingDomain.auth
    assert SettingDomain("auth") is not SettingDomain.auth
