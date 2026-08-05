"""Sub's declared product composition — metadata only, never a runtime mount.

Slice S3 of the selective kernel-adoption plan
(docs/PLATFORM_ADOPTION_LEDGER.md, "S3 acceptance claim"). This module is the
single Sub-owned declaration of product vocabulary for releases and licences:

- ``SUB_ASSEMBLY`` is a frozen :class:`ProductAssemblySpec` consumed by
  validation and composition preflight ONLY. ``app.main`` remains the runtime
  owner: nothing here is passed to ``create_app``, mounts a route, installs
  middleware, constructs an engine, or touches ``app.db``. The S1 import guard
  (``tests/architecture/test_kernel_import_boundary.py``) keeps
  ``mount_features`` unimportable in ``app/``, so these manifests physically
  cannot remount the app.
- The four :class:`FeatureManifest`s are COARSE declarations around existing
  SOT domains (the S4–S6/S8 surface), not one manifest per service and not a
  catalogue of the whole repository. Their router/nav fields are deliberately
  empty tuples: in Sub a manifest is pure metadata, never a mount request.
- Every capability code names exactly one existing domain owner registered in
  ``app/services/sot_registry/registry.py`` (see ``CAPABILITY_OWNERS``; enforced
  by ``tests/architecture/test_composition.py``). A capability code is a
  product-vocabulary statement that the capability exists and who owns it.
  It is NEVER an entitlement, a permission, an RBAC input, a subscriber
  financial-access decision, or a service-readiness decision (plan boundary 5).
- ``DEDICATED_ISP_PROFILE`` is the versioned dedicated-ISP composition
  preflight. Profile names are conveniences over independent axes
  (modules, providers, locale, currency, legal, residency); business logic
  under ``app/`` never reads a profile string or branches on a capability
  code (guard-tested).

This module must stay importable with no database configured and without
booting the application (canary-tested).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from dotmac_kernel.assembly import ProductAssemblySpec
from dotmac_kernel.capabilities import CapabilityCatalogue
from dotmac_kernel.features import FeatureManifest
from dotmac_kernel.profiles import (
    DeploymentProfileRegistry,
    DeploymentProfileSpec,
    ProfileValidationReport,
)

PRODUCT_NAME: Final = "dotmac-sub"

# Coarse product-module names — one per declared SOT domain slice, NOT one per
# service. Only the four domains the plan scopes for S4–S6/S8 are declared;
# later domain slices expand this list explicitly (ledger amendment each time).
MODULE_NETWORK_PROJECTION: Final = "sub.network_projection"
MODULE_BACKOFFICE_COLLABORATION: Final = "sub.backoffice_collaboration"
MODULE_BILLING_EXPORT: Final = "sub.billing_export"
MODULE_LICENSING_RECEPTION: Final = "sub.licensing_reception"

#: capability code -> the ONE existing owner in the executable SOT registry
#: (``app/services/sot_registry/registry.py``). The registry stays authoritative;
#: this mapping only references its exact service names and is cross-checked
#: by the no-orphan architecture test. Codes here are product vocabulary for
#: releases/licences — never entitlement or permission inputs.
CAPABILITY_OWNERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        # S4 pilot: the RADIUS projection/reconciliation boundary.
        "network_projection.radius": "access.radius_projection",
        # S6 first vertical slice: vendor material release collaboration.
        "backoffice_collaboration.material_release": (
            "operations.vendor_material_release"
        ),
        # S6 second slice: vendor advance collaboration.
        "backoffice_collaboration.vendor_advance": "operations.vendor_advances",
        # S5/S6: versioned ERP billing export staging + acknowledgement.
        "billing_export.erp_billing": "integration.dotmac_erp_billing_adapter",
        # S8: licence reception projects into product module enablement.
        "licensing_reception.module_enablement": "control.module_manager",
    }
)

#: The four coarse domain manifests. Router/nav fields are EMPTY on purpose:
#: Sub declares manifests as pure metadata (the starter's router-carrying use
#: is an app-factory concern Sub never adopts — ``mount_features`` is denied
#: by the S1 guard).
SUB_FEATURE_MANIFESTS: Final[tuple[FeatureManifest, ...]] = (
    FeatureManifest(
        name=MODULE_NETWORK_PROJECTION,
        capabilities=("network_projection.radius",),
    ),
    FeatureManifest(
        name=MODULE_BACKOFFICE_COLLABORATION,
        capabilities=(
            "backoffice_collaboration.material_release",
            "backoffice_collaboration.vendor_advance",
        ),
    ),
    FeatureManifest(
        name=MODULE_BILLING_EXPORT,
        capabilities=("billing_export.erp_billing",),
    ),
    FeatureManifest(
        name=MODULE_LICENSING_RECEPTION,
        capabilities=("licensing_reception.module_enablement",),
    ),
)

#: Fails closed at import on a duplicate capability declaration
#: (``DuplicateCapabilityError`` — kernel behaviour, negative-tested).
CAPABILITY_CATALOGUE: Final[CapabilityCatalogue] = CapabilityCatalogue.from_manifests(
    SUB_FEATURE_MANIFESTS
)

#: Composition metadata only. ``app.main`` remains the runtime owner; this
#: spec is consumed by validation/preflight and is never passed to any app
#: factory.
SUB_ASSEMBLY: Final[ProductAssemblySpec] = ProductAssemblySpec(
    name=PRODUCT_NAME,
    modules=SUB_FEATURE_MANIFESTS,
    # Sub mounts its own web surface in app.main; recorded here as metadata.
    web_enabled=True,
)

DEDICATED_ISP_PROFILE_CODE: Final = "sub-dedicated-isp"
DEDICATED_ISP_PROFILE_VERSION: Final = "1.0.0"

#: Provider-implementation names this deployment declares for the kernel's
#: eight provider seams. "none" is the explicit null selection for seams a
#: dedicated single-operator ISP deployment does not fill; "sub-owned-*"
#: names Sub's existing in-repo owners (they are labels for preflight, not
#: import paths, and confer no authority — the SOT registry stays the owner
#: map).
DECLARED_PROVIDER_IMPLEMENTATIONS: Final[frozenset[str]] = frozenset(
    {
        "none",
        "sub-owned-radius-projection",
        "sub-owned-identity",
        "sub-owned-observability",
        "nginx",
    }
)

#: Versioned dedicated-ISP composition preflight. Axes are independent by
#: construction (kernel ``DeploymentProfileSpec``): module set, each provider
#: seam, locale, currency, legal authority, and data residency are separate
#: deliberate declarations — the profile NAME bundles them for preflight and
#: is never read by business logic.
DEDICATED_ISP_PROFILE: Final[DeploymentProfileSpec] = DeploymentProfileSpec(
    code=DEDICATED_ISP_PROFILE_CODE,
    version=DEDICATED_ISP_PROFILE_VERSION,
    required_modules=frozenset(
        {
            MODULE_NETWORK_PROJECTION,
            MODULE_BACKOFFICE_COLLABORATION,
            MODULE_BILLING_EXPORT,
            MODULE_LICENSING_RECEPTION,
        }
    ),
    # Ledger-prohibited kernel surfaces must never appear in a Sub
    # deployment's module set (kernel reference CRUD/web features; kernel
    # messaging tables beside events.store/integration.* — defer-db until the
    # S7 ADR).
    forbidden_modules=frozenset(
        {
            "kernel.reference_features",
            "kernel.messaging",
        }
    ),
    # Provider seams — one named implementation per axis.
    commercial_provider="none",
    provisioning_provider="sub-owned-radius-projection",
    identity_provider="sub-owned-identity",
    telemetry_provider="sub-owned-observability",
    update_provider="none",
    ingress_provider="nginx",
    dns_verification_provider="none",
    tls_provider="none",
    # Locale / currency / legal / residency posture — independent axes.
    default_locale="en-NG",
    supported_locales=frozenset({"en-NG"}),
    allowed_currencies=frozenset({"NGN"}),
    legal_authority="NG",
    data_residency="NG",
)

DEPLOYMENT_PROFILES: Final[DeploymentProfileRegistry] = DeploymentProfileRegistry(
    (DEDICATED_ISP_PROFILE,)
)


def declared_module_names() -> frozenset[str]:
    """The declared coarse product-module names (metadata, not runtime state)."""
    return frozenset(manifest.name for manifest in SUB_FEATURE_MANIFESTS)


def dedicated_isp_preflight() -> ProfileValidationReport:
    """Deterministic dedicated-ISP composition preflight over declared metadata.

    Validates the versioned profile against exactly what this module declares
    (modules + provider implementations). Pure and repeatable: same inputs,
    byte-identical report. It inspects no runtime state and changes nothing.
    """
    declared = declared_module_names()
    return DEPLOYMENT_PROFILES.validate(
        DEDICATED_ISP_PROFILE_CODE,
        installed_modules=declared,
        enabled_modules=declared,
        available_providers=DECLARED_PROVIDER_IMPLEMENTATIONS,
    )


__all__ = [
    "CAPABILITY_CATALOGUE",
    "CAPABILITY_OWNERS",
    "DECLARED_PROVIDER_IMPLEMENTATIONS",
    "DEDICATED_ISP_PROFILE",
    "DEDICATED_ISP_PROFILE_CODE",
    "DEDICATED_ISP_PROFILE_VERSION",
    "DEPLOYMENT_PROFILES",
    "PRODUCT_NAME",
    "SUB_ASSEMBLY",
    "SUB_FEATURE_MANIFESTS",
    "declared_module_names",
    "dedicated_isp_preflight",
]
