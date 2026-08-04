"""Fail closed unless an immutable release has green GitHub workflow runs.

Deployment hosts do not run pytest.  This adapter verifies that GitHub-hosted
CI already accepted the exact OCI revision before the deployment owner backs up
or migrates the target database.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

GITHUB_API_ROOT = "https://api.github.com"
DEFAULT_REPOSITORY = "michaelayoade/dotmac_sub"
REQUIRED_WORKFLOWS = ("CI", "Mobile CI")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class ReleaseBranch(str, Enum):
    """Promotion branch accepted for a deployment environment."""

    DEV = "dev"
    MAIN = "main"


@dataclass(frozen=True, slots=True)
class RepositorySlug:
    """Validated GitHub owner/repository identity."""

    owner: str
    name: str

    @classmethod
    def parse(cls, value: str) -> RepositorySlug:
        parts = value.strip().split("/")
        if (
            len(parts) != 2
            or not all(parts)
            or any(_REPOSITORY_COMPONENT.fullmatch(part) is None for part in parts)
        ):
            raise ValueError("repository must use the validated owner/name form")
        return cls(owner=parts[0], name=parts[1])

    def render(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class ReleaseGateCommand:
    """Exact immutable release whose GitHub evidence must be accepted."""

    repository: RepositorySlug
    revision: str
    branch: ReleaseBranch
    required_workflows: tuple[str, ...] = REQUIRED_WORKFLOWS

    def __post_init__(self) -> None:
        if _FULL_SHA.fullmatch(self.revision) is None:
            raise ValueError("revision must be a full lowercase 40-character SHA")
        if not self.required_workflows or any(
            not workflow.strip() for workflow in self.required_workflows
        ):
            raise ValueError("at least one named required workflow is mandatory")
        if len(set(self.required_workflows)) != len(self.required_workflows):
            raise ValueError("required workflow names must be unique")


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """Normalized GitHub Actions observation used by release policy."""

    database_id: int
    name: str
    event: str
    status: str
    conclusion: str | None
    head_sha: str
    head_branch: str
    run_number: int
    run_attempt: int


@dataclass(frozen=True, slots=True)
class WorkflowDecision:
    """Decision evidence for one required workflow."""

    workflow: str
    approved: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ReleaseGateOutcome:
    """Typed result of evaluating every required workflow."""

    command: ReleaseGateCommand
    decisions: tuple[WorkflowDecision, ...]

    @property
    def approved(self) -> bool:
        return bool(self.decisions) and all(item.approved for item in self.decisions)

    def summary(self) -> str:
        return "; ".join(f"{item.workflow}={item.evidence}" for item in self.decisions)


class WorkflowRunSource(Protocol):
    """Observation adapter supplying Actions runs for one release."""

    def list_runs(self, command: ReleaseGateCommand) -> tuple[WorkflowRun, ...]: ...


class ReleaseGateError(RuntimeError):
    """Base release-gate failure safe for operator output."""


class ReleaseGateUnavailable(ReleaseGateError):
    """GitHub evidence could not be retrieved or normalized."""


class ReleaseGateRejected(ReleaseGateError):
    """GitHub evidence exists but does not approve the release."""


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ReleaseGateUnavailable(f"GitHub returned an invalid {label} object")
    return cast(Mapping[str, object], value)


def _required_string(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ReleaseGateUnavailable(f"GitHub workflow run omitted {key}")
    return value


def _optional_string(raw: Mapping[str, object], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReleaseGateUnavailable(f"GitHub workflow run has invalid {key}")
    return value


def _required_integer(raw: Mapping[str, object], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReleaseGateUnavailable(f"GitHub workflow run omitted {key}")
    return value


def parse_workflow_runs(payload: object) -> tuple[WorkflowRun, ...]:
    """Normalize the narrow GitHub response at the transport boundary."""

    root = _mapping(payload, label="workflow-runs response")
    raw_runs = root.get("workflow_runs")
    if not isinstance(raw_runs, list):
        raise ReleaseGateUnavailable("GitHub response omitted workflow_runs")
    normalized: list[WorkflowRun] = []
    for value in raw_runs:
        raw = _mapping(value, label="workflow run")
        normalized.append(
            WorkflowRun(
                database_id=_required_integer(raw, "id"),
                name=_required_string(raw, "name"),
                event=_required_string(raw, "event"),
                status=_required_string(raw, "status"),
                conclusion=_optional_string(raw, "conclusion"),
                head_sha=_required_string(raw, "head_sha"),
                head_branch=_required_string(raw, "head_branch"),
                run_number=_required_integer(raw, "run_number"),
                run_attempt=_required_integer(raw, "run_attempt"),
            )
        )
    return tuple(normalized)


class GitHubActionsSource:
    """Read exact-SHA workflow observations from GitHub's REST API."""

    def __init__(self, *, token: str | None = None, timeout_seconds: float = 15.0):
        self._token = (token or "").strip() or None
        self._timeout_seconds = timeout_seconds

    def list_runs(self, command: ReleaseGateCommand) -> tuple[WorkflowRun, ...]:
        query = urlencode(
            {
                "head_sha": command.revision,
                "event": "push",
                "per_page": "100",
            }
        )
        url = (
            f"{GITHUB_API_ROOT}/repos/{command.repository.owner}/"
            f"{command.repository.name}/actions/runs?{query}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "dotmac-sub-release-gate",
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        request = Request(url, headers=headers)  # noqa: S310 - fixed HTTPS API root
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                payload = json.load(response)
        except HTTPError as exc:
            raise ReleaseGateUnavailable(
                f"GitHub workflow evidence request failed with HTTP {exc.code}"
            ) from None
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise ReleaseGateUnavailable(
                f"GitHub workflow evidence request failed: {type(exc).__name__}"
            ) from None
        return parse_workflow_runs(payload)


def evaluate_release_gate(
    command: ReleaseGateCommand, runs: Sequence[WorkflowRun]
) -> ReleaseGateOutcome:
    """Require the latest exact-SHA push run of every workflow to be green."""

    decisions: list[WorkflowDecision] = []
    for required_name in command.required_workflows:
        candidates = [
            run
            for run in runs
            if run.name == required_name
            and run.event == "push"
            and run.head_sha == command.revision
            and run.head_branch == command.branch.value
        ]
        if not candidates:
            decisions.append(
                WorkflowDecision(required_name, False, "missing exact-branch push run")
            )
            continue
        latest = max(
            candidates,
            key=lambda run: (run.run_number, run.run_attempt, run.database_id),
        )
        approved = latest.status == "completed" and latest.conclusion == "success"
        evidence = (
            "success"
            if approved
            else f"status={latest.status}, conclusion={latest.conclusion or 'none'}"
        )
        decisions.append(WorkflowDecision(required_name, approved, evidence))
    return ReleaseGateOutcome(command=command, decisions=tuple(decisions))


def require_approved_release(
    command: ReleaseGateCommand, source: WorkflowRunSource
) -> ReleaseGateOutcome:
    """Fetch and enforce the GitHub-owned release decision."""

    outcome = evaluate_release_gate(command, source.list_runs(command))
    if not outcome.approved:
        raise ReleaseGateRejected(outcome.summary())
    return outcome


def _token_from_environment() -> str | None:
    for name in ("GITHUB_DEPLOY_GATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Require green GitHub workflow runs for an immutable release"
    )
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--branch",
        required=True,
        choices=tuple(branch.value for branch in ReleaseBranch),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        command = ReleaseGateCommand(
            repository=RepositorySlug.parse(args.repository),
            revision=args.revision,
            branch=ReleaseBranch(args.branch),
        )
        outcome = require_approved_release(
            command,
            GitHubActionsSource(token=_token_from_environment()),
        )
    except (ValueError, ReleaseGateError) as exc:
        print(f"GITHUB RELEASE GATE REJECTED: {exc}", file=sys.stderr)
        return 1
    print(
        "GITHUB RELEASE GATE APPROVED: "
        f"{command.repository.render()} {command.revision[:12]} "
        f"branch={command.branch.value}; {outcome.summary()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
