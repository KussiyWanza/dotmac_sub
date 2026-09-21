from __future__ import annotations

from types import SimpleNamespace

from app import celery_app as celery_app_module


def test_worker_logging_signal_installs_structured_formatter(monkeypatch):
    calls = []
    monkeypatch.setattr(
        celery_app_module,
        "configure_logging",
        lambda: calls.append("configured"),
    )

    celery_app_module._configure_worker_logging()

    assert calls == ["configured"]


def test_task_extra_includes_request_context():
    task = SimpleNamespace(
        name="app.tasks.billing.run_invoice_cycle",
        request=SimpleNamespace(
            root_id="root-1",
            parent_id="parent-1",
            correlation_id="corr-1",
            retries=2,
            eta=None,
        ),
    )

    extra = celery_app_module._task_extra(task, "task-1", task_state="SUCCESS")

    assert extra["event"] == "celery_task"
    assert extra["task_id"] == "task-1"
    assert extra["task_name"] == "app.tasks.billing.run_invoice_cycle"
    assert extra["root_id"] == "root-1"
    assert extra["correlation_id"] == "corr-1"
    assert extra["task_state"] == "SUCCESS"


def test_task_signal_handlers_emit_structured_logs(caplog):
    task = SimpleNamespace(
        name="app.tasks.tr069.reconcile_command_outcomes",
        request=SimpleNamespace(
            root_id="root-2",
            parent_id=None,
            correlation_id="corr-2",
            retries=1,
            eta=None,
        ),
    )

    caplog.set_level("INFO")

    celery_app_module._log_task_prerun(
        task_id="task-2",
        task=task,
        args=("a", "b"),
        kwargs={"force": True},
    )
    celery_app_module._log_task_postrun(
        task_id="task-2",
        task=task,
        state="SUCCESS",
        retval={"queued": 3},
    )

    start_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "celery_task_start"
    )
    complete_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "celery_task_complete"
    )

    assert start_record.event == "celery_task"
    assert start_record.task_id == "task-2"
    assert start_record.arg_count == 2
    assert start_record.kwarg_keys == ["force"]
    assert complete_record.task_state == "SUCCESS"
    assert complete_record.result_type == "dict"


def test_enqueue_celery_task_uses_headers_and_logs(monkeypatch, caplog):
    captured = {}

    class _Result:
        id = "task-3"

    def _fake_send_task(name, args=None, kwargs=None, headers=None, **extra):
        captured["name"] = name
        captured["args"] = args
        captured["kwargs"] = kwargs
        captured["headers"] = headers
        captured["extra"] = extra
        return _Result()

    monkeypatch.setattr(celery_app_module.celery_app, "send_task", _fake_send_task)
    caplog.set_level("INFO")

    result = celery_app_module.enqueue_celery_task(
        "app.tasks.integration_delivery.deliver_integration_event",
        args=["delivery-1"],
        correlation_id="webhook_event:event-1",
        source="event_webhook_handler",
    )

    assert result.id == "task-3"
    assert captured["headers"]["correlation_id"] == "webhook_event:event-1"
    assert captured["headers"]["source"] == "event_webhook_handler"
    queued_record = next(
        record
        for record in caplog.records
        if record.getMessage() == "celery_task_queued"
    )
    assert queued_record.task_id == "task-3"
    assert queued_record.correlation_id == "webhook_event:event-1"
