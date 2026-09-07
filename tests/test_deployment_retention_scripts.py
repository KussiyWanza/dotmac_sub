from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _bash_executable() -> str:
    if os.name == "nt":
        git_bash = (
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Git/bin/bash.exe"
        )
        if git_bash.is_file():
            return str(git_bash)
    bash = shutil.which("bash")
    if bash:
        return bash
    pytest.skip("bash is required for deployment retention script tests")


def _shell_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    drive = resolved.drive.rstrip(":").lower()
    remainder = resolved.as_posix()[2:]
    return f"/{drive}{remainder}"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _run_bash_script(
    script: Path,
    *,
    fake_bin: Path,
    env_overrides: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **env_overrides}
    command = (
        f"export PATH={shlex.quote(_shell_path(fake_bin))}:/usr/bin:/bin; "
        f"exec bash {shlex.quote(_shell_path(script))}"
    )
    return subprocess.run(
        [_bash_executable(), "-c", command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_fake_backup_commands(fake_bin: Path, *, no_op_remove: bool = False) -> None:
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
set -eu
if [[ "$1" == "inspect" ]]; then
  exit 0
fi
if [[ "$1" == "exec" ]]; then
  printf 'database backup payload\n'
  exit 0
fi
exit 1
""",
    )
    _write_executable(
        fake_bin / "mkdir",
        """#!/usr/bin/env bash
set -eu
[[ "$1" == "-p" && -d "$2" ]]
""",
    )
    if no_op_remove:
        _write_executable(fake_bin / "rm", "#!/usr/bin/env bash\nexit 0\n")


def test_database_backup_retains_five_across_current_and_legacy_directories(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / "deploy-root"
    backup_root = tmp_path / "backups"
    deployment_dir = backup_root / "deployments"
    fake_bin = tmp_path / "bin"
    root_dir.mkdir()
    deployment_dir.mkdir(parents=True)
    fake_bin.mkdir()
    (root_dir / ".env").write_text(
        "DATABASE_URL=postgresql://postgres@db/dotmac_sub\n"
        "DB_BACKUP_BASENAME=wrong_family\n"
        "DB_BACKUP_RETENTION_PREFIX=wrong_family_\n"
        "DB_BACKUP_RETENTION_COUNT=1\n",
        encoding="utf-8",
    )
    _write_fake_backup_commands(fake_bin)

    old_files: list[Path] = []
    for index in range(1, 8):
        directory = backup_root if index <= 3 else deployment_dir
        backup = directory / f"dotmac_sub_run_{index}_2026-08-{index:02d}_000000.sql.gz"
        backup.write_bytes(f"backup-{index}".encode())
        timestamp = time.time() - ((10 - index) * 3600)
        os.utime(backup, (timestamp, timestamp))
        old_files.append(backup)

    manual_backup = backup_root / "splynx_staging_pre_retirement.dump"
    manual_backup.write_bytes(b"manual evidence")

    result = _run_bash_script(
        ROOT / "scripts/db_backup.sh",
        fake_bin=fake_bin,
        env_overrides={
            "ROOT_DIR": _shell_path(root_dir),
            "DB_BACKUP_DIR": _shell_path(deployment_dir),
            "DB_BACKUP_LEGACY_DIR": _shell_path(backup_root),
            "DB_BACKUP_BASENAME": "dotmac_sub_run_999",
            "DB_BACKUP_RETENTION_PREFIX": "dotmac_sub_run_",
            "DB_BACKUP_RETENTION_COUNT": "5",
        },
    )

    assert result.returncode == 0, result.stderr
    retained = sorted(backup_root.glob("dotmac_sub_run_*.sql.gz"))
    retained += sorted(deployment_dir.glob("dotmac_sub_run_*.sql.gz"))
    assert len(retained) == 5
    assert all(not path.exists() for path in old_files[:3])
    assert all(path.exists() for path in old_files[3:])
    assert any(path.name.startswith("dotmac_sub_run_999_") for path in retained)
    assert manual_backup.is_file()
    assert "Backup retention verified: retained=5 removed=3 target=5" in result.stdout


def test_database_backup_verifies_that_excess_files_were_removed(
    tmp_path: Path,
) -> None:
    root_dir = tmp_path / "deploy-root"
    backup_root = tmp_path / "backups"
    deployment_dir = backup_root / "deployments"
    fake_bin = tmp_path / "bin"
    root_dir.mkdir()
    deployment_dir.mkdir(parents=True)
    fake_bin.mkdir()
    (root_dir / ".env").write_text(
        "DATABASE_URL=postgresql://postgres@db/dotmac_sub\n",
        encoding="utf-8",
    )
    _write_fake_backup_commands(fake_bin, no_op_remove=True)
    for index in range(1, 6):
        backup = deployment_dir / f"dotmac_sub_run_{index}_old.sql.gz"
        backup.write_bytes(f"backup-{index}".encode())

    result = _run_bash_script(
        ROOT / "scripts/db_backup.sh",
        fake_bin=fake_bin,
        env_overrides={
            "ROOT_DIR": _shell_path(root_dir),
            "DB_BACKUP_DIR": _shell_path(deployment_dir),
            "DB_BACKUP_BASENAME": "dotmac_sub_run_999",
            "DB_BACKUP_RETENTION_PREFIX": "dotmac_sub_run_",
            "DB_BACKUP_RETENTION_COUNT": "5",
        },
    )

    assert result.returncode != 0
    assert "Backup retention verification failed: expected=5 actual=6" in result.stderr


def _write_fake_docker(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "docker",
        r"""#!/usr/bin/env bash
set -euo pipefail
state="${DOCKER_STATE:?}"

lookup_id() {
  local target="$1"
  awk -F'|' -v target="${target}" '$2 == target || $3 == target { print $2; exit }' "${state}"
}

if [[ "$1" == "ps" ]]; then
  awk -F'|' '$4 == "used" { sub(/^sha256:/, "", $2); print "container-" $2 }' "${state}"
  exit 0
fi

if [[ "$1" == "inspect" ]]; then
  container_key="${4#container-}"
  awk -F'|' -v target="sha256:${container_key}" '$2 == target { print $2; exit }' "${state}"
  exit 0
fi

if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  shift 2
  formatted=0
  if [[ "${1:-}" == "--format" ]]; then
    formatted=1
    shift 2
  fi
  image_id="$(lookup_id "${1:?}")"
  [[ -n "${image_id}" ]] || exit 1
  if [[ "${formatted}" == "1" ]]; then
    printf '%s\n' "${image_id}"
  else
    printf '{}\n'
  fi
  exit 0
fi

if [[ "$1" == "image" && "$2" == "ls" ]]; then
  awk -F'|' '{ printf "%s\t%s\t%s\n", $1, $2, $3 }' "${state}"
  exit 0
fi

if [[ "$1" == "image" && "$2" == "rm" ]]; then
  target="${3:?}"
  if [[ "${FAIL_REMOVE_ID:-}" == "${target}" ]]; then
    exit 1
  fi
  awk -F'|' -v target="${target}" '$2 != target' "${state}" > "${state}.next"
  mv "${state}.next" "${state}"
  printf 'Deleted: %s\n' "${target}"
  exit 0
fi

exit 1
""",
    )


def _image_rows() -> list[tuple[str, str, str, str]]:
    repository = "ghcr.io/michaelayoade/dotmac_sub"
    return [
        (
            "2026-09-07 10:00:00 +0000 UTC",
            "sha256:active",
            f"{repository}:<none>",
            "used",
        ),
        (
            "2026-09-06 10:00:00 +0000 UTC",
            "sha256:u7",
            f"{repository}:<none>",
            "unused",
        ),
        (
            "2026-09-05 10:00:00 +0000 UTC",
            "sha256:u6",
            f"{repository}:<none>",
            "unused",
        ),
        (
            "2026-09-04 10:00:00 +0000 UTC",
            "sha256:u5",
            f"{repository}:<none>",
            "unused",
        ),
        (
            "2026-09-03 10:00:00 +0000 UTC",
            "sha256:u4",
            f"{repository}:<none>",
            "unused",
        ),
        (
            "2026-09-02 10:00:00 +0000 UTC",
            "sha256:u3",
            f"{repository}:<none>",
            "unused",
        ),
        (
            "2026-09-01 10:00:00 +0000 UTC",
            "sha256:u2",
            f"{repository}:<none>",
            "unused",
        ),
        (
            "2026-08-31 10:00:00 +0000 UTC",
            "sha256:u1",
            f"{repository}:<none>",
            "unused",
        ),
        (
            "2026-08-01 10:00:00 +0000 UTC",
            "sha256:mail",
            f"{repository}:<none>",
            "used",
        ),
    ]


def _write_image_state(path: Path) -> None:
    rows = ("|".join(row) for row in _image_rows())
    path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")


def test_image_retention_handles_unnamed_images_and_protects_active_images(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    state = tmp_path / "images.txt"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin)
    _write_image_state(state)

    result = _run_bash_script(
        ROOT / "scripts/docker_image_retention.sh",
        fake_bin=fake_bin,
        env_overrides={
            "DOCKER_STATE": _shell_path(state),
            "RETAIN_IMAGES": "5",
        },
    )

    assert result.returncode == 0, result.stderr
    remaining_ids = {
        row.split("|")[1] for row in state.read_text(encoding="utf-8").splitlines()
    }
    assert remaining_ids == {
        "sha256:active",
        "sha256:mail",
        "sha256:u7",
        "sha256:u6",
        "sha256:u5",
        "sha256:u4",
        "sha256:u3",
    }
    assert "Image retention verified:" in result.stdout
    assert "rollback=5 removed=2 target=5" in result.stdout


def test_image_retention_failure_is_reported(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    state = tmp_path / "images.txt"
    fake_bin.mkdir()
    _write_fake_docker(fake_bin)
    _write_image_state(state)

    result = _run_bash_script(
        ROOT / "scripts/docker_image_retention.sh",
        fake_bin=fake_bin,
        env_overrides={
            "DOCKER_STATE": _shell_path(state),
            "RETAIN_IMAGES": "5",
            "FAIL_REMOVE_ID": "sha256:u2",
        },
    )

    assert result.returncode != 0
    assert "Removing old unused image" in result.stdout
