"""Static ownership guards for the `ActionReadiness` shared contract.

`ActionReadiness` is a stateless, transport-neutral readiness/blocker verdict
for a gated action — explicitly not a workflow engine and never a second
authority over a domain decision. These checks make that claim inspectable:

* the three shared modules read no live state (no SQLAlchemy, no ORM models,
  no `Session`) and persist nothing;
* only a genuine domain owner may construct a blocker or readiness verdict —
  the shared layer and its consumers only read one;
* a blocker's `owner` must resolve to a real, decision-making SOT service —
  never the shared contract itself, and never another pure-vocabulary layer;
* the shared layer cannot itself dispatch or execute a repair.

Every rejection guard is paired with a sensitivity proof, following the
technique in `tests/architecture/test_receivable_projection_boundary.py`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

CONTRACT = ROOT / "app/services/action_readiness.py"
WEB_CONTEXT = ROOT / "app/services/web_action_readiness.py"
SCHEMA = ROOT / "app/schemas/action_readiness.py"
TEMPLATE = ROOT / "templates/components/actions/action_readiness.html"
ACTION_FORMS = ROOT / "app/services/action_forms.py"

SHARED_MODULES = (CONTRACT, WEB_CONTEXT, SCHEMA)

#: A real file that DOES import SQLAlchemy/app.models — sensitivity control.
KNOWN_VIOLATOR = ROOT / "app/services/web_billing_payment_proofs.py"

CONSTRUCTOR_NAMES = ("ActionableBlocker", "NextAction", "ActionReadiness")

FORBIDDEN_IMPORTS = ("sqlalchemy", "app.models", "app.db")

FORBIDDEN_PERSISTENCE_CALLS = (
    "add",
    "add_all",
    "commit",
    "flush",
    "rollback",
    "delete",
)
FORBIDDEN_PERSISTENCE_IDENTIFIERS = (
    "opened_at",
    "resolved_at",
    "execute_owner_command",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _code_identifiers(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
            names.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
            names.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _called_attributes(path: Path) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _constructed(path: Path, name: str) -> int:
    return sum(
        1
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    )


def _has_session_identifier(path: Path) -> bool:
    return "Session" in _code_identifiers(path)


# ── The shared layer reads no live state and persists nothing ──────────────


def test_shared_modules_import_no_orm_or_database_layer() -> None:
    for path in SHARED_MODULES:
        imported = _imported_modules(path)
        for forbidden in FORBIDDEN_IMPORTS:
            hit = any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for module in imported
            )
            assert not hit, (
                f"{path.relative_to(ROOT)} imports {forbidden!r}; the shared "
                "readiness layer must not read live state"
            )
        assert not _has_session_identifier(path), (
            f"{path.relative_to(ROOT)} references `Session`; the shared "
            "readiness layer never touches a database session"
        )


def test_the_import_cleanliness_guard_still_bites() -> None:
    """Sensitivity proof: a real file that DOES import these must trip it."""
    imported = _imported_modules(KNOWN_VIOLATOR)
    assert any(
        module == "sqlalchemy" or module.startswith("sqlalchemy.")
        for module in imported
    ) or any(
        module == "app.models" or module.startswith("app.models.")
        for module in imported
    ), (
        f"{KNOWN_VIOLATOR.relative_to(ROOT)} was expected to import SQLAlchemy "
        "or app.models; if it no longer does, pick another known violator — "
        "the detector, not the target file, is what's under test"
    )


def test_shared_modules_never_dynamically_dispatch_a_repair() -> None:
    """The shared layer cannot itself execute an arbitrary repair runner."""
    forbidden = {"import_module", "eval", "exec"}
    for path in SHARED_MODULES:
        identifiers = _code_identifiers(path)
        hit = identifiers & forbidden
        assert not hit, f"{path.relative_to(ROOT)} references {sorted(hit)}"
        assert "importlib" not in _imported_modules(path)


def test_shared_modules_write_nothing_and_track_no_second_lifecycle() -> None:
    for path in SHARED_MODULES:
        calls = _called_attributes(path)
        overlap = calls & set(FORBIDDEN_PERSISTENCE_CALLS)
        assert not overlap, (
            f"{path.relative_to(ROOT)} calls {sorted(overlap)}; the shared "
            "readiness layer persists nothing"
        )
        identifiers = _code_identifiers(path)
        overlap_ids = identifiers & set(FORBIDDEN_PERSISTENCE_IDENTIFIERS)
        assert not overlap_ids, (
            f"{path.relative_to(ROOT)} references {sorted(overlap_ids)}; that "
            "would be a second, competing blocker-lifecycle authority"
        )


def test_no_model_or_migration_exists_for_action_readiness() -> None:
    models_dir = ROOT / "app/models"
    migrations_dir = ROOT / "alembic/versions"
    for directory in (models_dir, migrations_dir):
        hits = list(directory.glob("*action_readiness*"))
        assert not hits, (
            f"found {hits} under {directory.relative_to(ROOT)}; "
            "ActionReadiness has no schema or migration in this change"
        )


# ── Only a genuine domain owner constructs a blocker or verdict ────────────


def test_web_and_schema_layers_never_construct_a_readiness_verdict() -> None:
    for path in (WEB_CONTEXT, ACTION_FORMS, SCHEMA):
        for name in CONSTRUCTOR_NAMES:
            count = _constructed(path, name)
            assert count == 0, (
                f"{path.relative_to(ROOT)} constructs {name}(...) "
                f"{count} time(s); only a genuine domain owner module may "
                "construct a readiness verdict or blocker"
            )


def test_the_constructor_guard_still_bites() -> None:
    """Sensitivity proof: a real owner module DOES construct these names."""
    owner = ROOT / "app/services/web_billing_payment_proofs.py"
    assert _constructed(owner, "ActionReadiness") > 0
    assert _constructed(owner, "ActionableBlocker") > 0


# ── A blocker's owner must be a real, decision-making SOT service ─────────


def test_blocker_owner_must_be_a_registered_service() -> None:
    from app.services.action_readiness import ActionableBlocker, BlockerEvidence

    with pytest.raises(ValueError, match="registered SOT service"):
        ActionableBlocker(
            code="x",
            owner="totally.unregistered.nonsense",
            customer_message="m",
            staff_detail="d",
            evidence=BlockerEvidence(summary="s"),
        )


def test_blocker_owner_rejects_the_shared_layers_own_pure_vocabulary_name() -> None:
    """A pure-vocabulary owner can't own a blocker, including this module's own.

    Proves the exclusion is "must be a REAL decision-making owner", not just
    "must be registered": `ui.action_readiness_contracts` (this module's own
    future registry name) is itself registered but is NOT_APPLICABLE, and
    must be rejected exactly like `ui.projection_contracts`.
    """
    from app.services.action_readiness import ActionableBlocker, BlockerEvidence

    with pytest.raises(ValueError, match="pure-vocabulary"):
        ActionableBlocker(
            code="x",
            owner="ui.action_readiness_contracts",
            customer_message="m",
            staff_detail="d",
            evidence=BlockerEvidence(summary="s"),
        )


def test_blocker_owner_accepts_a_real_decision_making_owner() -> None:
    from app.services.action_readiness import ActionableBlocker, BlockerEvidence

    blocker = ActionableBlocker(
        code="x",
        owner="financial.payment_proofs",
        customer_message="m",
        staff_detail="d",
        evidence=BlockerEvidence(summary="s"),
    )
    assert blocker.owner == "financial.payment_proofs"


# ── Template accessibility / forbidden-pattern markers ──────────────────


def test_template_carries_required_accessibility_markers() -> None:
    source = _source(TEMPLATE)
    for required in (
        'role="status"',
        "aria-labelledby",
        "app_datetime",
        "status_presentation_badge",
    ):
        assert required in source, f"template missing required marker {required!r}"


def test_template_avoids_forbidden_patterns() -> None:
    source = _source(TEMPLATE)
    for forbidden in (
        "bg-emerald",
        "bg-rose",
        "bg-red",
        "bg-green",
        "window.confirm",
        "onclick=",
        "| safe",
    ):
        assert forbidden not in source, (
            f"template contains forbidden pattern {forbidden!r}"
        )

    # Jinja isn't Python; scan the raw text for `== '...'` state/code
    # comparisons instead of AST-parsing the template.
    import re

    state_or_code_equality = re.findall(
        r"==\s*['\"](?:ready|blocked|waiting|needs_verification|failed|complete)['\"]",
        source,
    )
    assert not state_or_code_equality, (
        "template branches on a literal state value; that decision must "
        "already be resolved by the Python context builder"
    )


# ── Repair runner never reaches the transport schema ────────────────────


def test_repair_action_read_schema_never_exposes_runner() -> None:
    from app.schemas.action_readiness import RepairActionRead

    assert "runner" not in RepairActionRead.model_fields
    schema = RepairActionRead.model_json_schema()
    assert "runner" not in schema.get("properties", {})


# ── Registry parity ──────────────────────────────────────────────────────


def test_the_new_service_is_registered_with_a_complete_contract() -> None:
    from app.services.sot_manifest import contract_validation_errors
    from app.services.sot_registry import registry

    services = registry.all_services()
    names = {service.name for service in services}
    assert "ui.action_readiness_contracts" in names

    owner = next(
        service
        for service in services
        if service.name == "ui.action_readiness_contracts"
    )
    assert owner.module == "app.services.action_readiness"
    assert owner.contract is not None
    assert contract_validation_errors(owner, service_names=names) == ()
