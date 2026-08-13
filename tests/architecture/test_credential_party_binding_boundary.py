"""One runtime writer owns every field in the credential Party projection."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = "app/services/credential_party_binding.py"
STAFF_ADAPTER = PROJECT_ROOT / (
    "scripts/migration/execute_staff_party_credential_adoption.py"
)
STAFF_OWNER = PROJECT_ROOT / "app/services/staff_party_adoption.py"
FIELDS = {
    "party_id",
    "authentication_binding_id",
    "tenant_id",
    "party_bound_at",
    "party_binding_source",
    "party_binding_reason",
}


def _credential_projection_writers(app_root: Path) -> dict[str, set[str]]:
    writers: dict[str, set[str]] = {}
    for path in app_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "credential"
                    and target.attr in FIELDS
                ):
                    relative = str(path.relative_to(app_root.parent))
                    writers.setdefault(relative, set()).add(target.attr)
    return writers


def test_credential_projection_has_one_complete_runtime_writer() -> None:
    writers = _credential_projection_writers(PROJECT_ROOT / "app")

    assert writers == {OWNER: FIELDS}


def test_writer_detector_sensitivity(tmp_path: Path) -> None:
    app = tmp_path / "app"
    app.mkdir()
    violation = app / "bad_adapter.py"
    violation.write_text("credential.party_id = invented_party_id\n", encoding="utf-8")

    writers = _credential_projection_writers(app)

    assert writers == {"app/bad_adapter.py": {"party_id"}}


def _staff_adapter_boundary_violations(source: str) -> set[str]:
    tree = ast.parse(source)
    violations: set[str] = set()
    forbidden_calls = {
        "add",
        "begin",
        "begin_nested",
        "commit",
        "execute",
        "flush",
        "query",
        "rollback",
    }
    required_calls = {
        "BindExistingStaffPartyCommand",
        "CredentialPartyBinding",
        "bind_credential_party",
        "bind_existing_staff_party",
    }
    calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.add(node.func.attr)
                if node.func.attr in forbidden_calls:
                    violations.add(f"transaction_or_persistence:{node.func.attr}")
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            raw_targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if any(
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id
                in {"credential", "party", "staff", "system_user", "user"}
                for target in raw_targets
            ):
                violations.add("direct_attribute_mutation")
    for missing in required_calls - calls:
        violations.add(f"missing_typed_owner_delegation:{missing}")
    return violations


def test_staff_adoption_adapter_is_typed_transaction_neutral_delegation() -> None:
    source = STAFF_ADAPTER.read_text(encoding="utf-8")

    assert _staff_adapter_boundary_violations(source) == set()


def test_staff_adoption_adapter_detector_sensitivity() -> None:
    violation = """
credential.party_id = party_id
db.commit()
"""

    assert _staff_adapter_boundary_violations(violation) == {
        "direct_attribute_mutation",
        "transaction_or_persistence:commit",
        "missing_typed_owner_delegation:BindExistingStaffPartyCommand",
        "missing_typed_owner_delegation:CredentialPartyBinding",
        "missing_typed_owner_delegation:bind_credential_party",
        "missing_typed_owner_delegation:bind_existing_staff_party",
    }


def test_staff_adoption_owner_is_separate_from_completed_staff_provisioning() -> None:
    source = STAFF_OWNER.read_text(encoding="utf-8")

    assert 'OWNER = "party.staff_principal_adoption"' in source
    assert "app.services.staff_party_adoption" in (
        PROJECT_ROOT / "app/services/sot_registry/domains/party_identity.py"
    ).read_text(encoding="utf-8")
    assert "StaffPartyAdoption" not in (
        PROJECT_ROOT / "app/services/staff_provisioning.py"
    ).read_text(encoding="utf-8")
