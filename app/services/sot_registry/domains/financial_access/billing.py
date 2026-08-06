"""financial_access SOT declarations: billing."""

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
    owner_command_boundary_error_codes,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="billing.splynx_history_opening",
        module="app.services.billing.splynx_history_opening",
        owns=("complete Splynx-history customer opening target",),
        depends_on=(
            "customer.accounts",
            "financial.credit_notes",
            "financial.invoices",
            "financial.ledger",
            "financial.payments",
        ),
        notes=(
            "Cutover-only resolver over the frozen isolated Splynx audit "
            "restore. A complete empty transaction set is zero; missing, "
            "duplicate, malformed, or unreconciled source evidence fails the "
            "whole cohort. It never contacts Splynx, writes money, assigns an "
            "unknown balance, or remains a runtime authority after completion."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="complete Splynx-history customer opening target",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "frozen Splynx transaction-net evidence",
                        "canonical post-handoff native financial facts",
                        "canonical migrated customer identity",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="frozen Splynx transaction-net evidence",
                    owner="external:splynx_final_snapshot",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "isolated audit_splynx_final_balances rows produced from "
                        "the retained final source snapshot, including exact active "
                        "transaction count/net and source-deposit reconciliation"
                    ),
                ),
                AuthorityInput(
                    name="canonical post-handoff native financial facts",
                    owner="financial.ledger",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "currency-separated native payment, invoice, credit-note, "
                        "and ledger facts strictly after the fixed legacy handoff; "
                        "each underlying document remains owned by its named "
                        "financial service"
                    ),
                ),
                AuthorityInput(
                    name="canonical migrated customer identity",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscriber id and retained one-to-one "
                        "splynx_customer_id provenance"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "The isolated export adapter owns a read-only repeatable "
                    "snapshot; this resolver performs no write or transaction "
                    "completion."
                ),
                locking="No locks; the restored source snapshot is frozen.",
                idempotency=(
                    "The same complete cohort, source rows, handoff, native facts, "
                    "currency, and position time produce the same fingerprints."
                ),
                retries=(
                    "Exact read retries are safe; any integrity error aborts the "
                    "complete artifact."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "billing.splynx_history_opening.invalid_query",
                    "billing.splynx_history_opening.unsupported_currency",
                    "billing.splynx_history_opening.source_snapshot_missing",
                    "billing.splynx_history_opening.source_cohort_incomplete",
                    "billing.splynx_history_opening.source_identity_duplicate",
                    "billing.splynx_history_opening.source_identity_mismatch",
                    "billing.splynx_history_opening.source_history_malformed",
                    "billing.splynx_history_opening.source_history_unreconciled",
                ),
                mapping_owner=("scripts.one_off.export_prepaid_funding_snapshot"),
                fail_closed_on=(
                    "missing or duplicate customer/source identity",
                    "missing frozen source row",
                    "non-zero position with an empty transaction set",
                    "transaction net not equal to the frozen source position",
                    "non-NGN source request",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                old_owner=(
                    "partial independent replay with permanent funding quarantine"
                ),
                new_owner="billing.splynx_history_opening",
                verification=(
                    "Complete/empty history, source mismatch, missing cohort, "
                    "native advancement, signed manifest, incremental opening, "
                    "and full-cohort parity regressions."
                ),
                cutover_gate=(
                    "Every funding candidate has a signed history-derived target, "
                    "every account present at subledger activation has one "
                    "immutable opening, later native accounts start at zero, and "
                    "per-lane variance is zero."
                ),
                fallback_retirement=(
                    "Remove the partial-subset exporter, blocker adjudication, "
                    "quarantine work items, and Splynx evidence reader after the "
                    "one-time complete capture."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/designs/SPLYNX_RETIREMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_splynx_history_opening.py",
                "tests/test_billing_alignment_audit.py",
                "tests/test_subledger_opening_positions.py",
            ),
        ),
    ),
    SOTService(
        name="billing.addon_contract_backfill",
        module="app.services.billing.addon_contract_backfill",
        owns=("recurring add-on contract migration snapshot",),
        depends_on=(
            "billing.contracts",
            "events.dispatcher",
            "events.owner_outputs",
            "financial.addon_purchases",
        ),
        notes=(
            "ADR 0007 shadow migration only. This temporary observation "
            "owner binds one future service-period boundary to the exact "
            "legacy SubscriptionAddOn identities, quantities, intervals, "
            "and unique active recurring price ids. It never decides a "
            "price, charges money, writes a contract line, or repairs "
            "another owner. The confirmed fingerprint stages a durable "
            "output that billing.contracts must receipt into the shared "
            "next-boundary draft and exact durable timer."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="recurring add-on contract migration snapshot",
                    role=OwnerRole.OBSERVATION_COLLECTOR,
                    input_names=(
                        "legacy recurring add-on facts",
                        "recorded billing contract boundary",
                    ),
                    canonical_writer="billing.addon_contract_backfill",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="legacy recurring add-on facts",
                    owner="financial.addon_purchases",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "SubscriptionAddOn identity, quantity, start/end "
                        "interval, AddOn description, and the unique active "
                        "recurring AddOnPrice id, amount, and currency"
                    ),
                ),
                AuthorityInput(
                    name="recorded billing contract boundary",
                    owner="billing.contracts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "current effective shadow contract version, cadence, "
                        "currency, and structural SalesOrderLine anchor"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The capture command enters execute_owner_command once "
                    "on a transaction-free session; source locks, fingerprint "
                    "confirmation, idempotency evidence, and the staged "
                    "owner output commit together."
                ),
                locking=(
                    "The BillingContract and current effective version are "
                    "locked before the confirmed source snapshot is rebuilt."
                ),
                idempotency=(
                    "One durable idempotency row records the emitted event "
                    "for each business key; exact replay emits no second output."
                ),
                retries=(
                    "Retry the whole command with the same idempotency key. "
                    "Changed contract or add-on facts require a new preview."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "billing.addon_contract_backfill"
                    ),
                    "billing.addon_contract_backfill.already_captured",
                    "billing.addon_contract_backfill.ambiguous_recurring_price",
                    "billing.addon_contract_backfill.contract_not_found",
                    (
                        "billing.addon_contract_backfill."
                        "current_contract_version_not_found"
                    ),
                    "billing.addon_contract_backfill.invalid_addon_price",
                    "billing.addon_contract_backfill.invalid_addon_quantity",
                    ("billing.addon_contract_backfill.invalid_period_index"),
                    ("billing.addon_contract_backfill.invalid_preview_fingerprint"),
                    "billing.addon_contract_backfill.idempotency_conflict",
                    ("billing.addon_contract_backfill.incomplete_idempotency_evidence"),
                    ("billing.addon_contract_backfill.missing_idempotency_key"),
                    ("billing.addon_contract_backfill.missing_sales_order_anchor"),
                    "billing.addon_contract_backfill.mixed_currency_addon",
                    "billing.addon_contract_backfill.partial_period_addon",
                    "billing.addon_contract_backfill.stale_preview",
                ),
                mapping_owner="billing migration adapters",
                fail_closed_on=(
                    "ambiguous or mixed-currency recurring price",
                    "partial-period add-on terms",
                    "stale preview or current contract version",
                    "missing structural SalesOrderLine anchor",
                ),
            ),
            events=EventContract(
                event_types=("billing.addon_contract_backfill.captured",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact source ids, quantities, price "
                    "ids, currency, intervals, target period, and the current "
                    "contract version identity."
                ),
                replay=(
                    "The idempotency row and staged output commit together; "
                    "billing.contracts receipts each event exactly once."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "direct reads of mutable SubscriptionAddOn and AddOnPrice "
                    "rows by legacy invoice and renewal paths"
                ),
                new_owner="billing.addon_contract_backfill",
                verification=(
                    "Focused preview, fail-closed, replay, owner-output, "
                    "contract-version, and obligation-chain tests."
                ),
                cutover_gate=(
                    "Every active recurring SubscriptionAddOn has one exact "
                    "BillingContractLine identity and shadow obligation; all "
                    "live add-on writers emit owner-backed billing-term "
                    "outputs and the temporary backfill owner is retired."
                ),
                fallback_retirement=(
                    "Delete the temporary producer after all legacy rows are "
                    "captured and cancellation, admin, route, sales, and "
                    "remediation writers emit owner-backed contract changes "
                    "atomically."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_billing_addon_contract_backfill.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="billing.contracts",
        module="app.services.billing.contracts",
        owns=(
            "versioned billing contract terms",
            "billing contract version supersession",
            "effective billing contract resolution",
            "period-scoped postpaid entitlement history",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "events.dispatcher",
            "events.owner_outputs",
            "financial.addon_purchases",
            "financial.tax_configuration",
            "runtime.durable_timers",
            "sales.orders",
            "sales.fulfillment",
        ),
        notes=(
            "ADR 0007 Phase 1. Customer-specific contracted terms are "
            "versioned and immutable; a catalog or policy change "
            "supersedes a version instead of rewriting history. Rows "
            "stay BillingRecordAuthority.shadow while this contract "
            "declares migration state 'shadowing', so nothing reads "
            "them as money before the Phase 1 cutover gate."
            " The receipted sales.fulfillment shadow output now creates "
            "the proposed version and atomically emits the obligation "
            "inputs; it still has no financial consequence."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="versioned billing contract terms",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "accepted commercial order line",
                        "canonical subscription projection",
                        "effective tax treatment inputs",
                        "recurring add-on migration output",
                        "live recurring add-on purchase output",
                        "recorded billing contract terms",
                        "receipted owner-output deliveries",
                        "exact pending-terms time trigger",
                    ),
                    canonical_writer="billing.contracts",
                ),
                ConcernContract(
                    name="billing contract version supersession",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "recorded billing contract terms",
                        "exact pending-terms time trigger",
                        "receipted owner-output deliveries",
                    ),
                    canonical_writer="billing.contracts",
                ),
                ConcernContract(
                    name="effective billing contract resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=("recorded billing contract terms",),
                ),
                ConcernContract(
                    name="period-scoped postpaid entitlement history",
                    role=OwnerRole.RESOLVER,
                    input_names=("recorded billing contract terms",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="accepted commercial order line",
                    owner="sales.orders",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "accepted SalesOrderLine identity and negotiated "
                        "commercial terms"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription projection",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=("Subscription identity, account, and lifecycle state"),
                ),
                AuthorityInput(
                    name="effective tax treatment inputs",
                    owner="financial.tax_configuration",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="effective tax rate and treatment vocabulary",
                ),
                AuthorityInput(
                    name="recurring add-on migration output",
                    owner="billing.addon_contract_backfill",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "receipted exact recurring add-on source identities, "
                        "terms, current contract version, and future period "
                        "boundary from the confirmed migration snapshot"
                    ),
                ),
                AuthorityInput(
                    name="live recurring add-on purchase output",
                    owner="financial.addon_purchases",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "receipted exact SubscriptionAddOn, AddOnPrice, "
                        "quantity, price, currency, cadence, and purchase "
                        "instant from the live owner transition"
                    ),
                ),
                AuthorityInput(
                    name="exact pending-terms time trigger",
                    owner="runtime.durable_timers",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "fired billing-contract timer id, generation, due "
                        "boundary, and expected draft version"
                    ),
                ),
                AuthorityInput(
                    name="receipted owner-output deliveries",
                    owner="events.owner_outputs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the exact sales.fulfillment output and unique "
                        "(billing.contracts, event_id) receipt"
                    ),
                ),
                AuthorityInput(
                    name="recorded billing contract terms",
                    owner="billing.contracts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "billing_contracts, billing_contract_versions, and "
                        "billing_contract_lines rows"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Adapters own the session; record_version, cancel_version, "
                    "consume_sales_funding, and "
                    "consume_recurring_addon_backfill, "
                    "consume_recurring_addon_purchase, and "
                    "consume_pending_terms_effective_due each enter "
                    "execute_owner_command once on a transaction-free "
                    "session."
                ),
                locking=(
                    "The BillingContract row and the current effective "
                    "version are locked FOR UPDATE; live purchase outputs "
                    "coalesce additively into one locked next-boundary draft "
                    "and replace its exact durable timer generation."
                ),
                idempotency=(
                    "One version per (contract, business idempotency key). "
                    "Owner-output receipts prevent duplicate draft changes "
                    "and timer triggers; direct version replay returns the "
                    "recorded version without writing a second."
                ),
                retries=(
                    "The complete command is retryable. A unique-constraint "
                    "loss fails closed rather than duplicating terms."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "billing.contracts.active_caller_transaction",
                    "billing.contracts.command_contract_violation",
                    "billing.contracts.contract_account_mismatch",
                    "billing.contracts.contract_not_found",
                    "billing.contracts.contract_version_not_found",
                    "billing.contracts.duplicate_contract_line",
                    "billing.contracts.duplicate_subscription_output",
                    "billing.contracts.invalid_command_context",
                    "billing.contracts.invalid_contract_terms",
                    "billing.contracts.invalid_addon_period",
                    "billing.contracts.invalid_addon_purchase_time",
                    "billing.contracts.invalid_addon_terms",
                    "billing.contracts.invalid_pending_contract_boundary",
                    "billing.contracts.invalid_pending_terms_timer",
                    "billing.contracts.missing_idempotency_key",
                    "billing.contracts.mixed_currency_contract",
                    "billing.contracts.nested_owner_command",
                    "billing.contracts.nested_transaction_completion",
                    "billing.contracts.out_of_order_contract_version",
                    "billing.contracts.sales_order_anchor_mismatch",
                    "billing.contracts.stale_pending_contract_terms",
                    "billing.contracts.stale_addon_snapshot",
                    "billing.contracts.unsupported_addon_cadence",
                    "billing.contracts.ambiguous_pending_contract_terms",
                    "billing.contracts.duplicate_addon_term_conflict",
                    "billing.contracts.pending_contract_version_not_found",
                ),
                mapping_owner="billing and sales adapters",
                fail_closed_on=(
                    "mixed currency between contract and line",
                    "an add-on cadence differing from the service cadence",
                    "a version starting before the current effective one",
                    "a stale draft or timer generation",
                    "missing business idempotency key",
                ),
            ),
            events=EventContract(
                event_types=("billing.contracts.shadow_recorded",),
                schema_version=2,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 2 carries only contract-version, line, and "
                    "period identity; billing.obligations resolves money "
                    "through billing.rating. The consumer accepts legacy "
                    "Version 1 envelopes but never trusts their amount fields. "
                    "Live add-on activation uses the subscription envelope "
                    "while preserving the opening SalesOrder anchor as "
                    "migration evidence."
                ),
                replay=(
                    "The sales or add-on-backfill output receipt, contract "
                    "rows, or live purchase receipt, draft timer and due "
                    "receipt, and staged "
                    "billing.contracts.shadow_recorded output commit in "
                    "one owner transaction. Redelivery is an exact no-op."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "Subscription.billing_mode/billing_cycle/unit_price plus "
                    "catalog offer price cadence and account billing mode"
                ),
                new_owner="billing.contracts",
                verification=(
                    "Contract version, supersession, cadence, currency, and "
                    "idempotency tests plus the ADR 0007 ratchet guards."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 1 gate: every accepted order or service "
                    "change creates one structural contract, every active "
                    "subscription has one proposed effective version, and "
                    "the ambiguous and unexpected-unlinked cohorts are zero."
                ),
                fallback_retirement=(
                    "Duplicate account/catalog effective billing-mode reads "
                    "and metadata Sale-to-Money joins are removed once the "
                    "Phase 1 gate passes."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_billing_contracts.py",
                "tests/test_customer_service_level.py",
                "tests/test_billing_addon_contract_backfill.py",
                "tests/test_api_me_addons.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="billing.obligations",
        module="app.services.billing.obligations",
        owns=(
            "unique billing obligation identity",
            "immutable obligation rating provenance",
            "billing obligation state transition",
        ),
        depends_on=(
            "billing.contracts",
            "billing.rating",
            "events.dispatcher",
            "events.owner_outputs",
        ),
        notes=(
            "ADR 0007 Phase 1. The obligation is the finite billable "
            "unit. Its natural identity is enforced by a database unique "
            "constraint so replay and concurrency produce one obligation "
            "rather than a duplicate charge. An obligation is not an "
            "invoice, a payment, or an entitlement, and its state is "
            "never inferred from an invoice label or payment origin."
            " Phase 1 consumes the contract owner's output through a "
            "receipt and emits a terminal shadow result atomically. "
            "Phase 2 resolves every amount through billing.rating; producer "
            "payloads carry identity, never a parallel money formula. New "
            "obligations snapshot complete versioned rating inputs and replay "
            "from that immutable snapshot without consulting current tax "
            "configuration. Pre-snapshot rows remain explicitly incomplete."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="unique billing obligation identity",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "recorded billing contract terms",
                        "recorded billing obligations",
                        "deterministic target rating",
                        "receipted owner-output deliveries",
                    ),
                    canonical_writer="billing.obligations",
                ),
                ConcernContract(
                    name="immutable obligation rating provenance",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "recorded billing contract terms",
                        "deterministic target rating",
                        "recorded billing obligations",
                    ),
                    canonical_writer="billing.obligations",
                ),
                ConcernContract(
                    name="billing obligation state transition",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("recorded billing obligations",),
                    canonical_writer="billing.obligations",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="recorded billing contract terms",
                    owner="billing.contracts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "effective BillingContractVersion cadence, currency, "
                        "and line identity"
                    ),
                ),
                AuthorityInput(
                    name="receipted owner-output deliveries",
                    owner="events.owner_outputs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the exact billing.contracts output and unique "
                        "(billing.obligations, event_id) receipt"
                    ),
                ),
                AuthorityInput(
                    name="deterministic target rating",
                    owner="billing.rating",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "typed net, tax, gross, currency, versioned policy, "
                        "coverage, cadence, tax source/value, and input "
                        "fingerprint for the exact line and period"
                    ),
                ),
                AuthorityInput(
                    name="recorded billing obligations",
                    owner="billing.obligations",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "billing_obligations rows, natural identity, rated "
                        "result, and immutable rating replay provenance"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Adapters own the session; schedule, open, resolve, "
                    "and consume_contract_shadow each enter "
                    "execute_owner_command once on a transaction-free "
                    "session."
                ),
                locking=(
                    "The contract version is locked before an obligation is "
                    "inserted and the obligation row is locked before any "
                    "state transition or application."
                ),
                idempotency=(
                    "The natural identity unique constraint is the "
                    "guarantee. A replay returns the existing obligation "
                    "only after reproducing its recorded result from its "
                    "fingerprinted provenance. New coverage for the same "
                    "identity and incomplete legacy provenance fail closed."
                ),
                retries=(
                    "The complete command is retryable. Applications can "
                    "never exceed the obligation's gross amount."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "billing.obligations.active_caller_transaction",
                    "billing.obligations.command_contract_violation",
                    "billing.obligations.contract_line_not_found",
                    "billing.obligations.contract_version_not_found",
                    "billing.obligations.duplicate_obligation",
                    "billing.obligations.incomplete_rating_provenance",
                    "billing.obligations.invalid_command_context",
                    "billing.obligations.invalid_obligation_amount",
                    "billing.obligations.invalid_obligation_transition",
                    "billing.obligations.missing_idempotency_key",
                    "billing.obligations.nested_owner_command",
                    "billing.obligations.nested_transaction_completion",
                    "billing.obligations.obligation_not_found",
                    "billing.obligations.period_outside_contract_version",
                    "billing.obligations.rating_provenance_conflict",
                    "billing.obligations.recorded_rating_provenance_invalid",
                    "billing.obligations.recorded_rating_result_mismatch",
                    "billing.obligations.resolution_exceeds_obligation",
                ),
                mapping_owner="billing, invoicing, and collections adapters",
                fail_closed_on=(
                    "a duplicate natural identity under concurrency",
                    "missing, corrupt, or conflicting rating provenance",
                    "a stored result that cannot be reproduced from its "
                    "recorded inputs",
                    "an application exceeding the obligation gross amount",
                    "a period outside the contract version interval",
                ),
            ),
            events=EventContract(
                event_types=("billing.obligations.shadow_scheduled",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive. New outputs include the rating "
                    "input fingerprint; consumers validate obligation "
                    "identity and never re-decide state or money."
                ),
                replay=(
                    "The contract-output receipt, obligations, and staged "
                    "terminal shadow result commit in one owner transaction."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "postpaid invoice-period generation and monthly-specific "
                    "prepaid renewal decision forks"
                ),
                new_owner="billing.obligations",
                verification=(
                    "Natural-identity uniqueness, immutable-input replay "
                    "after tax mutation, coverage conflict, fingerprint "
                    "integrity, calendar period, and state-transition tests."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 2 gate: exact period and amount parity "
                    "against current invoice generation and prepaid renewal "
                    "for the active cohort; complete reproducible provenance "
                    "for every included obligation; and zero duplicate, "
                    "gapped, or overlapping obligations outside typed policy."
                ),
                fallback_retirement=(
                    "Independent _period_end and monthly-only renewal "
                    "calculations are removed once invoices and prepaid "
                    "flows consume obligations."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_billing_obligations.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="billing.shadow_verification",
        module="app.services.billing.shadow_verification",
        owns=(
            "shadow pipeline delivery evidence",
            "phase cutover verification evidence",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "billing.contracts",
            "billing.obligations",
            "billing.rating",
            "customer.accounts",
            "events.dispatcher",
            "events.owner_outputs",
            "financial.billing_automation",
            "financial.customer_subledger",
            "financial.prepaid_funding_reconstruction",
            "financial.prepaid_service_renewals",
        ),
        notes=(
            "ADR 0007 migration evidence only. The owner receipts the "
            "terminal Sale→Contract→Obligation shadow output and records "
            "content-addressed delivery evidence. Complete-cohort runs "
            "store source/result fingerprints, exhaustive blocker "
            "classifications, currency totals, delivery outcomes, and "
            "code/schema identity. Phase 2 adds current-owner preview and "
            "target rating totals plus explicit expected-difference, gap, "
            "and overlap categories. Postpaid comparison consumes the "
            "current owner's complete base-plus-recurring-add-on component "
            "result and exact SubscriptionAddOn component identities. "
            "Prepaid comparison preserves the current owner's explicit "
            "add-on exclusions as blockers. It never repairs another owner, "
            "asks a non-owner to repair, or changes authority; operator and "
            "finance approvals are separate commands and are forbidden while "
            "blockers remain. Phase 3 may derive an opening target without "
            "Splynx only when account provenance proves the customer was "
            "created after the fixed legacy handoff with no Splynx identity. "
            "The zero history component and canonical native facts are "
            "fingerprinted before normal approval and capture. After "
            "authority activation, a separate single-account preview derives "
            "the immutable original cutoff and evaluates only the explicitly "
            "selected eligible native account; it cannot satisfy initial "
            "complete-cohort cutover evidence."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="shadow pipeline delivery evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "terminal shadow obligation output",
                        "receipted owner-output deliveries",
                        "recorded shadow verification evidence",
                    ),
                    canonical_writer="billing.shadow_verification",
                ),
                ConcernContract(
                    name="phase cutover verification evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "complete active subscription cohort",
                        "recorded billing contract terms",
                        "recorded billing obligations",
                        "deterministic target rating",
                        "current postpaid billing preview",
                        "current prepaid renewal preview",
                        "verified prepaid opening targets",
                        "recorded customer postings",
                        "receipted owner-output deliveries",
                        "recorded shadow verification evidence",
                    ),
                    canonical_writer="billing.shadow_verification",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="terminal shadow obligation output",
                    owner="billing.obligations",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "billing.obligations.shadow_scheduled with exact "
                        "sales order and obligation identities"
                    ),
                ),
                AuthorityInput(
                    name="complete active subscription cohort",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "all active Subscription roots locked and classified "
                        "at the verification cutoff"
                    ),
                ),
                AuthorityInput(
                    name="recorded billing contract terms",
                    owner="billing.contracts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "current proposed BillingContractVersion rows for "
                        "the complete active cohort"
                    ),
                ),
                AuthorityInput(
                    name="recorded billing obligations",
                    owner="billing.obligations",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "shadow BillingObligation natural identities, "
                        "periods, rating values, and topology"
                    ),
                ),
                AuthorityInput(
                    name="deterministic target rating",
                    owner="billing.rating",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "typed net, tax, gross, currency, rate-unit, and "
                        "proration result for the exact target period"
                    ),
                ),
                AuthorityInput(
                    name="current postpaid billing preview",
                    owner="financial.billing_automation",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "typed current-owner period, base service, recurring "
                        "add-on identities, net/tax/gross components, and "
                        "unsafe exclusion issues for each postpaid cohort root"
                    ),
                ),
                AuthorityInput(
                    name="current prepaid renewal preview",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "typed current-owner monthly period, taxed base "
                        "renewal, and explicit recurring-add-on exclusions "
                        "for each prepaid cohort root"
                    ),
                ),
                AuthorityInput(
                    name="verified prepaid opening targets",
                    owner="financial.prepaid_funding_reconstruction",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "reviewed reconstruction/opening positions, or a "
                        "typed native-after-handoff target proven from the "
                        "account creation instant, absent Splynx identity, "
                        "fixed handoff, and canonical native financial facts; "
                        "post-cutover single-account evidence is bounded at "
                        "the original authority verification cutoff"
                    ),
                ),
                AuthorityInput(
                    name="recorded customer postings",
                    owner="financial.customer_subledger",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "currency-typed shadow posting lanes compared with "
                        "the exact opening target at the verification cutoff"
                    ),
                ),
                AuthorityInput(
                    name="receipted owner-output deliveries",
                    owner="events.owner_outputs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique consumer receipts and durable EventStore "
                        "delivery outcomes"
                    ),
                ),
                AuthorityInput(
                    name="recorded shadow verification evidence",
                    owner="billing.shadow_verification",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "billing_shadow_delivery_evidence and "
                        "billing_cutover_verification_runs"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each terminal consumption, complete-cohort run, explicit "
                    "post-cutover account run, or approval enters "
                    "execute_owner_command once on a transaction-free session."
                ),
                locking=(
                    "Terminal delivery uniqueness is database-enforced; "
                    "verification locks the complete selected Subscription, "
                    "contract-version, and obligation cohort; approvals lock "
                    "one run. Post-cutover capture separately locks and "
                    "revalidates the one selected customer account."
                ),
                idempotency=(
                    "One terminal evidence row per event and one run per "
                    "business idempotency key. The post-cutover account ID is "
                    "part of run identity; replays return stored evidence."
                ),
                retries=(
                    "Delivery and run commands are retryable. Expected new-"
                    "cadence differences require explicit approval; approval "
                    "fails closed until every blocker count is zero."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "billing.shadow_verification.active_caller_transaction",
                    "billing.shadow_verification.approval_already_recorded",
                    "billing.shadow_verification.command_contract_violation",
                    "billing.shadow_verification.idempotency_conflict",
                    "billing.shadow_verification.invalid_approval",
                    "billing.shadow_verification.invalid_command_context",
                    "billing.shadow_verification.invalid_observation_window",
                    "billing.shadow_verification.invalid_run_identity",
                    "billing.shadow_verification.missing_idempotency_key",
                    "billing.shadow_verification.nested_owner_command",
                    "billing.shadow_verification.nested_transaction_completion",
                    "billing.shadow_verification.operator_approval_required",
                    ("billing.shadow_verification.opening_position_already_captured"),
                    "billing.shadow_verification.account_not_found",
                    ("billing.shadow_verification.account_not_in_funding_cohort"),
                    "billing.shadow_verification.corrupt_authority_evidence",
                    "billing.shadow_verification.opening_not_required",
                    (
                        "billing.shadow_verification."
                        "post_cutover_scope_requires_native_account"
                    ),
                    ("billing.shadow_verification.post_cutover_scope_unavailable"),
                    ("billing.shadow_verification.shadow_fact_after_authority_cutoff"),
                    "billing.shadow_verification.source_cohort_incomplete",
                    "billing.shadow_verification.stale_reviewed_preview",
                    "billing.shadow_verification.verification_blockers_present",
                    "billing.shadow_verification.verification_run_not_found",
                ),
                mapping_owner="billing migration operator adapters",
                fail_closed_on=(
                    "an incomplete or non-timezone-aware observation window",
                    "any unresolved, ambiguous, unlinked, duplicate, gap, "
                    "overlap, or variance category",
                    "a selected account outside the native post-cutover "
                    "completion contract or changed after preview",
                    "finance approval without operator approval",
                ),
            ),
            events=EventContract(
                event_types=(
                    "billing.shadow_delivery.recorded",
                    "billing.cutover_verification.recorded",
                    "billing.cutover_verification.approved",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries evidence identity, immutable "
                    "fingerprints/counts, or one explicit approval kind."
                ),
                replay=(
                    "Delivery evidence and runs are idempotent on terminal "
                    "event or business key; approvals update only the named "
                    "run and emit their exact actor and instant."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner="WARNING logs and ad-hoc billing comparison output",
                new_owner="billing.shadow_verification",
                verification=(
                    "Terminal receipt replay, Phase 1/2 complete-cohort "
                    "classification, fingerprints, current/target currency "
                    "totals, topology blockers, and approval-gate tests."
                ),
                cutover_gate=(
                    "A phase-specific durable run meets ADR 0007's evidence "
                    "standard, all blockers are zero, expected differences "
                    "are explicitly reviewed, and operator and finance "
                    "approvals are recorded. Evidence does not move authority."
                ),
                fallback_retirement=(
                    "Log-only shadow completion and undocumented cutover "
                    "claims are rejected."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_billing_shadow_pipeline.py",
                "tests/test_billing_phase2_shadow.py",
                "tests/test_subledger_opening_positions.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="billing.rating",
        module="app.services.billing.rating",
        owns=("deterministic obligation rating",),
        depends_on=(
            "billing.contracts",
            "financial.tax_configuration",
        ),
        notes=(
            "ADR 0007 Phase 2. Read-only policy/resolver: the same "
            "contract version, line, period, coverage, and tax inputs "
            "always produce the same typed rated result and content-addressed "
            "provenance. Recorded provenance replays through its named policy "
            "without reading mutable current tax configuration. A contracted "
            "tax code with zero or multiple active rates fails closed."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="deterministic obligation rating",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "recorded billing contract terms",
                        "effective tax treatment inputs",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="recorded billing contract terms",
                    owner="billing.contracts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "effective BillingContractVersion cadence, lines, "
                        "price, currency, and tax inputs"
                    ),
                ),
                AuthorityInput(
                    name="effective tax treatment inputs",
                    owner="financial.tax_configuration",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active TaxRate records addressed by code",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller owns the session; rating reads contract and "
                    "tax records and completes no transaction."
                ),
                locking=(
                    "No read lock. The obligation owner locks its own "
                    "rows before recording a rated result."
                ),
                idempotency=(
                    "Deterministic: identical versioned policy, line, period, "
                    "coverage, cadence, price, and tax inputs produce an "
                    "identical fingerprint and typed result. Recorded policy "
                    "versions remain replayable when a new policy is added."
                ),
                retries=(
                    "Transient reads may be retried; a missing named tax "
                    "code remains a deterministic fail-closed error."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "billing.rating.ambiguous_tax_treatment",
                    "billing.rating.contract_line_not_found",
                    "billing.rating.contract_version_not_found",
                    "billing.rating.invalid_rating_provenance",
                    "billing.rating.rating_provenance_fingerprint_mismatch",
                    "billing.rating.unknown_tax_treatment",
                    "billing.rating.unsupported_policy_version",
                    "billing.rating.usage_rating_requires_observation",
                ),
                mapping_owner="billing and invoicing adapters",
                fail_closed_on=(
                    "a contracted tax treatment code with zero or multiple "
                    "active rates",
                    "corrupt or unsupported recorded rating provenance",
                    "usage-metered rating without an observed quantity",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "invoice-generation amount arithmetic and prepaid "
                    "renewal price resolution spread across billing tasks"
                ),
                new_owner="billing.rating",
                verification=(
                    "Deterministic rating, content-addressed replay, tax "
                    "mutation, proration, tax-inclusive, ambiguous-tax, and "
                    "fail-closed tests plus the ADR 0007 guards."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 2 gate: rated totals match current "
                    "postpaid invoice generation and prepaid renewal "
                    "previews for the complete active cohort and every "
                    "included obligation has complete replayable provenance."
                ),
                fallback_retirement=(
                    "Parallel money formulas in invoice generation and "
                    "renewal paths are removed once flows consume rated "
                    "obligations."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_billing_rating.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
)
