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


def test_released_artifact_preserves_the_exact_historical_a42_evidence() -> None:
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
