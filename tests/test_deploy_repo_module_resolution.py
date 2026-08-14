"""Release-control Python must come from the explicitly authorized checkout."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_repo_module.sh"


def _write_probe(root: Path, value: str) -> None:
    package = root / "scripts"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "release_probe.py").write_text(f"print({value!r})\n")


def test_repo_module_runner_ignores_a_shadow_module_in_the_current_directory(
    tmp_path: Path,
) -> None:
    authorized = tmp_path / "authorized"
    stale_deploy = tmp_path / "stale-deploy"
    _write_probe(authorized, "authorized")
    _write_probe(stale_deploy, "stale")

    result = subprocess.run(
        ["bash", str(RUNNER), "scripts.release_probe"],
        cwd=stale_deploy,
        env={
            **os.environ,
            "REPO_DIR": str(authorized),
            "PYTHON_BIN": sys.executable,
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "authorized\n"


def test_host_release_control_callers_use_the_repo_module_runner() -> None:
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    adapter = (ROOT / "scripts" / "deploy_production.sh").read_text(encoding="utf-8")

    assert "run_repo_module scripts.release_candidate_evidence" in deploy
    assert "run_repo_module scripts.release_backup_policy" in deploy
    assert "run_repo_module scripts.release_backup_policy" in adapter
    assert 'PYTHONPATH="${REPO_DIR}" "${PYTHON_BIN}" -m scripts.release_' not in (
        deploy + adapter
    )
