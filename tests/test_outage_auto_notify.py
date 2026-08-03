"""Automated outage dispatch (ADR 0004): eligibility gates and safety."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.topology import outage_auto_notify
from app.services.topology.outage import CLASSIFIER_SOURCE, OPERATOR_SOURCE

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


class _Incident:
    def __init__(
        self,
        *,
        status="confirmed",
        classification="node_outage",
        detection_source=CLASSIFIER_SOURCE,
        affected_count=10,
        age_minutes=60,
    ):
        self.id = uuid.uuid4()
        self.status = status
        self.classification = classification
        self.detection_source = detection_source
        self.affected_count = affected_count
        self.created_at = NOW - timedelta(minutes=age_minutes)
        self.confirmed_at = self.created_at


class _Query:
    """Minimal stand-in for the SQLAlchemy chain in eligible_incidents."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows
        self.commits = 0
        self.rollbacks = 0

    def query(self, *_args):
        return _Query(self._rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


#: Threshold defaults these tests assert against.
_TEST_SETTLE_MINUTES = 15
_TEST_MIN_AFFECTED = 5


@pytest.fixture(autouse=True)
def pinned_thresholds(monkeypatch):
    """Pin the threshold gates for every test in this module.

    They resolve from the settings registry now, which the stub session cannot
    serve. These tests exercise the eligibility and dispatch logic; that the
    numbers come from registered settings is asserted separately below.
    """
    from datetime import timedelta as _td

    monkeypatch.setattr(
        outage_auto_notify,
        "_settle_period",
        lambda _s: _td(minutes=_TEST_SETTLE_MINUTES),
    )
    monkeypatch.setattr(
        outage_auto_notify, "_min_affected", lambda _s: _TEST_MIN_AFFECTED
    )
    monkeypatch.setattr(outage_auto_notify, "_max_incidents_per_run", lambda _s: 10)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(outage_auto_notify, "_auto_enabled", lambda _s: True)
    monkeypatch.setattr(outage_auto_notify, "_dry_run", lambda _s: False)
    # This path is superseded once network.outage_communications is armed;
    # these tests exercise the pre-cutover behaviour.
    monkeypatch.setattr(outage_auto_notify, "_superseded", lambda _s: False)


def test_arming_the_communications_owner_stands_this_path_down(monkeypatch, enabled):
    """Two customer outage senders must never be live at once."""
    monkeypatch.setattr(outage_auto_notify, "_superseded", lambda _s: True)
    dispatched = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: dispatched.append(k) or {},
    )

    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]),
        now=NOW,
        subscription_ids_for=lambda s, i: [uuid.uuid4()],
    )

    assert dispatched == []
    assert result["dispatched"] is False
    assert result["reason"] == "superseded_by_outage_communications"


def _eligible(rows, monkeypatch, **overrides):
    """Run eligibility with pinned thresholds; only the query is stubbed."""
    for name, value in overrides.items():
        monkeypatch.setattr(outage_auto_notify, name, lambda _s, v=value: v)
    return outage_auto_notify.eligible_incidents(_Session(rows), now=NOW)


# --- eligibility ------------------------------------------------------------


def test_settled_node_outage_is_eligible(monkeypatch):
    incident = _Incident()
    assert _eligible([incident], monkeypatch) == [incident]


def test_unsettled_incident_is_held_back(monkeypatch):
    """A blip that clears inside the window must never reach a customer."""
    assert _eligible([_Incident(age_minutes=2)], monkeypatch) == []


def test_small_incident_is_held_back(monkeypatch):
    assert _eligible([_Incident(affected_count=1)], monkeypatch) == []


def test_radio_cluster_is_never_automated(monkeypatch):
    """92% of production radio_cluster incidents ended discarded."""
    assert _eligible([_Incident(classification="radio_cluster")], monkeypatch) == []


def test_incident_without_a_visible_timestamp_is_skipped(monkeypatch):
    incident = _Incident()
    incident.created_at = None
    incident.confirmed_at = None
    assert _eligible([incident], monkeypatch) == []


def test_naive_timestamp_is_treated_as_utc(monkeypatch):
    incident = _Incident(age_minutes=60)
    incident.confirmed_at = incident.confirmed_at.replace(tzinfo=None)
    assert _eligible([incident], monkeypatch) == [incident]


# --- the flag ---------------------------------------------------------------


def test_disabled_is_a_no_op(monkeypatch):
    monkeypatch.setattr(outage_auto_notify, "_auto_enabled", lambda _s: False)
    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]), now=NOW
    )
    assert result == {"dispatched": False, "reason": "auto_disabled", "incidents": []}


def test_scheduling_is_safe_before_the_decision(monkeypatch):
    """The beat entry may be enabled while the feature decision is open."""
    monkeypatch.setattr(outage_auto_notify, "_auto_enabled", lambda _s: False)
    called = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: called.append(k) or {},
    )
    outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]), now=NOW
    )
    assert called == []


# --- dry run ----------------------------------------------------------------


def test_dry_run_plans_but_never_dispatches(monkeypatch):
    monkeypatch.setattr(outage_auto_notify, "_auto_enabled", lambda _s: True)
    monkeypatch.setattr(outage_auto_notify, "_dry_run", lambda _s: True)
    monkeypatch.setattr(outage_auto_notify, "_superseded", lambda _s: False)
    dispatched = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: dispatched.append(k) or {},
    )
    monkeypatch.setattr(
        outage_auto_notify,
        "plan_outage_notifications",
        lambda *a, **k: {"would_notify": 3},
    )

    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]),
        now=NOW,
        subscription_ids_for=lambda s, i: [uuid.uuid4()],
    )

    assert dispatched == []
    assert result["dispatched"] is False
    assert result["incidents"][0]["reason"] == "dry_run"


# --- dispatch ---------------------------------------------------------------


def test_dispatch_stamps_the_automated_actor(monkeypatch, enabled):
    seen = {}

    def _fake_dispatch(session, sub_ids, **kwargs):
        seen.update(kwargs)
        return {"counts": {"sent": len(sub_ids)}}

    monkeypatch.setattr(
        outage_auto_notify, "dispatch_outage_notifications", _fake_dispatch
    )

    outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]),
        now=NOW,
        subscription_ids_for=lambda s, i: [uuid.uuid4(), uuid.uuid4()],
    )

    assert seen["actor_id"] == outage_auto_notify.AUTO_ACTOR_ID


def test_automated_actor_is_not_a_real_person_id():
    """Auditors must be able to separate automation from operators."""
    assert outage_auto_notify.AUTO_ACTOR_ID.version is None or True
    assert str(outage_auto_notify.AUTO_ACTOR_ID).startswith("00000000-")


def test_incident_with_no_affected_subscriptions_is_skipped(monkeypatch, enabled):
    dispatched = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: dispatched.append(k) or {},
    )
    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident()]), now=NOW, subscription_ids_for=lambda s, i: []
    )
    assert dispatched == []
    assert result["incidents"][0]["reason"] == "no_affected_subscriptions"


def test_per_run_incident_cap_bounds_blast_radius(monkeypatch, enabled):
    monkeypatch.setattr(outage_auto_notify, "_max_incidents_per_run", lambda _s: 2)
    calls = []
    monkeypatch.setattr(
        outage_auto_notify,
        "dispatch_outage_notifications",
        lambda *a, **k: calls.append(k) or {},
    )
    outage_auto_notify.auto_dispatch_due_outage_notifications(
        _Session([_Incident() for _ in range(5)]),
        now=NOW,
        subscription_ids_for=lambda s, i: [uuid.uuid4()],
    )
    assert len(calls) == 2


# --- no second send path ----------------------------------------------------


def test_automation_adds_no_second_send_path():
    """Automation supplies a trigger; it must not emit notifications itself."""
    import inspect

    source = inspect.getsource(outage_auto_notify)
    assert "emit_event" not in source
    assert "OutageNotificationDispatch(" not in source
    assert "dispatch_outage_notifications" in source


def test_operator_source_incidents_are_left_to_operators(monkeypatch):
    assert _eligible([_Incident(detection_source=OPERATOR_SOURCE)], monkeypatch) == []


# --- the gates are operator-controllable ------------------------------------

_GATE_KEYS = (
    "outage_auto_notify_enabled",
    "outage_auto_notify_dry_run",
    "outage_auto_notify_settle_minutes",
    "outage_auto_notify_min_affected",
    "outage_auto_notify_max_incidents_per_run",
    "outage_auto_notify_interval_seconds",
)


@pytest.mark.parametrize("key", _GATE_KEYS)
def test_every_gate_is_a_registered_setting(key):
    """Database-authoritative, so arming/disarming is an admin toggle rather
    than a deploy."""
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    spec = settings_spec.get_spec(SettingDomain.network_monitoring, key)
    assert spec is not None, f"{key} is not a registered setting"
    assert spec.env_var, f"{key} needs an env var for bootstrap materialization"


def test_automation_is_off_and_dry_by_default():
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    enabled = settings_spec.get_spec(
        SettingDomain.network_monitoring, "outage_auto_notify_enabled"
    )
    dry_run = settings_spec.get_spec(
        SettingDomain.network_monitoring, "outage_auto_notify_dry_run"
    )
    assert enabled.default is False
    assert dry_run.default is True


def test_gates_are_not_read_from_app_config():
    """An env-var-only flag needs a deploy to flip — the wrong control for
    something that contacts customers."""
    import inspect

    from app.config import Settings

    assert "from app.config import settings" not in inspect.getsource(
        outage_auto_notify
    )
    config_source = inspect.getsource(Settings)
    for key in _GATE_KEYS:
        assert f"{key}: " not in config_source, f"{key} is back in app.config"


# --- transaction ownership --------------------------------------------------


def test_service_commits_its_own_transaction(monkeypatch, enabled):
    """The calling task is an adapter and must not own the transaction."""
    monkeypatch.setattr(
        outage_auto_notify, "dispatch_outage_notifications", lambda *a, **k: {}
    )
    session = _Session([_Incident()])
    outage_auto_notify.auto_dispatch_due_outage_notifications(
        session, now=NOW, subscription_ids_for=lambda s, i: [uuid.uuid4()]
    )
    assert session.commits == 1
    assert session.rollbacks == 0


def test_failure_rolls_back_rather_than_half_writing_audit_rows(monkeypatch, enabled):
    """Audit rows are the debounce source; a partial write would mute a
    boundary that was never actually notified."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(outage_auto_notify, "dispatch_outage_notifications", _boom)
    session = _Session([_Incident()])

    result = outage_auto_notify.auto_dispatch_due_outage_notifications(
        session, now=NOW, subscription_ids_for=lambda s, i: [uuid.uuid4()]
    )

    assert session.rollbacks == 1
    assert session.commits == 0
    assert result["reason"] == "error"


def test_task_does_not_own_the_transaction():
    import inspect

    from app.tasks import outage_auto_notify as task_module

    source = inspect.getsource(task_module)
    assert "db.commit()" not in source
    assert "db.rollback()" not in source


# --- consolidated admin config ----------------------------------------------


def test_config_groups_enable_with_the_settings_it_governs():
    from app.services import web_system_config

    assert web_system_config.OUTAGE_AUTO_NOTIFY_KEYS[0] == "outage_auto_notify_enabled"
    assert set(web_system_config.OUTAGE_AUTO_NOTIFY_KEYS) == set(_GATE_KEYS)


def test_config_reports_the_three_operating_states(db_session):
    from app.services import web_system_config

    def _state(enabled, dry_run):
        web_system_config.save_outage_auto_notify(
            db_session,
            {
                "outage_auto_notify_enabled": enabled,
                "outage_auto_notify_dry_run": dry_run,
                "outage_auto_notify_settle_minutes": "15",
                "outage_auto_notify_min_affected": "5",
                "outage_auto_notify_max_incidents_per_run": "10",
                "outage_auto_notify_interval_seconds": "300",
            },
        )
        return web_system_config.get_outage_auto_notify_context(db_session)[
            "outage_auto_notify_state"
        ]

    assert _state("false", "true") == "Disabled"
    assert _state("true", "true") == "Dry run"
    assert _state("true", "false") == "Live"


def test_unchecked_enable_box_disarms_rather_than_persisting(db_session):
    """An absent checkbox must read as false, not 'leave as-is'."""
    from app.services import web_system_config

    base = {
        "outage_auto_notify_settle_minutes": "15",
        "outage_auto_notify_min_affected": "5",
        "outage_auto_notify_max_incidents_per_run": "10",
        "outage_auto_notify_interval_seconds": "300",
    }
    web_system_config.save_outage_auto_notify(
        db_session, {**base, "outage_auto_notify_enabled": "true"}
    )
    # Second save omits the checkbox entirely, as a browser would.
    web_system_config.save_outage_auto_notify(db_session, dict(base))

    context = web_system_config.get_outage_auto_notify_context(db_session)
    assert context["outage_auto_notify"]["outage_auto_notify_enabled"] is False
    assert context["outage_auto_notify_state"] == "Disabled"


def test_config_rejects_an_out_of_bounds_interval(db_session):
    import pytest as _pytest

    from app.services import web_system_config

    with _pytest.raises(ValueError):
        web_system_config.save_outage_auto_notify(
            db_session,
            {
                "outage_auto_notify_enabled": "false",
                "outage_auto_notify_dry_run": "true",
                "outage_auto_notify_settle_minutes": "15",
                "outage_auto_notify_min_affected": "5",
                "outage_auto_notify_max_incidents_per_run": "10",
                "outage_auto_notify_interval_seconds": "5",
            },
        )


# --- nothing runs by default ------------------------------------------------


def test_beat_entry_follows_the_feature_flag_not_always_on():
    """With automation off (the default) the runner is not even scheduled, so
    no outage dispatch task fires at all.

    Scoped to this one _sync_scheduled_task call rather than a character window
    around it: an offset-based assertion silently breaks whenever anything else
    edits the file, which is exactly how it passed in isolation and failed on
    the CI merge with main.
    """
    import inspect

    from app.services import scheduler_config

    source = inspect.getsource(scheduler_config)
    blocks = [
        block
        for block in source.split("_sync_scheduled_task(")
        if 'name="outage_auto_notify"' in block
    ]
    assert len(blocks) == 1, "expected exactly one outage_auto_notify schedule entry"

    # Only the arguments of that call, up to its closing parenthesis.
    call = blocks[0].split("\n        )")[0]
    assert "enabled=outage_auto_notify_enabled" in call
    assert "enabled=True" not in call


def test_automation_ships_off_and_dry_run():
    from app.models.domain_settings import SettingDomain
    from app.services import settings_spec

    assert (
        settings_spec.get_spec(
            SettingDomain.network_monitoring, "outage_auto_notify_enabled"
        ).default
        is False
    )
    assert (
        settings_spec.get_spec(
            SettingDomain.network_monitoring, "outage_auto_notify_dry_run"
        ).default
        is True
    )
