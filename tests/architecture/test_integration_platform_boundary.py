"""Architecture guards for the approved integration-platform migration."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from app.services import sot_relationships
from app.services.integrations.registry import (
    connector_definitions,
    supported_connector_definitions,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_ENV = PROJECT_ROOT / "alembic/env.py"
ACTIVE_MIGRATIONS = PROJECT_ROOT / "alembic/versions"
DESIGN = PROJECT_ROOT / "docs/designs/INTEGRATION_PLATFORM_SOT.md"
SOT_MAP = PROJECT_ROOT / "docs/SOT_RELATIONSHIP_MAP.md"
CONNECTOR_ROOT = PROJECT_ROOT / "app/services/integrations/connectors"
MANIFEST_PINS = PROJECT_ROOT / "tests/architecture/connector_manifest_pins.json"

RETIRED_PATHS = (
    "app/api/webhooks.py",
    "app/models/crm_webhook_delivery.py",
    "app/models/integration_hook.py",
    "app/models/webhook.py",
    "app/services/crm_native_sync.py",
    "app/services/crm_webhook_deliveries.py",
    "app/services/flutterwave.py",
    "app/services/integration_hooks.py",
    "app/services/integrations/connectors/whatsapp.py",
    "app/services/paystack.py",
    "app/services/webhook.py",
    "app/services/webhook_deliveries.py",
    "app/tasks/crm_native_sync.py",
    "app/tasks/webhooks.py",
    "templates/admin/integrations/register.html",
    "templates/admin/integrations/register_configure.html",
)

CURRENT_INTEGRATION_SERVICES = (
    "integration.registry",
    "integration.oauth_tokens",
    "integration.installations",
    "integration.runtime",
    "integration.delivery",
    "integration.inbox",
    "integration.jobs",
    "integration.sync",
    "integration.backoffice_adapter",
    "integration.workforce_attendance_adapter",
    "integration.dotmac_erp_payables_adapter",
    "integration.dotmac_erp_material_support_adapter",
)

# This is shrink-only compatibility debt. New connector modules must consume
# typed ports and runtime context rather than Sub's database or ORM models.
LEGACY_DIRECT_PERSISTENCE_IMPORTS: dict[str, set[str]] = {}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _direct_persistence_imports(path: Path) -> set[str]:
    tree = ast.parse(_read(path), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.db" or alias.name.startswith("app.models"):
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "app.db" or module.startswith("app.models"):
                imports.add(module)
    return imports


def _alembic_model_module_imports() -> set[str]:
    tree = ast.parse(_read(ALEMBIC_ENV), filename=str(ALEMBIC_ENV))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "app.models"
        for alias in node.names
    }


def test_integration_platform_design_records_the_approved_invariants() -> None:
    design = _read(DESIGN)
    normalized_design = " ".join(design.split())

    required_contracts = (
        "Status: implemented source-of-truth architecture, 2026-07-20.",
        "A connector is a transport and translation implementation",
        "Connector code cannot write Sub domain tables.",
        "the admin UI does not install arbitrary executable code",
        "no duplicate external send and no duplicate Sub domain write",
        "Service names enter the executable SOT registry only when",
    )
    for contract in required_contracts:
        assert contract in normalized_design


def test_connector_versions_are_immutable_reviewed_manifest_pins() -> None:
    expected = json.loads(_read(MANIFEST_PINS))
    current = {
        (definition.key, definition.version, definition.digest)
        for definition in connector_definitions()
    }
    actual = [
        {
            "current": (
                definition.key,
                definition.version,
                definition.digest,
            )
            in current,
            "key": definition.key,
            "manifest_digest": definition.digest,
            "version": definition.version,
        }
        for definition in supported_connector_definitions()
    ]

    assert actual == expected
    version_digests: dict[tuple[str, str], set[str]] = {}
    for definition in supported_connector_definitions():
        version_digests.setdefault(
            (definition.key, definition.version),
            set(),
        ).add(definition.digest)
    legacy_collisions = {
        identity: digests
        for identity, digests in version_digests.items()
        if len(digests) > 1
    }
    assert legacy_collisions == {
        (
            "meta.social",
            "1.0.0",
        ): {
            "1186a1b861b561480e609363baf2822dbc7091857054ef076fb686e581e4bc5b",
            "c2a13ab30bebb90b312ab4bbcf1d2feaa23ea706100b3e5ea35a101ec566cf08",
        },
        (
            "paystack",
            "1.0.0",
        ): {
            "53791d3e2e06fe1ca128a0e3e8ced86549392af7b6131f61bd21044d71aafc6e",
            "9f1e314860294696c825d8d49d300df903ced6c319b406f295047d25585e836c",
        },
    }


def test_manifest_adoption_has_one_owner_and_a_pre_replacement_gate() -> None:
    installation_owner = _read(
        PROJECT_ROOT / "app/services/integrations/installations.py"
    )
    runtime = _read(PROJECT_ROOT / "app/services/integrations/runtime_execution.py")
    deploy = _read(PROJECT_ROOT / "scripts/deploy.sh")

    assert "def adopt_installation_manifest(" in installation_owner
    assert "with_for_update()" in installation_owner
    assert "require_pinned_connector_definition" in installation_owner
    assert "require_pinned_connector_definition" in runtime
    assert "scripts.integrations.verify_manifest_pins" in deploy
    assert "scripts.integrations.verify_crm_ticket_readiness" in deploy
    assert deploy.index("scripts.integrations.verify_manifest_pins") < deploy.index(
        "scripts.integrations.verify_crm_ticket_readiness"
    )
    assert deploy.index(
        "scripts.integrations.verify_crm_ticket_readiness"
    ) < deploy.index("Starting warm candidate")


def test_alembic_registry_does_not_import_retired_integration_models() -> None:
    retired_model_modules = {
        Path(path).stem for path in RETIRED_PATHS if path.startswith("app/models/")
    }

    assert _alembic_model_module_imports().isdisjoint(retired_model_modules)


def test_active_migrations_do_not_extend_retired_integration_types() -> None:
    offenders = [
        path.name
        for path in sorted(ACTIVE_MIGRATIONS.glob("*.py"))
        if "ALTER TYPE webhookeventtype" in _read(path)
    ]

    assert not offenders, (
        "Active migrations must not depend on the retired webhook enum; "
        f"revision 380 owns cleanup of deployed legacy objects: {offenders}"
    )


def test_deployed_legacy_constraint_cleanup_is_idempotent() -> None:
    cleanup_statements = {
        node.value
        for revision in (
            "377_integration_capability_sync.py",
            "380_integration_platform_cutover.py",
        )
        for node in ast.walk(
            ast.parse(_read(ACTIVE_MIGRATIONS / revision), filename=revision)
        )
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    for table_name, constraint_name in (
        ("integration_targets", "integration_targets_connector_config_id_fkey"),
        ("payment_providers", "payment_providers_connector_config_id_fkey"),
    ):
        assert (
            f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {constraint_name}"
            in cleanup_statements
        )


def test_integration_sot_names_the_live_cutover_owners() -> None:
    assert (
        sot_relationships.service_names_for_domain("integration_control_plane")
        == CURRENT_INTEGRATION_SERVICES
    )
    assert sot_relationships.dependencies_for("integration.jobs") == (
        "integration.registry",
        "integration.installations",
        "scheduler.registry",
    )
    assert sot_relationships.dependencies_for("integration.oauth_tokens") == (
        "control.settings_spec",
        "secrets.reference_store",
        "events.dispatcher",
    )
    assert sot_relationships.dependencies_for("integration.installations") == (
        "integration.registry",
        "secrets.reference_store",
    )
    assert sot_relationships.dependencies_for("integration.sync") == (
        "integration.jobs",
        "integration.runtime",
    )
    assert sot_relationships.dependencies_for("integration.runtime") == (
        "integration.registry",
        "integration.installations",
        "secrets.reference_store",
    )
    assert sot_relationships.dependencies_for("integration.delivery") == (
        "events.store",
        "integration.installations",
        "integration.runtime",
    )
    assert sot_relationships.dependencies_for("integration.inbox") == (
        "integration.installations",
        "integration.runtime",
    )
    assert sot_relationships.dependencies_for("integration.backoffice_adapter") == (
        "integration.installations",
        "integration.runtime",
    )
    assert sot_relationships.dependencies_for(
        "integration.workforce_attendance_adapter"
    ) == (
        "auth.permission_gate",
        "integration.backoffice_adapter",
    )
    attendance = sot_relationships.service_relationship(
        "integration.workforce_attendance_adapter"
    )
    assert attendance.contract is not None
    assert attendance.contract.transaction.mode.value == "read_only"
    assert sot_relationships.dependencies_for(
        "integration.dotmac_erp_payables_adapter"
    ) == ("integration.backoffice_adapter",)
    assert sot_relationships.dependencies_for(
        "integration.dotmac_erp_material_support_adapter"
    ) == (
        "integration.backoffice_adapter",
        "operations.material_dependencies",
    )

    registry = sot_relationships.services_for_domain("integration_control_plane")[0]
    assert registry.notes is not None
    assert "INTEGRATION_PLATFORM_SOT.md" in registry.notes

    premature_target_owners = {
        "integration.links",
        "integration.health",
    }
    assert premature_target_owners.isdisjoint(
        sot_relationships.service_names_for_domain("integration_control_plane")
    )


def test_narrative_sot_names_each_integration_authority_migration() -> None:
    relationships = _read(SOT_MAP)

    assert "docs/designs/INTEGRATION_PLATFORM_SOT.md" in relationships
    for concern in (
        "Connector catalogue",
        "Installation configuration",
        "Sync dispatch",
        "CRM",
        "Outbound webhooks and hooks",
        "WhatsApp messaging",
        "Backoffice/ERP",
        "Payments",
    ):
        assert f"| {concern} |" in relationships

    assert "Authority cutover is complete" in relationships
    assert "Connectors translate\nbounded, typed contracts" in relationships


def test_retired_integration_paths_cannot_return() -> None:
    present = [path for path in RETIRED_PATHS if (PROJECT_ROOT / path).exists()]
    assert not present, f"Retired integration paths returned: {present}"

    application_text = "\n".join(
        _read(path)
        for root in (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts")
        for path in sorted(root.rglob("*.py"))
    )
    for retired_import in (
        "app.services.paystack",
        "app.services.flutterwave",
        "app.services.integration_hooks",
        "app.services.webhook_deliveries",
    ):
        assert retired_import not in application_text
    for retired_environment_read in (
        'os.getenv("CRM_BASE_URL"',
        'os.getenv("CRM_USERNAME"',
        'os.getenv("CRM_PASSWORD"',
        'os.getenv("PAYSTACK_SECRET_KEY"',
        'os.getenv("FLUTTERWAVE_SECRET_KEY"',
        'env_var="WHATSAPP_PROVIDER"',
        'env_var="WHATSAPP_API_KEY"',
        'env_var="WHATSAPP_API_SECRET"',
        'env_var="WHATSAPP_API_TIMEOUT_SECONDS"',
        'env_var="META_WEBHOOK_VERIFY_TOKEN"',
    ):
        assert retired_environment_read not in application_text


def test_crm_transport_is_constructed_only_by_the_registered_connector() -> None:
    allowed_constructor = (
        PROJECT_ROOT / "app/services/integrations/connectors/dotmac_crm.py"
    )
    violations: list[str] = []
    retired_factory_violations: list[str] = []
    for root in (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts"):
        for path in sorted(root.rglob("*.py")):
            source = _read(path)
            if "CRMClient(" in source and path != allowed_constructor:
                violations.append(str(path.relative_to(PROJECT_ROOT)))
            if "get_crm_client" in source:
                retired_factory_violations.append(str(path.relative_to(PROJECT_ROOT)))

    assert not violations, (
        "CRMClient is a connector-private transport; callers must use a typed "
        f"dotmac.crm capability binding: {violations}"
    )
    assert not retired_factory_violations, (
        f"The pre-platform CRM client factory is retired: {retired_factory_violations}"
    )


def test_arbitrary_integration_registration_surface_stays_retired() -> None:
    routes = _read(PROJECT_ROOT / "app/web/admin/integrations.py")
    service = _read(PROJECT_ROOT / "app/services/web_integrations.py")

    assert '"/register"' not in routes
    for retired_symbol in (
        "create_registered_integration",
        "registered_integration_config_state",
        "update_registered_integration_config",
    ):
        assert retired_symbol not in service


def test_new_connector_modules_cannot_import_sub_persistence() -> None:
    actual_legacy: dict[str, set[str]] = {}
    violations: dict[str, list[str]] = {}

    for path in sorted(CONNECTOR_ROOT.glob("*.py")):
        if path.name == "__init__.py":
            continue
        imports = _direct_persistence_imports(path)
        if path.name in LEGACY_DIRECT_PERSISTENCE_IMPORTS:
            actual_legacy[path.name] = imports
        elif imports:
            violations[path.name] = sorted(imports)

    assert actual_legacy == LEGACY_DIRECT_PERSISTENCE_IMPORTS, (
        "The shrink-only connector persistence baseline changed. Remove a "
        "resolved import from LEGACY_DIRECT_PERSISTENCE_IMPORTS, or perform an "
        "explicit ownership review before adding debt."
    )
    assert not violations, (
        "Connector modules must consume typed integration/domain ports and may "
        f"not import Sub persistence directly: {violations}"
    )


def test_meta_social_inbox_uses_one_typed_platform_boundary() -> None:
    webhook = _read(PROJECT_ROOT / "app/api/meta_inbox_webhooks.py")
    outbound = _read(PROJECT_ROOT / "app/tasks/notifications.py")
    connector = _read(
        PROJECT_ROOT / "app/services/integrations/connectors/meta_social_runtime.py"
    )

    assert "from app.services.integrations.meta_social_capability import (" in webhook
    assert "inbound_secret_material(db)" in webhook
    assert "whatsapp_capability" not in webhook
    assert "meta_social_capability.send_direct_message" in outbound
    assert "meta_pages.send_direct_message" not in outbound
    assert 'FACEBOOK_TOKEN_BINDING = "facebook_page_access_token"' in connector
    assert 'INSTAGRAM_TOKEN_BINDING = "instagram_login_access_token"' in connector
    assert "graph.facebook.com" in connector
    assert "graph.instagram.com" in connector


def test_whatsapp_setup_verification_uses_installation_owned_typed_query() -> None:
    webhook = _read(PROJECT_ROOT / "app/api/inbox_webhooks.py")
    owner = _read(PROJECT_ROOT / "app/services/integrations/whatsapp_installation.py")

    assert "VerifyWhatsAppWebhookChallengeQuery" in webhook
    assert "verify_whatsapp_webhook_challenge(" in webhook
    assert "resolve_secret" not in webhook
    assert "inbound_secret_material(db)" in webhook
    assert "IntegrationInstallationState.disabled.value" in owner
    assert "IntegrationInstallationState.enabled.value" in owner
    assert "hmac.compare_digest" in owner
