"""Tests for scheduler config services."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.scheduler import ScheduledTask, ScheduleType
from app.models.subscription_engine import SettingValueType
from app.services import control_registry, scheduler_config, settings_spec


def _control_overrides(values: dict[str, bool]):
    """Patch selected canonical controls while preserving all other defaults."""
    original = control_registry.is_enabled
    return patch.object(
        control_registry,
        "is_enabled",
        side_effect=lambda db, key: values.get(key, original(db, key)),
    )


def _integer_overrides(values: dict[str, int]):
    """Patch selected registered tuning values while preserving other specs."""
    original = scheduler_config.resolve_integer
    return patch.object(
        scheduler_config,
        "resolve_integer",
        side_effect=lambda db, domain, key: values.get(key, original(db, domain, key)),
    )


# =============================================================================
# Effective Value Helper Tests
# =============================================================================


class TestSchedulerSettingEnabled:
    """Tests for the registered scheduler-boolean resolver."""

    def test_uses_registered_database_value(self, db_session):
        db_session.add(
            DomainSetting(
                domain=SettingDomain.notification,
                key="ncc_report_email_enabled",
                value_text="true",
                is_active=True,
            )
        )
        db_session.commit()

        assert (
            scheduler_config._scheduler_setting_enabled(
                db_session,
                SettingDomain.notification,
                "ncc_report_email_enabled",
            )
            is True
        )

    def test_runtime_environment_does_not_override_database(
        self, db_session, monkeypatch
    ):
        monkeypatch.setenv("NCC_REPORT_EMAIL_ENABLED", "true")
        db_session.add(
            DomainSetting(
                domain=SettingDomain.notification,
                key="ncc_report_email_enabled",
                value_text="false",
                is_active=True,
            )
        )
        db_session.commit()

        assert (
            scheduler_config._scheduler_setting_enabled(
                db_session,
                SettingDomain.notification,
                "ncc_report_email_enabled",
            )
            is False
        )

    def test_rejects_unregistered_boolean(self, db_session):
        with pytest.raises(RuntimeError, match="Scheduler boolean must be registered"):
            scheduler_config._scheduler_setting_enabled(
                db_session,
                SettingDomain.scheduler,
                "not_registered",
            )

    def test_rejects_registered_non_boolean(self, db_session):
        with pytest.raises(RuntimeError, match="Scheduler boolean must be registered"):
            scheduler_config._scheduler_setting_enabled(
                db_session,
                SettingDomain.scheduler,
                "timezone",
            )


class TestRegisteredScalarSettings:
    def test_database_integer_is_authoritative_at_runtime(
        self, db_session, monkeypatch
    ):
        monkeypatch.setenv("FUP_EVALUATION_INTERVAL_SECONDS", "15")
        db_session.add(
            DomainSetting(
                domain=SettingDomain.usage,
                key="fup_evaluation_interval_seconds",
                value_type=SettingValueType.integer,
                value_text="120",
                is_active=True,
            )
        )
        db_session.commit()

        assert (
            settings_spec.resolve_integer(
                db_session,
                SettingDomain.usage,
                "fup_evaluation_interval_seconds",
            )
            == 120
        )

    def test_registered_integer_default_is_authoritative(self, db_session):
        assert (
            settings_spec.resolve_integer(
                db_session,
                SettingDomain.usage,
                "fup_evaluation_interval_seconds",
            )
            == 60
        )

    def test_registered_string_uses_database_value(self, db_session):
        db_session.add(
            DomainSetting(
                domain=SettingDomain.scheduler,
                key="timezone",
                value_type=SettingValueType.string,
                value_text="UTC",
                is_active=True,
            )
        )
        db_session.commit()

        assert (
            settings_spec.resolve_string(
                db_session,
                SettingDomain.scheduler,
                "timezone",
            )
            == "UTC"
        )

    def test_unregistered_integer_is_rejected(self, db_session):
        with pytest.raises(RuntimeError, match="Integer setting must be registered"):
            settings_spec.resolve_integer(
                db_session,
                SettingDomain.scheduler,
                "not_registered",
            )


# =============================================================================
# Sync Scheduled Task Tests
# =============================================================================


class TestSyncScheduledTask:
    """Tests for _sync_scheduled_task helper."""

    def test_creates_task_when_enabled(self, db_session):
        """Test creates new task when enabled and doesn't exist."""
        scheduler_config._sync_scheduled_task(
            db_session,
            name="test_task",
            task_name="app.tasks.test.run_test",
            enabled=True,
            interval_seconds=3600,
        )

        task = (
            db_session.query(ScheduledTask)
            .filter(ScheduledTask.task_name == "app.tasks.test.run_test")
            .first()
        )

        assert task is not None
        assert task.name == "test_task"
        assert task.interval_seconds == 3600
        assert task.enabled is True

    def test_does_not_create_when_disabled(self, db_session):
        """Test does not create task when disabled and doesn't exist."""
        scheduler_config._sync_scheduled_task(
            db_session,
            name="disabled_task",
            task_name="app.tasks.disabled.run",
            enabled=False,
            interval_seconds=3600,
        )

        task = (
            db_session.query(ScheduledTask)
            .filter(ScheduledTask.task_name == "app.tasks.disabled.run")
            .first()
        )

        assert task is None

    def test_updates_existing_task_task_name_in_place(self, db_session):
        """Match is by ``name``; a task_name rename updates the row in place.

        The sync identifies the row by its stable ``name``. When the task is
        renamed/moved (its ``task_name`` changes) the existing row's
        ``task_name`` is updated in place rather than a new row being inserted.
        """
        task = ScheduledTask(
            name="stable_name",
            task_name="app.tasks.update.old",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()
        original_id = task.id

        scheduler_config._sync_scheduled_task(
            db_session,
            name="stable_name",
            task_name="app.tasks.update.new",
            enabled=True,
            interval_seconds=3600,
        )

        rows = (
            db_session.query(ScheduledTask)
            .filter(ScheduledTask.name == "stable_name")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].id == original_id
        assert rows[0].task_name == "app.tasks.update.new"

    def test_updates_existing_task_interval(self, db_session):
        """Test updates interval of existing task."""
        task = ScheduledTask(
            name="interval_task",
            task_name="app.tasks.interval.run",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()

        scheduler_config._sync_scheduled_task(
            db_session,
            name="interval_task",
            task_name="app.tasks.interval.run",
            enabled=True,
            interval_seconds=7200,
        )

        db_session.refresh(task)
        assert task.interval_seconds == 7200

    def test_updates_existing_task_enabled(self, db_session):
        """Test updates enabled status of existing task."""
        task = ScheduledTask(
            name="enable_task",
            task_name="app.tasks.enable.run",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()

        scheduler_config._sync_scheduled_task(
            db_session,
            name="enable_task",
            task_name="app.tasks.enable.run",
            enabled=False,
            interval_seconds=3600,
        )

        db_session.refresh(task)
        assert task.enabled is False

    def test_no_update_when_unchanged(self, db_session):
        """Test no commit when nothing changed."""
        task = ScheduledTask(
            name="unchanged_task",
            task_name="app.tasks.unchanged.run",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()

        original_updated_at = task.updated_at

        scheduler_config._sync_scheduled_task(
            db_session,
            name="unchanged_task",
            task_name="app.tasks.unchanged.run",
            enabled=True,
            interval_seconds=3600,
        )

        db_session.refresh(task)
        # updated_at should be same since nothing changed
        assert task.updated_at == original_updated_at


class TestRetireScheduledTask:
    """Tests for _retire_scheduled_task helper."""

    def test_disables_enabled_matching_tasks(self, db_session):
        task = ScheduledTask(
            name="retired_task",
            task_name="app.tasks.removed.run",
            schedule_type=ScheduleType.interval,
            interval_seconds=3600,
            enabled=True,
        )
        db_session.add(task)
        db_session.commit()

        scheduler_config._retire_scheduled_task(db_session, "app.tasks.removed.run")

        db_session.refresh(task)
        assert task.enabled is False


# =============================================================================
# Get Celery Config Tests
# =============================================================================


class TestGetCeleryConfig:
    """Tests for get_celery_config function."""

    def test_returns_default_config(self, monkeypatch):
        """Test returns default config when nothing configured."""
        # Clear env vars
        for var in [
            "CELERY_BROKER_URL",
            "CELERY_RESULT_BACKEND",
            "CELERY_TIMEZONE",
            "REDIS_URL",
        ]:
            monkeypatch.delenv(var, raising=False)

        # Mock SessionLocal to return a mock session
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            config = scheduler_config.get_celery_config()

        assert config["broker_url"] == "redis://localhost:6379/0"
        assert config["result_backend"] == "redis://localhost:6379/1"
        assert config["timezone"] == "Africa/Lagos"
        assert config["beat_max_loop_interval"] == 5
        assert config["beat_refresh_seconds"] == 300

    def test_only_transport_endpoints_use_runtime_environment(self, monkeypatch):
        monkeypatch.setenv("CELERY_BROKER_URL", "redis://broker:6379/0")
        monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://backend:6379/1")
        monkeypatch.setenv("CELERY_TIMEZONE", "America/New_York")
        monkeypatch.setenv("CELERY_BEAT_MAX_LOOP_INTERVAL", "10")
        monkeypatch.setenv("CELERY_BEAT_REFRESH_SECONDS", "60")

        # Mock SessionLocal
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            config = scheduler_config.get_celery_config()

        assert config["broker_url"] == "redis://broker:6379/0"
        assert config["result_backend"] == "redis://backend:6379/1"
        assert config["timezone"] == "Africa/Lagos"
        assert config["beat_max_loop_interval"] == 5
        assert config["beat_refresh_seconds"] == 300

    def test_uses_redis_url_fallback(self, monkeypatch):
        """Test uses REDIS_URL as fallback for broker/backend."""
        for var in ["CELERY_BROKER_URL", "CELERY_RESULT_BACKEND"]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("REDIS_URL", "redis://fallback:6379/0")

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            config = scheduler_config.get_celery_config()

        assert config["broker_url"] == "redis://fallback:6379/0"
        assert config["result_backend"] == "redis://fallback:6379/0"


# =============================================================================
# Build Beat Schedule Tests
# =============================================================================


class TestBuildBeatSchedule:
    """Tests for build_beat_schedule function."""

    def test_builds_gis_sync_schedule(self, monkeypatch):
        """Test builds GIS sync schedule when enabled."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "true")
        monkeypatch.setenv("GIS_SYNC_INTERVAL_MINUTES", "30")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        # Return None for all setting queries
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        # Return empty list for tasks
        mock_session.query.return_value.filter.return_value.all.return_value = []
        # Mock order_by for _sync_scheduled_task
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with (
            _integer_overrides({"sync_interval_minutes": 30}),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
        ):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                schedule = scheduler_config.build_beat_schedule()

        assert "gis_sync" in schedule
        assert schedule["gis_sync"]["task"] == "app.tasks.gis.sync_gis_sources"
        assert schedule["gis_sync"]["schedule"] == timedelta(minutes=30)

    def test_registers_device_login_sync_by_default(self, db_session, monkeypatch):
        """The authoritative device-login reconcile is scheduled (default-on, 900s).

        Without this backstop, populate_device_login only runs on a device-login
        edit, so a deactivated/renamed staff member's router access would never
        be revoked in RADIUS.
        """
        # build_beat_schedule() closes the session it opens; keep ours usable.
        monkeypatch.setattr(db_session, "close", lambda: None)
        with patch.object(scheduler_config, "SessionLocal", return_value=db_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                scheduler_config.build_beat_schedule()

        row = (
            db_session.query(ScheduledTask)
            .filter(
                ScheduledTask.task_name
                == "app.tasks.radius_population.sync_device_login"
            )
            .first()
        )
        assert row is not None, "device_login_sync ScheduledTask not registered"
        assert row.enabled is True
        assert row.interval_seconds == 900

    def test_retires_parallel_radius_refresh_schedule(self, db_session, monkeypatch):
        refresh_task = "app.tasks.radius_population.refresh_radius_from_subs"
        stale_row = ScheduledTask(
            name="radius_refresh_safety_net",
            task_name=refresh_task,
            schedule_type=ScheduleType.interval,
            interval_seconds=900,
            enabled=True,
        )
        db_session.add(stale_row)
        db_session.commit()
        monkeypatch.setattr(db_session, "close", lambda: None)

        with patch.object(scheduler_config, "SessionLocal", return_value=db_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                schedule = scheduler_config.build_beat_schedule()

        db_session.refresh(stale_row)
        assert stale_row.enabled is False
        assert all(entry["task"] != refresh_task for entry in schedule.values())

    def test_registers_configured_event_outbox_dispatch(self, db_session, monkeypatch):
        from app.services.domain_settings import scheduler_settings

        scheduler_settings.ensure_by_key(
            db_session,
            key="event_dispatch_interval_seconds",
            value_type=SettingValueType.integer,
            value_text="30",
        )
        db_session.commit()
        monkeypatch.setattr(db_session, "close", lambda: None)

        with patch.object(scheduler_config, "SessionLocal", return_value=db_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                scheduler_config.build_beat_schedule()

        row = (
            db_session.query(ScheduledTask)
            .filter(ScheduledTask.name == "event_dispatch_runner")
            .one()
        )
        assert row.task_name == "app.tasks.events.dispatch_pending_events"
        assert row.enabled is True
        assert row.interval_seconds == 30

    def test_disables_ont_reconcile_row_via_canonical_control(
        self, db_session, monkeypatch
    ):
        db_session.add(
            ScheduledTask(
                name="ont_reconcile_sweep",
                task_name="app.tasks.ont_reconcile.run_ont_reconcile_sweep",
                schedule_type=ScheduleType.interval,
                interval_seconds=900,
                enabled=True,
            )
        )
        db_session.commit()
        control_registry.update_canonical_feature_controls(
            db_session, payload={"network.ont_reconcile": False}
        )
        db_session.commit()
        monkeypatch.setattr(db_session, "close", lambda: None)

        with patch.object(scheduler_config, "SessionLocal", return_value=db_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                scheduler_config.build_beat_schedule()

        row = (
            db_session.query(ScheduledTask)
            .filter(
                ScheduledTask.task_name
                == "app.tasks.ont_reconcile.run_ont_reconcile_sweep"
            )
            .first()
        )
        assert row is not None
        assert row.enabled is False

    def test_excludes_gis_sync_when_canonical_control_disabled(self, monkeypatch):
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with (
            _control_overrides({"gis.sync": False}),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
        ):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                schedule = scheduler_config.build_beat_schedule()

        assert "gis_sync" not in schedule

    def test_builds_integration_job_schedules(self, monkeypatch):
        """Test builds integration job schedules."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        # Mock integration job
        mock_job = MagicMock()
        mock_job.id = "job-123"
        mock_job.interval_minutes = 15

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[mock_job],
            ):
                schedule = scheduler_config.build_beat_schedule()

        assert "integration_job_job-123" in schedule
        assert (
            schedule["integration_job_job-123"]["task"]
            == "app.tasks.integrations.run_integration_job"
        )
        assert schedule["integration_job_job-123"]["schedule"] == timedelta(minutes=15)
        assert schedule["integration_job_job-123"]["args"] == ["job-123"]

    def test_skips_crm_ticket_pull_integration_interval_job(self, monkeypatch):
        """Dedicated CRM beat must be the only scheduled ticket pull path."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        mock_job = MagicMock()
        mock_job.id = "crm-job-123"
        mock_job.capability_binding.capability_id = "crm.ticket_observation.v1"
        mock_job.interval_minutes = 15

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[mock_job],
            ):
                schedule = scheduler_config.build_beat_schedule()

        assert "integration_job_crm-job-123" not in schedule

    @pytest.mark.parametrize(
        ("ready", "expected"),
        ((False, False), (True, True)),
    )
    def test_crm_ticket_schedule_requires_executable_cutover_readiness(
        self,
        ready,
        expected,
    ):
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None
        readiness = SimpleNamespace(
            schedule_enabled=ready,
            issue_codes=(
                ()
                if ready
                else (
                    "enabled_ticket_observation_binding_count:0",
                    "active_ticket_observation_job_count:0",
                )
            ),
        )

        with (
            _control_overrides({"crm.ticket_pull": True, "gis.sync": False}),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
            patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ),
            patch(
                "app.services.integrations.crm_ticket_readiness."
                "resolve_crm_ticket_pull_readiness",
                return_value=readiness,
            ),
        ):
            schedule = scheduler_config.build_beat_schedule()

        assert ("crm_ticket_pull" in schedule) is expected
        assert ("crm_ticket_pull_full" in schedule) is expected

    def test_builds_scheduled_task_schedules(self, monkeypatch):
        """Test builds scheduled task schedules."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_task = MagicMock()
        mock_task.id = "task-456"
        mock_task.task_name = "app.tasks.custom.run"
        mock_task.schedule_type = ScheduleType.interval
        mock_task.interval_seconds = 900
        mock_task.args_json = ["arg1"]
        mock_task.kwargs_json = {"key": "value"}

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = [
            mock_task
        ]
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with (
            _control_overrides({"gis.sync": False}),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
        ):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                schedule = scheduler_config.build_beat_schedule()

        assert "scheduled_task_task-456" in schedule
        assert schedule["scheduled_task_task-456"]["task"] == "app.tasks.custom.run"
        assert schedule["scheduled_task_task-456"]["schedule"] == timedelta(seconds=900)
        assert schedule["scheduled_task_task-456"]["args"] == ["arg1"]
        assert schedule["scheduled_task_task-456"]["kwargs"] == {"key": "value"}

    def test_minimum_interval_enforcement(self, monkeypatch):
        """Test minimum interval is enforced for GIS sync."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "true")
        monkeypatch.setenv("GIS_SYNC_INTERVAL_MINUTES", "0")  # Should become 1
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with (
            _integer_overrides({"sync_interval_minutes": 0}),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
        ):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                schedule = scheduler_config.build_beat_schedule()

        assert schedule["gis_sync"]["schedule"] == timedelta(minutes=1)

    def test_dunning_runner_targets_unified_billing_enforcement(self, monkeypatch):
        """The scheduled collections writer is the unified enforcement task."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.setenv("DUNNING_ENABLED", "true")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                scheduler_config.build_beat_schedule()

        assert any(
            getattr(call.args[0], "name", None) == "dunning_runner"
            and getattr(call.args[0], "task_name", None)
            == "app.tasks.collections.run_billing_enforcement"
            for call in mock_session.add.call_args_list
        )

    def test_prepaid_enforcement_sweep_uses_registered_cadence(self, monkeypatch):
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        original_resolve_integer = scheduler_config.resolve_integer

        def resolve_integer(db, domain, key):
            if key == "prepaid_balance_sweep_interval_seconds":
                return 1800
            return original_resolve_integer(db, domain, key)

        with (
            _control_overrides({"gis.sync": False}),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
            patch.object(
                scheduler_config, "resolve_integer", side_effect=resolve_integer
            ),
            patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ),
        ):
            scheduler_config.build_beat_schedule()

        assert any(
            getattr(call.args[0], "name", None) == "prepaid_balance_sweep"
            and getattr(call.args[0], "interval_seconds", None) == 1800
            and getattr(call.args[0], "enabled", None) is True
            for call in mock_session.add.call_args_list
        )

    def test_ignores_retired_splynx_sync_settings(self, monkeypatch):
        """Retired legacy-sync env toggles must not create beat entries."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.setenv("SPLYNX_CUSTOMER_SYNC_ENABLED", "true")
        monkeypatch.setenv("SPLYNX_CUSTOMER_SYNC_INTERVAL_HOURS", "24")
        monkeypatch.delenv("SPLYNX_SYNC_ENABLED", raising=False)
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                schedule = scheduler_config.build_beat_schedule()

        assert "splynx_customer_accounts_details_sync" not in schedule
        assert "splynx_incremental_sync" not in schedule

    def test_handles_exception_gracefully(self, monkeypatch, caplog):
        """Test handles database exceptions gracefully."""
        monkeypatch.delenv("GIS_SYNC_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.side_effect = Exception("Database error")

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            schedule = scheduler_config.build_beat_schedule()

        # Should return empty schedule without crashing
        assert schedule == {}

    def test_builds_compensation_retry_watchdog_schedule(self, monkeypatch):
        """Durable compensation failures always retain their retry watchdog."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with (
            _control_overrides({"gis.sync": False}),
            _integer_overrides({"compensation_retry_interval_seconds": 120}),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
        ):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                schedule = scheduler_config.build_beat_schedule()

        scheduled_calls = mock_session.add.call_args_list
        assert schedule.get("gis_sync") is None
        assert any(
            getattr(call.args[0], "task_name", None)
            == "app.tasks.provisioning.retry_pending_compensation_failures"
            and getattr(call.args[0], "interval_seconds", None) == 120
            and getattr(call.args[0], "enabled", None) is True
            for call in scheduled_calls
        )

    def test_builds_operational_escalation_delivery_schedule(self, monkeypatch):
        """Test operational escalation delivery runner is synced."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.setenv("OPERATIONAL_ESCALATION_DELIVERY_ENABLED", "true")
        monkeypatch.setenv("OPERATIONAL_ESCALATION_DELIVERY_INTERVAL_SECONDS", "45")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with (
            _integer_overrides(
                {"operational_escalation_delivery_interval_seconds": 45}
            ),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
        ):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                scheduler_config.build_beat_schedule()

        assert any(
            getattr(call.args[0], "task_name", None)
            == (
                "app.tasks.operational_escalations."
                "dispatch_operational_escalation_deliveries"
            )
            and getattr(call.args[0], "interval_seconds", None) == 45
            for call in mock_session.add.call_args_list
        )

    def test_builds_olt_profile_sync_due_task_runner(self, monkeypatch):
        """Test OLT profile sync due-task runner is opt-in and interval clamped."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.setenv("OLT_PROFILE_SYNC_WORKER_ENABLED", "true")
        monkeypatch.setenv("OLT_PROFILE_SYNC_INTERVAL_SECONDS", "45")
        monkeypatch.delenv("USAGE_RATING_ENABLED", raising=False)
        monkeypatch.delenv("DUNNING_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with (
            _control_overrides({"gis.sync": False, "network.olt_profile_sync": True}),
            _integer_overrides({"olt_profile_sync_interval_seconds": 45}),
            patch.object(scheduler_config, "SessionLocal", return_value=mock_session),
        ):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                scheduler_config.build_beat_schedule()

        scheduled_calls = mock_session.add.call_args_list
        assert any(
            getattr(call.args[0], "name", None) == "olt_profile_sync_due_task_runner"
            and getattr(call.args[0], "task_name", None)
            == "app.tasks.profile_sync.execute_due_profile_sync_tasks"
            and getattr(call.args[0], "interval_seconds", None) == 60
            for call in scheduled_calls
        )

    def test_builds_infra_availability_snapshot_and_prune(self, monkeypatch):
        """Infra availability snapshot + prune register as daily tasks."""
        monkeypatch.setenv("GIS_SYNC_ENABLED", "false")
        monkeypatch.delenv("INFRA_AVAILABILITY_SNAPSHOT_ENABLED", raising=False)
        monkeypatch.delenv("INFRA_AVAILABILITY_PRUNE_ENABLED", raising=False)

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.filter.return_value.filter.return_value.first.return_value = None
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch.object(scheduler_config, "SessionLocal", return_value=mock_session):
            with patch.object(
                scheduler_config.integration_service,
                "list_interval_jobs",
                return_value=[],
            ):
                scheduler_config.build_beat_schedule()

        scheduled_calls = mock_session.add.call_args_list
        assert any(
            getattr(call.args[0], "name", None) == "infra_availability_snapshot"
            and getattr(call.args[0], "task_name", None)
            == "app.tasks.infrastructure_availability."
            "snapshot_infrastructure_availability"
            and getattr(call.args[0], "interval_seconds", None) == 86400
            for call in scheduled_calls
        )
        assert any(
            getattr(call.args[0], "name", None) == "infra_availability_prune"
            and getattr(call.args[0], "task_name", None)
            == "app.tasks.infrastructure_availability.prune_infrastructure_availability"
            for call in scheduled_calls
        )


class TestIntervalToBeatSchedule:
    """Hour-multiple and day-long intervals become wall-clock crontabs
    (restart-proof); everything else stays timedelta."""

    def test_sub_hourly_interval_stays_timedelta(self):
        result = scheduler_config._interval_to_beat_schedule("task-1", 300)
        assert result == timedelta(seconds=300)

    def test_hourly_interval_becomes_crontab(self):
        # Hourly tasks starved for 17+ days when beat restarted more often
        # than hourly (topology_lldp_poll / topology_reconcile never fired).
        import uuid

        from celery.schedules import crontab

        result = scheduler_config._interval_to_beat_schedule(uuid.uuid4(), 3600)
        assert isinstance(result, crontab)
        # Fires every hour at one deterministic minute.
        assert result.hour == set(range(24))
        assert len(result.minute) == 1

    def test_multi_hour_interval_becomes_stepped_crontab(self):
        import uuid

        from celery.schedules import crontab

        result = scheduler_config._interval_to_beat_schedule(uuid.uuid4(), 4 * 3600)
        assert isinstance(result, crontab)
        assert result.hour == {0, 4, 8, 12, 16, 20}

    def test_hourly_crontab_is_deterministic_per_task(self):
        import uuid

        task_id = uuid.uuid4()
        first = scheduler_config._interval_to_beat_schedule(task_id, 3600)
        second = scheduler_config._interval_to_beat_schedule(task_id, 3600)
        assert first.minute == second.minute

    def test_non_hour_multiple_stays_timedelta(self):
        # crontab cannot express a 90-minute cadence without drift.
        result = scheduler_config._interval_to_beat_schedule("task-1", 5400)
        assert result == timedelta(seconds=5400)

    def test_daily_interval_becomes_crontab(self):
        import uuid

        from celery.schedules import crontab

        result = scheduler_config._interval_to_beat_schedule(uuid.uuid4(), 86400)
        assert isinstance(result, crontab)
        # Anchored into the 00:00-05:59 window.
        assert max(result.hour) <= 5

    def test_daily_crontab_is_deterministic_per_task(self):
        import uuid

        task_id = uuid.uuid4()
        first = scheduler_config._interval_to_beat_schedule(task_id, 86400)
        second = scheduler_config._interval_to_beat_schedule(task_id, 86400)
        assert first.hour == second.hour and first.minute == second.minute

    def test_multiday_interval_stays_timedelta(self):
        import uuid

        result = scheduler_config._interval_to_beat_schedule(uuid.uuid4(), 7 * 86400)
        assert result == timedelta(days=7)


class TestEntryExpires:
    """Periodic messages self-discard once their successor is queued."""

    def test_sub_daily_expiry_equals_interval(self):
        assert scheduler_config._entry_expires_seconds(300) == 300
        assert scheduler_config._entry_expires_seconds(3600) == 3600

    def test_daily_tasks_get_twelve_hours(self):
        assert scheduler_config._entry_expires_seconds(86400) == 43200
