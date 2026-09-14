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
    paid_repair = next(
        item
        for item in service.contract.concerns
        if item.name == "historical paid prepaid invoice identity and coverage repair"
    )
    assert paid_repair.role is OwnerRole.RECONCILER
    assert paid_repair.canonical_writer == service.name
    missing_invoice_repair = next(
        item
        for item in service.contract.concerns
        if item.name == "reviewed missing prepaid paid-invoice repair"
    )
    assert missing_invoice_repair.role is OwnerRole.RECONCILER
    assert missing_invoice_repair.canonical_writer == service.name
    opening_settlement = next(
        item
        for item in service.contract.concerns
        if item.name == "reviewed pre-opening invoice settlement correction"
    )
    assert opening_settlement.role is OwnerRole.RECONCILER
    assert opening_settlement.canonical_writer == service.name


def test_funding_change_checks_existing_draft_before_new_funded_invoice():
    source = inspect.getsource(
        prepaid_service_renewals.apply_due_prepaid_service_after_funding_change
    )

    draft_check = source.index("stage_prepaid_draft_after_funding_change(")
    direct_renewal = source.index("preview_prepaid_service_renewal(")
    assert draft_check < direct_renewal
    assert "draft_invoice_pending" in source
    assert "draft_result.drafts_voided" in source
    assert "draft_result.drafts_found and not duplicate_drafts_voided" in source


def test_funded_prepaid_renewal_uses_invoice_and_credit_participants_only():
    """Neither settlement lane re-enters the generic draft write path.

    Single-owner funding-consequence cutover (2026-09, round 2): the
    original defect was `confirm_prepaid_service_renewal` re-entering
    `stage_prepaid_draft_after_funding_change` to settle a document it had
    just created itself. Both settlement lanes now settle directly
    (`_settle_exact_payment_fundable_renewal`/
    `_settle_reviewed_opening_fundable_renewal`) and neither calls that
    function or its private `_stage_action` write path at all -- this
    assertion is intentionally inverted from what it required before that
    cutover.
    """
    confirm_source = inspect.getsource(
        prepaid_service_renewals.confirm_prepaid_service_renewal
    )
    exact_source = inspect.getsource(
        prepaid_service_renewals._settle_exact_payment_fundable_renewal
    )
    opening_source = inspect.getsource(
        prepaid_service_renewals._settle_reviewed_opening_fundable_renewal
    )
    combined = confirm_source + exact_source + opening_source

    assert "Invoices.stage_system_invoice_for_owner(" in confirm_source
    assert "InvoiceLines.stage_system_line_for_owner(" in confirm_source
    assert "AccountCreditApplications.apply_invoice_fully(" in exact_source
    assert "AccountCreditApplications.apply_invoice_available(" in opening_source
    assert "stage_prepaid_draft_after_funding_change(" not in combined
    assert "_stage_action(" not in combined
    assert "stage_system_account_adjustment(" not in combined
    assert "ensure_prepaid_entitlement_for_wallet_debit(" not in combined


def test_duplicate_draft_transition_stays_under_reconciliation_owner():
    source = inspect.getsource(
        prepaid_draft_reconciliation.stage_prepaid_draft_after_funding_change
    )

    assert "PrepaidDraftAction.void_duplicate" in source
    assert "_stage_action(" in source
    assert "invoice.status =" not in source


def test_reconciler_has_no_rounding_tolerance_or_raw_money_writes():
    source = inspect.getsource(prepaid_draft_reconciliation)

    assert "tolerance" not in source.lower()
    assert "PaymentAllocation(" not in source
    assert "LedgerEntry(" not in source
    assert "AccountAdjustment(" not in source
    assert "execute_owner_command(" in source
    assert "AccountCreditApplications.apply_invoice_fully(" in source
    assert (
        "AccountCreditApplications.apply_invoice_from_selected_payment_fully(" in source
    )
    assert "result.invoice_remaining" in source
    assert "Invoices.void_pristine_draft_for_owner(" in source
    assert "Invoices.adopt_prepaid_proforma_document_for_owner(" in source
    assert "Invoices.repair_paid_prepaid_document_for_owner(" in source
    assert "Invoices.stage_system_invoice_for_owner(" in source
    assert "InvoiceLines.stage_system_line_for_owner(" in source
    assert "confirm_financial_access_restoration_for_owner(" in source
    assert "invoice.is_proforma = False" not in source
    assert "line.subscription_id =" not in source
    assert "subscription.next_billing_at =" not in source


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
    assert 'parser.add_argument("--line-id", type=_uuid)' in source
    assert 'parser.add_argument("--repair-paid-invoice", action="store_true")' in source
    assert (
        'parser.add_argument("--repair-missing-paid-invoice", action="store_true")'
        in source
    )
    assert (
        'parser.add_argument("--repair-opening-settlement", action="store_true")'
        in source
    )
    assert "preview_funded_prepaid_proforma_adoption(" in source
    assert "adopt_funded_prepaid_proforma(" in source
    assert "preview_historical_paid_prepaid_invoice_repair(" in source
    assert "repair_historical_paid_prepaid_invoice(" in source
    assert "preview_missing_paid_prepaid_invoice_repair(" in source
    assert "create_reviewed_paid_prepaid_invoice(" in source
    assert "preview_opening_settlement_correction(" in source
    assert "reconcile_opening_settlement_correction(" in source


def test_admin_invoice_adapter_calls_only_the_authoritative_reconciler():
    source = inspect.getsource(web_prepaid_draft_reconciliation)
    invoice_adapter = (ROOT / "app/services/web_billing_invoices.py").read_text()

    assert "preview_prepaid_draft_reconciliation(" in source
    assert "reconcile_prepaid_draft_invoice(" in source
    assert "settle_prepaid_recovery_invoice" not in source
    assert "prepaid_recovery_billing" not in invoice_adapter


def test_historical_paid_invoice_repair_has_a_permission_gate():
    """The reviewed paid-invoice repair authoritative input names a real gate.

    PR #3092's independent risk review found this command reachable with no
    application-level permission check at all -- a free-text ``actor`` label
    through ``CommandContext.system(...)``. This pins the fix's shape so a
    later edit cannot quietly drop the gate.
    """

    service = service_relationship("financial.prepaid_draft_reconciliation")
    assert "auth.permission_gate" in service.depends_on

    repair_concern = next(
        item
        for item in service.contract.concerns
        if item.name == "historical paid prepaid invoice identity and coverage repair"
    )
    assert "reviewed historical paid-invoice repair command" in (
        repair_concern.input_names
    )

    gate_input = next(
        item
        for item in service.contract.authoritative_inputs
        if item.name == "reviewed historical paid-invoice repair command"
    )
    assert gate_input.owner == "auth.permission_gate"
    assert "billing:prepaid_reconciliation:repair" in gate_input.source

    assert (
        "financial.prepaid_draft_reconciliation.permission_denied"
        in service.contract.errors.domain_codes
    )

    source = inspect.getsource(prepaid_draft_reconciliation)
    assert 'REPAIR_SCOPE = "billing:prepaid_reconciliation:repair"' in source
    assert "permission_granted: bool" in source
    assert (
        "command.context.scope != REPAIR_SCOPE or not command.permission_granted"
        in source
    )


def test_reconciliation_cli_checks_a_real_staff_permission_before_repair():
    """The CLI resolves a real principal's RBAC grants, not a free-text actor.

    A caller could previously type any ``--actor`` string it liked; nothing
    checked it against an actual granted role. This pins that the CLI now
    resolves an operator-supplied staff identifier's real permissions via
    ``system_user_role_names`` (the real ``Role``/``SystemUserRole`` join,
    not ``auth_dependencies.user_role_names``, which reads a ``roles``
    attribute ``SystemUser`` does not have and always returns ``None``) and
    ``has_permission`` before treating the repair as authorized. This is a
    source-grep supplement only: ``tests/test_reconcile_prepaid_drafts_cli.py``
    is the non-vacuous proof that the resolver can actually return ``True``.
    """

    with open(
        "scripts/billing/reconcile_prepaid_drafts.py",
        encoding="utf-8",
    ) as handle:
        source = handle.read()

    assert "from app.services.auth_dependencies import has_permission" in source
    assert (
        "from app.services.system_user_assignments import system_user_role_names"
        in source
    )
    assert "auth_dependencies import has_permission, user_role_names" not in source
    assert "system_user.is_active" in source
    assert 'parser.add_argument("--actor-system-user-id", type=_uuid)' in source
    assert "_resolve_repair_permission_granted(" in source
    assert "permission_granted=repair_permission_granted" in source
    assert "actor_system_user_id=args.actor_system_user_id" in source
    assert "REPAIR_SCOPE" in source
    assert '("--actor-system-user-id", args.actor_system_user_id)' in source
