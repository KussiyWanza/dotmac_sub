"""The checked R1 evidence distinguishes candidate, release, and Sub adoption."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = ROOT / "docs/audits/audit-r1-kernel-candidate.json"
RELEASE = ROOT / "docs/audits/audit-r1-kernel-release.json"
RUNBOOK = ROOT / "docs/audits/AUDIT_R1_KERNEL_INTEGRATION.md"


def _candidate() -> dict[str, object]:
    return json.loads(CANDIDATE.read_text(encoding="utf-8"))


def _release() -> dict[str, object]:
    return json.loads(RELEASE.read_text(encoding="utf-8"))


def test_candidate_evidence_remains_historical() -> None:
    candidate = _candidate()
    kernel = candidate["kernel"]
    sub = candidate["sub"]

    assert candidate["status"] == "candidate_not_released"
    assert kernel["candidate_version"] == "0.1.0a42"
    assert len(kernel["commit"]) == 40
    assert len(kernel["wheel_sha256"]) == 64
    assert len(sub["implementation_commit"]) == 40
    assert sub["released_kernel_pin"] == "0.1.0a40"


def test_released_artifact_is_the_exact_sub_pin_and_lock() -> None:
    import tomllib

    release = _release()
    kernel = release["kernel"]
    sub = release["sub"]

    assert release["status"] == "released_pinned_and_rehearsed"
    assert kernel["version"] == "0.1.0a42"
    assert len(kernel["source_commit"]) == 40
    assert kernel["tag"] == "dotmac-kernel-v0.1.0a42"
    assert kernel["release_workflow_run"] == 31592573094
    assert len(kernel["wheel_sha256"]) == 64
    assert len(kernel["sdist_sha256"]) == 64
    assert sub["released_kernel_pin"] == kernel["version"]

    rehearsal = release["rehearsal"]
    assert rehearsal["host"] == "observe"
    assert rehearsal["installed_kernel_version"] == kernel["version"]
    assert rehearsal["migration_head"] == "524_audit_events_kernel_r1"
    assert rehearsal["integration_tests_collected"] == 103
    assert rehearsal["integration_tests_passed"] == 103
    assert rehearsal["exit_code"] == 0
    assert rehearsal["disposable_resources_removed"] is True

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dotmac-kernel==0.1.0a42" in pyproject
    assert "dotmac-kernel==0.1.0a40" not in pyproject

    with (ROOT / "poetry.lock").open("rb") as fh:
        lock = tomllib.load(fh)
    locked = [
        package for package in lock["package"] if package["name"] == "dotmac-kernel"
    ]
    assert len(locked) == 1
    assert locked[0]["version"] == kernel["version"]
    hashes = {entry["file"]: entry["hash"] for entry in locked[0]["files"]}
    assert hashes[f"dotmac_kernel-{kernel['version']}-py3-none-any.whl"] == (
        f"sha256:{kernel['wheel_sha256']}"
    )
    assert hashes[f"dotmac_kernel-{kernel['version']}.tar.gz"] == (
        f"sha256:{kernel['sdist_sha256']}"
    )


def test_runbook_preserves_the_expansion_and_release_boundaries() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    required = (
        "authored together on integration branches",
        "cannot be released atomically",
        "created_at timestamptz NULL",
        "DEFAULT now()",
        "counts only",
        "audit-r1-kernel-release.json",
        "dotmac-kernel-v0.1.0a42",
        "103 integration tests: all passed",
        "not be described as kernel-lineage adoption",
    )
    assert all(phrase in runbook for phrase in required)
