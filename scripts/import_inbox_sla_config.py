"""Plan a sanitized Inbox SLA configuration import without operational data.

Input is a sanitised JSON export containing configuration only.  The command
is dry-run by default and refuses unresolved team/channel/priority mappings.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True, slots=True)
class ImportSummary:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    rejected: int = 0
    unresolved: tuple[str, ...] = ()


class SourceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_team_id: str | int | None = None
    source_channel: str | None = None
    source_priority: str | int | None = None


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    rules: tuple[SourceRule, ...] = Field(min_length=1)


class ImportSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policies: tuple[SourcePolicy, ...]


class ImportMappings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team: dict[str, str] = Field(default_factory=dict)
    channel: dict[str, str] = Field(default_factory=dict)
    priority: dict[str, str] = Field(default_factory=dict)


def plan_import(source: ImportSource, mappings: ImportMappings) -> ImportSummary:
    unresolved: set[str] = set()
    for policy in source.policies:
        for rule in policy.rules:
            for kind, key, mapping in (
                ("team", rule.source_team_id, mappings.team),
                ("channel", rule.source_channel, mappings.channel),
                ("priority", rule.source_priority, mappings.priority),
            ):
                if key is not None and str(key) not in mapping:
                    unresolved.add(f"{kind}:{key}")
    return ImportSummary(
        skipped=len(source.policies),
        unresolved=tuple(sorted(unresolved)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--mappings",
        type=Path,
        required=True,
        help="JSON maps with team, channel, and priority keys",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply only after a clean dry-run and explicit mapping review",
    )
    args = parser.parse_args()
    source = ImportSource.model_validate_json(
        args.input.read_text(encoding="utf-8-sig")
    )
    mappings = ImportMappings.model_validate_json(
        args.mappings.read_text(encoding="utf-8-sig")
    )
    summary = plan_import(source, mappings)
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "dry-run",
                "created": summary.created,
                "updated": summary.updated,
                "skipped": summary.skipped,
                "rejected": summary.rejected,
                "unresolved_mappings": list(summary.unresolved),
            },
            sort_keys=True,
        )
    )
    if summary.unresolved:
        print(
            "Import refused: unresolved mappings must be reviewed; no configuration was changed."
        )
        return 2
    if not args.apply:
        print("Dry run only: no database writes were performed.")
        return 0
    raise SystemExit(
        "Apply mode requires the reviewed database adapter to be enabled in the deployment runbook."
    )


if __name__ == "__main__":
    raise SystemExit(main())
