"""Pin effective-dated customer SLA policy to one typed owner boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from app.models.catalog import PLAN_FAMILY_VALUES
from app.services.customer_service_level import RecordPolicyVersionCommand
from app.services.service_impact_contracts import SlaPlanFamily
from app.services.sot_manifest import contract_validation_errors
from app.services.sot_relationships import all_services, service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = "app/services/customer_service_level.py"


def test_customer_service_level_has_one_complete_owner_contract() -> None:
    service_names = {item.name for item in all_services()}
    owner = service_relationship("customer.service_level")

    assert owner.contract is not None
    assert not contract_validation_errors(owner, service_names=service_names)
    assert {concern.canonical_writer for concern in owner.contract.concerns} == {
        "customer.service_level",
        None,
    }


def test_sla_family_scope_is_a_typed_closed_protocol() -> None:
    assert RecordPolicyVersionCommand.__annotations__["plan_family"] == (
        "SlaPlanFamily | None"
    )
    assert tuple(family.value for family in SlaPlanFamily) == PLAN_FAMILY_VALUES


def test_only_customer_service_level_constructs_policy_records() -> None:
    constructors: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SlaPolicyVersionRecord"
            for node in ast.walk(tree)
        ):
            constructors.append(str(path.relative_to(ROOT)))

    assert constructors == [OWNER]


def test_precedence_and_sla_family_subset_are_documented_together() -> None:
    spine = (ROOT / "docs/designs/OUTAGE_SLA_SPINE.md").read_text(encoding="utf-8")
    plan_family = (ROOT / "docs/PLAN_FAMILY_ARCHITECTURE.md").read_text(
        encoding="utf-8"
    )
    normalized_spine = " ".join(spine.split())
    normalized_plan_family = " ".join(plan_family.replace("**", "").split())

    for source in (
        "subscription-specific contract",
        "customer/account contract",
        "subscribed offer version",
        "SLA-enabled commercial plan-family default",
        "internal measurement policy",
    ):
        assert source in normalized_spine
    assert "family vocabulary is closed" in normalized_plan_family
    assert "commercial decision" in normalized_plan_family
