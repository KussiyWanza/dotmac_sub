from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ci.merge_test_durations import merge_duration_files
from scripts.ci.pytest_durations import write_durations


def test_duration_writer_and_merger_preserve_aggregate_file_times(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_durations(
        first,
        {
            "tests/test_alpha.py": 1.25,
            "tests/test_shared.py": 0.75,
        },
    )
    write_durations(
        second,
        {
            "tests/test_beta.py": 2.5,
            "tests/test_shared.py": 0.25,
        },
    )

    assert merge_duration_files([first, second]) == {
        "tests/test_alpha.py": 1.25,
        "tests/test_beta.py": 2.5,
        "tests/test_shared.py": 1.0,
    }
    assert json.loads(first.read_text(encoding="utf-8"))["schema_version"] == 1


def test_duration_merger_rejects_an_unknown_schema(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps({"schema_version": 2, "durations": {}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported duration schema"):
        merge_duration_files([invalid])
