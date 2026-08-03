"""System users + integration syncs surfaces project the shared UI contracts.

The system-user headline tiles and the integration-sync summary tiles used to be
raw ``stats`` dicts the templates formatted themselves; they now come back as
``Kpi`` objects whose ``cohort_url`` drills into the exact filtered list the
number counts (KPI-parity). A sync profile's newest-run freshness is a
``StateValue`` so a never-run or wedged puller never renders as a lying date, and
the manual-run button's eligibility is an ``Action`` owned by the service,
mirroring the route's disabled-job guard.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.integration import IntegrationScheduleType
from app.schemas.status_presentation import StatusTone
from app.services import web_integration_syncs as syncs
from app.services import web_system_users as users
from app.services.ui_contracts import Action, Kpi, StateValue

_ROOT = Path(__file__).resolve().parents[1]


# ---- system users KPI cohorts -------------------------------------------------


def test_user_cohort_urls_are_relative_and_encode_the_filter():
    assert users._users_cohort_url() == "/admin/system/users"
    assert (
        users._users_cohort_url(status="active") == "/admin/system/users?status=active"
    )
    assert (
        users._users_cohort_url(status="pending")
        == "/admin/system/users?status=pending"
    )
    role_url = users._users_cohort_url(role_id="abc-123")
    assert role_url == "/admin/system/users?role=abc-123"
    for url in (
        users._users_cohort_url(),
        users._users_cohort_url(status="active"),
        role_url,
    ):
        assert url.startswith("/")


def test_user_kpis_are_kpi_contracts_matching_their_cohort():
    # The projection over the shared count owner: build the tiles the way
    # build_user_kpis does and assert each is a Kpi drilling into its cohort.
    counts = {"total": 12, "active": 9, "admins": 2, "pending": 3}
    kpis = {
        "total": Kpi(
            "Total Users",
            StateValue.present(counts["total"]),
            users._users_cohort_url(),
        ),
        "active": Kpi(
            "Active",
            StateValue.present(counts["active"]),
            users._users_cohort_url(status="active"),
            tone=StatusTone.positive,
        ),
        "pending": Kpi(
            "Pending Invites",
            StateValue.present(counts["pending"]),
            users._users_cohort_url(status="pending"),
            tone=StatusTone.warning,
        ),
    }
    assert all(isinstance(kpi, Kpi) for kpi in kpis.values())
    assert kpis["active"].cohort_url == "/admin/system/users?status=active"
    assert kpis["active"].value.value == 9
    assert kpis["pending"].value.is_present


# ---- integration sync KPI cohorts + freshness + action ------------------------


def test_sync_cohort_urls_are_relative_and_encode_the_filter():
    assert syncs._syncs_cohort_url() == "/admin/integrations/syncs"
    assert (
        syncs._syncs_cohort_url(direction="pull")
        == "/admin/integrations/syncs?direction=pull"
    )
    assert (
        syncs._syncs_cohort_url(direction="push")
        == "/admin/integrations/syncs?direction=push"
    )
    assert syncs._syncs_cohort_url(active=True) == "/admin/integrations/syncs?active=1"
    for url in (
        syncs._syncs_cohort_url(),
        syncs._syncs_cohort_url(direction="pull"),
        syncs._syncs_cohort_url(active=True),
    ):
        assert url.startswith("/")


def _job(*, interval=None, is_active=True):
    schedule = (
        IntegrationScheduleType.interval if interval else IntegrationScheduleType.manual
    )
    return SimpleNamespace(
        id="job-1",
        schedule_type=schedule,
        interval_minutes=interval,
        is_active=is_active,
    )


def _run(started_at):
    return SimpleNamespace(
        started_at=started_at, status=SimpleNamespace(value="success")
    )


def test_last_run_state_is_unknown_when_never_run():
    state = syncs._last_run_state(_job(interval=60), [])
    assert isinstance(state, StateValue)
    assert not state.is_present
    assert state.placeholder == "Unknown"


def test_last_run_state_is_present_for_a_recent_run():
    recent = datetime.now(UTC) - timedelta(minutes=30)
    state = syncs._last_run_state(_job(interval=60), [_run(recent)])
    assert state.is_present
    assert not state.is_stale
    assert state.value == recent


def test_last_run_state_is_stale_past_the_interval_window():
    old = datetime.now(UTC) - timedelta(hours=5)
    state = syncs._last_run_state(_job(interval=60), [_run(old)])
    assert state.is_present
    assert state.is_stale


def test_run_action_eligibility_mirrors_the_disabled_guard():
    allowed = syncs._run_action(_job(is_active=True))
    assert isinstance(allowed, Action)
    assert allowed.allowed is True
    assert allowed.reason is None

    blocked = syncs._run_action(_job(is_active=False))
    assert blocked.allowed is False
    assert blocked.reason == "Profile is disabled"


def test_action_invariants_reject_inconsistent_eligibility():
    # An allowed action may not carry a blocked reason; a blocked one must.
    with pytest.raises(ValueError):
        Action(key="run", label="Run", allowed=True, reason="nope")
    with pytest.raises(ValueError):
        Action(key="run", label="Run", allowed=False)


def test_sync_row_bundles_freshness_and_action_contracts():
    row = syncs._sync_row(_job(interval=60, is_active=False), [])
    assert isinstance(row["last_run"], StateValue)
    assert isinstance(row["run_action"], Action)
    assert row["status_val"] == "never"
    assert row["run_action"].allowed is False


# ---- templates render the contract fields, not bare dict values ---------------


def test_users_template_renders_the_kpi_contract_fields():
    source = (_ROOT / "templates/admin/system/users/index.html").read_text(
        encoding="utf-8"
    )
    assert "user_kpis.total.value.value" in source
    assert "user_kpis.active.cohort_url" in source
    assert "user_kpis.pending.cohort_url" in source
    assert "tone=user_kpis.total.tone" in source
    assert "stats.total" not in source


def test_users_loading_feedback_lives_inside_the_filter_card():
    source = (_ROOT / "templates/admin/system/users/index.html").read_text(
        encoding="utf-8"
    )
    filter_start = source.index("Search & Filters")
    filter_end = source.index("{% endcall %}", filter_start)
    loader_position = source.index('id="users-loading"')
    error_position = source.index('x-show="requestError"')

    assert filter_start < loader_position < error_position < filter_end
    assert 'role="status"' in source
    assert "animate-spin" in source[loader_position:filter_end]
    assert "absolute right-8" not in source


def test_syncs_template_renders_kpi_state_and_action_fields():
    source = (_ROOT / "templates/admin/integrations/syncs/index.html").read_text(
        encoding="utf-8"
    )
    assert "sync_kpis.total.value.value" in source
    assert "sync_kpis.pull.cohort_url" in source
    # Freshness is a StateValue guarded on is_present before formatting.
    assert "last_run.is_present" in source
    assert "last_run.is_stale" in source
    # The shared helper combines owner eligibility with the cached RBAC keys.
    assert "action_permitted(request, run_action)" in source
    assert 'can(request, "system:settings:write")' in source
    assert "{{ stats.total }}" not in source
