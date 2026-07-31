"""Governance: high-impact editors carry contracts or conscious waivers.

HIGH_IMPACT_EDITORS is the checked-in configuration listing every editor whose
submit moves money, changes service, or is irreversible. Each entry must be
"contracted" (with a registered FormContract), "waived" (with a reason), or
"pending" (reported, tolerated — adoption owed on touch per the design review
checklist). Contracts register through form_contracts.register() next to their
command owners.
"""

from __future__ import annotations

# Importing the flow module executes its register() call — mirrors app import.
import app.services.customer_portal_flow_changes  # noqa: F401
import app.services.web_prepaid_draft_reconciliation  # noqa: F401
from app.services.form_contracts import (
    FORM_CONTRACT_REGISTRY,
    HIGH_IMPACT_EDITORS,
)

_VALID_STATUSES = {"contracted", "pending", "waived"}


def test_every_entry_is_well_formed():
    seen: set[str] = set()
    for entry in HIGH_IMPACT_EDITORS:
        assert entry.key not in seen, f"duplicate governance entry: {entry.key}"
        seen.add(entry.key)
        assert entry.status in _VALID_STATUSES, (
            f"{entry.key}: unknown status {entry.status!r}"
        )
        assert entry.surface, f"{entry.key}: surface required"
        if entry.status == "contracted":
            assert entry.contract_key, f"{entry.key}: contracted without contract_key"
            assert entry.contract_key in FORM_CONTRACT_REGISTRY, (
                f"{entry.key}: contract {entry.contract_key!r} is not registered "
                "(declare it with form_contracts.register next to the command owner)"
            )
        if entry.status == "waived":
            assert entry.reason, f"{entry.key}: waiver requires a reason"


def test_registered_contracts_name_their_command_owner():
    for key, contract in FORM_CONTRACT_REGISTRY.items():
        assert contract.command_owner, f"{key}: command_owner required"
        assert contract.key == key


def test_pilot_is_contracted():
    assert "customer.plan_change" in FORM_CONTRACT_REGISTRY
    contracted = {
        e.contract_key for e in HIGH_IMPACT_EDITORS if e.status == "contracted"
    }
    assert "customer.plan_change" in contracted
