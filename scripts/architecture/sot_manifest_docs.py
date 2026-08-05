"""Render and verify the generated SOT manifest section in the relationship map."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RELATIONSHIP_MAP = PROJECT_ROOT / "docs" / "SOT_RELATIONSHIP_MAP.md"
BEGIN_MARKER = "<!-- BEGIN GENERATED SOT MANIFEST -->"
END_MARKER = "<!-- END GENERATED SOT MANIFEST -->"


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def render_manifest_block(services: Iterable[Any]) -> str:
    """Return deterministic Markdown for all fully contracted services."""

    lines = [
        BEGIN_MARKER,
        "## Contracted Ownership Manifest",
        "",
        "This section is generated from the canonical aggregate in",
        "`app/services/sot_registry/registry.py` and its domain declarations.",
        "Edit the owning domain shard and regenerate; do not hand-edit these rows.",
        "",
        "| Service | Concern | Role | Authoritative inputs | Transaction | Migration | Steward | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for service in services:
        contract = service.contract
        if contract is None:
            continue
        inputs = {item.name: item for item in contract.authoritative_inputs}
        evidence = "<br>".join(
            f"`{reference}`" for reference in contract.design_refs + contract.test_refs
        )
        for concern in contract.concerns:
            authoritative_inputs = "<br>".join(
                f"{input_name} ← `{inputs[input_name].owner}`"
                if input_name in inputs
                else f"{input_name} ← `UNKNOWN INPUT`"
                for input_name in concern.input_names
            )
            values = (
                f"`{service.name}`",
                concern.name,
                f"`{concern.role.value}`",
                authoritative_inputs,
                f"`{contract.transaction.mode.value}`",
                f"`{contract.migration.state.value}`",
                contract.steward,
                evidence,
            )
            lines.append("| " + " | ".join(_escape(value) for value in values) + " |")
    lines.extend((END_MARKER, ""))
    return "\n".join(lines)


def extract_manifest_block(document: str) -> str | None:
    """Return the current generated block, or ``None`` when markers are absent."""

    if document.count(BEGIN_MARKER) != 1 or document.count(END_MARKER) != 1:
        return None
    start = document.index(BEGIN_MARKER)
    end = document.index(END_MARKER, start) + len(END_MARKER)
    return document[start:end] + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--print",
        action="store_true",
        dest="print_block",
        help="print the expected generated block instead of checking the map",
    )
    output_group.add_argument(
        "--write",
        action="store_true",
        dest="write_block",
        help="replace the generated block in the relationship map",
    )
    args = parser.parse_args()

    from app.services.sot_registry.registry import all_services

    expected = render_manifest_block(all_services())
    if args.print_block:
        print(expected, end="")
        return 0

    document = RELATIONSHIP_MAP.read_text(encoding="utf-8")
    current = extract_manifest_block(document)
    if args.write_block:
        if current is None:
            print("SOT relationship-map manifest markers are missing or duplicated.")
            return 1
        RELATIONSHIP_MAP.write_text(
            document.replace(current, expected, 1),
            encoding="utf-8",
        )
        print("SOT relationship-map manifest updated.")
        return 0
    if current != expected:
        print(
            "SOT relationship-map manifest is stale; run with --print and "
            "update the generated block."
        )
        return 1
    print("SOT relationship-map manifest is current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
