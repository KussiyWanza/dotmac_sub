from pathlib import Path

TASK_SOURCE = Path("app/tasks/notifications.py").read_text()
ROUTE_SOURCE = Path("app/web/admin/inbox.py").read_text()
COMMAND_SOURCE = Path("app/services/team_inbox_commands.py").read_text()
OUTBOUND_SOURCE = Path("app/services/team_inbox_outbound.py").read_text()
RELIABILITY_SOURCE = Path("app/services/task_reliability.py").read_text()
COMPOSE_SOURCE = Path("docker-compose.yml").read_text()
MAKEFILE_SOURCE = Path("Makefile").read_text()


def test_immediate_delivery_uses_exact_typed_outbox_identity():
    assert "notification_id: UUID | None = None" in TASK_SOURCE
    assert "notification_id: UUID | None = None" in COMMAND_SOURCE
    assert "notification_id: UUID" in OUTBOUND_SOURCE
    assert "notification_id=typed_notification_id" in TASK_SOURCE
    assert "_wake_delivery_after_commit(db, notification.id)" in OUTBOUND_SOURCE
    assert "run_after_commit(" in OUTBOUND_SOURCE
    assert "deliver_notification.apply_async" in OUTBOUND_SOURCE
    assert "retry=False" in OUTBOUND_SOURCE


def test_immediate_and_recovery_delivery_share_row_locked_claim():
    assert ".with_for_update(skip_locked=True)" in TASK_SOURCE
    assert "_eligible_notification_query(" in TASK_SOURCE
    assert 'name="app.tasks.notifications.deliver_notification"' in TASK_SOURCE
    assert 'name="app.tasks.notifications.deliver_notification_queue"' in TASK_SOURCE
    assert '"app.tasks.notifications.deliver_notification": _c(' in RELIABILITY_SOURCE


def test_notification_worker_is_isolated_from_default_backlog():
    assert "celery-worker-notifications:" in COMPOSE_SOURCE
    assert "- notifications" in COMPOSE_SOURCE
    assert "celery-worker-notifications" in MAKEFILE_SOURCE
