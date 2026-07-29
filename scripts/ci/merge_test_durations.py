"""Merge duration records emitted by the parallel unit-test shards."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def merge_duration_files(paths: list[Path]) -> dict[str, float]:
    merged: defaultdict[str, float] = defaultdict(float)
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError(f"{path} has an unsupported duration schema")
        for name, duration in payload["durations"].items():
            merged[name] += float(duration)
    return dict(sorted(merged.items()))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    inputs = sorted(args.input_dir.glob("*.json"))
    if not inputs:
        raise SystemExit(f"no duration records found in {args.input_dir}")
    durations = merge_duration_files(inputs)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"schema_version": 1, "durations": durations}, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
