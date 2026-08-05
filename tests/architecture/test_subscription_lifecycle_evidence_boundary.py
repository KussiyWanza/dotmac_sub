"""Keep contractual lifecycle evidence behind its named participant owner."""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _callers(symbol: str) -> set[str]:
    callers: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == symbol)
                or (isinstance(node.func, ast.Attribute) and node.func.attr == symbol)
            )
            for node in ast.walk(tree)
        ):
            callers.add(path.relative_to(ROOT).as_posix())
    return callers


def test_manifest_names_the_evidence_writer_and_period_resolver() -> None:
    service = service_relationship("access.subscription_lifecycle_evidence")
    assert service.contract is not None
    roles = {concern.name: concern.role for concern in service.contract.concerns}

    assert roles == {
        "immutable subscription lifecycle transition evidence": (
            OwnerRole.AUTHORITATIVE_RECORD
        ),
        "period-scoped subscription lifecycle evidence history": OwnerRole.RESOLVER,
    }
    assert service.contract.transaction.mode is TransactionMode.PARTICIPANT
    assert service.contract.migration.state is AuthorityMigrationState.COMPLETE


def test_only_reviewed_owners_may_call_the_evidence_participant() -> None:
    assert _callers("record_lifecycle_evidence") == {
        "app/services/account_lifecycle.py",
        "app/services/subscription_lifecycle_evidence.py",
    }
    assert _callers("record_current_state_baseline") == {
        "app/services/catalog/subscriptions.py",
        "app/services/web_system_restore_tool.py",
    }
    assert _callers("SubscriptionLifecycleEvent") == {
        "app/services/subscription_lifecycle_evidence.py"
    }


def test_transport_and_generic_read_surface_cannot_write_history() -> None:
    handler = (ROOT / "app/services/events/handlers/lifecycle.py").read_text(
        encoding="utf-8"
    )
    generic = (ROOT / "app/services/lifecycle.py").read_text(encoding="utf-8")
    schemas = (ROOT / "app/schemas/lifecycle.py").read_text(encoding="utf-8")

    assert "SubscriptionLifecycleEvent(" not in handler
    assert "record_lifecycle_evidence" not in handler
    assert "def create(" not in generic
    assert "SubscriptionLifecycleEventCreate" not in schemas
    assert "SubscriptionLifecycleEventUpdate" not in schemas
