"""Real evidence on the integrations surfaces (audit P-C).

The UI must expose the last observation and whether it requires attention,
without collapsing installation state and runtime evidence into a health word.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.templating import Jinja2Templates

from app.models.auth import ApiKey
from app.models.integration import (
    IntegrationJob,
    IntegrationRun,
    IntegrationRunStatus,
    IntegrationTarget,
)
from app.models.integration_platform import IntegrationInstallation
from app.services import web_integrations
from app.services.auth import hash_api_key
from app.services.auth_dependencies import require_user_auth
from app.web.brand_globals import install_brand_jinja_global
from tests.integration_platform_helpers import enable_capability


def _make_installation(db, name: str | None = None) -> IntegrationInstallation:
    binding = enable_capability(
        db,
        connector_key="webhook.http",
        capability_id="events.deliver.v1",
        config={"url": "https://webhook.example.test/events"},
        secret_refs={},
        policy={"approved_egress_hosts": ["webhook.example.test"]},
    )
    installation = binding.installation
    installation.name = name or f"installation-{uuid4().hex[:10]}"
    db.commit()
    return installation


def _make_run(
    db,
    installation: IntegrationInstallation,
    *,
    status: IntegrationRunStatus,
    started_at: datetime,
    finished_at: datetime | None = None,
    job_name: str | None = None,
) -> IntegrationRun:
    target = IntegrationTarget(name=f"target-{uuid4().hex[:10]}")
    db.add(target)
    db.flush()
    job = IntegrationJob(
        target_id=target.id,
        name=job_name or f"job-{uuid4().hex[:10]}",
        capability_binding_id=installation.capability_bindings[0].id,
    )
    db.add(job)
    db.flush()
    run = IntegrationRun(
        job_id=job.id,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        installation_id=installation.id,
        capability_binding_id=installation.capability_bindings[0].id,
    )
    db.add(run)
    db.commit()
    return run


def _evidence_for(data: dict, installation: IntegrationInstallation) -> dict:
    for row in data["integrations"]:
        if row["installation"].id == installation.id:
            return row["operational_evidence"]
    raise AssertionError("installation not in installed integrations data")


def test_installed_integrations_evidence_template_compiles() -> None:
    install_brand_jinja_global()
    env = Jinja2Templates(directory="templates").env

    env.get_template("admin/integrations/installed.html")


def test_connectors_index_exposes_managed_installations(db_session) -> None:
    installation = _make_installation(db_session, name="Managed ERP transport")

    data = web_integrations.build_connectors_list_data(db_session)

    managed = data["managed_connectors"]
    assert [row["installation"].id for row in managed] == [installation.id]
    assert managed[0]["definition"].key == "webhook.http"
    assert data["stats"]["total"] == 1
    assert data["stats"]["active"] == 1


def test_connectors_index_and_erp_config_templates_compile() -> None:
    install_brand_jinja_global()
    env = Jinja2Templates(directory="templates").env

    env.get_template("admin/integrations/connectors/index.html")
    env.get_template("admin/integrations/erp/config.html")


def test_connector_evidence_says_when_no_signals(db_session):
    connector = _make_installation(db_session)

    data = web_integrations.build_installed_integrations_data(db_session)

    evidence = _evidence_for(data, connector)
    assert evidence["last_result"] == (
        "No completed job or delivery evidence has been recorded."
    )
    assert evidence["needs_attention"] is False
    assert data["stats"]["attention"] == 0


def test_connector_evidence_reports_last_success_without_health_label(db_session):
    connector = _make_installation(db_session)
    now = datetime.now(UTC)
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.failed,
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
    )
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.success,
        started_at=now - timedelta(hours=1),
        finished_at=now - timedelta(hours=1),
    )

    data = web_integrations.build_installed_integrations_data(db_session)

    evidence = _evidence_for(data, connector)
    assert evidence["last_result"] == "The last completed integration job succeeded."
    assert evidence["needs_attention"] is False
    assert data["stats"]["attention"] == 0


def test_connector_evidence_reports_last_failure_and_attention(db_session):
    connector = _make_installation(db_session)
    now = datetime.now(UTC)
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.success,
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
    )
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.failed,
        started_at=now - timedelta(hours=1),
        finished_at=now - timedelta(hours=1),
    )

    data = web_integrations.build_installed_integrations_data(db_session)

    evidence = _evidence_for(data, connector)
    assert evidence["last_result"] == "The last completed integration job failed."
    assert evidence["needs_attention"] is True
    assert data["stats"]["attention"] == 1


def test_connector_evidence_ignores_in_flight_runs(db_session):
    connector = _make_installation(db_session)
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.running,
        started_at=datetime.now(UTC),
    )

    data = web_integrations.build_installed_integrations_data(db_session)

    evidence = _evidence_for(data, connector)
    assert evidence["last_result"] == (
        "No completed job or delivery evidence has been recorded."
    )


def test_activity_log_resolves_connector_name_and_run_duration(db_session):
    connector = _make_installation(db_session, name=f"Billing sync {uuid4().hex[:6]}")
    started = datetime.now(UTC) - timedelta(minutes=10)
    _make_run(
        db_session,
        connector,
        status=IntegrationRunStatus.success,
        started_at=started,
        finished_at=started + timedelta(seconds=2, milliseconds=500),
        job_name="pull-tickets",
    )

    data = web_integrations.build_installed_integrations_data(db_session)

    entries = [
        item
        for item in data["activity_log"]
        if item["connector_id"] == str(connector.id)
    ]
    assert entries, "run should appear in the activity log"
    entry = entries[0]
    assert entry["connector_name"] == connector.name
    assert entry["event_type"] == "job: pull-tickets"
    assert entry["status"] == "success"
    assert entry["response_time_ms"] == 2500


def test_api_key_auth_stamps_last_used_at(db_session):
    raw = f"key-{uuid4().hex}"
    key = ApiKey(
        label="obs",
        key_hash=hash_api_key(raw),
        scopes=["audit:read"],
        is_active=True,
    )
    db_session.add(key)
    db_session.commit()
    assert key.last_used_at is None

    auth = require_user_auth(authorization=None, x_api_key=raw, db=db_session)

    assert auth["principal_type"] == "api_key"
    db_session.refresh(key)
    assert key.last_used_at is not None


def test_api_key_last_used_at_write_is_throttled(db_session):
    raw = f"key-{uuid4().hex}"
    recent = datetime.now(UTC) - timedelta(minutes=2)
    key = ApiKey(
        label="obs",
        key_hash=hash_api_key(raw),
        scopes=["audit:read"],
        is_active=True,
        last_used_at=recent,
    )
    db_session.add(key)
    db_session.commit()

    require_user_auth(authorization=None, x_api_key=raw, db=db_session)

    db_session.refresh(key)
    stamped = key.last_used_at
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    assert stamped == recent, "recent stamp must not be rewritten on every request"


def test_api_key_last_used_at_refreshes_after_window(db_session):
    raw = f"key-{uuid4().hex}"
    stale = datetime.now(UTC) - timedelta(minutes=30)
    key = ApiKey(
        label="obs",
        key_hash=hash_api_key(raw),
        scopes=["audit:read"],
        is_active=True,
        last_used_at=stale,
    )
    db_session.add(key)
    db_session.commit()

    require_user_auth(authorization=None, x_api_key=raw, db=db_session)

    db_session.refresh(key)
    stamped = key.last_used_at
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    assert stamped > stale + timedelta(minutes=20)
