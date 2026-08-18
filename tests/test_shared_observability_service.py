from __future__ import annotations

from app.models.admin_alert import AdminAlert
from app.models.network_monitoring import AlertSeverity
from app.services import observability


def test_record_task_run_updates_existing_sinks(monkeypatch):
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_success",
        lambda task_name, **kwargs: calls.setdefault("job_success", task_name),
    )
    monkeypatch.setattr(
        "app.services.observability.task_heartbeat.record_success",
        lambda task_name, result=None, **kwargs: calls.setdefault(
            "task_success", (task_name, result)
        ),
    )
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result",
        lambda task_name, **kwargs: calls.setdefault("job_result", (task_name, kwargs)),
    )

    observability.record_task_run(
        "app.tasks.notifications.deliver_notification_queue",
        status="success",
        counters={"delivered": 3},
        duration_seconds=0.25,
    )

    assert calls["job_success"] == "app.tasks.notifications.deliver_notification_queue"
    assert calls["task_success"] == (
        "app.tasks.notifications.deliver_notification_queue",
        {"delivered": 3},
    )
    task_name, kwargs = calls["job_result"]
    assert task_name == "app.tasks.notifications.deliver_notification_queue"
    assert kwargs["status"] == "ok"
    assert kwargs["detail"] == {"delivered": 3}


def test_record_task_skip_updates_existing_sinks(monkeypatch):
    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "app.services.observability.task_heartbeat.record_skip",
        lambda task_name: calls.setdefault("task_skip", task_name) and 4,
    )
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result",
        lambda task_name, **kwargs: calls.setdefault("job_result", (task_name, kwargs)),
    )

    streak = observability.record_task_skip("infrastructure_poll", reason="locked")

    assert streak == 4
    assert calls["task_skip"] == "infrastructure_poll"
    task_name, kwargs = calls["job_result"]
    assert task_name == "infrastructure_poll"
    assert kwargs["status"] == "skipped"
    assert kwargs["detail"] == {"reason": "locked", "skip_streak": 4}


def test_record_celery_task_success_records_money_job_result(monkeypatch):
    calls: dict[str, object] = {}
    task_name = "app.tasks.billing.run_invoice_cycle"

    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_success",
        lambda name, **kwargs: calls.setdefault("success", (name, kwargs)),
    )
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result",
        lambda name, **kwargs: calls.setdefault("result", (name, kwargs)),
    )

    observability.record_celery_task_success(task_name, result={"processed": 7})

    assert calls["success"][0] == task_name
    result_name, result_kwargs = calls["result"]
    assert result_name == task_name
    assert result_kwargs["status"] == "ok"
    assert result_kwargs["detail"] == {"processed": 7}


def test_reconciliation_success_with_rejections_records_partial_result(monkeypatch):
    calls: dict[str, object] = {}
    task_name = "app.tasks.payment_reconciliation.reconcile_topups"
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_success",
        lambda name, **kwargs: calls.setdefault("success", (name, kwargs)),
    )
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result",
        lambda name, **kwargs: calls.setdefault("result", (name, kwargs)),
    )

    observability.record_celery_task_success(
        task_name,
        result={"checked": 8, "recovered": 0, "errors": 3},
    )

    assert calls["success"][0] == task_name
    result_name, result_kwargs = calls["result"]
    assert result_name == task_name
    assert result_kwargs["status"] == "partial"
    assert result_kwargs["detail"]["errors"] == 3


def test_record_celery_task_success_skips_non_money_result(monkeypatch):
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_success",
        lambda name, **kwargs: calls.append(("success", name)),
    )
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result",
        lambda name, **kwargs: calls.append(("result", name)),
    )

    observability.record_celery_task_success(
        "app.tasks.tr069.reconcile_command_outcomes",
        result={"processed": 7},
    )

    assert calls == [("success", "app.tasks.tr069.reconcile_command_outcomes")]


def test_record_celery_task_failure_records_money_job_error(monkeypatch):
    calls: dict[str, object] = {}
    task_name = "app.tasks.collections.run_billing_enforcement"

    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result",
        lambda name, **kwargs: calls.setdefault("result", (name, kwargs)),
    )

    observability.record_celery_task_failure(task_name, error="boom")

    result_name, result_kwargs = calls["result"]
    assert result_name == task_name
    assert result_kwargs["status"] == "error"
    assert result_kwargs["detail"] == {"error": "boom"}


def test_record_finding_syncs_admin_alert(db_session):
    result = observability.record_finding(
        db_session,
        observability.Finding(
            fingerprint="observability:test:finding",
            domain="notification",
            source="test",
            severity=AlertSeverity.warning,
            title="Test finding",
            summary="A test finding was recorded.",
            details={"count": 1},
            target_url="/admin/notifications",
        ),
    )
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert result == "opened"
    assert alert.category == "notification"
    assert alert.fingerprint == "observability:test:finding"
    assert alert.details == {"count": 1}


def test_resolve_findings_resolves_absent_managed_alert(db_session):
    observability.record_finding(
        db_session,
        observability.Finding(
            fingerprint="observability:test:gone",
            domain="notification",
            source="test",
            severity=AlertSeverity.warning,
            title="Gone finding",
            summary="This finding should resolve.",
        ),
    )
    db_session.commit()

    resolved = observability.resolve_findings(
        db_session,
        managed_prefix="observability:test:",
        active_fingerprints=set(),
    )
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert resolved == 1
    assert alert.resolved_at is not None


def test_record_notification_queue_result_alerts_on_failures(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_success", lambda *_, **__: True
    )
    monkeypatch.setattr(
        "app.services.observability.task_heartbeat.record_success",
        lambda *_, **__: None,
    )
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result", lambda *_, **__: True
    )

    observability.record_notification_queue_result(
        db_session,
        task_name="app.tasks.notifications.deliver_notification_queue",
        result={
            "delivered": 0,
            "retried": 0,
            "failed": 2,
            "expired": 0,
            "reclaimed": 0,
            "stuck_dropped": 1,
            "rate_limited": 0,
        },
        duration_seconds=0.5,
    )
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert alert.fingerprint == "observability:notification:queue-failures"
    assert alert.category == "notification"
    assert alert.severity == AlertSeverity.warning
    assert alert.details["failed"] == 2
    assert alert.details["stuck_dropped"] == 1


def test_record_notification_queue_result_no_alert_on_clean_batch(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_success", lambda *_, **__: True
    )
    monkeypatch.setattr(
        "app.services.observability.task_heartbeat.record_success",
        lambda *_, **__: None,
    )
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result", lambda *_, **__: True
    )

    observability.record_notification_queue_result(
        db_session,
        task_name="app.tasks.notifications.deliver_notification_queue",
        result={"delivered": 4, "failed": 0, "stuck_dropped": 0},
        duration_seconds=0.5,
    )
    db_session.commit()

    assert db_session.query(AdminAlert).count() == 0


def test_record_notification_queue_result_alerts_on_stale_due_rows(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_success", lambda *_, **__: True
    )
    monkeypatch.setattr(
        "app.services.observability.task_heartbeat.record_success",
        lambda *_, **__: None,
    )
    monkeypatch.setattr(
        "app.services.observability.job_heartbeat.record_result", lambda *_, **__: True
    )

    observability.record_notification_queue_result(
        db_session,
        task_name="app.tasks.notifications.deliver_notification_queue",
        result={"delivered": 0, "failed": 0, "stuck_dropped": 0, "stale_due": 3},
    )
    db_session.commit()

    alert = db_session.query(AdminAlert).one()
    assert alert.details["stale_due"] == 3
