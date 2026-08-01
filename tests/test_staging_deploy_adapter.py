from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING_ADAPTER = ROOT / "scripts" / "deploy_staging.sh"


def _install_adapter_fixture(
    tmp_path: Path,
    *,
    env_lines: tuple[str, ...],
) -> tuple[Path, Path, dict[str, str]]:
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)

    adapter = scripts / "deploy_staging.sh"
    shutil.copy2(STAGING_ADAPTER, adapter)
    (checkout / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")

    output = tmp_path / "deploy-output.txt"
    (scripts / "deploy.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "printf '%s\\n' "
        '"${SKIP_BACKUP}|${REQUIRE_PROXY_HANDOFF}|$*" '
        '> "${STAGING_ADAPTER_TEST_OUTPUT}"\n',
        encoding="utf-8",
    )

    process_env = dict(os.environ)
    process_env.update(
        {
            "REQUIRE_PROXY_HANDOFF": "1",
            "SKIP_BACKUP": "0",
            "STAGING_ADAPTER_TEST_OUTPUT": str(output),
        }
    )
    return adapter, output, process_env


def test_staging_adapter_forces_staging_only_deploy_controls(tmp_path: Path) -> None:
    adapter, output, process_env = _install_adapter_fixture(
        tmp_path,
        env_lines=(
            "APP_ENV=staging",
            "SERVER_NAME=dotmac-sub-staging",
            "HEALTH_URL=http://10.120.121.20:8001/health",
        ),
    )

    result = subprocess.run(
        ["bash", str(adapter), "sha-deadbee"],
        check=False,
        capture_output=True,
        env=process_env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == "1|0|sha-deadbee\n"


def test_staging_adapter_refuses_non_staging_environment(tmp_path: Path) -> None:
    adapter, output, process_env = _install_adapter_fixture(
        tmp_path,
        env_lines=(
            "APP_ENV=production",
            "SERVER_NAME=dotmac-sub-production",
            "HEALTH_URL=https://sub.example.invalid/health",
        ),
    )

    result = subprocess.run(
        ["bash", str(adapter), "sha-deadbee"],
        check=False,
        capture_output=True,
        env=process_env,
        text=True,
    )

    assert result.returncode != 0
    assert "Staging deploy refused" in result.stderr
    assert not output.exists()
