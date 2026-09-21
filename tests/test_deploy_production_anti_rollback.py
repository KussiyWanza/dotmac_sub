from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from scripts.release_artifact_contract import (
    EvidenceConclusion,
    GitCommitSha,
    GitTreeSha,
    MainAuthorizationEvidence,
    OCIImageDigest,
    ProductionBootstrapAuthorization,
    ProductionServerName,
    ProductManifestDigest,
    ReleaseArtifactEvidence,
    ReleaseCandidateRecord,
    StagingAcceptanceEvidence,
    StagingDeploymentId,
    WorkflowRunId,
)
from scripts.release_candidate_evidence import (
    write_bootstrap_authorization,
    write_production_authorization,
)

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PRODUCTION = ROOT / "scripts" / "deploy_production.sh"
IMAGE_DIGEST = "sha256:" + "a" * 64

GATE_START = "# --- Anti-rollback gate"
GATE_END = "# --- End anti-rollback gate"

# Docker verbs that change the production host. A refusal must emit none of
# them: the gate has to run before the hotfix evidence collection (which pulls
# images and creates throwaway containers) and before deploy.sh (which owns the
# database backup and `alembic upgrade`).
MUTATING_DOCKER_VERBS = ("pull", "create", "run", "start", "exec", "cp", "compose")


class AdapterRun(NamedTuple):
    """One observed invocation of the production adapter."""

    result: subprocess.CompletedProcess[str]
    deploy_marker: Path
    docker_log: Path

    def deploy_was_delegated(self) -> bool:
        return self.deploy_marker.exists()

    def docker_commands(self) -> list[list[str]]:
        if not self.docker_log.exists():
            return []
        return [
            line.split()
            for line in self.docker_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def mutating_docker_commands(self) -> list[list[str]]:
        return [
            command
            for command in self.docker_commands()
            if command and command[0] in MUTATING_DOCKER_VERBS
        ]


class ReleaseHistory(NamedTuple):
    """A two-commit git history the adapter's ancestry check can resolve.

    The gate asks real git whether the running revision is an ancestor of the
    target, so the test needs real commits -- but it must NOT read this
    repository's own history. CI checks the repository out at ``fetch-depth:
    1``, where the checked-out commit is grafted as parentless and ``HEAD^``
    does not resolve at all. A purpose-built repository makes the ancestry
    facts explicit and independent of how the runner cloned anything.
    """

    repo: Path
    parent: str
    child: str
    parent_tree: str
    child_tree: str


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    ).stdout.strip()


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> ReleaseHistory:
    repo = tmp_path_factory.mktemp("release-history")
    _git(repo, "init", "--quiet", "--initial-branch=main")
    _git(repo, "config", "user.email", "release@dotmac.test")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "commit.gpgsign", "false")

    (repo / "release.txt").write_text("parent\n", encoding="utf-8")
    _git(repo, "add", "release.txt")
    _git(repo, "commit", "--quiet", "--no-verify", "-m", "parent revision")
    parent = _git(repo, "rev-parse", "HEAD")
    parent_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    (repo / "release.txt").write_text("child\n", encoding="utf-8")
    _git(repo, "add", "release.txt")
    _git(repo, "commit", "--quiet", "--no-verify", "-m", "child revision")
    child = _git(repo, "rev-parse", "HEAD")
    child_tree = _git(repo, "rev-parse", "HEAD^{tree}")

    return ReleaseHistory(repo, parent, child, parent_tree, child_tree)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_production_authorization(
    path: Path,
    target_revision: str,
    target_tree: str,
) -> None:
    revision = GitCommitSha(target_revision)
    tree = GitTreeSha(target_tree)
    digest = OCIImageDigest(IMAGE_DIGEST)
    artifact = ReleaseArtifactEvidence(
        source_revision=revision,
        source_tree=tree,
        image_digest=digest,
        product_manifest_digest=ProductManifestDigest("sha256:" + "b" * 64),
        build_run_id=WorkflowRunId(101),
        evidence_run_id=WorkflowRunId(101),
        source_ci_conclusion=EvidenceConclusion.SUCCESS,
    )
    write_production_authorization(
        path,
        ReleaseCandidateRecord(
            artifact=artifact,
            staging=StagingAcceptanceEvidence(
                deployment_id=StagingDeploymentId(202),
                source_revision=revision,
                source_tree=tree,
                image_digest=digest,
                conclusion=EvidenceConclusion.SUCCESS,
            ),
            main=MainAuthorizationEvidence(
                authorization_run_id=WorkflowRunId(303),
                authorization_main_revision=revision,
                release_revision=revision,
                release_tree=tree,
                required_ci_conclusion=EvidenceConclusion.SUCCESS,
                source_revision_is_ancestor=True,
            ),
        ),
    )


def _mutated_adapter(tmp_path: Path, *, replacement: str) -> Path:
    """Rewrite the adapter's gate region, keeping every other step intact.

    The gate is delimited in `deploy_production.sh` by explicit start and end
    banners so a mutation replaces exactly the decision under test rather than
    an approximation of it.
    """

    source = DEPLOY_PRODUCTION.read_text(encoding="utf-8")
    start = source.index(GATE_START)
    end = source.index(GATE_END)
    tmp_path.mkdir(parents=True, exist_ok=True)
    mutated = tmp_path / "deploy_production_mutated.sh"
    _write_executable(mutated, source[:start] + replacement + source[end:])
    return mutated


def _run_adapter(
    tmp_path: Path,
    history: ReleaseHistory,
    *,
    target_revision: str,
    container_state: str = "present",
    running_revision: str = "",
    docker_info_status: int = 0,
    inventory_status: int = 0,
    inspect_status: int = 0,
    bootstrap_revision: str | None = None,
    hotfix: bool = False,
    script: Path | None = None,
) -> AdapterRun:
    tmp_path.mkdir(parents=True, exist_ok=True)
    deploy_dir = tmp_path / "deploy"
    bin_dir = tmp_path / "bin"
    deploy_dir.mkdir()
    bin_dir.mkdir()
    (deploy_dir / ".env").write_text(
        "APP_ENV=production\n"
        "SERVER_NAME=dotmac-sub-prod\n"
        f"APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub@{IMAGE_DIGEST}\n",
        encoding="utf-8",
    )

    authorization = tmp_path / "authorization.json"
    _write_production_authorization(
        authorization,
        target_revision,
        history.child_tree if target_revision == history.child else history.parent_tree,
    )
    marker = tmp_path / "deploy-called"
    docker_log = tmp_path / "docker-commands.log"

    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${DOCKER_LOG}"
if [[ "${1:-}" == "info" ]]; then
  exit "${DOCKER_INFO_STATUS}"
fi
if [[ "${1:-} ${2:-}" == "container ls" ]]; then
  if [[ "${INVENTORY_STATUS}" != "0" ]]; then
    exit "${INVENTORY_STATUS}"
  fi
  if [[ "${CONTAINER_STATE}" == "present" ]]; then
    printf '%s\n' 'dotmac_sub_app'
  elif [[ "${CONTAINER_STATE}" == "ambiguous" ]]; then
    printf '%s\n%s\n' 'dotmac_sub_app' 'dotmac_sub_app_previous'
  fi
  exit 0
fi
if [[ "${1:-}" == "inspect" ]]; then
  if [[ "${INSPECT_STATUS}" != "0" ]]; then
    exit "${INSPECT_STATUS}"
  fi
  printf '%s\n' "${RUNNING_REVISION}"
  exit 0
fi
exit 64
""",
    )

    _write_executable(
        bin_dir / "bash",
        """#!/bin/bash
set -eu
if [[ "${1:-}" == "${REPO_ROOT}/scripts/deploy.sh" ]]; then
  printf '%s\n' delegated > "${DEPLOY_MARKER}"
  exit 0
fi
exec /bin/bash "$@"
""",
    )

    git_bin = shutil.which("git")
    assert git_bin is not None
    # `fetch` is stubbed out (no network); every other question is answered by
    # REAL git against the fixture history, so the ancestry logic under test is
    # exercised rather than mocked.
    _write_executable(
        bin_dir / "git",
        f"""#!/bin/bash
set -eu
rewritten=()
for arg in "$@"; do
  if [[ "$arg" == "fetch" ]]; then
    exit 0
  fi
  if [[ "$arg" == "${{REPO_ROOT}}" ]]; then
    arg="${{GIT_FIXTURE}}"
  fi
  rewritten+=("$arg")
done
exec "{git_bin}" "${{rewritten[@]}}"
""",
    )

    args = [str(script or DEPLOY_PRODUCTION), IMAGE_DIGEST, str(authorization)]
    if bootstrap_revision is not None:
        bootstrap = tmp_path / "bootstrap.json"
        write_bootstrap_authorization(
            bootstrap,
            ProductionBootstrapAuthorization(
                target_revision=GitCommitSha(bootstrap_revision),
                target_server=ProductionServerName("dotmac-sub-prod"),
                change_reference="CHG-2026-0829",
                reason="initialize the confirmed empty production host",
            ),
        )
        args.extend(("--bootstrap-authorization", str(bootstrap)))
    if hotfix:
        args.extend(
            (
                "--hotfix-no-migrations",
                "--change-reference",
                "CHG-2026-0829",
                "--reason",
                "no-migration hotfix",
            )
        )

    result = subprocess.run(
        ["/bin/bash", *args],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "REPO_DIR": str(ROOT),
            "DEPLOY_DIR": str(deploy_dir),
            "PYTHON_BIN": sys.executable,
            "REPO_ROOT": str(ROOT),
            "DEPLOY_MARKER": str(marker),
            "GIT_FIXTURE": str(history.repo),
            "DOCKER_LOG": str(docker_log),
            "DOCKER_INFO_STATUS": str(docker_info_status),
            "INVENTORY_STATUS": str(inventory_status),
            "INSPECT_STATUS": str(inspect_status),
            "CONTAINER_STATE": container_state,
            "RUNNING_REVISION": running_revision,
        },
        check=False,
        capture_output=True,
        text=True,
    )
    return AdapterRun(result, marker, docker_log)


# Every state in which the running production revision cannot be proven, and
# the exact refusal each must produce. An unauthorized empty host is in this
# table deliberately: absence is refused like any other unprovable state until
# a typed bootstrap authorization names it.
UNPROVABLE_STATES: list[tuple[str, dict[str, object], str]] = [
    ("docker_unavailable", {"docker_info_status": 1}, "Docker runtime is unreadable"),
    ("inventory_failed", {"inventory_status": 1}, "container inventory is unreadable"),
    ("inspect_failed", {"inspect_status": 1}, "could not inspect dotmac_sub_app"),
    (
        "absent_label",
        {"running_revision": ""},
        "has no org.opencontainers.image.revision",
    ),
    (
        "no_value_label",
        {"running_revision": "<no value>"},
        "has no org.opencontainers.image.revision",
    ),
    (
        "malformed_label",
        {"running_revision": "not-a-full-sha"},
        "has a malformed org.opencontainers.image.revision",
    ),
    (
        "ambiguous_inventory",
        {"container_state": "ambiguous"},
        "production container inventory is ambiguous",
    ),
    (
        "unauthorized_first_deployment",
        {"container_state": "absent"},
        "first deployment requires --bootstrap-authorization",
    ),
]


def _run_unprovable_state(
    tmp_path: Path,
    history: ReleaseHistory,
    overrides: dict[str, object],
    **extra: object,
) -> AdapterRun:
    options: dict[str, object] = {"running_revision": history.child}
    options.update(overrides)
    options.update(extra)
    return _run_adapter(  # type: ignore[arg-type]
        tmp_path, history, target_revision=history.child, **options
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [pytest.param(o, m, id=name) for name, o, m in UNPROVABLE_STATES],
)
def test_unprovable_running_state_stops_before_deploy(
    tmp_path: Path,
    history: ReleaseHistory,
    overrides: dict[str, object],
    message: str,
) -> None:
    run = _run_unprovable_state(tmp_path, history, overrides)

    assert run.result.returncode != 0
    assert message in run.result.stderr
    assert not run.deploy_was_delegated(), (
        "deploy.sh must not run after an unprovable observation"
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [pytest.param(o, m, id=name) for name, o, m in UNPROVABLE_STATES],
)
def test_every_refusal_precedes_any_host_mutation(
    tmp_path: Path,
    history: ReleaseHistory,
    overrides: dict[str, object],
    message: str,
) -> None:
    """Ordering proof, not merely a refusal proof.

    A guard that refuses after taking a backup has already had side effects, so
    each refusal is checked for what it did to the host first. `deploy.sh` owns
    the backup and `alembic upgrade` and is never reached. The hotfix
    migration-evidence path pulls images and creates throwaway containers, so
    the same refusals are replayed in hotfix mode; in both modes the docker
    command log must hold no mutating verb at all, only the read-only `info`,
    `container ls` and `inspect` observations the gate itself makes.
    """

    for hotfix in (False, True):
        run = _run_unprovable_state(
            tmp_path / f"hotfix-{hotfix}", history, overrides, hotfix=hotfix
        )

        assert run.result.returncode != 0
        assert message in run.result.stderr
        assert not run.deploy_was_delegated()
        assert run.mutating_docker_commands() == [], (
            "a refusal mutated the host before stopping: "
            f"{run.mutating_docker_commands()}"
        )


@pytest.mark.parametrize(
    ("running", "message"),
    [
        ("child", "Redeploying the running revision"),
        ("parent", "Forward deploy"),
    ],
)
def test_equal_and_forward_revisions_delegate_to_deploy(
    tmp_path: Path,
    history: ReleaseHistory,
    running: str,
    message: str,
) -> None:
    run = _run_adapter(
        tmp_path,
        history,
        target_revision=history.child,
        running_revision=getattr(history, running),
    )

    assert run.result.returncode == 0, run.result.stderr
    assert message in run.result.stdout
    assert run.deploy_marker.read_text(encoding="utf-8") == "delegated\n"


def test_confirmed_empty_host_requires_exact_typed_bootstrap(
    tmp_path: Path,
    history: ReleaseHistory,
) -> None:
    rejected = _run_adapter(
        tmp_path / "rejected",
        history,
        target_revision=history.child,
        container_state="absent",
        bootstrap_revision=history.parent,
    )
    accepted = _run_adapter(
        tmp_path / "accepted",
        history,
        target_revision=history.child,
        container_state="absent",
        bootstrap_revision=history.child,
    )

    assert rejected.result.returncode != 0
    assert "bootstrap authorization does not authorize" in rejected.result.stderr
    assert not rejected.deploy_was_delegated()
    assert rejected.mutating_docker_commands() == []
    assert accepted.result.returncode == 0, accepted.result.stderr
    assert "Authorized first deployment" in accepted.result.stdout
    assert accepted.deploy_marker.read_text(encoding="utf-8") == "delegated\n"


def test_bootstrap_authorization_is_refused_when_container_exists(
    tmp_path: Path,
    history: ReleaseHistory,
) -> None:
    run = _run_adapter(
        tmp_path,
        history,
        target_revision=history.child,
        running_revision=history.child,
        bootstrap_revision=history.child,
    )

    assert run.result.returncode != 0
    assert "accepted only when dotmac_sub_app is confirmed absent" in run.result.stderr
    assert not run.deploy_was_delegated()


def _permitted_states(
    tmp_path: Path,
    history: ReleaseHistory,
    script: Path,
) -> list[AdapterRun]:
    """The three states the gate must PERMIT, run against a given adapter."""

    return [
        _run_adapter(
            tmp_path / "redeploy",
            history,
            target_revision=history.child,
            running_revision=history.child,
            script=script,
        ),
        _run_adapter(
            tmp_path / "forward",
            history,
            target_revision=history.child,
            running_revision=history.parent,
            script=script,
        ),
        _run_adapter(
            tmp_path / "bootstrap",
            history,
            target_revision=history.child,
            container_state="absent",
            bootstrap_revision=history.child,
            script=script,
        ),
    ]


def test_an_always_refusing_gate_fails_this_suite(
    tmp_path: Path,
    history: ReleaseHistory,
) -> None:
    """Sensitivity proof -- the single most important test in this file.

    In a suite made only of refusal cases, a gate that refuses everything is
    indistinguishable from a gate that works. Replace the whole decision with
    an unconditional `die` and assert that every permitted state -- forward
    movement, redeploying the running revision, and an authorized first
    deployment -- stops passing. If this test ever fails, the suite has lost
    its ability to notice that production can no longer be deployed at all.
    """

    always_refuse = _mutated_adapter(
        tmp_path / "mutant",
        replacement='die "mutated: unconditional refusal"\n\n',
    )

    for run in _permitted_states(tmp_path / "refusing", history, always_refuse):
        assert run.result.returncode != 0
        assert "mutated: unconditional refusal" in run.result.stderr
        assert not run.deploy_was_delegated()


def test_a_removed_gate_fails_this_suite(
    tmp_path: Path,
    history: ReleaseHistory,
) -> None:
    """Sensitivity proof for the other direction -- the refusals are the gate's.

    Delete the gate entirely and every unprovable state reaches `deploy.sh`.
    That is what the refusal assertions above are actually detecting, so they
    cannot be passing for some unrelated reason.
    """

    no_gate = _mutated_adapter(tmp_path / "mutant", replacement="")

    for name, overrides, _message in UNPROVABLE_STATES:
        run = _run_unprovable_state(
            tmp_path / f"no-gate-{name}", history, overrides, script=no_gate
        )

        assert run.result.returncode == 0, run.result.stderr
        assert run.deploy_was_delegated(), (
            f"removing the gate did not let {name} through, so the refusal "
            f"assertion for {name} is not proving the gate"
        )
