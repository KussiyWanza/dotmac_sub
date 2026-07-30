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
