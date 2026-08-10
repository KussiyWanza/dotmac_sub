"""Translate Sub's setting specs into the kernel's registry.

The cutover point. Sub declares 560 `SettingSpec`s in
`app/services/settings_spec.py`; the kernel resolves against its OWN registry,
so every one has to be registered there before `dotmac_kernel` can answer a
read. This module is that translation and nothing else — it holds no defaults,
makes no decisions, and is not a second declaration site. `settings_spec`
remains the owner of a setting's shape (`docs/SOT_RELATIONSHIP_MAP.md`).

The two shapes are close enough that the translation is mechanical, with three
places where they genuinely differ:

- **`value_type`.** Sub's is an `enum.Enum`; the kernel's is an open `str`
  subclass since kernel `0.1.0a15`. Translation is by VALUE, which also means a
  value type Sub does not have (`money`) is reachable later without touching
  this function.
- **`required`.** Sub's is a bool; the kernel's `required_at` names the SCOPE a
  value is required at, because a kernel deployment can require a setting of
  each tenant without requiring it of the platform. Sub has one tenant and its
  settings are tenant-scoped (ADR-0009), so a required spec is required at
  `tenant`.
- **`description` and `validator`.** The kernel has them, Sub does not. Left
  unset rather than invented.
"""

from __future__ import annotations

from dotmac_kernel.setting_value_types import SettingValueType as KernelValueType
from dotmac_kernel.settings_models import SettingDomain as KernelSettingDomain
from dotmac_kernel.settings_resolver import SettingSpec as KernelSettingSpec
from dotmac_kernel.settings_resolver import register_specs

from app.services.settings_spec import SETTINGS_SPECS, SettingSpec

#: The scope a Sub setting is required at. Sub provisions exactly one tenant
#: and its settings belong to it, so "required" means required of that tenant.
_REQUIRED_SCOPE = "tenant"


def to_kernel_spec(spec: SettingSpec) -> KernelSettingSpec:
    """One Sub spec as the kernel declares it."""

    return KernelSettingSpec(
        # The kernel's own member type, not a bare `str`: it carries `.value`,
        # which the kernel's own default-validation reads.
        domain=KernelSettingDomain(str(spec.domain)),
        key=spec.key,
        value_type=KernelValueType(spec.value_type.value),
        default=spec.default,
        label=spec.label,
        env_var=spec.env_var,
        required_at=_REQUIRED_SCOPE if spec.required else None,
        allowed=spec.allowed,
        min_value=spec.min_value,
        max_value=spec.max_value,
        is_secret=spec.is_secret,
    )


def kernel_specs() -> list[KernelSettingSpec]:
    """Every Sub spec, in declaration order."""

    return [to_kernel_spec(spec) for spec in SETTINGS_SPECS]


def register_with_kernel() -> int:
    """Register Sub's specs with the kernel registry. Returns the count.

    Called at import of `settings_spec`'s resolver path so that a kernel read
    can never precede the declaration it depends on.
    """

    specs = kernel_specs()
    register_specs(specs)
    return len(specs)
