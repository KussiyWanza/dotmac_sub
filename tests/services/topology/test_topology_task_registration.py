"""Topology status warmer task is registered + routed.

(The Zabbix topology reconcile task that used to live alongside it was
retired with the native monitoring cutover.)
"""

from __future__ import annotations

from app.celery_app import celery_app

WARM_TASK = "app.tasks.topology_sync.warm_topology_status"


def test_warm_task_registered_and_routed():
    import app.tasks  # noqa: F401 - triggers task module imports

    assert WARM_TASK in celery_app.tasks
    assert celery_app.conf.task_routes[WARM_TASK] == {"queue": "monitoring"}


def test_retired_reconcile_task_not_registered():
    import app.tasks as tasks

    assert "run_topology_reconcile" not in tasks.__all__
    assert (
        "app.tasks.topology_sync.run_topology_reconcile"
        not in celery_app.conf.task_routes
    )
