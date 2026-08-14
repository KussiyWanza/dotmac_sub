"""Production code reaches a host only as a registry-built image.

``scripts/deploy.sh`` is the production deployment owner (see
``docs/runbooks/PRODUCTION_DEPLOYMENT.md``): it deploys one immutable GHCR
image with backup, OCI-revision validation, health gates and rollback. Every
other path onto a prod box is a deliberate, explicitly-acknowledged fallback.

These tests pin the two properties that kept regressing:

- ``docker-compose.yml`` must not carry a ``build:`` stanza. GenieACS was the
  last one; it forced every prod host to keep a full application checkout,
  which is how working trees drifted onto feature branches for days.
- The legacy Makefile paths that bypass ``scripts/deploy.sh`` must refuse to
  run without their explicit opt-in variables, and the supported ``deploy``
  target must still delegate to the deploy script.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_compose_services_are_image_only() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    built = sorted(name for name, svc in services.items() if "build" in (svc or {}))
    assert not built, (
        f"compose services {built} declare a build: stanza — prod hosts must "
        "pull registry-built images only (publish via .github/workflows/ghcr.yml)"
    )
    missing_image = sorted(
        name for name, svc in services.items() if "image" not in (svc or {})
    )
    assert not missing_image, f"compose services {missing_image} declare no image:"


def test_bypass_deploy_targets_require_explicit_opt_in() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    # Host-build fallbacks stay gated.
    assert "define require-host-build" in makefile
    for target in ("prod-build:", "prod-deploy:"):
        recipe = makefile[makefile.index(target) :].split("\n\n")[0]
        assert "$(require-host-build)" in recipe, (
            f"{target} must invoke require-host-build"
        )

    # Legacy GHCR pin/deploy (no backup, no revision validation, no health
    # gate, latest-tag default) stays gated.
    assert "define require-legacy-ghcr-deploy" in makefile
    for target in ("prod-ghcr-pin:", "prod-ghcr-deploy:"):
        recipe = makefile[makefile.index(target) :].split("\n\n")[0]
        assert "$(require-legacy-ghcr-deploy)" in recipe, (
            f"{target} must invoke require-legacy-ghcr-deploy"
        )

    # The supported path still exists and still delegates to the owner script.
    deploy_recipe = makefile[makefile.index("\ndeploy:") :].split("\n\n")[0]
    assert 'bash scripts/deploy.sh "$(TAG)"' in deploy_recipe


def test_deploy_requires_exact_branch_github_evidence_before_database_work() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert "production:dotmac-sub-prod)" in deploy
    assert 'GITHUB_RELEASE_BRANCH="main"' in deploy
    assert "staging:dotmac-sub-staging)" in deploy
    assert 'GITHUB_RELEASE_BRANCH="dev"' in deploy
    assert '"${REPO_DIR}/scripts/verify_github_release.py"' in deploy
    assert deploy.index('scripts/verify_github_release.py"') < deploy.index(
        "Backing up database before migrations"
    )
    assert deploy.index('scripts/verify_github_release.py"') < deploy.index(
        'log "Applying migrations (alembic upgrade heads)"'
    )
    assert "production does not accept SKIP_BACKUP=1" in deploy
    assert "typed production authorization is required" in deploy
    assert "verify-production-decision" in deploy


def test_deploy_accepts_and_reverifies_an_exact_oci_digest() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert '[[ "${TAG}" =~ ^sha256:[0-9a-f]{64}$ ]]' in deploy
    assert 'IMAGE="${IMAGE_REPO}@${TAG}"' in deploy
    assert 'grep -Fxq "${image}" <<<"${repo_digests}"' in deploy
    assert 'docker manifest inspect "${IMAGE}"' in deploy
    assert 'docker pull "${IMAGE}"' in deploy


def test_deploy_uses_and_verifies_the_authorized_release_compose_tree() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    assert 'RELEASE_COMPOSE_FILE="${REPO_DIR}/docker-compose.yml"' in deploy
    assert '--project-directory "${DEPLOY_DIR}"' in deploy
    assert '--env-file "${DEPLOY_DIR}/.env"' in deploy
    assert 'HOST_COMPOSE_OVERRIDE="${DEPLOY_DIR}/docker-compose.override.yml"' in deploy
    assert '"io.dotmac.release.source-tree"' in deploy
    assert "git -C \"${REPO_DIR}\" rev-parse 'HEAD^{tree}'" in deploy
    assert "does not match image source tree" in deploy


def test_deploy_checks_openbao_boot_secrets_before_migrations() -> None:
    deploy = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")

    preflight = "python -m scripts.setup.verify_openbao_boot_secrets"
    migration = 'log "Applying migrations (alembic upgrade heads)"'
    assert preflight in deploy
    assert deploy.index(preflight) < deploy.index(migration)


def test_openbao_initializer_seeds_kernel_secret_source_paths() -> None:
    initializer = (ROOT / "scripts/setup/openbao_init.sh").read_text(encoding="utf-8")
    source = (ROOT / "app/services/kernel_secret_source.py").read_text(encoding="utf-8")
    required_source = source[
        source.index("SECRET_REFS:") : source.index("OPTIONAL_SECRET_REFS:")
    ]
    required_bindings = set(
        re.findall(r"bao://secret/([^#]+)#([a-z_]+)", required_source)
    )

    for path, field in required_bindings:
        assert path in initializer
        assert field in initializer
    assert 'kv patch "secret/${path}"' in initializer


def test_openbao_initializer_seeds_optional_material_without_requiring_it() -> None:
    """Optional material is provisioned by the bootstrap and gates nothing.

    Two separate failures this closes. An unseeded path means the feature
    reports itself unconfigured far from the cause — prepaid manifest
    verification refuses everything, and a secret setting cannot be written at
    all. And a `seed_group` here would fail `--strict` for every deployment
    that does not use the feature, which is most of them.
    """

    initializer = (ROOT / "scripts/setup/openbao_init.sh").read_text(encoding="utf-8")
    source = (ROOT / "app/services/kernel_secret_source.py").read_text(encoding="utf-8")
    provider = (ROOT / "app/services/kernel_key_provider.py").read_text(
        encoding="utf-8"
    )

    optional_source = source[source.index("OPTIONAL_SECRET_REFS:") :]
    bindings = set(re.findall(r"bao://secret/([^#]+)#([a-z_]+)", optional_source))
    bindings |= set(re.findall(r"bao://secret/([^#]+)#([a-z_]+)", provider))
    assert bindings, "no optional bindings found to check"

    for path, field in bindings:
        assert path in initializer, f"{path} is never seeded"
        assert field in initializer, f"{field} is never seeded"
        # Seeded by the helper that skips under `--strict`, not by `seed_group`.
        assert f"seed_optional_group {path}" in initializer
