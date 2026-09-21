from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import bump_version


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_update_files_refreshes_dependency_manifest_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(bump_version, "VERSION_FILE", tmp_path / "VERSION")
    monkeypatch.setattr(
        bump_version,
        "COMPOSITION_OBSERVATION_FILE",
        tmp_path / "docs/kernel-runtime-composition.json",
    )

    _write(tmp_path / "VERSION", "1.2.3\n")
    _write(tmp_path / "package.json", '{"version": "1.2.3"}\n')
    _write(
        tmp_path / "package-lock.json",
        '{"version": "1.2.3", "packages": {"": {"version": "1.2.3"}}}\n',
    )
    _write(tmp_path / "pyproject.toml", 'version = "1.2.3"\n')
    _write(tmp_path / "mobile/pubspec.yaml", "version: 1.2.3+7\n")
    _write(
        tmp_path / "mobile/lib/src/config/env.dart",
        "const version = String.fromEnvironment('VERSION', defaultValue: '1.2.3');\n",
    )
    _write(
        tmp_path / "mobile/lib/main.dart",
        "options.release = 'dotmac-mobile@1.2.3';\n",
    )
    _write(tmp_path / "CHANGELOG.md", "# Changelog\n\n## 1.2.3 - 2026-01-01\n")
    _write(
        tmp_path / "docs/kernel-runtime-composition.json",
        json.dumps(
            {
                "schema_version": "dimensional-composition.v3",
                "observations": [
                    {
                        "id": "dependency-manifest",
                        "source_path": "pyproject.toml",
                        "source_blob_sha256": "stale-source",
                        "selector": "whole-file.v1",
                        "extract_sha256": "stale-extract",
                    },
                    {"id": "unrelated-observation", "evidence": "preserved"},
                ],
            },
            indent=2,
        )
        + "\n",
    )

    bump_version.update_files("1.2.4")

    expected_digest = hashlib.sha256(
        (tmp_path / "pyproject.toml").read_bytes()
    ).hexdigest()
    document = json.loads(
        (tmp_path / "docs/kernel-runtime-composition.json").read_text(encoding="utf-8")
    )
    dependency_manifest = document["observations"][0]
    assert dependency_manifest["source_blob_sha256"] == expected_digest
    assert dependency_manifest["extract_sha256"] == expected_digest
    assert document["observations"][1] == {
        "id": "unrelated-observation",
        "evidence": "preserved",
    }


def test_refresh_dependency_manifest_observation_fails_closed_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bump_version, "ROOT", tmp_path)
    monkeypatch.setattr(
        bump_version,
        "COMPOSITION_OBSERVATION_FILE",
        tmp_path / "docs/kernel-runtime-composition.json",
    )
    _write(tmp_path / "pyproject.toml", 'version = "1.2.4"\n')
    _write(
        tmp_path / "docs/kernel-runtime-composition.json",
        '{"observations": []}\n',
    )

    with pytest.raises(RuntimeError, match="exactly one dependency-manifest"):
        bump_version.refresh_dependency_manifest_observation()
