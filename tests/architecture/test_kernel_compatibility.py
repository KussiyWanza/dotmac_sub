"""The pinned dotmac-kernel wheel is compatible with Sub, and stays contained.

Slice S2 of the selective kernel-adoption plan
(docs/PLATFORM_ADOPTION_LEDGER.md) pinned the wheel while Sub's runtime stayed
UNCHANGED. That is no longer the state and this file says so rather than
asserting a claim it has outgrown: the settings cutover made
``app/services/settings_spec.py`` register Sub's specs with
``dotmac_kernel.settings_resolver`` and resolve through it, so Sub now CONSUMES
the kernel rather than merely depending on it.

What this file proves, with zero skips:

- every ledger-allowlisted pure surface (and the tests/-only test kit) imports
  in a subprocess with ``DATABASE_URL`` and all DB-ish env stripped — the
  "testing is DB-free" claim is exercised, not assumed;
- the installed distribution is exactly the reviewed pin and ships ``py.typed``;
- ``pyproject.toml`` pins the kernel EXACTLY (no ``^``/``~``/``>=``/``*``
  range) from the named private index — an unreviewed range upgrade is a CI
  failure, not a lockfile surprise;
- the pure value contracts behave (exact ``Decimal`` money, float rejection,
  provisioning protocol/result types, assembly/feature/capability/profile
  specs, the reusable provider contract check);
- the Sub app builds with the kernel installed, and every ``dotmac_kernel``
  module in its import graph is one the ledger allowlist admits OR one the
  kernel reaches for itself (``TRANSITIVE_KERNEL_MODULES``, a reviewed
  snapshot — eighteen of them, which is worth knowing) — and no kernel
  middleware is mounted, no kernel route endpoint is served, and the top-level
  route prefix set is exactly the reviewed pin.

The containment that matters is unchanged: ``dotmac_kernel.deps``,
``.middleware``, ``.audit`` and the rest stay out of ``app/`` entirely, and a
side-door import of one fails here even though the AST guard cannot see it.

``dotmac_kernel.testing`` usage stays confined to ``tests/`` per the ledger;
this file is itself test-side and imports kernel modules freely (the S1 guard
covers ``app/`` only).
"""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

# The one allowlist, imported rather than restated: two copies would drift,
# and the ledger already keeps THAT module's copy in byte-for-byte sync.
from tests.architecture.test_kernel_import_boundary import ALLOWED_KERNEL_MODULES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"

#: The reviewed kernel pin. Changing it is a ledger amendment
#: (docs/PLATFORM_ADOPTION_LEDGER.md), never a lockfile side effect.
KERNEL_PIN = "0.1.0a40"

#: The private index source name pyproject must route the kernel through.
KERNEL_SOURCE = "forgejo"

#: What the kernel loads FOR ITS OWN USE once `app/` imports the settings
#: resolver, measured rather than assumed — and larger than anyone expected.
#:
#: Consuming one kernel subsystem pulls eighteen more modules into the process,
#: including `audit`, `security`, `identity`, `permissions` and `entitlements`
#: — precisely the surfaces the adoption ledger keeps out of `app/`. Nothing in
#: `app/` imports them and the AST guard still refuses one that tries; they are
#: here because `settings_resolver` reaches them internally.
#:
#: Being LOADED is not being USED: a module in `sys.modules` creates no second
#: authority, mounts no route, and answers no question Sub asks. The sibling
#: test below proves that separately, on middleware and routes. But the cost of
#: adoption is not "one module" and this list is what stops that being a
#: comfortable assumption.
#:
#: This is a reviewed snapshot, like `EXPECTED_ROUTE_PREFIXES`. A NEW name
#: appearing means the kernel started reaching somewhere new, which is a thing
#: to look at rather than absorb. It is NOT a licence for `app/` to import any
#: of these — that list is `ALLOWED_KERNEL_MODULES`, and it is the boundary.
TRANSITIVE_KERNEL_MODULES = frozenset(
    {
        "dotmac_kernel.audit",
        "dotmac_kernel.audit_actions",
        "dotmac_kernel.cache",
        "dotmac_kernel.config",
        "dotmac_kernel.entitlements",
        "dotmac_kernel.exceptions",
        "dotmac_kernel.flags",
        "dotmac_kernel.identity",
        "dotmac_kernel.models_platform",
        "dotmac_kernel.modules",
        "dotmac_kernel.namespaces",
        "dotmac_kernel.permissions",
        "dotmac_kernel.query",
        "dotmac_kernel.security",
        "dotmac_kernel.setting_domains",
        "dotmac_kernel.setting_scopes",
        "dotmac_kernel.settings_cache",
        "dotmac_kernel.settings_crypto",
    }
)

#: Ledger-allowlisted pure surfaces (app/-importable) plus the tests/-only
#: test kit. Every one must import with no database configured.
PURE_IMPORT_SURFACES = (
    "dotmac_kernel",
    "dotmac_kernel.assembly",
    "dotmac_kernel.capabilities",
    "dotmac_kernel.features",
    "dotmac_kernel.money",
    "dotmac_kernel.profiles",
    "dotmac_kernel.providers.provisioning",
    "dotmac_kernel.testing",
    "dotmac_kernel.testing.fakes",
    "dotmac_kernel.testing.provisioning",
    "dotmac_kernel.testing.licensing",
)

#: Sub's top-level route prefixes at import time (``app.main.app``). A kernel
#: mount would add e.g. ``/admin`` feature surfaces or platform auth routes;
#: any NEW first segment must be a reviewed change to this pin.
EXPECTED_ROUTE_PREFIXES = frozenset(
    {
        "/api",
        "/auth",
        "/docs",
        "/health",
        "/metrics",
        "/openapi.json",
        "/redoc",
        "/static",
        "/widget",
    }
)


def _no_db_env() -> dict[str, str]:
    """The current env with every database-ish variable stripped.

    If any kernel surface below read ``DATABASE_URL`` (or constructed an
    engine) at import time, its canary subprocess would fail — that is the
    point.
    """
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


@pytest.mark.parametrize("module", PURE_IMPORT_SURFACES)
def test_pure_surface_imports_without_database(module: str) -> None:
    """Each allowed surface imports in a clean subprocess with no DB env."""
    result = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [sys.executable, "-c", f"import {module}"],
        env=_no_db_env(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"`import {module}` failed with DATABASE_URL/DB env stripped — the "
        f"surface is not pure-contract:\n{result.stderr}"
    )


def test_installed_kernel_is_the_reviewed_pin_and_ships_py_typed() -> None:
    import dotmac_kernel

    assert dotmac_kernel.__version__ == KERNEL_PIN
    dist = importlib.metadata.distribution("dotmac-kernel")
    assert dist.version == KERNEL_PIN
    files = dist.files or []
    assert any(str(f).endswith("dotmac_kernel/py.typed") for f in files), (
        "dotmac-kernel wheel does not ship py.typed — typed-contract "
        "consumption (AGENTS.md typed-contract rule) would silently degrade"
    )


def test_pyproject_pins_kernel_exactly_from_the_named_index() -> None:
    """Reject any range for dotmac-kernel: an upgrade is a reviewed change.

    This is the CI gate against an unreviewed range upgrade — ``^``, ``~``,
    ``>=``, ``*``, or a bare name would let the lockfile move the kernel
    without a ledger amendment.
    """
    import tomllib

    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)

    exact = f"dotmac-kernel=={KERNEL_PIN}"
    main_deps = data["project"]["dependencies"]
    kernel_main = [
        d for d in main_deps if d.replace(" ", "").startswith("dotmac-kernel")
    ]
    assert kernel_main == [exact], (
        f"[project.dependencies] must pin exactly {exact!r}, found {kernel_main!r}"
    )

    dev_deps = data["dependency-groups"]["dev"]
    kernel_dev = [d for d in dev_deps if d.replace(" ", "").startswith("dotmac-kernel")]
    assert kernel_dev == [f"dotmac-kernel[testing]=={KERNEL_PIN}"], (
        "[dependency-groups].dev must pin the [testing] extra at exactly "
        f"{KERNEL_PIN}, found {kernel_dev!r}"
    )

    enrich = data["tool"]["poetry"]["dependencies"]["dotmac-kernel"]
    assert enrich == {"version": KERNEL_PIN, "source": KERNEL_SOURCE}, enrich
    dev_enrich = data["tool"]["poetry"]["group"]["dev"]["dependencies"]["dotmac-kernel"]
    assert dev_enrich["version"] == KERNEL_PIN, dev_enrich
    assert dev_enrich["source"] == KERNEL_SOURCE, dev_enrich

    sources = {s["name"]: s for s in data["tool"]["poetry"]["source"]}
    assert KERNEL_SOURCE in sources, "the private index source is not declared"
    forgejo = sources[KERNEL_SOURCE]
    assert forgejo["priority"] == "explicit", forgejo
    assert forgejo["url"].startswith("https://registry.dotmac.io/"), forgejo
    # No credential material belongs in pyproject — URL only.
    assert "@" not in forgejo["url"].split("//", 1)[1], (
        "index URL must not embed credentials"
    )


def test_money_is_exact_decimal_and_rejects_float() -> None:
    from dotmac_kernel.money import CurrencyMismatchError, Money, MoneyError, currency

    ngn = currency("NGN")
    a = Money.of("10.50", ngn)
    b = Money.of("0.25", ngn)
    assert a.amount == Decimal("10.50")
    assert a.add(b).amount == Decimal("10.75")
    assert a.subtract(b).amount == Decimal("10.25")
    assert Money.of("0.1", ngn).add(Money.of("0.2", ngn)).amount == Decimal("0.30")
    with pytest.raises(MoneyError):
        Money.of(10.5, ngn)  # type: ignore[arg-type] — float is the offence
    with pytest.raises(CurrencyMismatchError):
        a.add(Money.of("1.00", currency("USD")))


def test_provisioning_contract_types_construct_and_check_runs() -> None:
    from dotmac_kernel.providers.provisioning import (
        ApplyResult,
        ObserveResult,
        PlanResult,
        ProvisioningProvider,
        ProvisioningRequest,
        ProvisioningStatus,
    )
    from dotmac_kernel.testing.provisioning import (
        FakeProvisioningProvider,
        check_provisioning_provider_contract,
    )

    request = ProvisioningRequest(intent_id="intent-1", spec={"profile": "basic"})
    plan = PlanResult(intent_id=request.intent_id, plan_hash="hash-1")
    assert plan.is_noop
    applied = ApplyResult(
        intent_id=request.intent_id,
        operation_id="op-1",
        plan_hash=plan.plan_hash,
        status=ProvisioningStatus.SUCCEEDED,
    )
    assert applied.is_terminal and applied.succeeded
    observed = ObserveResult(
        intent_id=request.intent_id,
        operation_id="op-1",
        status=ProvisioningStatus.SUCCEEDED,
    )
    assert observed.is_terminal
    assert isinstance(FakeProvisioningProvider(), ProvisioningProvider)
    # The reusable contract the S4 adapter must pass, proven runnable today.
    check_provisioning_provider_contract(FakeProvisioningProvider)


def test_composition_metadata_types_construct() -> None:
    from dotmac_kernel.assembly import ProductAssemblySpec
    from dotmac_kernel.capabilities import CapabilityCatalogue, DuplicateCapabilityError
    from dotmac_kernel.features import FeatureManifest
    from dotmac_kernel.profiles import DeploymentProfileSpec

    manifest = FeatureManifest(name="sub.demo", capabilities=("demo.use",))
    spec = ProductAssemblySpec(name="dotmac-sub", modules=(manifest,))
    assert spec.modules == (manifest,)

    catalogue = CapabilityCatalogue.from_manifests([manifest])
    assert catalogue.is_declared("demo.use")
    assert catalogue.owner("demo.use") == "sub.demo"
    with pytest.raises(DuplicateCapabilityError):
        CapabilityCatalogue.from_manifests(
            [manifest, FeatureManifest(name="sub.other", capabilities=("demo.use",))]
        )

    profile = DeploymentProfileSpec(
        code="dedicated-isp",
        version="1",
        required_modules=frozenset({"sub.demo"}),
        commercial_provider="none",
        provisioning_provider="sub-owned",
        identity_provider="sub-owned",
        telemetry_provider="sub-owned",
        update_provider="none",
        ingress_provider="nginx",
        dns_verification_provider="none",
        tls_provider="none",
        default_locale="en-NG",
        supported_locales=frozenset({"en-NG"}),
        allowed_currencies=frozenset({"NGN"}),
        legal_authority="NG",
        data_residency="NG",
    )
    assert profile.provider_selections()["provisioning"] == "sub-owned"


def test_fake_licence_signer_works_with_subs_cryptography_pin() -> None:
    """Sub pins cryptography==42.0.8 itself; a8's [testing] extra no longer
    forces a cryptography floor, and the signer works against Sub's pin."""
    assert importlib.metadata.version("cryptography") == "42.0.8"
    from dotmac_kernel.testing.licensing import FakeLicenceSigner

    signer = FakeLicenceSigner()
    assert signer.key().public_key_b64
    assert signer.keyring().get(signer.key_id) is not None


def test_app_import_graph_holds_only_allowlisted_kernel_modules() -> None:
    """The runtime graph agrees with the allowlist — no side-door import.

    This test asserted ZERO kernel modules until the settings cutover, and the
    change is deliberate rather than a relaxation to make a build pass.
    ``app/services/settings_spec.py`` now registers Sub's 560 specs with
    ``dotmac_kernel.settings_resolver`` at import time and resolves through it,
    so the count cannot be zero and Sub is no longer merely PINNED to the
    kernel — it consumes it. The ledger records the same fact.

    What the guard still proves is the thing that mattered all along: the AST
    check (``test_kernel_import_boundary``) proves no source-level import
    outside the allowlist exists, and this proves the RUNTIME graph agrees —
    an entry point, plugin, or transitive side door that dragged in
    ``dotmac_kernel.deps``, ``.middleware`` or ``.audit`` would still fail
    here, because those are what a "kernel-free Sub app" was ever about.

    Submodules of an allowlisted package are permitted: importing
    ``settings_resolver`` legitimately pulls ``dotmac_kernel.settings_models``
    and the value-type registry it validates against.

    ``TRANSITIVE_KERNEL_MODULES`` is the rest, and writing it down is the point
    — see that constant.
    """
    allowed = repr(sorted(ALLOWED_KERNEL_MODULES | TRANSITIVE_KERNEL_MODULES))
    result = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                f"allowed = set({allowed})\n"
                "import app.main\n"
                "loaded = {m for m in sys.modules if m.startswith('dotmac_kernel')}\n"
                "bad = sorted(\n"
                "    m for m in loaded\n"
                "    if m != 'dotmac_kernel'\n"
                "    and not any(m == a or m.startswith(a + '.') for a in allowed)\n"
                ")\n"
                "sys.exit(\n"
                "    'kernel modules in the app import graph that the ledger "
                "allowlist does not admit: %s' % bad if bad else 0\n"
                ")"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


def test_app_middleware_and_routes_are_kernel_free_and_prefixes_unchanged() -> None:
    """The unchanged Sub app: no kernel middleware, no kernel endpoints, and
    the top-level route prefix set is exactly the reviewed pin."""
    expected = repr(set(EXPECTED_ROUTE_PREFIXES))
    result = subprocess.run(  # noqa: S603 — fixed argv, our own interpreter
        [
            sys.executable,
            "-c",
            (
                "from app.main import app\n"
                f"expected = {expected}\n"
                "for middleware in app.user_middleware:\n"
                "    module = middleware.cls.__module__\n"
                "    assert not module.startswith('dotmac_kernel'), middleware\n"
                "    dispatch = middleware.kwargs.get('dispatch')\n"
                "    if dispatch is not None:\n"
                "        module = dispatch.__module__\n"
                "        assert not module.startswith('dotmac_kernel'), middleware\n"
                "prefixes = set()\n"
                "for route in app.routes:\n"
                "    path = getattr(route, 'path', '')\n"
                "    segments = path.split('/')\n"
                "    prefixes.add('/' + segments[1] if len(segments) > 1 else path)\n"
                "    endpoint = getattr(route, 'endpoint', None)\n"
                "    if endpoint is not None:\n"
                "        module = getattr(endpoint, '__module__', '')\n"
                "        assert not module.startswith('dotmac_kernel'), (\n"
                "            f'kernel endpoint mounted at {path}'\n"
                "        )\n"
                "assert prefixes == expected, (\n"
                "    'top-level route prefixes changed with the kernel installed — '"
                "    'a new prefix must be a reviewed change to this pin:\\n'\n"
                "    f'added: {sorted(prefixes - expected)}\\n'\n"
                "    f'removed: {sorted(expected - prefixes)}'\n"
                ")\n"
            ),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
