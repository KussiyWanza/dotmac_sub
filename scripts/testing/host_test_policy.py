"""Fail-closed policy for pytest execution on deployment hosts.

GitHub-hosted CI and development workspaces own full-suite validation. The
staging host may run a small, explicit, serial set of test files for diagnosis;
the production host must never run pytest. Deployment identity is read from
the two non-secret ``.env`` markers without sourcing the file.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

MAX_FOCUSED_TEST_FILES = 10
_IDENTITY_KEYS = frozenset({"APP_ENV", "SERVER_NAME"})
_OPTIONS_WITH_VALUES = frozenset(
    {
        "-c",
        "-k",
        "-m",
        "-o",
        "--basetemp",
        "--confcutdir",
        "--junitxml",
        "--override-ini",
        "--rootdir",
        "--tb",
    }
)


class DeploymentHostKind(StrEnum):
    """Execution environments relevant to repository test safety."""

    NON_DEPLOYMENT = "non_deployment"
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    UNKNOWN_DEPLOYMENT = "unknown_deployment"


class TestSelectionKind(StrEnum):
    """Supported pytest selection shapes on a deployment host."""

    FOCUSED_FILES = "focused_files"
    FULL_OR_AMBIGUOUS = "full_or_ambiguous"


@dataclass(frozen=True)
class DeploymentHostIdentity:
    """Classified deployment identity without secret configuration values."""

    kind: DeploymentHostKind


@dataclass(frozen=True)
class PytestInvocation:
    """Normalized pytest scope and worker request."""

    selection: TestSelectionKind
    selected_files: tuple[Path, ...]
    parallel_workers: str | None


@dataclass(frozen=True)
class HostTestDecision:
    """Typed allow/deny outcome consumed by pytest and Make adapters."""

    allowed: bool
    reason: str


class HostTestPolicyError(RuntimeError):
    """Raised when a deployment host refuses a test invocation."""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _dotenv_identity(env_path: Path) -> tuple[str | None, str | None]:
    if not env_path.is_file():
        return None, None

    values: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return "__unreadable__", "__unreadable__"

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key in _IDENTITY_KEYS:
            values[key] = _unquote(raw_value)
    return values.get("APP_ENV"), values.get("SERVER_NAME")


def classify_test_host(
    *,
    repo_root: Path,
    environ: Mapping[str, str],
) -> DeploymentHostIdentity:
    """Classify a host from consistent shell and deployment-file markers."""

    file_app_env, file_server_name = _dotenv_identity(repo_root / ".env")
    shell_app_env = environ.get("APP_ENV") or None
    shell_server_name = environ.get("SERVER_NAME") or None

    if file_app_env is not None or file_server_name is not None:
        if shell_app_env is not None and shell_app_env != file_app_env:
            return DeploymentHostIdentity(DeploymentHostKind.UNKNOWN_DEPLOYMENT)
        if shell_server_name is not None and shell_server_name != file_server_name:
            return DeploymentHostIdentity(DeploymentHostKind.UNKNOWN_DEPLOYMENT)
        app_env, server_name = file_app_env, file_server_name
    else:
        app_env, server_name = shell_app_env, shell_server_name

    if app_env is None and server_name is None:
        return DeploymentHostIdentity(DeploymentHostKind.NON_DEPLOYMENT)
    if (app_env, server_name) == ("staging", "dotmac-sub-staging"):
        return DeploymentHostIdentity(DeploymentHostKind.STAGING)
    if (app_env, server_name) == ("production", "dotmac-sub-prod"):
        return DeploymentHostIdentity(DeploymentHostKind.PRODUCTION)
    if app_env in {"development", "local", "test", "testing"} and server_name not in {
        "dotmac-sub-prod",
        "dotmac-sub-staging",
    }:
        return DeploymentHostIdentity(DeploymentHostKind.DEVELOPMENT)
    return DeploymentHostIdentity(DeploymentHostKind.UNKNOWN_DEPLOYMENT)


def _parallel_workers(args: Sequence[str]) -> str | None:
    for index, token in enumerate(args):
        value: str | None = None
        if token in {"-n", "--numprocesses"}:
            if index + 1 < len(args):
                value = args[index + 1]
        elif token.startswith("--numprocesses="):
            value = token.split("=", 1)[1]
        elif token.startswith("-n="):
            value = token.split("=", 1)[1]
        elif token.startswith("-n") and len(token) > 2:
            value = token[2:]
        if value is not None and value not in {"0", "no"}:
            return value
    return None


def _path_tokens(args: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    skip_next = False
    after_separator = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        # This literal is pytest's option separator, not credential material.
        if token == "--":  # nosec B105
            after_separator = True
            continue
        if not after_separator and token in _OPTIONS_WITH_VALUES | {
            "-n",
            "--numprocesses",
        }:
            skip_next = True
            continue
        if not after_separator and token.startswith("-"):
            continue
        found.append(token)
    return tuple(found)


def parse_pytest_invocation(
    *,
    repo_root: Path,
    argv: Sequence[str],
    environ: Mapping[str, str],
) -> PytestInvocation:
    """Normalize selectors from CLI and ``PYTEST_ADDOPTS`` inputs."""

    addopts = shlex.split(environ.get("PYTEST_ADDOPTS", ""))
    args = (*addopts, *argv)
    selected: list[Path] = []
    ambiguous = False
    for token in _path_tokens(args):
        raw_path = token.split("::", 1)[0]
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        if candidate.is_dir():
            ambiguous = True
        elif candidate.is_file() and candidate.suffix == ".py":
            selected.append(candidate.resolve())

    focused = (
        not ambiguous and bool(selected) and len(selected) <= MAX_FOCUSED_TEST_FILES
    )
    return PytestInvocation(
        selection=(
            TestSelectionKind.FOCUSED_FILES
            if focused
            else TestSelectionKind.FULL_OR_AMBIGUOUS
        ),
        selected_files=tuple(selected),
        parallel_workers=_parallel_workers(args),
    )


def decide_host_test(
    *,
    identity: DeploymentHostIdentity,
    invocation: PytestInvocation | None,
    full_suite_owner: bool = False,
) -> HostTestDecision:
    """Decide whether this host may execute the requested test scope."""

    if identity.kind in {
        DeploymentHostKind.NON_DEPLOYMENT,
        DeploymentHostKind.DEVELOPMENT,
    }:
        return HostTestDecision(True, "non-deployment test execution is allowed")
    if identity.kind is DeploymentHostKind.PRODUCTION:
        return HostTestDecision(
            False,
            "pytest is forbidden on the production host; run full validation in GitHub CI",
        )
    if identity.kind is DeploymentHostKind.UNKNOWN_DEPLOYMENT:
        return HostTestDecision(
            False,
            "pytest refused because APP_ENV and SERVER_NAME do not identify an approved host pair",
        )
    if full_suite_owner or invocation is None:
        return HostTestDecision(
            False,
            "full test suites are forbidden on staging; run them in GitHub CI",
        )
    if invocation.parallel_workers is not None:
        return HostTestDecision(
            False,
            "parallel pytest workers are forbidden on staging; run focused files serially",
        )
    if invocation.selection is not TestSelectionKind.FOCUSED_FILES:
        return HostTestDecision(
            False,
            "staging permits at most 10 explicitly named Python test files, run serially",
        )
    return HostTestDecision(True, "focused serial staging diagnosis is allowed")


def enforce_pytest_host_policy(
    *,
    repo_root: Path,
    argv: Sequence[str],
    environ: Mapping[str, str],
) -> None:
    """Raise a typed policy error before pytest imports the application graph."""

    identity = classify_test_host(repo_root=repo_root, environ=environ)
    invocation = parse_pytest_invocation(
        repo_root=repo_root,
        argv=argv,
        environ=environ,
    )
    decision = decide_host_test(identity=identity, invocation=invocation)
    if not decision.allowed:
        raise HostTestPolicyError(decision.reason)


def require_full_suite_host(
    *,
    repo_root: Path,
    environ: Mapping[str, str],
) -> None:
    """Refuse a Make-owned full suite before preparation or collection begins."""

    identity = classify_test_host(repo_root=repo_root, environ=environ)
    decision = decide_host_test(
        identity=identity,
        invocation=None,
        full_suite_owner=True,
    )
    if not decision.allowed:
        raise HostTestPolicyError(decision.reason)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI adapter used by Make before any full-suite setup step."""

    args = tuple(argv if argv is not None else sys.argv[1:])
    if args != ("full-suite",):
        print(
            "usage: python -m scripts.testing.host_test_policy full-suite",
            file=sys.stderr,
        )
        return 2
    repo_root = Path(__file__).resolve().parents[2]
    try:
        require_full_suite_host(repo_root=repo_root, environ=os.environ)
    except HostTestPolicyError as exc:
        print(f"TEST HOST POLICY: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
