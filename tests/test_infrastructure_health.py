from types import SimpleNamespace

from app.services import infrastructure_health


def test_skipped_health_checks_parses_known_names(monkeypatch):
    monkeypatch.setattr(
        infrastructure_health,
        "settings",
        SimpleNamespace(
            infrastructure_health_skip_checks=("genieacs, celery, radius-db, unknown")
        ),
    )

    assert infrastructure_health._skipped_health_checks() == {
        "genieacs",
        "celery",
        "radius_db",
    }


def test_check_all_services_marks_skipped_probe_not_configured(monkeypatch):
    monkeypatch.setattr(
        infrastructure_health,
        "settings",
        SimpleNamespace(infrastructure_health_skip_checks="genieacs"),
    )
    called: list[str] = []

    def fake(name):
        def check(_db):
            called.append(name)
            return infrastructure_health.ServiceStatus(name=name, status="up")

        return check

    for key in (
        "postgres",
        "redis",
        "victoriametrics",
        "radius_db",
        "minio",
        "celery",
        "nominatim",
    ):
        monkeypatch.setattr(infrastructure_health, f"_check_{key}", fake(key))

    def forbidden(_db):
        raise AssertionError("skipped GenieACS probe was called")

    monkeypatch.setattr(infrastructure_health, "_check_genieacs", forbidden)

    services = infrastructure_health.check_all_services(object())

    genieacs = next(service for service in services if service.name == "GenieACS")
    assert genieacs.status == "not_configured"
    assert genieacs.details["reason"] == "disabled_by_configuration"
    assert "genieacs" not in called


def test_expected_celery_queues_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("CELERY_EXPECTED_QUEUES", raising=False)

    queues = infrastructure_health._expected_celery_queues()

    assert queues == [
        "celery",
        "notifications_immediate",
        "notifications",
        "nin",
        "tr069",
        "acs",
        "bandwidth",
        "monitoring",
        "ingestion",
        "crm",
        "billing",
    ]


def test_expected_celery_queues_parses_env(monkeypatch):
    monkeypatch.setenv("CELERY_EXPECTED_QUEUES", "celery, billing, tr069")

    queues = infrastructure_health._expected_celery_queues()

    assert queues == ["celery", "billing", "tr069"]


def test_celery_queue_restart_targets_default_mapping(monkeypatch):
    monkeypatch.delenv("CELERY_QUEUE_RESTART_TARGETS", raising=False)

    targets = infrastructure_health._celery_queue_restart_targets()

    assert targets["billing"] == "celery-worker-billing"
    assert targets["notifications_immediate"] == "celery-worker-notifications-immediate"
    assert targets["notifications"] == "celery-worker-notifications"
    assert targets["tr069"] == "celery-worker-tr069"
    assert targets["monitoring"] == "celery-worker-monitoring"
    assert targets["ingestion"] == "celery-worker-ingestion"


def test_celery_queue_restart_targets_parses_env(monkeypatch):
    monkeypatch.setenv(
        "CELERY_QUEUE_RESTART_TARGETS",
        "celery=worker-default,billing=worker-billing",
    )

    targets = infrastructure_health._celery_queue_restart_targets()

    assert targets == {
        "celery": "worker-default",
        "billing": "worker-billing",
    }


def test_celery_unavailable_details_marks_expected_queues_missing(monkeypatch):
    monkeypatch.setenv("CELERY_EXPECTED_QUEUES", "celery,billing")
    monkeypatch.setenv(
        "CELERY_QUEUE_RESTART_TARGETS",
        "celery=worker-default,billing=worker-billing",
    )

    details = infrastructure_health._celery_unavailable_details("No workers")

    assert details["error"] == "No workers"
    assert details["expected_queues"] == ["celery", "billing"]
    assert details["missing_queues"] == ["celery", "billing"]
    assert details["queue_restart_targets"] == {
        "celery": "worker-default",
        "billing": "worker-billing",
    }
