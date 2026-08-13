"""The one invalidation owner, and the two halves it is split into.

Three service-level tests used to assert "this function invalidated the cache"
— `module_manager`, `web_system_config`, and five sites in `domain_settings`.
That is a cached projection with ten writers and no owner, and the eleventh
writer is the one that forgets. The listeners on `DomainSetting` are the owner
now, so the coverage lives here rather than being repeated per service.

Exercised directly rather than through a real commit, because the interesting
behaviour is the SPLIT: what is collected at flush, what is dropped at commit,
and what happens to a write that never commits. A session fixture that does or
does not commit would test the fixture.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.domain_settings import (
    DomainSetting,
    SettingDomain,
    _collect_invalidations,
    _flush_invalidations,
)


class _FakeSession:
    """Enough of a session for the listeners: `info`, and the three sets."""

    def __init__(self, new=(), dirty=(), deleted=()):
        self.info: dict[str, object] = {}
        self.new = list(new)
        self.dirty = list(dirty)
        self.deleted = list(deleted)


def _row(domain: str, key: str, tenant_id=None) -> DomainSetting:
    row = DomainSetting()
    row.domain = SettingDomain(domain)
    row.key = key
    row.tenant_id = tenant_id
    return row


@pytest.fixture
def recorded(monkeypatch) -> list[tuple[str, str, object]]:
    """Record what the kernel was asked to invalidate."""

    calls: list[tuple[str, str, object]] = []

    def _fake_invalidate(domain, key, *, scope):
        calls.append((str(domain), key, scope.tenant_id))
        return 1

    import dotmac_kernel.settings_cache as kernel_cache

    monkeypatch.setattr(kernel_cache, "invalidate", _fake_invalidate)
    return calls


def test_a_committed_write_drops_its_entry(recorded) -> None:
    tenant = uuid4()
    session = _FakeSession(dirty=[_row("billing", "default_currency", tenant)])

    _collect_invalidations(session)
    _flush_invalidations(session)

    assert recorded == [("billing", "default_currency", tenant)]


def test_nothing_is_dropped_before_the_commit(recorded) -> None:
    """The reason this is two halves and not one listener.

    Invalidating during the flush leaves a window where the row is not visible
    to other transactions yet: a concurrent reader repopulates the entry with
    the OLD value, the commit lands, and the stale entry outlives the write.
    """

    session = _FakeSession(new=[_row("audit", "enabled")])

    _collect_invalidations(session)

    assert recorded == [], "invalidated at flush — the stale-repopulate window is open"


def test_a_rollback_invalidates_nothing(recorded) -> None:
    """Collected state is discarded with the session, so a write that never
    happened drops nobody's cache entry."""

    session = _FakeSession(new=[_row("audit", "enabled")])
    _collect_invalidations(session)

    session.info.clear()  # what a rollback leaves behind
    _flush_invalidations(session)

    assert recorded == []


def test_a_platform_write_is_scoped_platform(recorded) -> None:
    """`tenant_id IS NULL` means platform, and the kernel widens that to every
    scope — every tenant inherits the platform row when it has none of its own.
    The widening is the kernel's; what this asserts is that Sub reports the
    scope correctly, because reporting it as a tenant write would leave every
    other tenant reading a stale value."""

    session = _FakeSession(dirty=[_row("billing", "default_currency", None)])

    _collect_invalidations(session)
    _flush_invalidations(session)

    assert recorded == [("billing", "default_currency", None)]


def test_deletes_count_as_writes(recorded) -> None:
    """A deleted row changes what resolves — to the next scope, or the default
    — as surely as an updated one."""

    session = _FakeSession(deleted=[_row("audit", "methods")])

    _collect_invalidations(session)
    _flush_invalidations(session)

    assert recorded == [("audit", "methods", None)]


def test_a_non_setting_write_is_ignored(recorded) -> None:
    """The listeners are on the Session, so they see every flush in the app."""

    session = _FakeSession(dirty=[SimpleNamespace(domain="billing", key="nope")])

    _collect_invalidations(session)
    _flush_invalidations(session)

    assert recorded == []


def test_a_failing_invalidation_does_not_raise(monkeypatch) -> None:
    """`after_commit` runs on a write that already succeeded.

    A missed invalidation leaves a stale read until the store's TTL, which is
    bad. Raising here would fail a request whose database work is already
    committed, which is worse and less recoverable.
    """

    import dotmac_kernel.settings_cache as kernel_cache

    def _boom(*_args, **_kwargs):
        raise RuntimeError("redis is down")

    monkeypatch.setattr(kernel_cache, "invalidate", _boom)

    session = _FakeSession(dirty=[_row("billing", "default_currency", uuid4())])
    _collect_invalidations(session)

    _flush_invalidations(session)  # must not raise
