"""Select a deterministic, approximately balanced CI unit-test shard."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from statistics import median

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = REPOSITORY_ROOT / "tests"
EXCLUDED_PARTS = {"architecture", "e2e", "integration", "playwright"}
DEFAULT_DURATIONS_PATH = REPOSITORY_ROOT / ".ci-cache/test-durations.json"


def _test_files() -> list[Path]:
    return [
        path
        for path in TEST_ROOT.rglob("test_*.py")
        if not EXCLUDED_PARTS.intersection(path.relative_to(TEST_ROOT).parts)
    ]


def _load_durations(path: Path | None) -> dict[str, float]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    raw = payload.get("durations", payload) if isinstance(payload, Mapping) else {}
    if not isinstance(raw, Mapping):
        return {}
    durations: dict[str, float] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not isinstance(value, (int, float)):
            continue
        if value >= 0:
            durations[name] = float(value)
    return durations


def _weighted_files(
    paths: list[Path], *, durations_path: Path | None
) -> list[tuple[float, Path]]:
    durations = _load_durations(durations_path)
    measured_rates = [
        duration / path.stat().st_size
        for path in paths
        if path.stat().st_size
        and (duration := durations.get(path.relative_to(REPOSITORY_ROOT).as_posix()))
        is not None
    ]
    seconds_per_byte = median(measured_rates) if measured_rates else 1.0
    return [
        (
            durations.get(
                path.relative_to(REPOSITORY_ROOT).as_posix(),
                path.stat().st_size * seconds_per_byte,
            ),
            path,
        )
        for path in paths
    ]


def select_shard(
    *, shard: int, shards: int, durations_path: Path | None = DEFAULT_DURATIONS_PATH
) -> list[Path]:
    """Greedily balance files by measured time, with source size as fallback."""
    if shards < 1:
        raise ValueError("shards must be positive")
    if shard < 1 or shard > shards:
        raise ValueError(f"shard must be between 1 and {shards}")

    groups: list[list[Path]] = [[] for _ in range(shards)]
    weights = [0] * shards
    weighted_files = sorted(
        _weighted_files(_test_files(), durations_path=durations_path),
        key=lambda item: (-item[0], item[1].as_posix()),
    )
    for weight, path in weighted_files:
        target = min(range(shards), key=lambda index: (weights[index], index))
        groups[target].append(path)
        weights[target] += weight
    return sorted(groups[shard - 1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument(
        "--durations-file",
        type=Path,
        default=DEFAULT_DURATIONS_PATH,
    )
    args = parser.parse_args()
    paths = select_shard(
        shard=args.shard,
        shards=args.shards,
        durations_path=args.durations_file,
    )
    print(" ".join(path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths))


if __name__ == "__main__":
    main()
