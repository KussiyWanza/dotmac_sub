from __future__ import annotations

import inspect
from pathlib import Path

from app.services import (
    prepaid_draft_reconciliation,
    prepaid_service_renewals,
    subscription_lifecycle,
    web_prepaid_draft_reconciliation,
)
from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_prepaid_draft_reconciliation_has_one_contracted_owner():
    service = service_relationship("financial.prepaid_draft_reconciliation")

    assert service.module == "app.services.prepaid_draft_reconciliation"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.CUT_OVER
    concern = next(
        item
        for item in service.contract.concerns
        if item.name == "stranded prepaid draft invoice reconciliation"
    )
    assert concern.role is OwnerRole.RECONCILER
    assert concern.canonical_writer == service.name
    adoption = next(
        item
        for item in service.contract.concerns
        if item.name == "funded onboarding proforma documentary adoption"
    )
    assert adoption.role is OwnerRole.RECONCILER
    assert adoption.canonical_writer == service.name


def test_funding_change_checks_draft_before_invoice_less_renewal():
    source = inspect.getsource(
        prepaid_service_renewals.apply_due_prepaid_service_after_funding_change
    )

    draft_check = source.index("stage_prepaid_draft_after_funding_change(")
    direct_renewal = source.index("preview_prepaid_service_renewal(")
    assert draft_check < direct_renewal
    assert "draft_invoice_pending" in source


def test_reconciler_has_no_rounding_tolerance_or_raw_money_writes():
    source = inspect.getsource(prepaid_draft_reconciliation)

    assert "tolerance" not in source.lower()
    assert "PaymentAllocation(" not in source
    assert "LedgerEntry(" not in source
    assert "AccountAdjustment(" not in source
    assert "execute_owner_command(" in source
    assert "AccountCreditApplications.apply_invoice_fully(" in source
    assert "result.invoice_remaining" in source
    assert "Invoices.void_pristine_draft_for_owner(" in source
    assert "Invoices.adopt_prepaid_proforma_document_for_owner(" in source
    assert "invoice.is_proforma = False" not in source
    assert "line.subscription_id =" not in source


def test_opening_consumption_and_exception_have_one_writer_owner():
    constructors = {
        "PrepaidOpeningFundingConsumption(": [],
        "PrepaidDraftReconciliationException(": [],
    }
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/models/prepaid_funding.py":
            continue
        source = path.read_text(encoding="utf-8")
        for constructor in constructors:
            if constructor in source:
                constructors[constructor].append(relative)

    expected_owner = ["app/services/prepaid_draft_reconciliation.py"]
    assert constructors == {
        "PrepaidOpeningFundingConsumption(": expected_owner,
        "PrepaidDraftReconciliationException(": expected_owner,
    }


def test_generic_restore_redirects_prepaid_financial_locks_to_reconciliation():
    source = inspect.getsource(subscription_lifecycle._eligibility_reasons)

    assert "EnforcementReason.prepaid" in source
    assert "prepaid_financial_reconciliation_required" in source
    assert "reconcile_prepaid_draft_invoice" not in source


def test_reconciliation_cli_is_dry_run_first():
    with open(
        "scripts/billing/reconcile_prepaid_drafts.py",
        encoding="utf-8",
    ) as handle:
        source = handle.read()

    assert 'parser.add_argument("--apply", action="store_true")' in source
    assert "if args.apply:" in source
    assert "owner_command_session()" in source
    assert "read_session()" in source
    assert 'parser.add_argument("--adopt-proforma", action="store_true")' in source
    assert 'parser.add_argument("--subscription-id", type=_uuid)' in source
    assert "preview_funded_prepaid_proforma_adoption(" in source
    assert "adopt_funded_prepaid_proforma(" in source


def test_admin_invoice_adapter_calls_only_the_authoritative_reconciler():
    source = inspect.getsource(web_prepaid_draft_reconciliation)
    invoice_adapter = (ROOT / "app/services/web_billing_invoices.py").read_text()

    assert "preview_prepaid_draft_reconciliation(" in source
    assert "reconcile_prepaid_draft_invoice(" in source
    assert "settle_prepaid_recovery_invoice" not in source
    assert "prepaid_recovery_billing" not in invoice_adapter
