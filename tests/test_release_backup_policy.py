from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.release_artifact_contract import BackupMode
from scripts.release_backup_policy import (
    BackupEvidenceError,
    _decision_from_document,
    describe_migration_tree,
    main,
)


def _write_migration(
    directory: Path,
    name: str,
    revision: str,
    down_revision: str | tuple[str, ...] | None,
) -> None:
    (directory / name).write_text(
        f"revision: str = {revision!r}\ndown_revision = {down_revision!r}\n",
        encoding="utf-8",
    )


def test_describe_migration_tree_hashes_bytes_and_derives_all_heads(
    tmp_path: Path,
) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    _write_migration(versions, "001.py", "001", None)
    _write_migration(versions, "002.py", "002", "001")
    _write_migration(versions, "003.py", "003", "001")

    first_digest, heads = describe_migration_tree(versions)
    _write_migration(versions, "003.py", "003", "002")
    second_digest, second_heads = describe_migration_tree(versions)

    assert heads.revisions == ("002", "003")
    assert second_heads.revisions == ("003",)
    assert first_digest != second_digest


def test_production_hotfix_decision_revalidates_identical_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    image = {
        "schema_version": 1,
        "kind": "dotmac.migration_image_state",
        "graph_digest": "sha256:" + "a" * 64,
        "heads": ["002"],
    }
    running = tmp_path / "running.json"
    candidate = tmp_path / "candidate.json"
    decision = tmp_path / "decision.json"
    running.write_text(json.dumps(image), encoding="utf-8")
    candidate.write_text(json.dumps(image), encoding="utf-8")

    assert (
        main(
            [
                "write-production-decision",
                "--running-image",
                str(running),
                "--candidate-image",
                str(candidate),
                "--database-head",
                "002",
                "--change-reference",
                "INC-42",
                "--reason",
                "Route-only hotfix",
                "--output",
                str(decision),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "skip_production_hotfix"
    assert _decision_from_document(decision).mode is BackupMode.SKIP_PRODUCTION_HOTFIX


def test_changed_candidate_graph_forces_backup_and_tampering_is_rejected(
    tmp_path: Path,
) -> None:
    running = tmp_path / "running.json"
    candidate = tmp_path / "candidate.json"
    decision = tmp_path / "decision.json"
    for path, digest in ((running, "a"), (candidate, "b")):
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "dotmac.migration_image_state",
                    "graph_digest": "sha256:" + digest * 64,
                    "heads": ["002"],
                }
            ),
            encoding="utf-8",
        )
    main(
        [
            "write-production-decision",
            "--running-image",
            str(running),
            "--candidate-image",
            str(candidate),
            "--database-head",
            "002",
            "--change-reference",
            "INC-42",
            "--reason",
            "Route-only hotfix",
            "--output",
            str(decision),
        ]
    )
    document = json.loads(decision.read_text(encoding="utf-8"))
    assert document["mode"] == "required"
    document["mode"] = "skip_production_hotfix"
    decision.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(BackupEvidenceError, match="does not match"):
        _decision_from_document(decision)
