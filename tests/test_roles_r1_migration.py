from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/528_roles_kernel_r1_additive.py"
    )
    spec = importlib.util.spec_from_file_location("migration_528_roles_r1", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_is_linear_expand_only_and_kernel_shaped() -> None:
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert migration.revision == "528_roles_kernel_r1_additive"
    assert migration.down_revision == "527_credential_party_binding_additive"
    assert "sa.String(length=120)" in source
    assert "sa.String(length=63)" in source
    assert '"uq_roles_tenant_slug"' in source
    assert '"uq_roles_tenant_id_id"' in source
    assert '"ck_roles_kernel_identity_projection"' in source
    assert '"ix_roles_tenant_id"' in source
    assert 'ondelete="CASCADE"' in source
    assert "ENABLE ROW LEVEL SECURITY" not in source
    assert "FORCE ROW LEVEL SECURITY" not in source
    assert "UPDATE roles" not in source


def test_semantic_fk_adoption_accepts_kernel_cascade_shape() -> None:
    migration = _load_migration()
    generated_name = "roles_tenant_id_fkey"

    actual = migration._matching_fk_name(
        [
            {
                "name": generated_name,
                "constrained_columns": ["tenant_id"],
                "referred_table": "tenants",
                "referred_columns": ["id"],
                "options": {"ondelete": "CASCADE"},
            }
        ],
        name="fk_roles_tenant",
        columns=["tenant_id"],
        referred_table="tenants",
        ondelete="CASCADE",
    )

    assert actual == generated_name
