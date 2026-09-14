"""Canonical SOT declarations for the feature_control_plane domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
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
            name="control.settings_form_updates",
            module="app.services.domain_settings",
            owns=("atomic administrative setting form updates",),
            depends_on=(
                "auth.permission_gate",
                "control.domain_settings",
                "control.settings_spec",
                "observability.audit_log",
            ),
            notes=(
                "The web form validates its complete submitted batch through the "
                "registered setting specification before this owner stages every "
                "row and one value-free audit record in a single transaction."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="atomic administrative setting form updates",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authenticated settings administrator",
                            "normalized declared setting batch",
                            "canonical domain setting rows",
                        ),
                        canonical_writer="control.settings_form_updates",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated settings administrator",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "system:settings:write permission and typed CommandContext"
                        ),
                    ),
                    AuthorityInput(
                        name="normalized declared setting batch",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "complete form batch normalized against registered "
                            "types, defaults, bounds, allowed values, and secrecy"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical domain setting rows",
                        owner="control.domain_settings",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active database-authoritative domain setting rows",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "One apply_admin_settings_form_updates command enters "
                        "execute_owner_command once and commits every setting and "
                        "its audit evidence together."
                    ),
                    locking=(
                        "The database setting identity constraint arbitrates "
                        "concurrent inserts; the owner transaction isolates the "
                        "complete submitted batch."
                    ),
                    idempotency=(
                        "Repeated absolute setting values converge on the same rows; "
                        "the command id and request id identify each attempt."
                    ),
                    retries=(
                        "Validation failures are terminal form errors; unexpected "
                        "persistence failures roll back the full batch for an "
                        "explicit operator retry."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "control.settings_form_updates.invalid_scope",
                        "control.settings_form_updates.invalid_update",
                        *owner_command_boundary_error_codes(
                            "control.settings_form_updates"
                        ),
                    ),
                    mapping_owner="admin system-settings web adapter",
                    fail_closed_on=(
                        "ambiguous or undeclared setting identity",
                        "invalid setting value or relationship",
                        "incomplete persistence batch",
                    ),
                ),
                events=EventContract(
                    event_types=("control.settings_form_updated",),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility=(
                        "Version 1 records only setting identities and batch count; "
                        "setting values and secret material are excluded."
                    ),
                    replay=(
                        "Canonical domain setting rows reconstruct current state; "
                        "the immutable audit record preserves change provenance."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="admin settings form per-setting commit loop",
                    new_owner="control.settings_form_updates",
                    verification=(
                        "focused blank, bounds, atomic rollback, secret, route, "
                        "and architecture tests"
                    ),
                    cutover_gate=(
                        "the admin form calls only the staged batch owner for setting "
                        "persistence"
                    ),
                    fallback_retirement=(
                        "the form no longer calls the legacy committing "
                        "upsert_by_key method"
                    ),
                ),
                steward="platform operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/runbooks/ADMIN_SYSTEM_SETTINGS_ATOMICITY.md",
                ),
                test_refs=(
                    "tests/test_web_system_settings_forms.py",
                    "tests/architecture/test_settings_form_update_boundary.py",
                ),
            ),
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
