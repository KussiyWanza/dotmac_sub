"""financial_access SOT declarations: financial core."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="financial.ledger",
        module="app.services.billing.ledger",
        owns=(
            "append-only ledger record lifecycle",
            "ledger reversal invariants",
            "financial transaction history",
        ),
    ),
    SOTService(
        name="financial.prepaid_funding_reconstruction",
        module="app.services.prepaid_funding_reconstruction",
        owns=(
            "reviewed full-cohort prepaid funding manifests",
            "prepaid opening-position baselines and supersession",
            "final prepaid funding authority cutover",
            "opening balance plus post-cutover native funding projection",
        ),
        depends_on=(
            "billing.opening_balance_history",
            "financial.ledger",
        ),
        notes=(
            "The first approved batch permanently retires carried-in funding "
            "authority. The complete-history migration supersession requires "
            "one target for every funding candidate and aborts on any source "
            "integrity defect; later corrections are reviewed append-only "
            "supersessions. The frozen opening-balance snapshot is one-time migration "
            "evidence, never a runtime money source or fallback. The separate "
            "customer.financial_position verifier owns the post-activation "
            "composition of an approved subledger opening with later native "
            "facts. For opening verification only, a customer created after "
            "the fixed handoff with no carried-in identity has a typed "
            "zero history component plus canonical native facts; runtime "
            "money actions remain quarantined until approved immutable "
            "opening capture. A post-cutover single-account review bounds "
            "those native facts at the immutable original authority cutoff so "
            "later authoritative postings are not absorbed twice. This "
            "reconstruction owner never rewrites an "
            "opening or posts money."
        ),
    ),
    SOTService(
        name="financial.account_adjustments",
        module="app.services.billing.adjustments",
        owns=(
            "prepaid account-debit eligibility and preview",
            "locked account-debit confirmation",
            "account-adjustment idempotency and audit evidence",
            "exact account-adjustment ledger links",
            "previewed account-adjustment reversal evidence",
        ),
        depends_on=(
            "financial.ledger",
            "customer.financial_position",
            "customer.accounts",
            "control.settings_spec",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes=(
            "This owner accepts debits only. Customer credits remain "
            "owned by financial.credit_notes, and account adjustments "
            "do not decide service-access state."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid account-debit eligibility and preview",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical Subscriber account state",
                        "canonical append-only ledger state",
                        "resolved customer financial position",
                        "billing default-currency setting",
                    ),
                ),
                ConcernContract(
                    name="locked account-debit confirmation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "account-adjustment command evidence",
                        "canonical Subscriber account state",
                        "canonical append-only ledger state",
                        "resolved customer financial position",
                        "billing default-currency setting",
                    ),
                    canonical_writer="financial.account_adjustments",
                ),
                ConcernContract(
                    name="account-adjustment idempotency and audit evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "account-adjustment command evidence",
                        "canonical Subscriber account state",
                        "canonical append-only ledger state",
                    ),
                    canonical_writer="financial.account_adjustments",
                ),
                ConcernContract(
                    name="exact account-adjustment ledger links",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "account-adjustment command evidence",
                        "canonical append-only ledger state",
                    ),
                    canonical_writer="financial.account_adjustments",
                ),
                ConcernContract(
                    name="previewed account-adjustment reversal evidence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "account-adjustment command evidence",
                        "canonical Subscriber account state",
                        "canonical append-only ledger state",
                        "resolved customer financial position",
                    ),
                    canonical_writer="financial.account_adjustments",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="account-adjustment command evidence",
                    owner="financial.account_adjustments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "typed command context, confirmed preview fingerprint, "
                        "and origin-scoped idempotency key"
                    ),
                ),
                AuthorityInput(
                    name="canonical Subscriber account state",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="subscribers account identity",
                ),
                AuthorityInput(
                    name="canonical append-only ledger state",
                    owner="financial.ledger",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="ledger_entries and structural reversal links",
                ),
                AuthorityInput(
                    name="resolved customer financial position",
                    owner="customer.financial_position",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "prepaid availability, receivables, and "
                        "collection-blocking balance resolver"
                    ),
                ),
                AuthorityInput(
                    name="billing default-currency setting",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="billing.default_currency",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Public debit and reversal commands enter one "
                    "manifest-verified owner transaction. Explicit nested "
                    "staging collaborators flush only inside approved plan-"
                    "change, add-on, or renewal coordinator transactions."
                ),
                locking=(
                    "Debit confirmation locks the Subscriber account before "
                    "re-preview and append. Reversal locks the account, "
                    "AccountAdjustment, and original ledger entry in that order."
                ),
                idempotency=(
                    "Database uniqueness scopes debit and reversal keys by "
                    "origin; exact account, preview, effective-date, and "
                    "structural ledger evidence are revalidated on replay."
                ),
                retries=(
                    "Exact replay is safe. Only write_conflict is retryable "
                    "after the owner rolls back; stale previews require a new "
                    "preview and insufficient funding requires new source state."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.account_adjustments.invalid_command",
                    "financial.account_adjustments.invalid_configuration",
                    "financial.account_adjustments.account_not_found",
                    "financial.account_adjustments.adjustment_not_found",
                    "financial.account_adjustments.insufficient_funding",
                    "financial.account_adjustments.idempotency_conflict",
                    "financial.account_adjustments.stale_preview",
                    "financial.account_adjustments.already_reversed",
                    "financial.account_adjustments.incomplete_evidence",
                    "financial.account_adjustments.write_conflict",
                    "financial.account_adjustments.active_caller_transaction",
                    "financial.account_adjustments.command_contract_violation",
                    "financial.account_adjustments.invalid_command_context",
                    "financial.account_adjustments.nested_owner_command",
                    "financial.account_adjustments.nested_transaction_completion",
                ),
                mapping_owner="API and enclosing financial coordinator adapters",
                retryable_codes=("financial.account_adjustments.write_conflict",),
                fail_closed_on=(
                    "stale or mismatched preview",
                    "insufficient prepaid funding",
                    "ambiguous idempotency evidence",
                    "incomplete or inconsistent structural ledger evidence",
                    "active caller transaction",
                ),
            ),
            events=EventContract(
                event_types=(
                    "account_adjustment.confirmed",
                    "account_adjustment.reversed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "PII-free versioned payloads retain aggregate, account, "
                    "money, origin, exact ledger, and command evidence fields."
                ),
                replay=(
                    "Idempotent command replay emits no duplicate event; the "
                    "durable dispatcher retries each staged event."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "generic ledger API plus plan-change and add-on debit paths"
                ),
                new_owner="financial.account_adjustments",
                verification=(
                    "The billing alignment audit recorded zero historical "
                    "adjustment-debit drift; structural evidence inspection and "
                    "focused replay, stale-preview, funding, and reversal tests "
                    "remain the cutover proof."
                ),
                cutover_gate=(
                    "All application debits use a public command or an approved "
                    "nested staging collaborator and carry exact ledger evidence."
                ),
                fallback_retirement=(
                    "Generic ledger posting/reversal stays gated; direct "
                    "AccountAdjustment construction and legacy commit flags are "
                    "forbidden by architecture tests."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/CODING_STANDARD.md",
                "docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_account_adjustment_evidence.py",
                "tests/architecture/test_account_adjustment_boundary.py",
                "tests/architecture/test_financial_action_boundaries.py",
                "tests/architecture/test_financial_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.billing_accounts",
        module="app.services.billing.billing_accounts",
        owns=(
            "billing account identity and configuration",
            "consolidated billing account statement projection",
        ),
        depends_on=("financial.ledger",),
    ),
    SOTService(
        name="financial.consolidated_payments",
        module="app.services.billing.consolidated_payments",
        owns=(
            "consolidated payment settlement preview and confirmation",
            "consolidated payment idempotency and actor audit evidence",
            "historical consolidated settlement evidence reconciliation",
            "exact consolidated settlement cash provenance links",
            "exact member-invoice allocation ledger links",
            "exact consolidated-credit ledger links",
            "consolidated-credit allocation preview and confirmation",
            "exact source-credit consumption and subscriber-ledger links",
            "consolidated-credit allocation idempotency and actor audit",
            "historical consolidated-credit consumption reconciliation",
            "exact billing-account projection-debit repair evidence",
            "consolidated payment refund eligibility and preview",
            "billing-account refund confirmation and exact ledger evidence",
            "consolidated payment reversal eligibility and preview",
            "billing-account reversal confirmation and exact ledger evidence",
            "consolidated return idempotency and actor audit evidence",
            "historical consolidated refund/reversal evidence reconciliation",
            "exact historical consolidated return provenance links",
            "historical consolidated return document reconstruction",
            "reviewed historical return source references",
            "consolidated payment access-reconciliation handoff",
        ),
        depends_on=(
            "financial.ledger",
            "financial.billing_accounts",
            "financial.payments",
        ),
        notes=(
            "Subscriber invoice receivable credits remain subscriber "
            "ledger rows; reseller-held surplus is recorded in the "
            "billing-account ledger and never assigned to a fake "
            "subscriber. Moving held credit to a member receivable is a "
            "separate preview-bound transfer with exact source and result "
            "links. Payment state and access state remain separate."
        ),
    ),
    SOTService(
        name="financial.account_credit_applications",
        module="app.services.billing.account_credit",
        owns=(
            "eligible invoice selection for evidenced account credit",
            "deterministic payment-credit source selection",
            "oldest-payable-debt application orchestration",
            "exact invoice payment-backed funding preview",
            "all-or-nothing exact invoice credit application",
            "invoice-void release of exact account-credit allocations",
            "account-credit application invariant monitoring",
            "bounded account-credit invariant summary",
        ),
        depends_on=("financial.payments", "financial.invoices"),
        notes=(
            "Account credit is derived from exact unconsumed settlement "
            "evidence, never a wallet counter. This owner composes the "
            "payment-allocation owner and does not write money directly."
        ),
    ),
)
