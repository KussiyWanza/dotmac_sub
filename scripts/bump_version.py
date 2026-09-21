#!/usr/bin/env python3
"""Bump DotMac Sub's app version across package metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"
COMPOSITION_OBSERVATION_FILE = ROOT / "docs/kernel-runtime-composition.json"

DEPENDENCY_MANIFEST_OBSERVATION_ID = "dependency-manifest"
DEPENDENCY_MANIFEST_SOURCE_PATH = "pyproject.toml"
DEPENDENCY_MANIFEST_SELECTOR = "whole-file.v1"

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def parse_version(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(version.strip())
    if not match:
        raise ValueError(f"Expected semantic version like 1.2.3, got {version!r}")
    return tuple(int(part) for part in match.groups())


def current_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def next_version(current: str, bump: str | None, explicit: str | None) -> str:
    if explicit:
        parse_version(explicit)
        return explicit

    major, minor, patch = parse_version(current)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"

    raise ValueError("Choose a bump type or pass --set VERSION")


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        rel = path.relative_to(ROOT)
        raise RuntimeError(f"Expected exactly one match in {rel} for {pattern!r}")
    path.write_text(new_text, encoding="utf-8")


def update_json_version(path: Path, version: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def update_package_lock(version: str) -> None:
    path = ROOT / "package-lock.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["version"] = version
    root_package = data.get("packages", {}).get("")
    if isinstance(root_package, dict):
        root_package["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def update_mobile_pubspec(version: str) -> None:
    path = ROOT / "mobile/pubspec.yaml"
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\d+\.\d+\.\d+)\+(\d+)", text, re.MULTILINE)
    if not match:
        raise RuntimeError("Expected one Flutter version line in mobile/pubspec.yaml")

    current_version_text, current_build_text = match.groups()
    build = int(current_build_text)
    if current_version_text != version:
        build += 1

    new_text = (
        text[: match.start()] + f"version: {version}+{build}" + text[match.end() :]
    )
    path.write_text(new_text, encoding="utf-8")


def update_changelog(version: str) -> None:
    path = ROOT / "CHANGELOG.md"
    today = date.today().isoformat()
    text = path.read_text(encoding="utf-8")
    heading = f"## {version} - {today}"
    if heading in text:
        return

    insert = f"## {version} - {today}\n\n- Version bump.\n\n"
    marker = "\n## "
    index = text.find(marker)
    if index == -1:
        text = text.rstrip() + "\n\n" + insert
    else:
        text = text[: index + 1] + insert + text[index + 1 :]
    path.write_text(text, encoding="utf-8")


def _normalize_json_object(value: object, *, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected {context} to be a JSON object")

    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RuntimeError(f"Expected every key in {context} to be a string")
        normalized[key] = item
    return normalized


def refresh_dependency_manifest_observation() -> None:
    """Bind the dependency-manifest observation to the updated manifest bytes."""

    document_value: object = json.loads(
        COMPOSITION_OBSERVATION_FILE.read_text(encoding="utf-8")
    )
    document = _normalize_json_object(
        document_value,
        context="the Kernel composition observation document",
    )
    observation_values = document.get("observations")
    if not isinstance(observation_values, list):
        raise RuntimeError(
            "Expected the Kernel composition observation document to contain "
            "an observations list"
        )

    observations = [
        _normalize_json_object(
            value,
            context=f"Kernel composition observation row {index}",
        )
        for index, value in enumerate(observation_values)
    ]
    dependency_manifest_rows = [
        row
        for row in observations
        if row.get("id") == DEPENDENCY_MANIFEST_OBSERVATION_ID
    ]
    if len(dependency_manifest_rows) != 1:
        raise RuntimeError(
            "Expected exactly one dependency-manifest Kernel composition "
            f"observation, found {len(dependency_manifest_rows)}"
        )

    dependency_manifest = dependency_manifest_rows[0]
    expected_coordinates = {
        "source_path": DEPENDENCY_MANIFEST_SOURCE_PATH,
        "selector": DEPENDENCY_MANIFEST_SELECTOR,
    }
    for field, expected in expected_coordinates.items():
        actual = dependency_manifest.get(field)
        if actual != expected:
            raise RuntimeError(
                f"Expected dependency-manifest {field} to be {expected!r}, "
                f"got {actual!r}"
            )

    manifest_digest = hashlib.sha256(
        (ROOT / DEPENDENCY_MANIFEST_SOURCE_PATH).read_bytes()
    ).hexdigest()
    dependency_manifest["source_blob_sha256"] = manifest_digest
    dependency_manifest["extract_sha256"] = manifest_digest
    document["observations"] = observations
    COMPOSITION_OBSERVATION_FILE.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )


def update_files(version: str) -> None:
    VERSION_FILE.write_text(version + "\n", encoding="utf-8")
    update_json_version(ROOT / "package.json", version)
    update_package_lock(version)

    replace_once(
        ROOT / "pyproject.toml",
        r'^version = "[^"]+"',
        f'version = "{version}"',
    )
    update_mobile_pubspec(version)
    replace_once(
        ROOT / "mobile/lib/src/config/env.dart",
        r"defaultValue: '\d+\.\d+\.\d+'",
        f"defaultValue: '{version}'",
    )
    replace_once(
        ROOT / "mobile/lib/main.dart",
        r"options\.release = 'dotmac-mobile@\d+\.\d+\.\d+';",
        f"options.release = 'dotmac-mobile@{version}';",
    )
    update_changelog(version)
    refresh_dependency_manifest_observation()


def run_git(args: list[str]) -> None:
    subprocess.run(["git", *args], cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bump DotMac Sub's semantic app version."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("bump", nargs="?", choices=("major", "minor", "patch"))
    group.add_argument("--set", dest="explicit_version", metavar="VERSION")
    parser.add_argument(
        "--tag",
        action="store_true",
        help="Create an annotated Git tag like v1.2.3 after updating files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the target version without changing files.",
    )
    args = parser.parse_args()

    current = current_version()
    version = next_version(current, args.bump, args.explicit_version)

    if args.dry_run:
        print(version)
        return 0

    update_files(version)
    print(f"Bumped version: {current} -> {version}")
    print("Updated VERSION, package.json, package-lock.json, pyproject.toml,")
    print("mobile/pubspec.yaml, mobile Dart version defaults, CHANGELOG.md, and")
    print("the dependency-manifest Kernel composition observation.")

    if args.tag:
        run_git(["tag", "-a", f"v{version}", "-m", f"Release v{version}"])
        print(f"Created Git tag v{version}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
