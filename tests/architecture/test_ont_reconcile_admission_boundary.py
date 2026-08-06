"""Architecture guards for positive automatic-reconcile authority."""

from __future__ import annotations

from pathlib import Path

from app.services.sot_manifest import OwnerRole, TransactionMode
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/network/ont_reconcile_eligibility.py"
SWEEPER = ROOT / "app/services/network/reconcile/sweeper.py"


def _function_body(source: str, name: str) -> str:
    start = source.index(f"def {name}(")
    tail = source[start:]
    end = tail.find("\ndef ", 1)
    return tail if end == -1 else tail[:end]


def test_registry_names_the_positive_admission_owner_and_input():
    service = service_relationship("network.ont_reconcile_eligibility")
    assert service is not None and service.contract is not None
    contract = service.contract
    concern = next(
        item
        for item in contract.concerns
        if item.name == "reviewed automatic reconciliation cohort admission"
    )

    assert concern.role is OwnerRole.COMMAND_WRITER
    assert concern.canonical_writer == service.name
    assert concern.input_names == ("reviewed cohort admission",)
    assert contract.transaction.mode is TransactionMode.OWNER_MANAGED


def test_only_the_registered_owner_constructs_or_transitions_admissions():
    prohibited = (
        "OntReconcileAdmission(",
        ".status = OntReconcileAdmissionStatus.",
    )
    allowed = {
        OWNER.resolve(),
        (ROOT / "app/models/network.py").resolve(),
    }
    offenders: list[str] = []
    for base in (ROOT / "app", ROOT / "scripts"):
        for path in base.rglob("*.py"):
            if path.resolve() in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            if any(token in source for token in prohibited):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_executor_catalogs_admitted_rows_and_rechecks_before_contact():
    source = SWEEPER.read_text(encoding="utf-8")
    run_body = _function_body(source, "run_sweep_once")
    one_body = _function_body(source, "_sweep_one")

    assert "admitted_sweep_candidates(" in run_body
    assert "for candidate in sweep_candidates(" not in run_body
    assert "eligibility_under_lock(" in one_body
    assert one_body.index("eligibility_under_lock(") < one_body.index("is_pingable(")


def test_owner_commands_share_the_ont_first_lock_order():
    source = OWNER.read_text(encoding="utf-8")
    for name, child_lock in (
        ("_admit", "_active_admission("),
        ("_revoke_admission", "with_for_update"),
    ):
        body = _function_body(source, name)
        assert "_lock_ont(" in body
        assert body.index("_lock_ont(") < body.index(child_lock)
