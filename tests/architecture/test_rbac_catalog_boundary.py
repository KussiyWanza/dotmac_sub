"""Keep RBAC catalog writes behind one contracted command owner."""

from __future__ import annotations

import ast
import runpy
from pathlib import Path

from app.models.rbac import Permission, Role

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = PROJECT_ROOT / "app" / "services" / "rbac_catalog.py"
LEGACY = PROJECT_ROOT / "app" / "services" / "rbac.py"
API_ADAPTER = PROJECT_ROOT / "app" / "api" / "rbac.py"
ADMIN_ADAPTER = PROJECT_ROOT / "app" / "web" / "admin" / "system.py"
ROLE_FORM = PROJECT_ROOT / "app" / "services" / "web_system_role_forms.py"
SEED = PROJECT_ROOT / "scripts" / "seed" / "seed_rbac.py"
TEST_SEED = PROJECT_ROOT / "scripts" / "seed" / "seed_test_fixtures.py"
APPLICATION_ROOTS = (PROJECT_ROOT / "app", PROJECT_ROOT / "scripts")
MIGRATION = (
    PROJECT_ROOT / "alembic" / "versions" / "385_rbac_catalog_normalized_identity.py"
)


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def _function_calls(path: Path, names: set[str]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in names:
            continue
        for statement in node.body:
            for child in ast.walk(statement):
                if isinstance(child, ast.Call) and isinstance(
                    child.func, ast.Attribute
                ):
                    calls.add(child.func.attr)
    return calls


def _kernel_role_identity_writes(source: str, *, filename: str) -> set[str]:
    """Find direct writes to the two derived Role identity columns.

    A module that imports the RBAC `Role` model may not construct either field
    or assign either attribute outside the canonical owner. Limiting attribute
    detection to Role-importing modules avoids treating every unrelated
    `tenant_id`/`slug` assignment in this large application as an RBAC write.
    """

    tree = ast.parse(source, filename=filename)
    imports_role = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.models.rbac"
        and any(alias.name == "Role" for alias in node.names)
        for node in ast.walk(tree)
    )
    if not imports_role:
        return set()

    writes: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "Role":
                writes.update(
                    keyword.arg
                    for keyword in node.keywords
                    if keyword.arg in {"slug", "tenant_id"}
                )
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets.extend(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets.append(node.target)
        for target in targets:
            for child in ast.walk(target):
                if isinstance(child, ast.Attribute) and child.attr in {
                    "slug",
                    "tenant_id",
                }:
                    writes.add(child.attr)
    return writes


def test_catalog_owner_uses_one_verified_transaction_boundary() -> None:
    source = OWNER.read_text(encoding="utf-8")
    calls = _calls(OWNER)

    assert "execute_owner_command" in calls
    assert ".commit(" not in source
    assert ".rollback(" not in source
    assert {"Role", "Permission", "RolePermission"} <= calls


def test_legacy_forms_and_seed_no_longer_construct_catalog_rows() -> None:
    forbidden = {"Role", "Permission", "RolePermission"}
    assert not LEGACY.exists()
    for path in (ROLE_FORM, SEED, TEST_SEED):
        assert not forbidden & _calls(path), path

    seed_source = SEED.read_text(encoding="utf-8")
    assert "rbac_catalog.ensure_role" in seed_source
    assert "rbac_catalog.ensure_permission" in seed_source
    assert "rbac_catalog.replace_seeded_role_permissions" in seed_source


def test_kernel_role_identity_projection_has_one_application_writer() -> None:
    writers: dict[Path, set[str]] = {}
    for root in APPLICATION_ROOTS:
        for path in root.rglob("*.py"):
            found = _kernel_role_identity_writes(
                path.read_text(encoding="utf-8"), filename=str(path)
            )
            if found:
                writers[path.relative_to(PROJECT_ROOT)] = found

    assert writers == {
        Path("app/services/rbac_catalog.py"): {"slug", "tenant_id"},
    }


def test_kernel_role_identity_writer_guard_is_sensitive() -> None:
    source = """
from app.models.rbac import Role

def parallel_writer(role: Role) -> None:
    role.slug = "shadow"
    role.tenant_id = None

def parallel_constructor() -> Role:
    return Role(name="shadow", slug="shadow")
"""
    assert _kernel_role_identity_writes(source, filename="canary.py") == {
        "slug",
        "tenant_id",
    }


def test_api_and_admin_catalog_adapters_delegate_without_persistence() -> None:
    api_calls = _function_calls(
        API_ADAPTER,
        {
            "create_role",
            "update_role",
            "delete_role",
            "create_permission",
            "update_permission",
            "delete_permission",
            "create_role_permission",
            "update_role_permission",
            "delete_role_permission",
        },
    )
    admin_calls = _function_calls(
        ADMIN_ADAPTER,
        {
            "role_create",
            "role_update",
            "role_delete",
            "permission_create",
            "permission_update",
            "permission_delete",
        },
    )

    assert {
        "create_role",
        "update_role",
        "deactivate_role",
        "create_permission",
        "update_permission",
        "deactivate_permission",
        "grant_role_permission",
        "update_role_permission",
        "revoke_role_permission",
    } <= api_calls
    assert {
        "create_role_with_permissions",
        "update_role_with_permissions",
        "deactivate_role",
        "create_permission",
        "update_permission",
        "deactivate_permission",
    } <= admin_calls
    assert not {"add", "delete", "flush", "commit", "rollback"} & (
        api_calls | admin_calls
    )


def test_model_and_migration_enforce_normalized_catalog_identity() -> None:
    assert "uq_roles_normalized_name" in {
        index.name for index in Role.__table__.indexes
    }
    assert "uq_permissions_normalized_key" in {
        index.name for index in Permission.__table__.indexes
    }

    source = MIGRATION.read_text(encoding="utf-8")
    assert 'down_revision = "384_collection_account_payment_details"' in source
    assert "HAVING count(*) > 1" in source
    assert "lower(btrim(name))" in source
    assert "lower(btrim(key))" in source
    assert '"uq_roles_normalized_name"' in source
    assert '"uq_permissions_normalized_key"' in source


def test_seed_never_grants_admin_only_permissions_to_non_admin_roles() -> None:
    seed = runpy.run_path(str(SEED), run_name="_rbac_catalog_seed_contract")
    admin_only = set(seed["ADMIN_ONLY_PERMISSION_KEYS"])
    role_permissions = seed["ROLE_PERMISSIONS"]

    conflicts = {
        role: sorted(set(permission_keys) & admin_only)
        for role, permission_keys in role_permissions.items()
        if role != "admin" and set(permission_keys) & admin_only
    }
    assert conflicts == {}
