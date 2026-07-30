"""Architecture guardrails for financial write ownership.

Routes and tasks are already guarded, but service-layer adapters can otherwise
write money or lifecycle state directly.  These allowlists capture existing
debt; they are not authorization for new writers and should only shrink.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

from tests.architecture.source_index import (
    call_lines,
    identifier_names,
    python_files,
    python_nodes,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "app"

APPROVED_LEDGER_WRITERS = {
    Path("app/services/billing/ledger.py"),
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
    # Existing debt outside the canonical billing package.
    Path("app/services/cutover_balance_audit.py"),
}

APPROVED_ACCOUNT_ADJUSTMENT_WRITERS = {Path("app/services/billing/adjustments.py")}

APPROVED_ALLOCATION_WRITERS = {
    Path("app/services/billing/payments.py"),
}

APPROVED_PAYMENT_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_PAYMENT_SETTLEMENT_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_PAYMENT_ALLOCATION_EXCEPTION_WRITERS = {
    Path("app/services/billing/payments.py")
}

APPROVED_BILLING_ACCOUNT_LEDGER_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_BILLING_ACCOUNT_BALANCE_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}


APPROVED_CREDIT_APPLICATION_WRITERS = {Path("app/services/billing/credit_notes.py")}

APPROVED_CREDIT_NOTE_WRITERS = {Path("app/services/billing/credit_notes.py")}

APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS = {
    Path("app/services/billing/_common.py"),
    Path("app/services/billing/credit_notes.py"),
}

APPROVED_PAYMENT_REFUND_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_PAYMENT_REVERSAL_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_CONSOLIDATED_RETURN_EVIDENCE_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_CONSOLIDATED_SETTLEMENT_RECONCILIATION_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_CONSOLIDATED_CREDIT_RECONCILIATION_WRITERS = {
    Path("app/services/billing/consolidated_payments.py")
}

APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS = {
    Path("app/services/financial_import_batch_reversals.py")
}

APPROVED_PAYMENT_LIFECYCLE_WRITERS = {
    Path("app/services/billing/payments.py"),
    Path("app/services/billing/consolidated_payments.py"),
}

APPROVED_INVOICE_CLOSURE_WRITERS = {Path("app/services/billing/invoices.py")}

APPROVED_INVOICE_DOCUMENT_WRITERS = {Path("app/services/billing/invoices.py")}

APPROVED_FINANCIAL_ACCESS_CONSEQUENCE_WRITERS = {
    Path("app/services/collections/_core.py")
}

APPROVED_PAYMENT_ARRANGEMENT_WRITERS = {Path("app/services/payment_arrangements.py")}

APPROVED_INVOICE_LIFECYCLE_WRITERS = {
    Path("app/services/billing/_common.py"),
    Path("app/services/billing/invoices.py"),
}


def _python_files() -> list[Path]:
    return list(python_files(APP_DIR))


def _constructor_lines(path: Path, class_name: str) -> list[int]:
    return list(call_lines(path).get(class_name, ()))


@cache
def _enum_status_write_lines(path: Path, enum_name: str) -> list[int]:
    lines: list[int] = []
    for node in python_nodes(path):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == enum_name
        ):
            continue
        if any(
            isinstance(target, ast.Attribute) and target.attr == "status"
            for target in targets
        ):
            lines.append(node.lineno)
    return lines


def _invoice_status_write_lines(path: Path) -> list[int]:
    return _enum_status_write_lines(path, "InvoiceStatus")


@cache
def _billing_account_balance_write_lines(path: Path) -> list[int]:
    """Find balance assignments in modules that depend on BillingAccount."""
    if "BillingAccount" not in identifier_names(path):
        return []
    lines: list[int] = []
    for node in python_nodes(path):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        else:
            continue
        if any(
            isinstance(target, ast.Attribute) and target.attr == "balance"
            for target in targets
        ):
            lines.append(node.lineno)
    return lines


@cache
def _invoice_terminal_status_write_lines(path: Path) -> list[int]:
    lines: list[int] = []
    for node in python_nodes(path):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "InvoiceStatus"
            and value.attr in {"void", "written_off"}
        ):
            continue
        if any(
            isinstance(target, ast.Attribute) and target.attr == "status"
            for target in targets
        ):
            lines.append(node.lineno)
    return lines


def _violations(
    finder,
    approved: set[Path],
) -> list[str]:
    found: list[str] = []
    for path in _python_files():
        rel = path.relative_to(PROJECT_ROOT)
        if rel in approved or rel.parts[:2] == ("app", "models"):
            continue
        for line in finder(path):
            found.append(f"{rel}:{line}")
    return found


def test_only_approved_modules_post_to_the_ledger() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "LedgerEntry"),
        APPROVED_LEDGER_WRITERS,
    )
    assert not violations, (
        "LedgerEntry constructed outside an approved money owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_account_adjustment_owner_constructs_adjustment_evidence() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "AccountAdjustment"),
        APPROVED_ACCOUNT_ADJUSTMENT_WRITERS,
    )
    assert not violations, (
        "AccountAdjustment constructed outside its owner:\n  " + "\n  ".join(violations)
    )


def test_only_the_payment_owner_allocates_payments() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "PaymentAllocation"),
        APPROVED_ALLOCATION_WRITERS,
    )
    assert not violations, (
        "PaymentAllocation constructed outside an approved payment owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_payment_owner_creates_payment_documents_and_settlements() -> None:
    payments = _violations(
        lambda path: _constructor_lines(path, "Payment"),
        APPROVED_PAYMENT_WRITERS,
    )
    settlements = _violations(
        lambda path: _constructor_lines(path, "PaymentSettlement"),
        APPROVED_PAYMENT_SETTLEMENT_WRITERS,
    )
    assert not payments, "Payment constructed outside its owner:\n  " + "\n  ".join(
        payments
    )
    assert not settlements, (
        "PaymentSettlement constructed outside its owner:\n  "
        + "\n  ".join(settlements)
    )


def test_only_payment_owner_constructs_allocation_reconciliation_exceptions() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "PaymentAllocationReconciliationException"
        ),
        APPROVED_PAYMENT_ALLOCATION_EXCEPTION_WRITERS,
    )
    assert not violations, (
        "PaymentAllocationReconciliationException constructed outside its owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_consolidated_payment_owner_writes_billing_account_ledger() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "BillingAccountLedgerEntry"),
        APPROVED_BILLING_ACCOUNT_LEDGER_WRITERS,
    )
    assert not violations, (
        "BillingAccountLedgerEntry constructed outside its owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_consolidated_credit_owner_constructs_allocation_evidence() -> None:
    decisions = _violations(
        lambda path: _constructor_lines(path, "BillingAccountCreditAllocation"),
        APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS,
    )
    items = _violations(
        lambda path: _constructor_lines(path, "BillingAccountCreditAllocationItem"),
        APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS,
    )
    assert not decisions, (
        "BillingAccountCreditAllocation constructed outside its owner:\n  "
        + "\n  ".join(decisions)
    )
    assert not items, (
        "BillingAccountCreditAllocationItem constructed outside its owner:\n  "
        + "\n  ".join(items)
    )


def test_only_consolidated_owner_constructs_reconciliation_evidence() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedPaymentSettlementReconciliationEvidence"
        ),
        APPROVED_CONSOLIDATED_SETTLEMENT_RECONCILIATION_WRITERS,
    )
    assert not violations, (
        "Consolidated settlement reconciliation evidence constructed outside "
        "its owner:\n  " + "\n  ".join(violations)
    )


def test_only_consolidated_owner_constructs_credit_reconciliation_evidence() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedCreditConsumptionReconciliationEvidence"
        ),
        APPROVED_CONSOLIDATED_CREDIT_RECONCILIATION_WRITERS,
    )
    assert not violations, (
        "Consolidated credit-consumption reconciliation evidence constructed "
        "outside its owner:\n  " + "\n  ".join(violations)
    )


def test_only_consolidated_owner_constructs_return_reconciliation_evidence() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedPaymentReturnReconciliationEvidence"
        ),
        APPROVED_CONSOLIDATED_RETURN_EVIDENCE_WRITERS,
    )
    assert not violations, (
        "Consolidated return reconciliation evidence constructed outside "
        "its owner:\n  " + "\n  ".join(violations)
    )


def test_only_consolidated_owner_constructs_return_document_reconstruction() -> None:
    violations = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedPaymentReturnDocumentReconstructionEvidence"
        ),
        APPROVED_CONSOLIDATED_RETURN_EVIDENCE_WRITERS,
    )
    assert not violations, (
        "Consolidated return-document reconstruction evidence constructed "
        "outside its owner:\n  " + "\n  ".join(violations)
    )


def test_only_consolidated_money_owner_writes_billing_account_balance() -> None:
    violations = _violations(
        _billing_account_balance_write_lines,
        APPROVED_BILLING_ACCOUNT_BALANCE_WRITERS,
    )
    assert not violations, (
        "BillingAccount.balance written outside its ledger-backed owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_invoice_owner_constructs_terminal_closure_evidence() -> None:
    closures = _violations(
        lambda path: _constructor_lines(path, "InvoiceClosure"),
        APPROVED_INVOICE_CLOSURE_WRITERS,
    )
    ledger_links = _violations(
        lambda path: _constructor_lines(path, "InvoiceClosureLedgerEvidence"),
        APPROVED_INVOICE_CLOSURE_WRITERS,
    )
    transitions = _violations(
        _invoice_terminal_status_write_lines,
        APPROVED_INVOICE_CLOSURE_WRITERS,
    )
    assert not closures, (
        "InvoiceClosure constructed outside its owner:\n  " + "\n  ".join(closures)
    )
    assert not ledger_links, (
        "Invoice closure ledger evidence constructed outside its owner:\n  "
        + "\n  ".join(ledger_links)
    )
    assert not transitions, (
        "Invoice void/write-off status transitioned outside its owner:\n  "
        + "\n  ".join(transitions)
    )


def test_only_the_invoice_owner_constructs_invoice_documents_and_lines() -> None:
    invoices = _violations(
        lambda path: _constructor_lines(path, "Invoice"),
        APPROVED_INVOICE_DOCUMENT_WRITERS,
    )
    lines = _violations(
        lambda path: _constructor_lines(path, "InvoiceLine"),
        APPROVED_INVOICE_DOCUMENT_WRITERS,
    )
    assert not invoices, "Invoice constructed outside its owner:\n  " + "\n  ".join(
        invoices
    )
    assert not lines, "InvoiceLine constructed outside its owner:\n  " + "\n  ".join(
        lines
    )


def test_only_collections_owner_constructs_financial_access_evidence() -> None:
    consequences = _violations(
        lambda path: _constructor_lines(path, "FinancialAccessConsequence"),
        APPROVED_FINANCIAL_ACCESS_CONSEQUENCE_WRITERS,
    )
    evidence = _violations(
        lambda path: _constructor_lines(path, "FinancialAccessConsequenceEvidence"),
        APPROVED_FINANCIAL_ACCESS_CONSEQUENCE_WRITERS,
    )
    assert not consequences, (
        "Financial access consequence constructed outside collections owner:\n  "
        + "\n  ".join(consequences)
    )
    assert not evidence, (
        "Financial access evidence constructed outside collections owner:\n  "
        + "\n  ".join(evidence)
    )


def test_only_the_credit_note_owner_writes_credit_applications() -> None:
    violations = _violations(
        lambda path: _constructor_lines(path, "CreditNoteApplication"),
        APPROVED_CREDIT_APPLICATION_WRITERS,
    )
    assert not violations, (
        "CreditNoteApplication constructed outside the credit-note owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_credit_note_owner_constructs_credit_documents_and_lines() -> None:
    documents = _violations(
        lambda path: _constructor_lines(path, "CreditNote"),
        APPROVED_CREDIT_NOTE_WRITERS,
    )
    lines = _violations(
        lambda path: _constructor_lines(path, "CreditNoteLine"),
        APPROVED_CREDIT_NOTE_WRITERS,
    )
    assert not documents, "CreditNote constructed outside its owner:\n  " + "\n  ".join(
        documents
    )
    assert not lines, "CreditNoteLine constructed outside its owner:\n  " + "\n  ".join(
        lines
    )


def test_only_the_credit_note_owner_transitions_credit_status() -> None:
    violations = _violations(
        lambda path: _enum_status_write_lines(path, "CreditNoteStatus"),
        APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS,
    )
    assert not violations, (
        "CreditNote status transitioned outside its owner:\n  "
        + "\n  ".join(violations)
    )


def test_only_the_arrangement_owner_writes_payment_arrangements() -> None:
    constructors = _violations(
        lambda path: _constructor_lines(path, "PaymentArrangement"),
        APPROVED_PAYMENT_ARRANGEMENT_WRITERS,
    )
    transitions = _violations(
        lambda path: _enum_status_write_lines(path, "ArrangementStatus"),
        APPROVED_PAYMENT_ARRANGEMENT_WRITERS,
    )
    assert not constructors, (
        "PaymentArrangement constructed outside its owner:\n  "
        + "\n  ".join(constructors)
    )
    assert not transitions, (
        "PaymentArrangement status transitioned outside its owner:\n  "
        + "\n  ".join(transitions)
    )


def test_only_the_payment_owner_writes_refunds_reversals_and_payment_status() -> None:
    refunds = _violations(
        lambda path: _constructor_lines(path, "PaymentRefund"),
        APPROVED_PAYMENT_REFUND_WRITERS,
    )
    reversals = _violations(
        lambda path: _constructor_lines(path, "PaymentReversal"),
        APPROVED_PAYMENT_REVERSAL_WRITERS,
    )
    consolidated_evidence = _violations(
        lambda path: _constructor_lines(
            path, "ConsolidatedPaymentReturnAllocationEvidence"
        ),
        APPROVED_CONSOLIDATED_RETURN_EVIDENCE_WRITERS,
    )
    transitions = _violations(
        lambda path: _enum_status_write_lines(path, "PaymentStatus"),
        APPROVED_PAYMENT_LIFECYCLE_WRITERS,
    )
    assert not refunds, (
        "PaymentRefund constructed outside its owner:\n  " + "\n  ".join(refunds)
    )
    assert not reversals, (
        "PaymentReversal constructed outside its owner:\n  " + "\n  ".join(reversals)
    )
    assert not consolidated_evidence, (
        "Consolidated return evidence constructed outside its owner:\n  "
        + "\n  ".join(consolidated_evidence)
    )
    assert not transitions, (
        "Payment status transitioned outside its owner:\n  " + "\n  ".join(transitions)
    )


def test_only_the_import_batch_owner_constructs_batch_reversal_evidence() -> None:
    batches = _violations(
        lambda path: _constructor_lines(path, "PaymentImportBatchReversal"),
        APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS,
    )
    items = _violations(
        lambda path: _constructor_lines(path, "PaymentImportBatchReversalItem"),
        APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS,
    )
    assert not batches, (
        "PaymentImportBatchReversal constructed outside its owner:\n  "
        + "\n  ".join(batches)
    )
    assert not items, (
        "PaymentImportBatchReversalItem constructed outside its owner:\n  "
        + "\n  ".join(items)
    )


def test_only_approved_modules_transition_invoice_status() -> None:
    violations = _violations(
        _invoice_status_write_lines,
        APPROVED_INVOICE_LIFECYCLE_WRITERS,
    )
    assert not violations, (
        "Invoice status assigned outside an approved lifecycle owner:\n  "
        + "\n  ".join(violations)
    )


def test_financial_writer_allowlists_only_name_real_writers() -> None:
    operations = (
        (
            APPROVED_LEDGER_WRITERS,
            lambda path: _constructor_lines(path, "LedgerEntry"),
        ),
        (
            APPROVED_ACCOUNT_ADJUSTMENT_WRITERS,
            lambda path: _constructor_lines(path, "AccountAdjustment"),
        ),
        (
            APPROVED_ALLOCATION_WRITERS,
            lambda path: _constructor_lines(path, "PaymentAllocation"),
        ),
        (
            APPROVED_PAYMENT_WRITERS,
            lambda path: _constructor_lines(path, "Payment"),
        ),
        (
            APPROVED_PAYMENT_SETTLEMENT_WRITERS,
            lambda path: _constructor_lines(path, "PaymentSettlement"),
        ),
        (
            APPROVED_PAYMENT_ALLOCATION_EXCEPTION_WRITERS,
            lambda path: _constructor_lines(
                path, "PaymentAllocationReconciliationException"
            ),
        ),
        (
            APPROVED_BILLING_ACCOUNT_LEDGER_WRITERS,
            lambda path: _constructor_lines(path, "BillingAccountLedgerEntry"),
        ),
        (
            APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS,
            lambda path: _constructor_lines(path, "BillingAccountCreditAllocation"),
        ),
        (
            APPROVED_CONSOLIDATED_CREDIT_ALLOCATION_WRITERS,
            lambda path: _constructor_lines(path, "BillingAccountCreditAllocationItem"),
        ),
        (
            APPROVED_BILLING_ACCOUNT_BALANCE_WRITERS,
            _billing_account_balance_write_lines,
        ),
        (
            APPROVED_CREDIT_APPLICATION_WRITERS,
            lambda path: _constructor_lines(path, "CreditNoteApplication"),
        ),
        (
            APPROVED_CREDIT_NOTE_WRITERS,
            lambda path: _constructor_lines(path, "CreditNote"),
        ),
        (
            APPROVED_CREDIT_NOTE_WRITERS,
            lambda path: _constructor_lines(path, "CreditNoteLine"),
        ),
        (
            APPROVED_CREDIT_NOTE_LIFECYCLE_WRITERS,
            lambda path: _enum_status_write_lines(path, "CreditNoteStatus"),
        ),
        (
            APPROVED_PAYMENT_ARRANGEMENT_WRITERS,
            lambda path: _constructor_lines(path, "PaymentArrangement"),
        ),
        (
            APPROVED_PAYMENT_ARRANGEMENT_WRITERS,
            lambda path: _enum_status_write_lines(path, "ArrangementStatus"),
        ),
        (
            APPROVED_PAYMENT_REFUND_WRITERS,
            lambda path: _constructor_lines(path, "PaymentRefund"),
        ),
        (
            APPROVED_PAYMENT_REVERSAL_WRITERS,
            lambda path: _constructor_lines(path, "PaymentReversal"),
        ),
        (
            APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS,
            lambda path: _constructor_lines(path, "PaymentImportBatchReversal"),
        ),
        (
            APPROVED_PAYMENT_IMPORT_BATCH_REVERSAL_WRITERS,
            lambda path: _constructor_lines(path, "PaymentImportBatchReversalItem"),
        ),
        (
            APPROVED_PAYMENT_LIFECYCLE_WRITERS,
            lambda path: _enum_status_write_lines(path, "PaymentStatus"),
        ),
        (APPROVED_INVOICE_LIFECYCLE_WRITERS, _invoice_status_write_lines),
    )
    stale = sorted(
        str(rel)
        for allowlist, finder in operations
        for rel in allowlist
        if not (PROJECT_ROOT / rel).exists() or not finder(PROJECT_ROOT / rel)
    )
    assert not stale, (
        "stale financial-writer allowlist entries must be removed:\n  "
        + "\n  ".join(stale)
    )
