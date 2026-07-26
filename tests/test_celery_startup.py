from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_celery_app_discovers_tasks_in_clean_process() -> None:
    """Production-style task discovery must not depend on pytest import order."""

    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTEST_VERSION": "1",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.celery_app import celery_app; "
                "required = {"
                "'app.tasks.olt_mac_harvest.run_olt_mac_harvest', "
                "'app.tasks.olt_mac_harvest.run_single_olt_mac_harvest'"
                "}; "
                "missing = required.difference(celery_app.tasks); "
                "assert not missing, missing"
            ),
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
