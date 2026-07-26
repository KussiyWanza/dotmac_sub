from __future__ import annotations

import os
import subprocess
from pathlib import Path

REVISION = "32eebc1a6ac05a21275ed4db6f3d1dd28514a045"

# Everything the deploy recreates when a host declares the full stack.
FULL_SERVICES = (
    "app",
    "celery-worker",
    "celery-worker-bandwidth",
    "celery-worker-ingestion",
    "celery-worker-monitoring",
    "celery-worker-billing",
    "celery-worker-tr069",
    "celery-beat",
    "bandwidth-poller",
    "syslog-listener",
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _run_deploy(
    tmp_path: Path,
    *,
    revision: str = REVISION,
    health_success: bool = True,
    proxy_ready: bool = True,
    migration_lock_failures: int = 0,
    manifest_pins_ready: bool = True,
    crm_ticket_ready: bool = True,
    background_runtime_ready: bool = True,
    declared_services: tuple[str, ...] = FULL_SERVICES,
    write_override: bool = False,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    deploy_dir = tmp_path / "deploy"
    bin_dir = tmp_path / "bin"
    deploy_dir.mkdir()
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    docker_log.write_text("")
    migration_attempts = tmp_path / "migration-attempts"
    migration_attempts.write_text("0")
    (deploy_dir / ".env").write_text(
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000\n"
        "GIT_SHA=old0000000000000000000000000000000000000\n"
    )
    declared_services_literal = " ".join(
        f"'{service}'" for service in declared_services
    )
    if write_override:
        (deploy_dir / "docker-compose.override.yml").write_text(
            "services:\n  app: {}\n"
        )
    _write_executable(
        bin_dir / "docker",
        f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$DOCKER_LOG"
if [[ "$1 $2" == "image inspect" ]]; then
  printf '%s\\n' "{revision}"
fi
if [[ "$*" == *"alembic upgrade heads"* ]]; then
  attempts="$(cat "$MIGRATION_ATTEMPTS")"
  if ((attempts < {migration_lock_failures})); then
    printf '%s\\n' "$((attempts + 1))" > "$MIGRATION_ATTEMPTS"
    echo "canceling statement due to lock timeout" >&2
    exit 1
  fi
fi
if [[ "$*" == *"scripts.integrations.verify_manifest_pins"* ]]; then
  exit {0 if manifest_pins_ready else 1}
fi
if [[ "$*" == *"scripts.integrations.verify_crm_ticket_readiness"* ]]; then
  exit {0 if crm_ticket_ready else 1}
fi
if [[ "$*" == *"config --services"* ]]; then
  printf '%s\\n' {declared_services_literal}
  exit 0
fi
if [[ "$*" == "compose "*" ps -q "* ]]; then
  printf 'container-%s\\n' "${{@: -1}}"
  exit 0
fi
if [[ "$1" == "inspect" && "$*" == *".State.Status"* ]]; then
  printf 'running\\n'
  exit 0
fi
if [[ "$1" == "inspect" && "$*" == *".RestartCount"* ]]; then
  printf '0\\n'
  exit 0
fi
if [[ "$1" == "inspect" && "$*" == *".Config.Hostname"* ]]; then
  printf '%s\\n' "$2"
  exit 0
fi
if [[ "$1" == "exec" && "$*" == *"scripts.verify_celery_workers"* ]]; then
  exit {0 if background_runtime_ready else 1}
fi
exit 0
""",
    )
    nginx_config = (
        "upstream dotmac_sub_app {\n  server 127.0.0.1:18001 backup;\n}"
        if proxy_ready
        else "upstream dotmac_sub_app {\n  server 127.0.0.1:8001;\n}"
    )
    _write_executable(
        bin_dir / "nginx",
        f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "{nginx_config}"
""",
    )
    curl_exit_code = 0 if health_success else 1
    _write_executable(bin_dir / "curl", f"#!/usr/bin/env bash\nexit {curl_exit_code}\n")
    _write_executable(bin_dir / "pgrep", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(bin_dir / "flock", "#!/usr/bin/env bash\nexit 0\n")

    repo_root = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DEPLOY_DIR": str(deploy_dir),
        "REPO_DIR": str(repo_root),
        "DEPLOY_LOCK_FILE": str(tmp_path / "deploy.lock"),
        "SKIP_BACKUP": "1",
        "IMAGE_RETAIN_COUNT": "0",
        "HEALTH_TIMEOUT_SECONDS": "0" if not health_success else "180",
        "CANDIDATE_DRAIN_SECONDS": "0",
        "BACKGROUND_RUNTIME_TIMEOUT_SECONDS": "0",
        "BACKGROUND_STABILITY_SECONDS": "0",
        "MIGRATION_RETRY_SECONDS": "0",
        "DOCKER_LOG": str(docker_log),
        "MIGRATION_ATTEMPTS": str(migration_attempts),
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", str(repo_root / "scripts/deploy.sh"), "sha-32eebc1"],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, deploy_dir / ".env", docker_log


def test_deploy_pins_git_sha_from_image_revision(tmp_path: Path) -> None:
    result, env_file, _docker_log = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-32eebc1" in env_text
    assert f"GIT_SHA={REVISION}" in env_text


def test_deploy_rejects_tag_revision_mismatch_without_changing_env(
    tmp_path: Path,
) -> None:
    result, env_file, _docker_log = _run_deploy(tmp_path, revision="f" * 40)

    assert result.returncode != 0
    assert "IMAGE INTEGRITY FAILURE" in result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_text


def test_deploy_restores_image_and_git_sha_after_health_failure(tmp_path: Path) -> None:
    result, env_file, _docker_log = _run_deploy(tmp_path, health_success=False)

    assert result.returncode != 0
    assert "Warm candidate health gate failed" in result.stderr
    env_text = env_file.read_text()
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_text
    assert "GIT_SHA=old0000000000000000000000000000000000000" in env_text


def test_deploy_verifies_schema_then_warms_candidate_before_recreate(
    tmp_path: Path,
) -> None:
    result, _env_file, docker_log = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    commands = docker_log.read_text().splitlines()
    migration = next(
        index
        for index, command in enumerate(commands)
        if "alembic upgrade heads" in command
    )
    verification = next(
        index
        for index, command in enumerate(commands)
        if "scripts.migration.verify_schema_contracts" in command
    )
    manifest_pins = next(
        index
        for index, command in enumerate(commands)
        if "scripts.integrations.verify_manifest_pins" in command
    )
    crm_ticket = next(
        index
        for index, command in enumerate(commands)
        if "scripts.integrations.verify_crm_ticket_readiness" in command
    )
    candidate = next(
        index
        for index, command in enumerate(commands)
        if "127.0.0.1:18001:8001" in command
    )
    recreate = next(
        index
        for index, command in enumerate(commands)
        if "compose -f docker-compose.yml up -d app" in command
    )

    assert migration < verification < manifest_pins < crm_ticket < candidate < recreate


def test_deploy_rejects_unavailable_manifest_pin_before_candidate(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        manifest_pins_ready=False,
    )

    assert result.returncode != 0
    assert (
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_file.read_text()
    )
    commands = docker_log.read_text().splitlines()
    assert any("scripts.integrations.verify_manifest_pins" in item for item in commands)
    assert not any("127.0.0.1:18001:8001" in item for item in commands)


def test_deploy_rejects_unready_crm_ticket_cutover_before_candidate(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        crm_ticket_ready=False,
    )

    assert result.returncode != 0
    assert (
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_file.read_text()
    )
    commands = docker_log.read_text().splitlines()
    assert any(
        "scripts.integrations.verify_crm_ticket_readiness" in item for item in commands
    )
    assert not any("127.0.0.1:18001:8001" in item for item in commands)


def test_deploy_refuses_replacement_without_proxy_handoff(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(tmp_path, proxy_ready=False)

    assert result.returncode != 0
    assert "DEPLOY AVAILABILITY FAILURE" in result.stderr
    assert (
        "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in env_file.read_text()
    )
    assert docker_log.read_text() == ""


def test_deploy_retries_a_bounded_migration_lock_timeout(tmp_path: Path) -> None:
    result, _env_file, docker_log = _run_deploy(
        tmp_path,
        migration_lock_failures=2,
    )

    assert result.returncode == 0, result.stderr
    attempts = [
        command
        for command in docker_log.read_text().splitlines()
        if "alembic upgrade heads" in command
    ]
    assert len(attempts) == 3


def test_deploy_verifies_every_worker_and_beat_before_success(tmp_path: Path) -> None:
    result, _env_file, docker_log = _run_deploy(tmp_path)

    assert result.returncode == 0, result.stderr
    commands = docker_log.read_text().splitlines()
    ping_commands = [
        command for command in commands if "scripts.verify_celery_workers" in command
    ]
    assert len(ping_commands) == 2
    for service in (
        "celery-worker",
        "celery-worker-bandwidth",
        "celery-worker-ingestion",
        "celery-worker-monitoring",
        "celery-worker-billing",
        "celery-worker-tr069",
        "celery-beat",
    ):
        assert any(f"ps -q {service}" in command for command in commands)
        if service != "celery-beat":
            assert all(
                f"celery@container-{service}" in command for command in ping_commands
            )


def test_deploy_rolls_back_when_a_celery_worker_is_unavailable(
    tmp_path: Path,
) -> None:
    result, env_file, docker_log = _run_deploy(
        tmp_path,
        background_runtime_ready=False,
    )

    assert result.returncode != 0
    assert "Background runtime health gate failed" in result.stderr
    assert "APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-old0000" in (
        env_file.read_text()
    )
    assert any(
        "scripts.verify_celery_workers" in command
        for command in docker_log.read_text().splitlines()
    )


def test_deploy_never_starts_a_service_the_host_does_not_declare(
    tmp_path: Path,
) -> None:
    """Staging gates celery-beat behind a profile so it cannot originate
    production work. Naming a profile-gated service on the command line
    activates it, so the deploy must recreate only declared services."""
    staging_services = tuple(
        service for service in FULL_SERVICES if service != "celery-beat"
    )
    result, _env_file, docker_log = _run_deploy(
        tmp_path,
        declared_services=staging_services,
    )

    assert result.returncode == 0, result.stderr
    recreate_commands = [
        command
        for command in docker_log.read_text().splitlines()
        if " up -d " in command
    ]
    assert recreate_commands
    assert not any("celery-beat" in command for command in recreate_commands)
    # ...and it must not be health-gated as if it were running either.
    assert "BACKGROUND RUNTIME FAILURE: celery-beat" not in result.stderr


def test_deploy_loads_the_compose_override_when_the_host_has_one(
    tmp_path: Path,
) -> None:
    result, _env_file, docker_log = _run_deploy(tmp_path, write_override=True)

    assert result.returncode == 0, result.stderr
    compose_commands = [
        command
        for command in docker_log.read_text().splitlines()
        if command.startswith("compose ")
    ]
    assert compose_commands
    assert all(
        "-f docker-compose.yml -f docker-compose.override.yml" in command
        for command in compose_commands
    )


def test_deploy_ignores_the_compose_override_when_told_to(tmp_path: Path) -> None:
    result, _env_file, docker_log = _run_deploy(
        tmp_path,
        write_override=True,
        extra_env={"IGNORE_COMPOSE_OVERRIDE": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert not any(
        "docker-compose.override.yml" in command
        for command in docker_log.read_text().splitlines()
    )


def test_deploy_requires_the_proxy_contract_unless_explicitly_opted_out(
    tmp_path: Path,
) -> None:
    """Production must keep the warm-handoff gate; a host with no proxy in
    front of the app has to opt out deliberately."""
    result, _env_file, _docker_log = _run_deploy(
        tmp_path,
        proxy_ready=False,
        extra_env={"REQUIRE_PROXY_HANDOFF": "0"},
    )

    assert result.returncode == 0, result.stderr
    assert "DEPLOY AVAILABILITY FAILURE" not in result.stderr
