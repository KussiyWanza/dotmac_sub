"""Architecture guard for customer-visible device verification capacity."""

from __future__ import annotations

import re
from pathlib import Path

from app.celery_app import celery_app

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MONITORING_TASKS = {
    "app.tasks.infrastructure_polling.run_infrastructure_poll",
    "app.tasks.topology_sync.warm_topology_status",
}
MAC_HARVEST_TASKS = {
    "app.tasks.olt_mac_harvest.run_olt_mac_harvest",
    "app.tasks.olt_mac_harvest.run_single_olt_mac_harvest",
}


def test_monitoring_tasks_have_a_reserved_declared_queue():
    declared = {queue.name for queue in celery_app.conf.task_queues}

    assert "monitoring" in declared
    for task_name in MONITORING_TASKS:
        assert celery_app.conf.task_routes[task_name] == {"queue": "monitoring"}
    for task_name in MAC_HARVEST_TASKS:
        assert celery_app.conf.task_routes[task_name] == {"queue": "ingestion"}


def test_compose_has_a_dedicated_monitoring_consumer():
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    match = re.search(
        r"(?ms)^  celery-worker-monitoring:\n(?P<body>.*?)(?=^  [\w-]+:\n|\Z)",
        compose,
    )

    assert match is not None
    worker = match.group("body")
    assert "dotmac_sub_celery_worker_monitoring" in worker
    assert "\n    - --concurrency=2\n" in worker
    assert "\n    - monitoring\n" in worker
    assert "\n    - ingestion\n" not in worker


def test_all_runtime_workflows_include_the_monitoring_worker():
    dev_compose = (PROJECT_ROOT / "docker-compose.dev.yml").read_text()
    makefile = (PROJECT_ROOT / "Makefile").read_text()
    deploy = (PROJECT_ROOT / "scripts/deploy.sh").read_text()

    assert "celery-worker-monitoring:" in dev_compose
    assert "prod-restart" in makefile
    assert "celery-worker-monitoring" in makefile
    assert "APP_SERVICES=" in deploy
    assert "celery-worker-monitoring" in deploy
