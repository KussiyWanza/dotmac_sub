"""The pre-migration backup must read ``.env`` from the deploy dir.

``scripts/db_backup.sh`` resolves ``.env`` from ``ROOT_DIR``, which defaults to
the script's own parent — the code checkout. That is only correct on hosts
where the deploy directory and the repository are the same directory.

On the runner-based production path they are not: ``REPO_DIR`` is the ephemeral
GitHub Actions workspace and ``DEPLOY_DIR`` is the pinned host directory that
actually holds ``.env``. Without an explicit ``ROOT_DIR`` the backup aborted
with ``Missing <workspace>/.env`` and **no production deploy ever completed** —
six dispatches of "Deploy authorized digest to production", zero successes.

Existing deploy tests all pass ``SKIP_BACKUP=1``, so none of them exercised the
backup invocation. This guard covers it directly.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = ROOT / "scripts/deploy.sh"
DEPLOY_PRODUCTION_SH = ROOT / "scripts/deploy_production.sh"
DB_BACKUP_SH = ROOT / "scripts/db_backup.sh"


def test_backup_invocation_pins_root_dir_to_the_deploy_dir() -> None:
    source = DEPLOY_SH.read_text(encoding="utf-8")

    invocations = [
        line.strip()
        for line in source.splitlines()
        if "db_backup.sh" in line and not line.strip().startswith("#")
    ]
    assert invocations, "deploy.sh no longer invokes db_backup.sh"

    for line in invocations:
        assert 'ROOT_DIR="${DEPLOY_DIR}"' in line, (
            "db_backup.sh must be invoked with ROOT_DIR pinned to DEPLOY_DIR; "
            "it otherwise reads .env from the code checkout, which on the "
            f"production runner has no .env: {line}"
        )


def test_db_backup_still_reads_env_from_root_dir() -> None:
    """The guard above is only meaningful while this contract holds."""
    source = DB_BACKUP_SH.read_text(encoding="utf-8")

    assert re.search(r'^ROOT_DIR="\$\{ROOT_DIR:-', source, re.MULTILINE), (
        "db_backup.sh no longer takes ROOT_DIR as an override — re-check how "
        "deploy.sh should point it at the deploy directory"
    )
    assert '"${ROOT_DIR}/.env"' in source, (
        "db_backup.sh no longer resolves .env from ROOT_DIR — the invocation "
        "guard in this module may need updating"
    )


def test_deploy_keeps_repo_dir_and_deploy_dir_distinct() -> None:
    """Both variables must remain separately overridable.

    The production workflow passes REPO_DIR=$GITHUB_WORKSPACE and
    DEPLOY_DIR=$PRODUCTION_DEPLOY_DIR. Collapsing them would silently
    reintroduce the same class of fault.
    """
    source = DEPLOY_SH.read_text(encoding="utf-8")

    assert 'DEPLOY_DIR="${DEPLOY_DIR:-' in source
    assert 'REPO_DIR="${REPO_DIR:-${DEPLOY_DIR}}"' in source


def test_production_evidence_verifier_ignores_deploy_checkout_shadow(
    tmp_path: Path,
) -> None:
    """The authorized workflow checkout must win over the mutable deploy cwd.

    Production run 31762013926 set ``PYTHONPATH`` to the authorized Actions
    checkout, but Python still prepended the persistent deployment checkout to
    ``sys.path``. Its stale ``scripts.release_candidate_evidence`` therefore
    parsed a valid authorization document and rejected its newer schema.

    Deriving the interpreter flag from the real deploy invocation makes this a
    sensitivity proof: removing the safe-path flag sends the import back to the
    hostile deployment checkout and this test fails.
    """

    source = DEPLOY_SH.read_text(encoding="utf-8")
    host_python_invocations = [
        (path.name, line.strip())
        for path in (DEPLOY_SH, DEPLOY_PRODUCTION_SH)
        for line in path.read_text(encoding="utf-8").splitlines()
        if 'PYTHONPATH="${REPO_DIR}" "${PYTHON_BIN}"' in line and "-m scripts." in line
    ]
    assert host_python_invocations
    assert all(
        '"${PYTHON_BIN}" -P -m' in line for _, line in host_python_invocations
    ), (
        "every host-side Python module must ignore the mutable deployment "
        f"checkout: {host_python_invocations}"
    )
    invocations = [
        line.strip()
        for line in source.splitlines()
        if '"${PYTHON_BIN}"' in line and "-m scripts.release_candidate_evidence" in line
    ]
    assert len(invocations) == 1, (
        "deploy.sh must have exactly one production-evidence module invocation"
    )
    safe_path_enabled = '"${PYTHON_BIN}" -P -m' in invocations[0]

    authorized_root = tmp_path / "authorized"
    deployment_root = tmp_path / "deployment"
    for root, identity in (
        (authorized_root, "authorized"),
        (deployment_root, "hostile"),
    ):
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "__init__.py").write_text("", encoding="utf-8")
        (scripts / "release_candidate_evidence.py").write_text(
            f"print({identity!r})\n",
            encoding="utf-8",
        )

    command = [sys.executable]
    if safe_path_enabled:
        command.append("-P")
    command.extend(("-m", "scripts.release_candidate_evidence"))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(authorized_root)

    completed = subprocess.run(
        command,
        cwd=deployment_root,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "authorized"
