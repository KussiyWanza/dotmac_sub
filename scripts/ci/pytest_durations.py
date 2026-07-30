"""Capture aggregate test-file durations from pytest and pytest-xdist."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

_durations: defaultdict[str, float] = defaultdict(float)
_output_path: Path | None = None


def pytest_addoption(parser: Any) -> None:
    group = parser.getgroup("dotmac-ci")
    group.addoption(
        "--ci-durations-output",
        type=Path,
        help="Write aggregate test-file durations as JSON.",
    )


def pytest_configure(config: Any) -> None:
    global _output_path
    if hasattr(config, "workerinput"):
        return
    _durations.clear()
    _output_path = config.getoption("--ci-durations-output")


def pytest_runtest_logreport(report: Any) -> None:
    if _output_path is None:
        return
    test_file = report.nodeid.split("::", 1)[0]
    _durations[test_file] += float(report.duration)


def pytest_sessionfinish(session: Any) -> None:
    if hasattr(session.config, "workerinput") or _output_path is None:
        return
    write_durations(_output_path, _durations)


def write_durations(path: Path, durations: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "durations": {
            name: round(duration, 6)
            for name, duration in sorted(durations.items())
            if duration >= 0
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
