"""Select one deterministic, approximately balanced integration-test shard."""

from __future__ import annotations

import argparse
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INTEGRATION_ROOT = REPOSITORY_ROOT / "tests" / "integration"


def select_integration_shard(*, shard: int, shards: int) -> tuple[Path, ...]:
    """Balance integration files by source size without splitting test modules."""

    if shards < 1:
        raise ValueError("shards must be positive")
    if shard < 1 or shard > shards:
        raise ValueError(f"shard must be between 1 and {shards}")

    groups: list[list[Path]] = [[] for _ in range(shards)]
    weights = [0] * shards
    files = sorted(
        INTEGRATION_ROOT.glob("test_*.py"),
        key=lambda path: (-path.stat().st_size, path.as_posix()),
    )
    for path in files:
        target = min(range(shards), key=lambda index: (weights[index], index))
        groups[target].append(path)
        weights[target] += path.stat().st_size
    return tuple(sorted(groups[shard - 1]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--shards", type=int, required=True)
    args = parser.parse_args()
    paths = select_integration_shard(shard=args.shard, shards=args.shards)
    print(" ".join(path.relative_to(REPOSITORY_ROOT).as_posix() for path in paths))


if __name__ == "__main__":
    main()
