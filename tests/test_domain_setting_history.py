"""Every settings change leaves a record, and a secret's value never does.

`AuditEvent` records that a settings change happened; it has never recorded the
value. "Who turned this off, and what was it before" was unanswerable — the
question asked during an incident, when the answer matters most.

Recorded at the MODEL boundary, so the paths nobody remembered are covered too:
the seed, the generic settings API, the admin surface and several domain
services all write settings rows, and a history covering only the service layer
would read as complete while missing most of them.
"""

from __future__ import annotations

import pytest

from app.models.domain_setting_history import (
    DomainSettingHistory,
    SettingChangeAction,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
from app.schemas.settings import DomainSettingCreate, DomainSettingUpdate
from app.services import domain_settings as domain_settings_service
from app.services import setting_history


def _rows(db_session, key: str) -> list[DomainSettingHistory]:
    return (
        db_session.query(DomainSettingHistory)
        .filter(DomainSettingHistory.key == key)
        .order_by(DomainSettingHistory.changed_at)
        .all()
    )


def _service() -> domain_settings_service.DomainSettings:
    return domain_settings_service.DomainSettings(domain=SettingDomain.gis)


def test_a_created_setting_records_its_first_value(db_session):
    _service().create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="history_probe_create",
            value_type=SettingValueType.integer,
            value_text="30",
        ),
    )

    rows = _rows(db_session, "history_probe_create")
    assert [row.action for row in rows] == [SettingChangeAction.create]
    assert rows[0].value_before is None
    assert rows[0].value_after == "30"
    assert rows[0].secret_changed is False


def test_an_update_records_both_sides_of_the_transition(db_session):
    created = _service().create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="history_probe_update",
            value_type=SettingValueType.integer,
            value_text="30",
        ),
    )
    _service().update(db_session, str(created.id), DomainSettingUpdate(value_text="45"))

    rows = _rows(db_session, "history_probe_update")
    assert [row.action for row in rows] == [
        SettingChangeAction.create,
        SettingChangeAction.update,
    ]
    assert rows[1].value_before == "30"
    assert rows[1].value_after == "45"


def test_a_change_that_is_not_a_value_change_records_nothing(db_session):
    """Deactivating or relabelling is not a value transition.

    Recording one would make the history noisy in exactly the way that stops
    anybody reading it, which is the only way a history table fails silently.
    """

    created = _service().create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="history_probe_quiet",
            value_type=SettingValueType.integer,
            value_text="30",
        ),
    )
    created.is_active = False
    db_session.commit()

    assert [row.action for row in _rows(db_session, "history_probe_quiet")] == [
        SettingChangeAction.create
    ]


def test_a_secret_records_that_it_moved_and_never_what_it_became(db_session):
    """The rule that makes this table safe to keep.

    A history that stored credentials would mean rotating a compromised secret
    leaves the compromised one readable for as long as history is retained —
    the table meant to explain a change becoming the place a leak persists.
    """

    secret = "the-actual-credential"
    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="history_probe_secret",
            value_type=SettingValueType.string,
            value_text=secret,
            is_secret=True,
        )
    )
    db_session.commit()

    rows = _rows(db_session, "history_probe_secret")
    assert len(rows) == 1
    assert rows[0].secret_changed is True
    assert rows[0].value_before is None
    assert rows[0].value_after is None
    assert secret not in str(rows[0].__dict__)


def test_a_write_that_bypasses_the_service_is_still_recorded(db_session):
    """The reason this lives on the model.

    The seed, the generic settings API and several domain services write rows
    without going through `DomainSettings`. A recorder in the service layer
    would miss every one of them and still look complete.
    """

    db_session.add(
        DomainSetting(
            domain=SettingDomain.gis,
            key="history_probe_direct",
            value_type=SettingValueType.string,
            value_text="written-directly",
        )
    )
    db_session.commit()

    assert [row.action for row in _rows(db_session, "history_probe_direct")] == [
        SettingChangeAction.create
    ]


def test_the_actor_is_recorded_when_one_is_named(db_session):
    token = setting_history.set_change_context(
        setting_history.SettingChangeContext(
            reason="raising the sync interval",
            ip_address="203.0.113.7",
            request_id="req-abc123",
        )
    )
    try:
        _service().create(
            db_session,
            DomainSettingCreate(
                domain=SettingDomain.gis,
                key="history_probe_actor",
                value_type=SettingValueType.integer,
                value_text="30",
            ),
        )
    finally:
        setting_history.reset_change_context(token)

    row = _rows(db_session, "history_probe_actor")[0]
    assert row.change_reason == "raising the sync interval"
    assert row.ip_address == "203.0.113.7"
    assert row.request_id == "req-abc123"


def test_no_actor_is_recorded_as_none_rather_than_invented(db_session):
    """A seed, a migration or a CLI genuinely has no actor."""

    _service().create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="history_probe_no_actor",
            value_type=SettingValueType.integer,
            value_text="30",
        ),
    )

    row = _rows(db_session, "history_probe_no_actor")[0]
    assert row.changed_by_party_id is None
    assert row.change_reason is None


def test_history_reads_newest_first(db_session):
    created = _service().create(
        db_session,
        DomainSettingCreate(
            domain=SettingDomain.gis,
            key="history_probe_order",
            value_type=SettingValueType.integer,
            value_text="1",
        ),
    )
    _service().update(db_session, str(created.id), DomainSettingUpdate(value_text="2"))

    rows = setting_history.history_for(
        db_session, SettingDomain.gis, "history_probe_order"
    )

    assert [row.value_after for row in rows][0] == "2"


@pytest.mark.parametrize("action", list(SettingChangeAction))
def test_every_action_the_vocabulary_declares_is_storable(db_session, action):
    """The enum persists by VALUE, matching the migration's CHECK.

    SQLAlchemy stores a Python enum by its NAME unless told otherwise, and that
    mismatch is invisible on write and fatal on read — the defect that took the
    fiber map down in #2279.
    """

    db_session.add(
        DomainSettingHistory(
            domain=SettingDomain.gis,
            key=f"history_probe_action_{action.value}",
            action=action,
        )
    )
    db_session.commit()

    stored = (
        db_session.query(DomainSettingHistory)
        .filter(DomainSettingHistory.key == f"history_probe_action_{action.value}")
        .one()
    )
    assert stored.action is action
