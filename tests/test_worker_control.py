from types import SimpleNamespace

from app.services import worker_control


def test_restart_worker_target_rejects_unknown_target(monkeypatch):
    monkeypatch.setenv("CELERY_WORKER_RESTART_ENABLED", "true")

    result = worker_control.restart_worker_target("unknown-worker")

    assert result.ok is False
    assert result.message == "Worker restart target is not allowed."


def test_restart_worker_target_respects_disable_flag(monkeypatch):
    monkeypatch.setenv("CELERY_WORKER_RESTART_ENABLED", "false")

    result = worker_control.restart_worker_target("celery-worker")

    assert result.ok is False
    assert result.message == "Worker restart is disabled by configuration."


def test_restart_worker_target_runs_configured_command(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setenv("CELERY_WORKER_RESTART_ENABLED", "true")
    monkeypatch.setenv(
        "CELERY_WORKER_RESTART_COMMAND",
        "docker compose restart {target}",
    )
    monkeypatch.setattr(worker_control.subprocess, "run", fake_run)

    result = worker_control.restart_worker_target("celery-worker-billing")

    assert result.ok is True
    assert result.message == "Restart requested for celery-worker-billing."
    assert captured["command"] == [
        "docker",
        "compose",
        "restart",
        "celery-worker-billing",
    ]
    assert captured["kwargs"]["timeout"] == 20.0


def test_restart_containers_uses_default_mapping(monkeypatch):
    monkeypatch.delenv("CELERY_WORKER_RESTART_CONTAINERS", raising=False)

    containers = worker_control.restart_containers()

    assert containers["celery-worker-billing"] == "dotmac_sub_celery_worker_billing"
    assert (
        containers["celery-worker-notifications-immediate"]
        == "dotmac_sub_celery_worker_notifications_immediate"
    )
    assert (
        containers["celery-worker-notifications"]
        == "dotmac_sub_celery_worker_notifications"
    )
    assert (
        containers["celery-worker-monitoring"] == "dotmac_sub_celery_worker_monitoring"
    )


def test_restart_worker_target_uses_docker_api_by_default(monkeypatch):
    captured = {}

    def fake_restart(target):
        captured["target"] = target
        return worker_control.WorkerRestartResult(
            target=target,
            ok=True,
            message=f"Restart requested for {target}.",
            returncode=0,
        )

    monkeypatch.setenv("CELERY_WORKER_RESTART_ENABLED", "true")
    monkeypatch.delenv("CELERY_WORKER_RESTART_COMMAND", raising=False)
    monkeypatch.setattr(worker_control, "_restart_with_docker_api", fake_restart)

    result = worker_control.restart_worker_target("celery-worker-billing")

    assert result.ok is True
    assert captured["target"] == "celery-worker-billing"
