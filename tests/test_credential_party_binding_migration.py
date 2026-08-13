from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/525_credential_party_binding_additive.py"
    )
    spec = importlib.util.spec_from_file_location("migration_525", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_is_linear_additive_and_adoption_aware() -> None:
    migration = _load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert migration.revision == "525_credential_party_binding_additive"
    assert migration.down_revision == "524_audit_events_kernel_r1"
    assert "inspect(bind)" in source
    assert "authentication_bindings" in source
    assert "binding_key" in source
    assert "ck_user_credentials_party_binding_projection" in source
    assert "uq_user_credentials_tenant_party_auth_binding" in source
    assert "trg_authentication_binding_identity_immutable" in source
    assert "ENABLE ROW LEVEL SECURITY" not in source
    assert "FORCE ROW LEVEL SECURITY" not in source
    assert "UPDATE user_credentials" not in source


def test_seed_matches_declared_runtime_mechanisms_and_is_deterministic() -> None:
    migration = _load_migration()
    from app.services.authentication_mechanism_registry import (
        declared_authentication_mechanisms,
    )

    assert {row[2] for row in migration._SEED} == declared_authentication_mechanisms()
    assert {row[1] for row in migration._SEED} == {"local.default", "radius.default"}
    assert len({row[0] for row in migration._SEED}) == len(migration._SEED)
    assert "sso" not in {row[2] for row in migration._SEED}


def test_semantic_fk_adoption_returns_the_database_name_for_downgrade() -> None:
    migration = _load_migration()
    generated_name = "user_credentials_authentication_binding_id_fkey"

    actual = migration._matching_fk_name(
        [
            {
                "name": generated_name,
                "constrained_columns": ["authentication_binding_id"],
                "referred_table": "authentication_bindings",
                "referred_columns": ["id"],
                "options": {"ondelete": "RESTRICT"},
            }
        ],
        name="fk_user_credentials_auth_binding",
        columns=["authentication_binding_id"],
        referred_table="authentication_bindings",
        ondelete="RESTRICT",
    )

    assert actual == generated_name
