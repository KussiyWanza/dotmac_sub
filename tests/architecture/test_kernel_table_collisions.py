"""Keep the kernel/Product competing-model inventory executable.

The installed kernel is pinned, but Sub's model set keeps moving.  A new table
on either side must therefore fail CI until the adoption ledger names its owner
and disposition.  This guard scans declarations only; it never combines the
two SQLAlchemy metadata objects or touches a database.  The broader migration
lineage inventory also includes ``tenants`` and ``tenant_domains``; those are
intentionally hosted by Sub through the kernel models, so they are not competing
model declarations and are governed separately by ADR-0009.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dotmac_kernel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KERNEL_ROOT = Path(dotmac_kernel.__file__).resolve().parent
SUB_MODELS = PROJECT_ROOT / "app" / "models"

EXPECTED_COMPETING_MODEL_TABLES = frozenset(
    {
        "audit_events",
        "communication_suppressions",
        "domain_setting_history",
        "domain_settings",
        "parties",
        "party_roles",
        "roles",
        "user_credentials",
    }
)


def _declared_tables(root: Path) -> frozenset[str]:
    tables: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__tablename__"
                for target in node.targets
            ):
                value = node.value
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "__tablename__"
            ):
                value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                tables.add(value.value)
    return frozenset(tables)


def test_kernel_sub_competing_models_match_the_reviewed_a40_inventory() -> None:
    collisions = _declared_tables(KERNEL_ROOT) & _declared_tables(SUB_MODELS)
    assert collisions == EXPECTED_COMPETING_MODEL_TABLES, (
        "kernel/Sub competing-model inventory changed; classify every added or "
        "removed name in docs/PLATFORM_ADOPTION_LEDGER.md and the lineage "
        "disposition inventory before changing this reviewed set.\n"
        f"added: {sorted(collisions - EXPECTED_COMPETING_MODEL_TABLES)}\n"
        f"removed: {sorted(EXPECTED_COMPETING_MODEL_TABLES - collisions)}"
    )


def test_collision_detector_is_sensitive_to_a_new_shared_table(tmp_path: Path) -> None:
    kernel = tmp_path / "kernel"
    product = tmp_path / "product"
    kernel.mkdir()
    product.mkdir()
    (kernel / "model.py").write_text(
        'class KernelModel:\n    __tablename__ = "new_collision"\n',
        encoding="utf-8",
    )
    (product / "model.py").write_text(
        'class ProductModel:\n    __tablename__ = "new_collision"\n',
        encoding="utf-8",
    )

    assert _declared_tables(kernel) & _declared_tables(product) == {"new_collision"}
