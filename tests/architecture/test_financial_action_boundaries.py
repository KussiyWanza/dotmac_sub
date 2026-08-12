"""Financial actions render owner previews; templates do not decide money."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_invoice_template_does_not_derive_receivable_or_credit_eligibility() -> None:
    template = _source("templates/admin/billing/invoice_detail.html")

    forbidden = (
        "totals.paid = totals.paid +",
        "totals.credit = totals.credit +",
        "note.total or 0) - (note.applied_total",
        "if available > 0",
    )
    assert not [pattern for pattern in forbidden if pattern in template]
    assert "invoice_financial_summary.receivable_balance" in template
    assert "credit_application_options" in template


def test_credit_application_adapter_requires_preview_confirmation_evidence() -> None:
    route = _source("app/web/admin/billing_invoice_actions.py")
    confirmation = _source("templates/admin/billing/credit_apply_confirm.html")

    assert '"/invoices/{invoice_id:uuid}/apply-credit/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact ledger transaction" in confirmation


def test_credit_issue_adapter_requires_owner_preview_and_confirmation() -> None:
    route = _source("app/web/admin/billing_credits.py")
    form = _source("templates/admin/billing/credit_form.html")
    confirmation = _source("templates/admin/billing/credit_issue_confirm.html")
    schema = _source("app/schemas/billing.py")
    owner = _source("app/services/billing/credit_notes.py")

    assert '"/credits/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert "confirm('Issue this credit" not in form
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact ledger result" in confirmation
    assert "no direct change" in confirmation
    assert "class CreditNoteIssueApplicationDisposition(StrEnum)" in schema
    assert "application_disposition: CreditNoteIssueApplicationDisposition" in schema
    assert "application_reason: CreditNoteIssueApplicationReason" in schema
    assert owner.count("application = CreditNotes._stage_issue_application(") == 2
    assert "def _decide_issue_application(" in owner
    assert "def _stage_application_with_evidence(" in owner
    assert "def apply_with_evidence(" in owner
    assert (
        "commit: bool"
        not in owner.split("def apply_with_evidence(", 1)[1].split(
            ") -> CreditApplicationResult:", 1
        )[0]
    )


def test_account_adjustments_and_addons_require_owner_confirmation_evidence() -> None:
    api = _source("app/api/billing.py")
    addon_service = _source("app/services/customer_portal_flow_addons.py")
    addon_schema = _source("app/schemas/catalog.py")
    mobile_repository = _source("mobile/lib/src/repositories/catalog_repository.dart")
    data_bundle_screen = _source(
        "mobile/lib/src/features/service/data_bundle_screen.dart"
    )
    plan_change_model = _source("mobile/lib/src/models/plan_change.dart")
    plan_change_template = _source("templates/customer/services/change_plan.html")

    assert '"/account-adjustments/preview"' in api
    assert '"/account-adjustments"' in api
    assert '"/account-adjustments/{adjustment_id}/reversal/preview"' in api
    assert "Direct ledger posting is disabled" in api
    assert "Direct ledger reversal is disabled" in api
    assert "LedgerEntry(" not in addon_service
    assert "ConfirmAccountAdjustmentCommand(" in addon_service
    assert "stage_account_adjustment(" in addon_service
    assert "sub_add_on.account_adjustment_id" in addon_service
    assert "purchase_preview_fingerprint" in addon_service
    assert "preview_fingerprint: str" in addon_schema
    assert "'preview_fingerprint': previewFingerprint" in mobile_repository
    assert ".addonQuote(" in data_bundle_screen
    assert "quote.previewFingerprint" in data_bundle_screen
    assert "wallet_balance" not in addon_schema
    assert "wallet_balance" not in plan_change_model
    assert "current_balance" not in plan_change_model
    assert "Wallet Balance" not in plan_change_template
    assert "Prepaid Funding" in plan_change_template
    assert "Postpaid Receivables" in plan_change_template


def test_plan_changes_bind_human_preview_to_exact_request_evidence() -> None:
    owner = _source("app/services/prepaid_plan_changes.py")
    request_model = _source("app/models/subscription_change.py")
    customer_route = _source("app/web/customer/routes.py")
    customer_template = _source("templates/customer/services/change_plan.html")
    customer_api_schema = _source("app/schemas/catalog.py")
    mobile_repository = _source("mobile/lib/src/repositories/catalog_repository.dart")
    admin_template = _source("templates/admin/catalog/subscription_detail.html")
    admin_bulk_template = _source("templates/admin/catalog/subscriptions.html")
    admin_edit_template = _source("templates/admin/catalog/subscription_form.html")
    history_template = _source("templates/customer/services/change_requests.html")

    assert '"preview_fingerprint": self.fingerprint' in owner
    assert "expected_preview_fingerprint" in owner
    assert "PrepaidPlanChangePreviewStale" in owner
    assert "confirmation_preview_fingerprint" in request_model
    assert "confirmation_idempotency_key" in request_model
    assert "account_adjustment_id" in request_model
    assert "credit_note_id" in request_model
    assert "ledger_entry_id" in request_model
    assert "preview_fingerprint: str = Form(...)" in customer_route
    assert "idempotency_key: str = Form(...)" in customer_route
    assert 'name="preview_fingerprint"' in customer_template
    assert 'name="idempotency_key"' in customer_template
    assert "Exact ledger result" in customer_template
    assert "preview_fingerprint: str" in customer_api_schema
    assert "idempotency_key: str" in customer_api_schema
    assert "'preview_fingerprint': previewFingerprint" in mobile_repository
    assert "Exact ledger result" in admin_template
    assert (
        "body.set('preview_fingerprint', this.billingQuote().preview_fingerprint)"
        in admin_template
    )
    assert "kind === 'change_plan' ? 'next_cycle' : 'immediate'" in admin_bulk_template
    assert ":disabled=\"kind === 'change_plan'\"" in admin_bulk_template
    assert "Use <strong>Change Plan</strong>" in admin_edit_template
    assert "Exact Result" in history_template
    assert "req.ledger_entry_id" in history_template


def test_payment_refund_adapters_require_owner_preview_and_exact_evidence() -> None:
    route = _source("app/web/admin/billing_payments.py")
    detail = _source("templates/admin/billing/payment_detail.html")
    edit = _source("templates/admin/billing/payment_form.html")
    confirmation = _source("templates/admin/billing/payment_refund_confirm.html")
    provider = _source("app/services/payment_provider_events.py")
    owner = _source("app/services/billing/consolidated_payments.py")
    web_service = _source("app/services/web_billing_payments.py")
    generic_api = _source("app/api/billing.py")

    assert '"/payments/{payment_id:uuid}/refund/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert "status_val == 'succeeded'" not in detail
    assert "edit_capability.allowed" in detail
    assert "refund_capability.allowed" in detail
    assert "onsubmit=\"return confirm('Refund" not in detail
    assert '<option value="refunded"' not in edit
    assert "managed by exact owner evidence" in edit
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact evidence and access consequence" in confirmation
    assert "does not promise a particular service-access state" in confirmation
    assert "Refunds.stage_provider_event_refund" in provider
    assert "ConsolidatedPaymentRefunds.stage_provider_event" in provider
    assert "consolidated_payment_refunds.preview" in web_service
    assert "consolidated_payment_refunds.confirm" in web_service
    assert "consolidated_payment_refunds.preview" in generic_api
    assert "consolidated_payment_refunds.confirm" in generic_api
    assert "class ConsolidatedPaymentRefunds" in owner
    assert 'action="refund_consolidated_payment"' in owner
    assert "ConsolidatedPaymentReturnAllocationEvidence(" in owner
    assert "Consolidated credit" in confirmation
    assert "subscriber allocation-reversal ledger row(s)" in confirmation
    assert "Payments.stage_status_transition(" in provider
    assert "origin=PaymentSettlementOrigin.provider_event" in provider


def test_payment_reversal_adapters_require_owner_preview_and_exact_evidence() -> None:
    route = _source("app/web/admin/billing_payments.py")
    detail = _source("templates/admin/billing/payment_detail.html")
    edit = _source("templates/admin/billing/payment_form.html")
    confirmation = _source("templates/admin/billing/payment_reversal_confirm.html")
    provider = _source("app/services/payment_provider_events.py")
    verified_webhook = _source("app/services/payment_webhook_commands.py")
    generic_api = _source("app/api/billing.py")
    owner = _source("app/services/billing/consolidated_payments.py")
    web_service = _source("app/services/web_billing_payments.py")

    assert '"/payments/{payment_id:uuid}/reversal/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert "reversal_capability.allowed" in detail
    assert "edit_capability.allowed" in detail
    assert "PaymentStatus.reversed" not in detail
    assert '<option value="reversed"' not in edit
    assert "managed by exact owner evidence" in edit
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact evidence and access consequence" in confirmation
    assert "does not contact a bank or provider" in confirmation
    assert "PaymentReversals.stage_provider_event_reversal" in provider
    assert "ConsolidatedPaymentReversals.stage_provider_event" in provider
    assert "consolidated_payment_reversals.preview" in web_service
    assert "consolidated_payment_reversals.confirm" in web_service
    assert "consolidated_payment_reversals.preview" in generic_api
    assert "consolidated_payment_reversals.confirm" in generic_api
    assert "class ConsolidatedPaymentReversals" in owner
    assert 'action="reverse_consolidated_payment"' in owner
    assert "Consolidated credit" in confirmation
    assert "subscriber allocation-reversal ledger row(s)" in confirmation
    assert "stage_verified_webhook_event" in verified_webhook
    assert "PaymentWebhookProvider" in verified_webhook
    assert "trusted_financial_effects" not in verified_webhook
    assert "trusted_financial_effects=True" not in generic_api


def test_imported_payment_batch_reversal_requires_provenance_and_confirmation() -> None:
    owner = _source("app/services/financial_import_batch_reversals.py")
    import_runs = _source("app/services/import_runs.py")
    legacy_rollback = _source("app/services/web_system_import_wizard.py")
    route = _source("app/web/admin/system.py")
    detail = _source("templates/admin/system/import_run_detail.html")
    confirmation = _source(
        "templates/admin/system/import_payment_batch_reversal_confirm.html"
    )

    assert "record_created = persisted.created_new" in import_runs
    assert "obj.import_run_id = run.id" in import_runs
    assert "row.record_created is None or row.payment_id is None" in owner
    assert "payment.import_run_id != run.id" in owner
    assert "PaymentReversals.process_with_evidence(" in owner
    assert "db.delete(" not in owner
    assert "Legacy financial and subscription import history cannot be raw-deleted" in (
        legacy_rollback
    )
    assert '"/import-runs/{run_id}/payment-reversal-preview"' in route
    assert '"/import-runs/{run_id}/payment-reversal-confirm"' in route
    assert "batch_reversal_capability.allowed" in detail
    assert 'name="reason"' in detail
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact ledger result" in confirmation
    assert "Prepaid funding" in confirmation
    assert "Unallocated credit" in confirmation
    assert "Postpaid receivables" in confirmation
    assert "no restoration or suspension is predicted" in confirmation


def test_payment_creation_settlement_and_allocation_use_owner_confirmation() -> None:
    route = _source("app/web/admin/billing_payments.py")
    api = _source("app/api/billing.py")
    form = _source("templates/admin/billing/payment_form.html")
    creation = _source("templates/admin/billing/payment_create_confirm.html")
    settlement = _source("templates/admin/billing/payment_settlement_confirm.html")
    allocation = _source("templates/admin/billing/payment_allocation_confirm.html")

    assert '"/payments/create/preview"' in route
    assert '"/payments/{payment_id:uuid}/settlement/preview"' in route
    assert '"/payments/{payment_id:uuid}/allocation/preview"' in route
    assert '"/payments/creation/preview"' in api
    assert '"/payment-allocations/preview"' in api
    assert "Record this payment" not in form
    assert 'name="idempotency_token"' in creation
    for confirmation in (creation, settlement, allocation):
        assert 'name="preview_fingerprint"' in confirmation
    for confirmation in (settlement, allocation):
        assert 'name="idempotency_key"' in confirmation
    assert "Prepaid funding position" in creation
    assert "Unallocated account credit" in creation
    assert "Receivable" in allocation
    assert "Exact account-credit ledger" in allocation


def test_consolidated_credit_allocation_uses_owner_preview_and_confirmation() -> None:
    web_route = _source("app/web/reseller/routes.py")
    api = _source("app/api/reseller.py")
    adapter = _source("app/services/reseller_portal_billing.py")
    index = _source("templates/reseller/billing/index.html")
    confirmation = _source("templates/reseller/billing/allocation_confirm.html")

    assert '"/billing/subscribers/{subscriber_id}/allocate/preview"' in web_route
    assert '"/billing/subscribers/{subscriber_id}/allocate/confirm"' in web_route
    assert "preview_fingerprint: str = Form(...)" in web_route
    assert "idempotency_key: str = Form(...)" in web_route
    assert '"/billing/subscribers/{subscriber_id}/allocation/preview"' in api
    assert '"/billing/subscribers/{subscriber_id}/allocation/confirm"' in api
    assert "BillingAccountCreditAllocationConfirm" in api
    assert "consolidated_credit_allocations.preview(" in adapter
    assert "consolidated_credit_allocations.confirm(" in adapter
    assert "allocate_consolidated_balance_to_subscriber(" not in adapter
    assert "[s.open_balance, unallocated_balance] | min" not in index
    assert "s.allocation_allowed" in index
    assert "s.allocation_max" in index
    assert "onsubmit=" not in index
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Consolidated prepaid credit" in confirmation
    assert "Subscriber postpaid receivable" in confirmation
    assert "Service access is not decided by this screen" in confirmation


def test_consolidated_settlement_reconciliation_is_an_evidence_only_adapter() -> None:
    api = _source("app/api/billing.py")
    owner = _source("app/services/billing/consolidated_payments.py")

    assert '"/consolidated-payments/{payment_id}/settlement/evidence"' in api
    assert '"/consolidated-payments/{payment_id}/settlement/evidence/preview"' in api
    assert '"/consolidated-payments/{payment_id}/settlement/evidence/reconcile"' in api
    assert "inspect_reconciliation_evidence(" in api
    assert "preview_reconciliation(" in api
    assert "reconcile_historical_evidence(" in api
    assert "PaymentSettlement(" not in api
    assert "ConsolidatedPaymentSettlementReconciliationEvidence(" not in api
    assert "def reconcile_historical_evidence(" in owner
    assert 'action="reconcile_consolidated_settlement_evidence"' in owner
    assert '"money_posted": False' in owner
    assert '"none_evidence_only_no_access_decision"' in owner


def test_consolidated_credit_consumption_reconciliation_stays_in_its_owner() -> None:
    api = _source("app/api/billing.py")
    owner = _source("app/services/billing/consolidated_payments.py")

    base = '"/billing-accounts/{billing_account_id}/credit-consumption/evidence"'
    assert base in api
    assert base[:-1] + '/preview"' in api
    assert base[:-1] + '/reconcile"' in api
    assert "inspect_reconciliation_evidence(" in api
    assert "preview_reconciliation(" in api
    assert "reconcile_historical_consumption(" in api
    assert "BillingAccountLedgerEntry(" not in api
    assert "BillingAccountCreditAllocation(" not in api
    assert "ConsolidatedCreditConsumptionReconciliationEvidence(" not in api
    assert "def reconcile_historical_consumption(" in owner
    assert 'action="reconcile_consolidated_credit_consumption"' in owner
    assert '"billing_account_balance_changed": False' in owner
    assert '"none_historical_evidence_reconciliation_no_access_decision"' in owner


def test_consolidated_return_reconciliation_is_an_evidence_only_adapter() -> None:
    api = _source("app/api/billing.py")
    owner = _source("app/services/billing/consolidated_payments.py")

    base = (
        '"/consolidated-payments/{payment_id}/returns/'
        '{return_type}/{return_id}/evidence"'
    )
    assert base in api
    assert base[:-1] + '/preview"' in api
    assert base[:-1] + '/reconcile"' in api
    assert "consolidated_payment_return_reconciliations.inspect_evidence(" in api
    assert "consolidated_payment_return_reconciliations.preview(" in api
    assert (
        "consolidated_payment_return_reconciliations.reconcile_historical_evidence("
        in api
    )
    assert "PaymentRefund(" not in api
    assert "PaymentReversal(" not in api
    assert "BillingAccountLedgerEntry(" not in api
    assert "LedgerEntry(" not in api
    assert "ConsolidatedPaymentReturnReconciliationEvidence(" not in api
    assert "class ConsolidatedPaymentReturnReconciliations:" in owner
    assert 'action="reconcile_consolidated_return_evidence"' in owner
    assert '"money_posted": False' in owner
    assert '"billing_account_balance_changed": False' in owner
    assert '"none_historical_return_evidence_no_access_decision"' in owner


def test_missing_consolidated_return_document_composes_the_evidence_owner() -> None:
    api = _source("app/api/billing.py")
    owner = _source("app/services/billing/consolidated_payments.py")

    base = (
        '"/consolidated-payments/{payment_id}/return-document-reconstruction/'
        "{return_type}"
    )
    assert base in api
    assert '/evidence"' in api
    assert '/preview"' in api
    assert '/reconstruct"' in api
    assert "inspect_missing_document_evidence(" in api
    assert "preview_document_reconstruction(" in api
    assert "reconstruct_missing_document(" in api
    assert "PaymentRefund(" not in api
    assert "PaymentReversal(" not in api
    assert "LedgerEntry(" not in api
    assert "BillingAccountLedgerEntry(" not in api
    assert "def reconstruct_missing_document(" in owner
    assert "cls.reconcile_historical_evidence(" in owner
    assert 'action="reconstruct_consolidated_return_document"' in owner
    assert '"return_document_created": True' in owner
    assert '"money_posted": False' in owner
    assert '"billing_account_balance_changed": False' in owner
    assert '"none_return_document_reconstruction_no_access_decision"' in owner


def test_native_credit_reconciliation_composes_the_payment_allocation_owner() -> None:
    reconciliation = _source("app/services/billing/reconcile_unposted.py")

    assert "PaymentAllocations.preview(" in reconciliation
    assert "PaymentAllocations.stage_confirm(" in reconciliation
    assert "PaymentAllocationConfirm(" in reconciliation
    assert "preview_fingerprint=preview.fingerprint" in reconciliation
    assert "reconcile-unposted-" in reconciliation
    assert "_apply_payment_allocation(" not in reconciliation
    assert "LedgerEntry(" not in reconciliation


def test_deposit_adapters_compose_named_credit_owners() -> None:
    deposit_owner = _source("app/services/account_credit_deposits.py")
    application_owner = _source("app/services/billing/account_credit.py")
    portal = _source("app/services/customer_portal_flow_payments.py")
    customer_route = _source("app/web/customer/routes.py")
    customer_template = _source("templates/customer/billing/topup.html")
    customer_api = _source("app/api/me.py")
    gateway_intent_owner = _source("app/services/gateway_topup_intents.py")
    webhook = _source("app/services/payment_webhook_commands.py")
    proofs = _source("app/services/payment_proofs.py")
    invoice_owner = _source("app/services/billing/invoices.py")

    assert "Payments.create_account_credit_deposit(" in deposit_owner
    assert "AccountCreditApplications.apply(" in deposit_owner
    assert "PaymentAllocation(" not in deposit_owner
    assert "LedgerEntry(" not in deposit_owner
    assert "PaymentAllocations.preview(" in application_owner
    assert "PaymentAllocations.stage_confirm(" in application_owner
    assert "PaymentAllocation(" not in application_owner
    assert "LedgerEntry(" not in application_owner
    assert '"/billing/topup/preview"' in customer_route
    assert "preview_topup(db, customer, amount_value)" in customer_route
    assert "preview_fingerprint: str = Field(min_length=64, max_length=64)" in _source(
        "app/schemas/billing.py"
    )
    assert "preview_fingerprint=payload.preview_fingerprint" in customer_api
    assert "preview_fingerprint: this.previewFingerprint" in customer_template
    assert "create_customer_gateway_topup_intent(" in portal
    assert "preview_topup(" in portal
    assert "AccountCreditDeposits.stage_intent(" in gateway_intent_owner
    assert "AccountCreditDeposits.create_intent(" not in portal
    assert "AccountCreditDeposits.settle_verified(" in portal
    assert "AccountCreditDeposits.stage_verified_settlement(" in webhook
    assert "AccountCreditDeposits.stage_verified_settlement(" in proofs
    assert "AccountCreditApplications.apply(" in invoice_owner
    assert "AccountCreditApplications.release_for_invoice_void(" in invoice_owner
    assert "def release_for_invoice_void(" in application_owner
    assert "restore_account_services(" not in deposit_owner
    assert "if intent.purpose is None:" in portal


def test_invoice_void_and_writeoff_use_owner_preview_and_exact_evidence() -> None:
    route = _source("app/web/admin/billing_invoice_actions.py")
    bulk_route = _source("app/web/admin/billing_invoice_bulk.py")
    api = _source("app/api/billing.py")
    detail = _source("templates/admin/billing/invoice_detail.html")
    edit = _source("templates/admin/billing/invoice_form.html")
    confirmation = _source("templates/admin/billing/invoice_closure_confirm.html")
    bulk_confirmation = _source(
        "templates/admin/billing/invoice_bulk_void_confirm.html"
    )

    assert '"/invoices/{invoice_id:uuid}/void/preview"' in route
    assert '"/invoices/{invoice_id:uuid}/write-off/preview"' in route
    assert "preview_fingerprint: str = Form(...)" in route
    assert "idempotency_key: str = Form(...)" in route
    assert '"/invoices/{invoice_id}/void/preview"' in api
    assert '"/invoices/{invoice_id}/write-off/preview"' in api
    assert '"/invoices/{invoice_id}/closure-evidence/reconcile"' in api
    assert "invoice_void_capability.allowed" in detail
    assert "invoice_write_off_capability.allowed" in detail
    assert "onsubmit=\"return confirm('Void" not in detail
    assert '<option value="void"' not in edit
    assert '<option value="written_off"' not in edit
    assert '<option value="paid"' not in edit
    assert 'name="preview_fingerprint"' in confirmation
    assert 'name="idempotency_key"' in confirmation
    assert "Exact resulting ledger evidence" in confirmation
    assert "does not promise a particular service-access state" in confirmation
    assert "preview_bulk_void" in bulk_route
    assert 'name="preview_fingerprints_json"' in bulk_confirmation


def test_automation_and_usage_stage_invoice_documents_through_owner() -> None:
    owner = _source("app/services/billing/invoices.py")
    automation = _source("app/services/billing_automation.py")
    usage = _source("app/services/usage.py")

    assert "def stage_system_invoice(" in owner
    assert "def stage_system_line(" in owner
    assert 'action="stage_system_invoice"' in owner
    assert 'action="stage_system_invoice_line"' in owner
    assert '"ledger_transaction_id": None' in owner
    assert "Invoices.stage_system_invoice(" in automation
    assert "InvoiceLines.stage_system_line(" in automation
    assert "Invoices.stage_system_invoice(" in usage
    assert "InvoiceLines.stage_system_line(" in usage
    assert "Invoice(" not in automation
    assert "InvoiceLine(" not in automation
    assert "Invoice(" not in usage
    assert "InvoiceLine(" not in usage


def test_dunning_consumes_arrangement_shields_from_the_arrangement_owner() -> None:
    dunning = _source("app/services/collections/_core.py")

    assert "from app.models.payment_arrangement import" not in dunning
    assert "active_arrangement_shield_reason(db, account_id)" in dunning
    assert "bulk_active_arrangement_shield_reasons(db, ids)" in dunning


def test_dunning_and_restore_use_one_evidenced_access_consequence_owner() -> None:
    dunning = _source("app/services/collections/_core.py")
    event_adapter = _source("app/services/events/handlers/enforcement.py")
    billing_automation = _source("app/services/billing_automation.py")
    billing_settings = _source("templates/admin/system/config/billing.html")
    subscriber_projection = _source("app/services/web_subscriber_details.py")

    assert "def preview_financial_access_consequence(" in dunning
    assert "def confirm_financial_access_consequence(" in dunning
    assert "def preview_financial_access_restoration(" in dunning
    assert "def confirm_financial_access_restoration(" in dunning
    assert "FinancialAccessConsequenceEvidence(" in dunning
    assert "access_consequence=access_consequence" in dunning
    assert "restore_account_services(" in event_adapter
    assert "has_overdue_balance(" not in event_adapter
    assert "_suspension_shield_reason" not in event_adapter
    assert "suspension_warning_sent_at" not in event_adapter
    assert "restore_account_services(" not in billing_automation
    assert "has_overdue_balance(" not in billing_automation
    assert "_emit_dunning_escalations" not in billing_automation
    assert "_emit_post_grace_suspension_escalation" not in billing_automation
    assert "post_grace_suspension" not in billing_automation
    for retired_key in (
        "auto_suspend_on_overdue",
        "suspension_grace_hours",
        "dunning_escalation_days",
        "blocking_period_days",
        "deactivation_period_days",
    ):
        assert f'name="{retired_key}"' not in billing_settings
    assert "next_block_at" not in subscriber_projection
    assert "next_block_label" not in subscriber_projection
