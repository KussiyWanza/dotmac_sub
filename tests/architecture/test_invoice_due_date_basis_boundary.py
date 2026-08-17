"""Issued due dates retain provenance and cannot bypass their owner."""

from __future__ import annotations

from pathlib import Path

from app.models.billing import InvoiceDueDateBasis
from app.services.billing.invoices import InvoiceIssuanceInput

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app" / "services" / "billing" / "invoices.py"
COLLECTIBILITY = ROOT / "app" / "services" / "invoice_collectibility.py"
DUNNING = ROOT / "app" / "services" / "collections" / "_core.py"
DESIGN = ROOT / "docs" / "designs" / "INVOICE_DUE_DATE_BASIS.md"


def test_due_date_basis_has_an_honest_unknown_state() -> None:
    assert {item.value for item in InvoiceDueDateBasis} == {
        "contract_terms",
        "prepaid_service_period",
        "provider_observation",
        "approved_manual_override",
        "unknown_unverified",
    }
    assert set(InvoiceIssuanceInput.__annotations__) == {
        "issued_at",
        "due_at",
        "due_date_basis",
        "due_date_basis_ref",
        "due_date_policy_version",
        "reason",
    }


def test_only_invoice_owner_assigns_invoice_due_at() -> None:
    offenders = {}
    for path in (ROOT / "app" / "services").rglob("*.py"):
        if path == OWNER:
            continue
        source = path.read_text(encoding="utf-8")
        if "invoice.due_at =" in source:
            offenders[str(path.relative_to(ROOT))] = True
    assert offenders == {}


def test_unverified_due_dates_are_quarantined_from_collections() -> None:
    owner = OWNER.read_text(encoding="utf-8")
    collectibility = COLLECTIBILITY.read_text(encoding="utf-8")
    dunning = DUNNING.read_text(encoding="utf-8")
    design = DESIGN.read_text(encoding="utf-8")

    assert "financial.invoice.unverified_due_date" in owner
    assert "InvoiceDueDateBasis.unknown_unverified" in collectibility
    assert "collection_due_date_eligible_filter()" in dunning
    assert "unknown_unverified" in design
    assert "cannot" in design


def test_migrations_form_one_due_date_then_active_anchor_chain() -> None:
    due_date = (
        ROOT / "alembic" / "versions" / "538_invoice_due_date_basis.py"
    ).read_text(encoding="utf-8")
    active_anchor = (
        ROOT / "alembic" / "versions" / "539_active_sub_billing_anchor.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision = "537_team_inbox_plain_bodies"' in due_date
    assert 'down_revision = "538_invoice_due_date_basis"' in active_anchor
    assert "NOT VALID" in active_anchor
