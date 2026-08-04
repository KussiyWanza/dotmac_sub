"""Typed release-gate policy tests; no live GitHub calls."""

from __future__ import annotations

import pytest

from scripts.verify_github_release import (
    ReleaseBranch,
    ReleaseGateCommand,
    ReleaseGateRejected,
    ReleaseGateUnavailable,
    RepositorySlug,
    WorkflowRun,
    evaluate_release_gate,
    parse_workflow_runs,
    require_approved_release,
)

REVISION = "32eebc1a6ac05a21275ed4db6f3d1dd28514a045"


def _command(*, branch: ReleaseBranch = ReleaseBranch.MAIN) -> ReleaseGateCommand:
    return ReleaseGateCommand(
        repository=RepositorySlug.parse("michaelayoade/dotmac_sub"),
        revision=REVISION,
        branch=branch,
    )


def _run(
    name: str,
    *,
    branch: str = "main",
    revision: str = REVISION,
    status: str = "completed",
    conclusion: str | None = "success",
    run_number: int = 100,
    run_attempt: int = 1,
) -> WorkflowRun:
    return WorkflowRun(
        database_id=run_number * 10 + run_attempt,
        name=name,
        event="push",
        status=status,
        conclusion=conclusion,
        head_sha=revision,
        head_branch=branch,
        run_number=run_number,
        run_attempt=run_attempt,
    )


class _Source:
    def __init__(self, runs: tuple[WorkflowRun, ...]):
        self._runs = runs

    def list_runs(self, command: ReleaseGateCommand) -> tuple[WorkflowRun, ...]:
        assert command.revision == REVISION
        return self._runs


def test_exact_main_revision_requires_both_green_workflows() -> None:
    command = _command()
    outcome = require_approved_release(
        command,
        _Source((_run("CI"), _run("Mobile CI"))),
    )

    assert outcome.approved is True
    assert outcome.summary() == "CI=success; Mobile CI=success"


@pytest.mark.parametrize(
    "runs",
    [
        (_run("CI"),),
        (_run("CI", conclusion="failure"), _run("Mobile CI")),
        (_run("CI", status="in_progress", conclusion=None), _run("Mobile CI")),
        (_run("CI", branch="dev"), _run("Mobile CI", branch="dev")),
        (
            _run("CI", revision="f" * 40),
            _run("Mobile CI", revision="f" * 40),
        ),
    ],
)
def test_missing_failed_pending_or_wrong_release_evidence_is_rejected(
    runs: tuple[WorkflowRun, ...],
) -> None:
    with pytest.raises(ReleaseGateRejected):
        require_approved_release(_command(), _Source(runs))


def test_latest_attempt_is_authoritative() -> None:
    runs = (
        _run("CI", run_attempt=1),
        _run("CI", run_attempt=2, conclusion="failure"),
        _run("Mobile CI"),
    )

    outcome = evaluate_release_gate(_command(), runs)

    assert outcome.approved is False
    assert "CI=status=completed, conclusion=failure" in outcome.summary()


def test_staging_accepts_only_dev_evidence() -> None:
    command = _command(branch=ReleaseBranch.DEV)
    outcome = require_approved_release(
        command,
        _Source((_run("CI", branch="dev"), _run("Mobile CI", branch="dev"))),
    )

    assert outcome.approved is True


def test_github_payload_is_normalized_at_one_typed_boundary() -> None:
    payload = {
        "workflow_runs": [
            {
                "id": 101,
                "name": "CI",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
                "head_sha": REVISION,
                "head_branch": "main",
                "run_number": 10,
                "run_attempt": 2,
            }
        ]
    }

    assert parse_workflow_runs(payload) == (
        WorkflowRun(
            database_id=101,
            name="CI",
            event="push",
            status="completed",
            conclusion="success",
            head_sha=REVISION,
            head_branch="main",
            run_number=10,
            run_attempt=2,
        ),
    )


def test_malformed_github_payload_fails_closed() -> None:
    with pytest.raises(ReleaseGateUnavailable, match="workflow_runs"):
        parse_workflow_runs({"unexpected": []})
