"""Run inbox controller regressions in the canonical non-integration test lane."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def test_inbox_navigation_controller_regressions() -> None:
    """Missing Node or failing controller tests are failures, never silent skips."""
    node = shutil.which("node")
    assert node is not None, "Node.js is required for inbox controller regression tests"
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    # The override is only for a manual red/green demonstration against old code.
    # Normal CI must exercise the controller actually checked into this tree.
    environment.pop("INBOX_SCRIPT", None)
    result = subprocess.run(
        [node, "--test", "tests/js/inbox_navigation.test.js"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
