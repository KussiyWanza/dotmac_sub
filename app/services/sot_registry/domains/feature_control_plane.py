"""Canonical SOT declarations for the feature_control_plane domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    SOTService,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="feature_control_plane",
    setting_domains=("modules",),
    services=(
        SOTService(
            name="control.feature_registry",
            module="app.services.control_registry",
            owns=(
                "module/feature/safety control resolution",
                "legacy feature-flag alias mapping",
                "feature-to-module composition",
            ),
            depends_on=("control.module_manager", "control.domain_settings"),
            notes=(
                "Optional capabilities only. Core billing, catalog lifecycle, "
                "collections, prepaid renewal/enforcement, customer notifications, "
                "and event recovery are permanently owned runtime responsibilities "
                "and are absent from this registry."
            ),
        ),
        SOTService(
            name="control.module_manager",
            module="app.services.module_manager",
            owns=("product module enablement", "module labels and feature states"),
        ),
        SOTService(
            name="control.domain_settings",
            module="app.services.domain_settings",
            owns=("domain setting persistence", "setting update validation"),
        ),
        SOTService(
            name="control.settings_spec",
            module="app.services.settings_spec",
            owns=(
                "setting schema and validation bounds",
                "setting value coercion",
                "DB-authoritative runtime setting resolution",
                "registered setting defaults",
            ),
            depends_on=("control.domain_settings",),
            notes=(
                "Runtime precedence is Redis cache, active database row, then "
                "the registered default. SettingSpec.env_var is bootstrap and "
                "migration metadata, never an implicit live override."
            ),
        ),
        SOTService(
            name="control.settings_bootstrap",
            module="app.services.settings_seed",
            owns=(
                "startup default-setting materialization",
                "environment-to-setting bootstrap",
                "default notification-template seeding",
            ),
            depends_on=("control.domain_settings", "control.settings_spec"),
            notes=(
                "Environment inputs are materialized one way into stored "
                "settings and do not override runtime database decisions."
            ),
        ),
        SOTService(
            name="control.relationships",
            module="app.services.control_relationships",
            owns=(
                "setting exclusivity and migration-chain validation",
                "event handler stage and capability ownership",
                "control relationship diagnostics",
            ),
            depends_on=("control.domain_settings", "control.settings_spec"),
        ),
    ),
    entrypoints=(
        "app.services.scheduler_config",
        "app.tasks.*",
        "app.web.admin.system",
        "app.api.settings",
    ),
    rule="Settings are inputs, not decision owners. Callers ask the named "
    "owner or resolver for a decision; they do not independently compose "
    "module, environment, database, and legacy state. Business and "
    "operational tuning is database-authoritative unless a separately "
    "registered, visible emergency override says otherwise.",
)
