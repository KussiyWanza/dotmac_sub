from pathlib import Path

from app.services.sot_manifest import TransactionMode
from app.services.sot_relationships import owning_service_for, service_relationship

ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_tax_report_web_adapter_delegates_to_tax_source_owner() -> None:
    source = _read("app/services/web_reports_extended.py")
    start = source.index("def get_tax_report_data(")
    end = source.index("\n\n#", start)
    function_source = source[start:end]

    assert "tax_accounting.build_tax_report" in function_source
    assert "select(Invoice)" not in function_source
    assert "Invoice.tax_total" not in function_source


def test_tax_report_template_uses_truthful_source_projection() -> None:
    source = _read("templates/admin/reports/tax.html")

    assert "Total Tax Collected" not in source
    assert "Net output-tax liability" in source
    assert "credit_note_rows" in source
    assert "wht_rows" in source
    assert "status_presentation_badge" in source
    assert "bg-teal-" not in source
    assert "bg-blue-" not in source
    assert "inv.tax_amount" not in source


def test_sub_owner_does_not_claim_erp_accounting_authority() -> None:
    owner = owning_service_for("tax report semantics")

    assert owner is not None
    assert owner.name == "financial.tax_accounting"
    assert owning_service_for("withholding-tax lifecycle") == owner
    assert owning_service_for("withholding-tax official timeline") == owner
    assert owning_service_for("credit-note tax recognition point") == owner
    assert owning_service_for("tax control-account mappings") is None
    assert owning_service_for("immutable shadow tax postings") is None


def test_sub_has_no_parallel_tax_account_or_posting_path() -> None:
    assert not (ROOT / "app/models/tax_accounting.py").exists()
    assert not (
        ROOT / "alembic/versions/285_tax_accounting_shadow_postings.py"
    ).exists()
    assert not (ROOT / "alembic/versions/286_tax_shadow_lifecycle_state.py").exists()

    forbidden = (
        "TaxControlAccount",
        "TaxPosting(",
        "billing.tax_shadow_posting",
        "schedule_shadow_reconciliation",
    )
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if any(token in source for token in forbidden):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_sync_contract_carries_tax_treatment_and_wht_facts() -> None:
    schema = _read("app/schemas/billing.py")
    invoice_start = schema.index("class InvoiceSyncLineRead")
    invoice_end = schema.index("class InvoiceSyncRead", invoice_start)
    payment_start = schema.index("class PaymentSyncRead")
    payment_end = schema.index("# --- Customer-initiated", payment_start)
    credit_start = schema.index("class CreditNoteSyncLineRead")
    credit_end = schema.index("class CreditNoteSyncRead", credit_start)

    assert "tax_application" in schema[invoice_start:invoice_end]
    assert "tax_application" in schema[credit_start:credit_end]
    for field in (
        "gross_amount",
        "net_amount",
        "wht_amount",
        "wht_rate",
        "wht_status",
        "wht_record_id",
        "wht_certificate_reference",
        "wht_resolved_at",
    ):
        assert field in schema[payment_start:payment_end]


def test_wht_lifecycle_has_one_owner_and_advances_payment_feed() -> None:
    payment_proof_source = _read("app/services/payment_proofs.py")
    tax_source = _read("app/services/tax_accounting.py")
    route_source = _read("app/web/admin/billing_reporting.py")

    assert "tax_accounting.stage_withholding_tax_receivable(" in payment_proof_source
    assert "def stage_withholding_tax_receivable(" in tax_source
    assert "_initialize_withholding_tax_lifecycle(" not in payment_proof_source
    assert "WithholdingTaxRecord(" not in payment_proof_source
    assert "payment.updated_at = now" in tax_source
    assert "web_billing_tax_accounting_service.transition_wht(" in route_source
    assert "WithholdingTaxTransition(" not in route_source
    assert "/tax-accounting/mappings" not in route_source
    assert "/tax-accounting/shadow-control" not in route_source


def test_wht_transition_uses_the_contracted_owner_command_boundary() -> None:
    service = service_relationship("financial.tax_accounting")
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED

    owner_source = _read("app/services/tax_accounting.py")
    transition_start = owner_source.index("def transition_withholding_tax(")
    transition_end = owner_source.index(
        "\n\ndef _stage_withholding_tax_transition(", transition_start
    )
    public_transition = owner_source[transition_start:transition_end]
    route_source = _read("app/web/admin/billing_reporting.py")
    route_start = route_source.index("def billing_wht_transition(")
    route_end = route_source.index("\n\n@router.get", route_start)
    route_transition = route_source[route_start:route_end]

    assert "execute_owner_command(" in public_transition
    assert "commit" not in public_transition
    assert "rollback" not in public_transition
    assert "db.rollback" not in route_transition
    assert "log_audit_event" not in route_transition
    assert "except DomainError" in route_transition


def test_tax_owner_stages_wht_audit_and_events_once() -> None:
    owner_source = _read("app/services/tax_accounting.py")
    payment_proof_source = _read("app/services/payment_proofs.py")

    assert "stage_audit_event(" in owner_source
    assert "EventType.withholding_tax_receivable_recorded" in owner_source
    assert "EventType.withholding_tax_status_changed" in owner_source
    assert "EventType.withholding_tax_receivable_recorded" not in payment_proof_source


def test_wht_policy_and_basis_stay_server_owned() -> None:
    direct_transfer_source = _read("app/services/direct_transfer_intents.py")
    proof_source = _read("app/services/payment_proofs.py")
    reseller_route = _read("app/web/reseller/routes.py")
    reseller_template = _read("templates/reseller/billing/index.html")
    customer_template = _read("templates/customer/billing/topup_transfer.html")

    assert "customer_tax_policies.get_customer_withholding_tax_policy" in (
        direct_transfer_source
    )
    assert "subtotal * rate_percent" in direct_transfer_source
    assert "wht_amount = round_money(total * rate_percent" not in (
        direct_transfer_source
    )
    assert "withholding_tax_basis_unavailable" in proof_source
    assert "allow_server_wht_snapshot=True" in proof_source
    assert 'name="wht_rate"' not in reseller_template
    assert 'name="gross_amount"' not in reseller_template
    assert "wht_rate: str | None = Form" not in reseller_route
    assert "gross_amount: str | None = Form" not in reseller_route
    assert "WHT rate" in customer_template
    assert "Certificate amount outstanding" in customer_template


def test_credit_note_owner_persists_tax_point_for_issuance_adapters() -> None:
    owner = _read("app/services/billing/credit_notes.py")
    assert "issued_at=datetime.now(UTC)" in owner

    for relative in (
        "app/services/billing_automation.py",
        "app/services/prepaid_plan_changes.py",
    ):
        source = _read(relative)
        assert "CreditNotes.issue_system(" in source, relative
        assert "CreditNote(" not in source, relative

    assert "CreditNote(" not in _read("app/services/billing/payments.py")


def test_wht_and_credit_note_migrations_preserve_evidence() -> None:
    wht = _read("alembic/versions/290_wht_lifecycle_timeline.py")
    tax_point = _read("alembic/versions/291_credit_note_tax_point.py")

    assert "uq_withholding_tax_records_payment_id" in wht
    assert "withholding_tax_transitions_append_only" in wht
    assert 'down_revision = "289_merge_support_subscription_and_firmware_heads"' in wht
    assert 'down_revision = "290_wht_lifecycle"' in tax_point
    assert "issued_at = created_at" in tax_point
