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

import re
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


def test_release_modules_run_through_the_repo_module_runner() -> None:
    """The authorized checkout must win over the mutable deploy cwd.

    Production run 31762013926 set ``PYTHONPATH`` to the authorized Actions
    checkout, but Python prepends the current directory ahead of it, so the
    persistent deployment checkout's stale ``scripts.release_candidate_evidence``
    won anyway and rejected the newer evidence schema.

    ``scripts/run_repo_module.sh`` fixes that by making the selected checkout
    the import root before the interpreter starts. This is the sensitivity
    half: if any host-side release module goes back to being invoked directly,
    the shadowing path reopens and this fails. The behavioural proof that a
    stale module cannot win lives in
    ``tests/test_deploy_repo_module_resolution.py``.
    """

    direct_invocations = [
        (path.name, line.strip())
        for path in (DEPLOY_SH, DEPLOY_PRODUCTION_SH)
        for line in path.read_text(encoding="utf-8").splitlines()
        if '"${PYTHON_BIN}"' in line and "-m scripts." in line
    ]
    assert not direct_invocations, (
        "host-side release modules must run through run_repo_module, or the "
        f"deploy checkout can shadow them again: {direct_invocations}"
    )

    runner_invocations = [
        (path.name, line.strip())
        for path in (DEPLOY_SH, DEPLOY_PRODUCTION_SH)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "run_repo_module scripts." in line
    ]
    assert runner_invocations, "no release module is routed through run_repo_module"
