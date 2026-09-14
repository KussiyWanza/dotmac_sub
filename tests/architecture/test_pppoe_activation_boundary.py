"""Protect activation-owned PPPoE credentials and IPv4-only projection."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
    contract_validation_errors,
)
from app.services.sot_registry.registry import all_services
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE = ROOT / "app/services/account_lifecycle.py"
CREDENTIAL_OWNER = ROOT / "app/services/pppoe_credentials.py"
PROVISIONING_HANDLER = ROOT / "app/services/events/handlers/provisioning.py"
PROVISIONING_HELPERS = ROOT / "app/services/provisioning_helpers.py"


def test_pppoe_credential_owner_has_complete_typed_contract() -> None:
    service = service_relationship("access.pppoe_credentials")

    assert service.module == "app.services.pppoe_credentials"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert service.contract.migration.state is AuthorityMigrationState.SHADOWING
    assert service.contract.concerns[0].role is OwnerRole.AUTHORITATIVE_RECORD
    assert service.contract.concerns[1].role is OwnerRole.PROJECTION_WRITER
    services = all_services()
    assert not contract_validation_errors(
        service,
        service_names={item.name for item in services},
    )


def test_pending_activation_ensures_credential_before_active_status() -> None:
    source = LIFECYCLE.read_text(encoding="utf-8")
    activation = source.split("def activate_subscription(", 1)[1].split("\ndef ", 1)[0]

    ensure_at = activation.index("ensure_pppoe_credential(")
    active_at = activation.index("subscription.status = SubscriptionStatus.active")
    event_at = activation.index("EventType.subscription_activated")
    assert ensure_at < active_at < event_at


def test_pppoe_owner_is_typed_flush_only_and_secret_safe() -> None:
    source = CREDENTIAL_OWNER.read_text(encoding="utf-8")

    assert "class EnsurePppoeCredentialCommand:" in source
    assert "class EnsurePppoeCredentialOutcome:" in source
    assert ".commit(" not in source
    assert ".rollback(" not in source
    tree = ast.parse(source)
    outcome = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "EnsurePppoeCredentialOutcome"
    )
    field_names = {
        node.target.id
        for node in outcome.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert field_names == {"credential_id", "username", "disposition", "changed"}
    event_payload = source.split("EventType.access_credential_ensured", 1)[1].split(
        "account_id=", 1
    )[0]
    assert "secret_hash" not in event_payload
    assert '"username"' not in event_payload


def test_activation_projection_is_ipv4_only_and_flush_only() -> None:
    handler = PROVISIONING_HANDLER.read_text(encoding="utf-8")
    helpers = PROVISIONING_HELPERS.read_text(encoding="utf-8")

    assert "ensure_ipv4_assignment_for_subscription(" in handler
    assert "ensure_ip_assignments_for_subscription(" not in handler
    ipv4_helper = helpers.split("def ensure_ipv4_assignment_for_subscription(", 1)[
        1
    ].split("\ndef ", 1)[0]
    assert "db.flush()" in ipv4_helper
    assert "db.commit()" not in ipv4_helper
