"""S3 — Sub's declared composition is metadata that changes nothing at runtime.

``app/composition.py`` declares the frozen ``ProductAssemblySpec``, the four
coarse domain ``FeatureManifest``s, the ``CapabilityCatalogue``, and the
versioned dedicated-ISP ``DeploymentProfileSpec``. This file is the executable
acceptance for slice S3 (docs/PLATFORM_ADOPTION_LEDGER.md, "S3 acceptance
claim"):

- the module imports with no database configured and without booting the app;
- catalogue and preflight reports are deterministic;
- duplicate-capability, missing-provider, missing-module, forbidden-module,
  and unknown-profile failures all fail closed (negative tests);
- every capability code names exactly one owner that EXISTS in the executable
  SOT registry (``app/services/sot_registry/registry.py``) — no orphan codes;
- manifests are pure metadata (no routers/nav/seed — nothing mountable);
- no file under ``app/`` outside the composition module mentions the profile
  code or a capability code (capability codes are product vocabulary, never
  entitlements/permissions — plan boundary 5);
- importing the composition module changes zero routes and zero middleware
  (differential canary extending the S2 app-unchanged pattern).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"
COMPOSITION_FILE = APP_DIR / "composition.py"


def _no_db_env() -> dict[str, str]:
    """Current env with every database-ish variable stripped (S2 pattern)."""
    return {
        key: value
        for key, value in os.environ.items()
        if not (
            "DATABASE" in key
            or "SQLALCHEMY" in key
            or key.startswith(("POSTGRES", "PG", "MYSQL"))
            or key == "DB_URL"
        )
    }


# --- import purity -----------------------------------------------------------


def test_composition_imports_without_database_or_app_boot() -> None:
    """``app.composition`` is importable alone: no DB env, no ``app.main``.

    The docstring claim "importable without the app booting" is executed, not
    assumed: the subprocess strips DB env and asserts the runtime app module
    never enters the import graph.
    """
    code = (
        "import sys\n"
        "import app.composition as c\n"
        "assert 'app.main' not in sys.modules, 'composition import booted app.main'\n"
        "assert len(c.SUB_FEATURE_MANIFESTS) == 4\n"
        "assert c.dedicated_isp_preflight().ok\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=_no_db_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"app.composition is not a pure metadata import:\n{result.stderr}"
    )


# --- shape: coarse, pure-metadata manifests ---------------------------------


def test_exactly_four_coarse_domain_manifests_all_pure_metadata() -> None:
    """Four declared domains (S4–S6/S8 scope), each a metadata-only manifest.

    Empty router/nav/seed fields are load-bearing: a router reference would be
    a mount request, and ``mount_features`` is denied by the S1 guard.
    """
    from app.composition import SUB_FEATURE_MANIFESTS

    assert len(SUB_FEATURE_MANIFESTS) == 4
    assert [m.name for m in SUB_FEATURE_MANIFESTS] == [
        "sub.network_projection",
        "sub.backoffice_collaboration",
        "sub.billing_export",
        "sub.licensing_reception",
    ]
    for manifest in SUB_FEATURE_MANIFESTS:
        assert tuple(manifest.routers) == (), manifest.name
        assert tuple(manifest.web_routers) == (), manifest.name
        assert tuple(manifest.nav) == (), manifest.name
        assert manifest.seed is None, manifest.name
        assert manifest.capabilities, f"{manifest.name} declares no capability"


def test_assembly_spec_is_frozen_metadata() -> None:
    import dataclasses

    from app.composition import SUB_ASSEMBLY, SUB_FEATURE_MANIFESTS

    assert SUB_ASSEMBLY.name == "dotmac-sub"
    assert SUB_ASSEMBLY.modules == SUB_FEATURE_MANIFESTS
    assert SUB_ASSEMBLY.tenancy == "single"
    assert SUB_ASSEMBLY.platform_surface_enabled is False
    assert SUB_ASSEMBLY.web_enabled is True
    assert SUB_ASSEMBLY.startup_checks == ()
    assert SUB_ASSEMBLY.startup_hooks == ()
    assert SUB_ASSEMBLY.security_policy.content_security_policy == ""
    assert SUB_ASSEMBLY.security_policy.cross_origin_opener_policy == ""
    assert SUB_ASSEMBLY.security_policy.cross_origin_resource_policy == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        SUB_ASSEMBLY.name = "other"  # type: ignore[misc]


# --- determinism -------------------------------------------------------------


def test_capability_catalogue_is_deterministic() -> None:
    from dotmac_kernel.capabilities import CapabilityCatalogue

    from app.composition import CAPABILITY_CATALOGUE, SUB_FEATURE_MANIFESTS

    first = CapabilityCatalogue.from_manifests(SUB_FEATURE_MANIFESTS)
    second = CapabilityCatalogue.from_manifests(SUB_FEATURE_MANIFESTS)
    assert first.codes() == second.codes() == CAPABILITY_CATALOGUE.codes()
    for code in sorted(first.codes()):
        assert first.owner(code) == second.owner(code)
        assert first.owner(code) == CAPABILITY_CATALOGUE.owner(code)


def test_dedicated_isp_preflight_is_deterministic_and_green() -> None:
    from app.composition import (
        DEDICATED_ISP_PROFILE_CODE,
        DEDICATED_ISP_PROFILE_VERSION,
        dedicated_isp_preflight,
    )

    first = dedicated_isp_preflight()
    second = dedicated_isp_preflight()
    assert first == second
    assert first.render() == second.render()
    assert first.ok, first.render()
    assert first.errors == ()
    assert DEDICATED_ISP_PROFILE_CODE in first.render()
    assert DEDICATED_ISP_PROFILE_VERSION in first.render()


# --- negative tests: everything fails closed --------------------------------


def test_duplicate_capability_declaration_fails() -> None:
    """Kernel behaviour: two modules declaring one code is a conflict."""
    from dotmac_kernel.capabilities import (
        CapabilityCatalogue,
        DuplicateCapabilityError,
    )
    from dotmac_kernel.features import FeatureManifest

    from app.composition import SUB_FEATURE_MANIFESTS

    rogue = FeatureManifest(
        name="sub.rogue", capabilities=("network_projection.radius",)
    )
    with pytest.raises(DuplicateCapabilityError):
        CapabilityCatalogue.from_manifests((*SUB_FEATURE_MANIFESTS, rogue))


def test_missing_provider_fails_closed() -> None:
    from app.composition import (
        DECLARED_PROVIDER_IMPLEMENTATIONS,
        DEDICATED_ISP_PROFILE_CODE,
        DEPLOYMENT_PROFILES,
        declared_module_names,
    )

    report = DEPLOYMENT_PROFILES.validate(
        DEDICATED_ISP_PROFILE_CODE,
        installed_modules=declared_module_names(),
        enabled_modules=declared_module_names(),
        available_providers=DECLARED_PROVIDER_IMPLEMENTATIONS - {"nginx"},
    )
    assert not report.ok
    assert any("'ingress'" in error and "'nginx'" in error for error in report.errors)


def test_missing_required_module_fails_closed() -> None:
    from app.composition import (
        DECLARED_PROVIDER_IMPLEMENTATIONS,
        DEDICATED_ISP_PROFILE_CODE,
        DEPLOYMENT_PROFILES,
        declared_module_names,
    )

    without_billing = declared_module_names() - {"sub.billing_export"}
    report = DEPLOYMENT_PROFILES.validate(
        DEDICATED_ISP_PROFILE_CODE,
        installed_modules=without_billing,
        enabled_modules=without_billing,
        available_providers=DECLARED_PROVIDER_IMPLEMENTATIONS,
    )
    assert not report.ok
    assert any("sub.billing_export" in error for error in report.errors)


def test_forbidden_module_fails_closed() -> None:
    """A ledger-prohibited kernel module in the deployment fails preflight."""
    from app.composition import (
        DECLARED_PROVIDER_IMPLEMENTATIONS,
        DEDICATED_ISP_PROFILE_CODE,
        DEPLOYMENT_PROFILES,
        declared_module_names,
    )

    report = DEPLOYMENT_PROFILES.validate(
        DEDICATED_ISP_PROFILE_CODE,
        installed_modules=declared_module_names() | {"kernel.reference_features"},
        enabled_modules=declared_module_names(),
        available_providers=DECLARED_PROVIDER_IMPLEMENTATIONS,
    )
    assert not report.ok
    assert any("kernel.reference_features" in error for error in report.errors)


def test_unknown_profile_code_fails() -> None:
    from dotmac_kernel.profiles import UnknownProfileError

    from app.composition import DEPLOYMENT_PROFILES

    assert not DEPLOYMENT_PROFILES.is_valid_code("sub-multi-tenant-saas")
    with pytest.raises(UnknownProfileError):
        DEPLOYMENT_PROFILES.get("sub-multi-tenant-saas")


def test_profile_references_only_declared_modules() -> None:
    """The profile may not require an unknown/undeclared module code."""
    from app.composition import DEDICATED_ISP_PROFILE, declared_module_names

    unknown = DEDICATED_ISP_PROFILE.required_modules - declared_module_names()
    assert not unknown, f"profile requires undeclared modules: {sorted(unknown)}"
    overlap = DEDICATED_ISP_PROFILE.required_modules & (
        DEDICATED_ISP_PROFILE.forbidden_modules
    )
    assert not overlap, f"module both required and forbidden: {sorted(overlap)}"


def test_profile_declares_only_declared_provider_implementations() -> None:
    from app.composition import (
        DECLARED_PROVIDER_IMPLEMENTATIONS,
        DEDICATED_ISP_PROFILE,
    )

    named = set(DEDICATED_ISP_PROFILE.provider_selections().values())
    unknown = named - DECLARED_PROVIDER_IMPLEMENTATIONS
    assert not unknown, f"profile names undeclared providers: {sorted(unknown)}"


# --- no orphan capabilities: every code has a registered SOT owner ----------


def test_every_capability_names_exactly_one_registered_sot_owner() -> None:
    """No orphan codes: each capability maps to one EXISTING registry owner.

    ``app/services/sot_registry/registry.py`` stays the owner authority; the
    composition module may only reference exact registered service names.
    """
    from app.composition import CAPABILITY_CATALOGUE, CAPABILITY_OWNERS
    from app.services.sot_relationships import service_relationship

    assert set(CAPABILITY_OWNERS) == CAPABILITY_CATALOGUE.codes(), (
        "CAPABILITY_OWNERS and the declared catalogue drifted apart"
    )
    for code, owner in sorted(CAPABILITY_OWNERS.items()):
        service = service_relationship(owner)  # KeyError == orphan capability
        assert service.name == owner, code
    # Negative control: the lookup is red-sensitive, not vacuously green.
    with pytest.raises(KeyError):
        service_relationship("sub.not_a_real_owner")


# --- capability codes are not entitlements/permissions ----------------------


def _composition_string_leaks(root: Path) -> list[str]:
    """Files under ``root`` (minus the composition module itself) that mention
    the profile code or a capability code — business logic must not branch on
    either (profile names are preflight conveniences; capability codes are
    product vocabulary, never permissions)."""
    from app.composition import CAPABILITY_OWNERS, DEDICATED_ISP_PROFILE_CODE

    needles = {DEDICATED_ISP_PROFILE_CODE, *CAPABILITY_OWNERS}
    leaks: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == COMPOSITION_FILE.resolve():
            continue
        text = path.read_text(encoding="utf-8")
        for needle in sorted(needles):
            if needle in text:
                leaks.append(f"{path.relative_to(root)}: {needle!r}")
    return leaks


def test_no_profile_or_capability_string_in_business_logic() -> None:
    leaks = _composition_string_leaks(APP_DIR)
    assert not leaks, (
        "app/ references composition vocabulary outside app/composition.py — "
        "profile names and capability codes are never entitlement/permission "
        "inputs:\n" + "\n".join(leaks)
    )


def test_string_leak_guard_is_red_sensitive(tmp_path: Path) -> None:
    """Negative control: the leak scanner actually flags an offender."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "if profile == 'sub-dedicated-isp':\n    allow('network_projection.radius')\n",
        encoding="utf-8",
    )
    leaks = _composition_string_leaks(tmp_path)
    assert len(leaks) == 2, leaks


# --- zero route / middleware change (S2 canary pattern, differential) -------


_SNAPSHOT_BODY = (
    "import json, sys\n"
    "from app.main import app\n"
    "routes = sorted(\n"
    "    (getattr(r, 'path', ''),\n"
    "     ','.join(sorted(getattr(r, 'methods', None) or ())))\n"
    "    for r in app.routes\n"
    ")\n"
    "middleware = [\n"
    "    m.cls.__module__ + '.' + m.cls.__name__ for m in app.user_middleware\n"
    "]\n"
    "print(json.dumps({'routes': routes, 'middleware': middleware}))\n"
)


def _app_snapshot(*, with_composition: bool) -> dict[str, object]:
    prefix = "import app.composition  # noqa: F401\n" if with_composition else ""
    result = subprocess.run(
        [sys.executable, "-c", prefix + _SNAPSHOT_BODY],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    snapshot: dict[str, object] = json.loads(result.stdout)
    return snapshot


def test_importing_composition_changes_no_route_or_middleware() -> None:
    """Differential canary: the app with the composition module imported first
    is route-for-route and middleware-for-middleware identical to the app
    without it. Extends the S2 app-unchanged canary
    (``test_kernel_compatibility.py`` pins the top-level prefixes; this pins
    the delta to exactly zero)."""
    baseline = _app_snapshot(with_composition=False)
    with_composition = _app_snapshot(with_composition=True)
    assert baseline["routes"] == with_composition["routes"], (
        "importing app.composition changed the route table"
    )
    assert baseline["middleware"] == with_composition["middleware"], (
        "importing app.composition changed the middleware stack"
    )
    assert baseline["routes"], "empty route snapshot — canary is broken"
