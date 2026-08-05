"""financial_access SOT declarations: payment intents."""

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
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="financial.topup_intents",
        module="app.services.topup_intents",
        owns=(
            "direct bank-transfer availability and configured-account projection",
            "invoice direct-transfer intent record creation and replacement",
            "direct-transfer top-up intent proof submission transition",
            "direct-transfer reviewed-proof resolution projection",
            "gateway invoice and reseller checkout intent record creation",
            "saved-card top-up intent failure projection",
            "top-up intent completed-payment projection",
            "gateway top-up intent expiry decision",
        ),
        depends_on=(
            "control.feature_registry",
            "control.settings_spec",
            "customer.accounts",
            "events.dispatcher",
            "financial.billing_accounts",
            "financial.collection_accounts",
            "financial.payments",
        ),
        notes=(
            "The participant derives direct-transfer availability from canonical "
            "active collection-account destinations and customer "
            "instructions. It is the canonical invoice-intent, proof-link, "
            "reviewed-proof resolution, completed-payment, and gateway-expiry "
            "projection writer. Cash remains authoritative in the payment owner; "
            "callers compose or idempotently repair the intent projection without "
            "parallel field writers."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=(
                        "direct bank-transfer availability and configured-account "
                        "projection"
                    ),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical direct-transfer bank destinations",
                        "canonical direct-transfer customer instructions",
                    ),
                ),
                ConcernContract(
                    name=(
                        "invoice direct-transfer intent record creation and replacement"
                    ),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical direct-transfer top-up intent",
                        "direct-transfer creation command evidence",
                        "top-up intent transition protocol",
                    ),
                    canonical_writer="financial.topup_intents",
                ),
                ConcernContract(
                    name=("direct-transfer top-up intent proof submission transition"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical direct-transfer top-up intent",
                        "direct-transfer proof-link command evidence",
                        "top-up intent transition protocol",
                    ),
                    canonical_writer="financial.topup_intents",
                ),
                ConcernContract(
                    name=("direct-transfer reviewed-proof resolution projection"),
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "canonical direct-transfer top-up intent",
                        "typed reviewed-proof resolution evidence",
                        "canonical succeeded payment evidence",
                        "top-up intent transition protocol",
                    ),
                    canonical_writer="financial.topup_intents",
                ),
                ConcernContract(
                    name=(
                        "gateway invoice and reseller checkout intent record creation"
                    ),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical gateway checkout creation evidence",
                        "top-up intent transition protocol",
                    ),
                    canonical_writer="financial.topup_intents",
                ),
                ConcernContract(
                    name="saved-card top-up intent failure projection",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical top-up intent projection target",
                        "typed saved-card failure evidence",
                        "top-up intent transition protocol",
                    ),
                    canonical_writer="financial.topup_intents",
                ),
                ConcernContract(
                    name="top-up intent completed-payment projection",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical top-up intent projection target",
                        "canonical succeeded payment evidence",
                        "typed top-up intent completion evidence",
                    ),
                    canonical_writer="financial.topup_intents",
                ),
                ConcernContract(
                    name="gateway top-up intent expiry decision",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical top-up intent projection target",
                        "canonical top-up reconciliation expiry policy",
                        "typed gateway expiry observation",
                    ),
                    canonical_writer="financial.topup_intents",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical direct-transfer bank destinations",
                    owner="financial.collection_accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active, complete, currency-matched collection-account "
                        "identities in explicit customer-presentment order"
                    ),
                ),
                AuthorityInput(
                    name="canonical direct-transfer customer instructions",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed billing-domain customer transfer instructions with "
                        "a checked-in default"
                    ),
                ),
                AuthorityInput(
                    name="canonical direct-transfer top-up intent",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked TopupIntent identity, subscriber account, provider, "
                        "reference, requested amount, status, and metadata evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical top-up intent projection target",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked TopupIntent identity, subscriber or billing-account "
                        "scope, provider, currency, status, expiry, and current "
                        "completed-payment projection"
                    ),
                ),
                AuthorityInput(
                    name="canonical gateway checkout creation evidence",
                    owner="financial.gateway_topup_intent_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed invoice or reseller scope, provider, reference, "
                        "amount, currency, lifetime, actor, and flow evidence"
                    ),
                ),
                AuthorityInput(
                    name="typed saved-card failure evidence",
                    owner="financial.gateway_topup_intent_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed intent identity, named failure source, reason code, "
                        "and correlated command context"
                    ),
                ),
                AuthorityInput(
                    name="canonical succeeded payment evidence",
                    owner="financial.payments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active succeeded Payment scope, provider, gross "
                        "amount, currency, external transaction, and paid timestamp"
                    ),
                ),
                AuthorityInput(
                    name="typed top-up intent completion evidence",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed intent/payment identities and named completion source "
                        "with correlated command context"
                    ),
                ),
                AuthorityInput(
                    name="canonical top-up reconciliation expiry policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "bounded database-authoritative expiry grace setting plus "
                        "the intent's canonical expiry timestamp"
                    ),
                ),
                AuthorityInput(
                    name="typed gateway expiry observation",
                    owner="external:payment_provider",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "provider not-found or definitive unsuccessful verification "
                        "evidence normalized with its observation time"
                    ),
                ),
                AuthorityInput(
                    name="direct-transfer creation command evidence",
                    owner="financial.direct_transfer_intent_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed account, invoice, amount, actor, command, correlation, "
                        "and idempotency evidence admitted by the creation coordinator"
                    ),
                ),
                AuthorityInput(
                    name="direct-transfer proof-link command evidence",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed PaymentProof identity, selected configured bank "
                        "account snapshot, and correlated CommandContext admitted "
                        "by the payment-proof owner"
                    ),
                ),
                AuthorityInput(
                    name="typed reviewed-proof resolution evidence",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "exact linked PaymentProof identity, closed verified or "
                        "rejected outcome, optional canonical Payment identity, "
                        "named review or reconciliation source, and correlated "
                        "CommandContext supplied by the payment-proof owner"
                    ),
                ),
                AuthorityInput(
                    name="top-up intent transition protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "canonical provider identity, status vocabulary, pending-to-"
                        "submitted eligibility, exact proof-link uniqueness, "
                        "submitted-to-completed/canceled reviewed-proof resolution, "
                        "late-payment recovery, and event vocabulary"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "A creation, payment-proof, payment-settlement, webhook, portal, "
                    "reseller, or reconciliation caller supplies the transaction; "
                    "this participant stages intent creation, replacement, proof "
                    "submission/resolution, failure/completion/expiry projection, "
                    "and events without "
                    "committing or rolling back. Cash-first payment owners may commit confirmed "
                    "money before invoking this idempotent repairable projection."
                ),
                locking=(
                    "Creation holds the canonical account lock before pending intent "
                    "replacement. Gateway creation locks the account or billing "
                    "account before its reference; proof submission and resolution "
                    "lock the exact intent. Completion "
                    "locks subscriber or billing-account scope, exact intent, then "
                    "succeeded Payment before scope/provider/currency/link evidence "
                    "is rechecked; expiry uses the same scope and intent locks."
                ),
                idempotency=(
                    "A stable creation key replays the matching pending invoice "
                    "intent; a fresh creation explicitly cancels prior pending "
                    "attempts. One pending intent accepts one proof link. Exact "
                    "verified/rejected proof-resolution replay performs no second "
                    "transition or event, while changed outcome, proof, or payment "
                    "evidence conflicts. Replaying the same succeeded Payment or "
                    "expired state performs no second field transition or event."
                ),
                retries=(
                    "Only the caller retries after rollback. If cash was already "
                    "committed, webhook or scheduled reconciliation safely replays "
                    "the projection from canonical Payment evidence; this participant "
                    "never retries or completes a transaction independently."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.topup_intents.invalid_status",
                    "financial.topup_intents.invalid_bank_account_evidence",
                    "financial.topup_intents.not_found",
                    "financial.topup_intents.account_mismatch",
                    "financial.topup_intents.provider_mismatch",
                    "financial.topup_intents.invalid_transition",
                    "financial.topup_intents.proof_link_conflict",
                    "financial.topup_intents.proof_resolution_link_mismatch",
                    "financial.topup_intents.proof_resolution_conflict",
                    "financial.topup_intents.proof_resolution_payment_required",
                    "financial.topup_intents.proof_resolution_payment_forbidden",
                    "financial.topup_intents.amount_non_positive",
                    "financial.topup_intents.idempotency_key_invalid",
                    "financial.topup_intents.idempotency_conflict",
                    "financial.topup_intents.billing_account_not_found",
                    "financial.topup_intents.scope_missing",
                    "financial.topup_intents.payment_not_found",
                    "financial.topup_intents.payment_not_succeeded",
                    "financial.topup_intents.payment_scope_mismatch",
                    "financial.topup_intents.payment_currency_mismatch",
                    "financial.topup_intents.payment_provider_mismatch",
                    "financial.topup_intents.payment_amount_invalid",
                    "financial.topup_intents.completion_conflict",
                    "financial.topup_intents.external_id_conflict",
                    "financial.topup_intents.expiry_grace_invalid",
                    "financial.topup_intents.expiry_time_invalid",
                    "financial.topup_intents.gateway_scope_invalid",
                    "financial.topup_intents.gateway_identity_invalid",
                    "financial.topup_intents.gateway_currency_invalid",
                    "financial.topup_intents.gateway_expiry_invalid",
                    "financial.topup_intents.gateway_reference_conflict",
                ),
                mapping_owner=(
                    "payment, webhook, portal, reseller, and reconciliation adapters"
                ),
                retryable_codes=(),
                fail_closed_on=(
                    "missing or wrong-account intent identity",
                    "non-direct-transfer provider identity",
                    "incomplete configured-bank evidence",
                    "non-pending intent lifecycle state",
                    "an existing proof evidence link",
                    "a reviewed proof that does not exactly match the intent link",
                    "conflicting reviewed-proof outcome or payment evidence",
                    "missing or conflicting creation idempotency evidence",
                    "missing, inactive, unsettled, wrong-scope, wrong-currency, or "
                    "wrong-provider payment evidence",
                    "a conflicting completed-payment or external transaction link",
                    "invalid expiry evidence or non-pending expiry transition",
                ),
            ),
            events=EventContract(
                event_types=(
                    "topup_intent.direct_transfer_created",
                    "topup_intent.direct_transfer_canceled",
                    "topup_intent.direct_transfer_submitted",
                    "topup_intent.direct_transfer_proof_rejected",
                    "topup_intent.completed",
                    "topup_intent.expired",
                    "topup_intent.gateway_created",
                    "topup_intent.failed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 retains intent, optional proof/replacement/payment, "
                    "account scope, provider, flow/source, status, amount/time, "
                    "command, and correlation evidence; evolution is additive."
                ),
                replay=(
                    "Event-store rows are replayable for projections and delivery. "
                    "Event replay never mutates TopupIntent or creates intent, proof, "
                    "or payment evidence. Command replay idempotently repairs the "
                    "projection from the canonical Payment."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="direct-transfer reviewed-proof intent resolution",
                    input_names=(
                        "canonical direct-transfer top-up intent",
                        "typed reviewed-proof resolution evidence",
                        "canonical succeeded payment evidence",
                        "top-up intent transition protocol",
                    ),
                    writer="financial.topup_intents",
                    freshness=(
                        "Synchronous with payment-proof review; exact historical "
                        "drift is repaired by the named reconciliation owner."
                    ),
                    stale_behavior=(
                        "A terminal proof with a submitted exact-linked intent is "
                        "reported as drift. Ambiguous or non-current payment evidence "
                        "is quarantined without changing money or intent state."
                    ),
                    drift_signal=(
                        "A direct-transfer TopupIntent remains submitted while its "
                        "exact metadata-linked PaymentProof is verified or rejected."
                    ),
                    rebuild_operation=(
                        "Run reconcile_topup_intent_proofs and apply only exact "
                        "complete or cancel candidates through the canonical writer."
                    ),
                    repair_owner=("financial.topup_intent_proof_reconciliation"),
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "customer portal invoice-intent construction/replacement plus "
                    "follow-up proof-link mutation and completion/expiry field writes "
                    "spread across deposit, webhook, portal, reconciliation, and "
                    "reseller services"
                ),
                new_owner="financial.topup_intents",
                verification=(
                    "Configured-account projection, atomic create/replace/proof-link, "
                    "typed reviewed-proof completion/rejection, completion/expiry "
                    "success, idempotent replay/repair, rollback, mismatch rejection, "
                    "caller, event, and architecture tests."
                ),
                cutover_gate=(
                    "Every reviewed-proof/completion/expiry caller supplies typed "
                    "evidence; only this participant writes canceled/completed/"
                    "expired intent status, completion identity, provider evidence, "
                    "amount/time, proof-resolution metadata, and lifecycle events."
                ),
                fallback_retirement=(
                    "Portal-owned direct-transfer construction/replacement/proof "
                    "writes and all caller-owned completion/expiry field assignments "
                    "are removed; local reconciliation expiry constants are retired."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_payment_proofs.py",
                "tests/test_direct_transfer_intents.py",
                "tests/test_topup_intent_projection.py",
                "tests/test_payment_webhook_settlement.py",
                "tests/test_topup_intent_status.py",
                "tests/architecture/test_topup_intent_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.direct_transfer_intent_commands",
        module="app.services.direct_transfer_intents",
        owns=("customer direct-transfer intent creation coordination",),
        depends_on=(
            "control.settings_spec",
            "customer.accounts",
            "financial.customer_tax_policies",
            "financial.account_credit_deposits",
            "financial.invoices",
            "financial.topup_intents",
            "integration.installations",
        ),
        notes=(
            "This coordinator admits one typed customer creation command, resolves "
            "configuration plus customer-specific WHT policy from their owners, "
            "fails closed when invoice-linked WHT lacks an authoritative VAT-"
            "exclusive basis, and commits the selected canonical intent "
            "participant exactly once."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="customer direct-transfer intent creation coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated direct-transfer creation command",
                        "canonical customer account",
                        "canonical payable invoice",
                        "canonical customer WHT policy",
                        "canonical direct-transfer configuration",
                        "canonical direct-transfer lifetime and amount policy",
                        "canonical deposit intent protocol",
                        "canonical invoice direct-transfer intent protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated direct-transfer creation command",
                    owner="financial.direct_transfer_intent_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed account, optional invoice/amount, actor, scope, reason, "
                        "command, correlation, and optional idempotency evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer account",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked authenticated subscriber account identity",
                ),
                AuthorityInput(
                    name="canonical payable invoice",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked invoice account, lifecycle status, currency, and "
                        "current outstanding balance"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer WHT policy",
                    owner="financial.customer_tax_policies",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked customer withholding-tax eligibility, version, "
                        "and operator provenance"
                    ),
                ),
                AuthorityInput(
                    name="canonical direct-transfer configuration",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=("feature-controlled enabled configured-bank projection"),
                ),
                AuthorityInput(
                    name="canonical direct-transfer lifetime and amount policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed top-up minimum/maximum, direct-transfer lifetime, "
                        "and global WHT percentage settings with checked-in "
                        "defaults and bounds"
                    ),
                ),
                AuthorityInput(
                    name="canonical deposit intent protocol",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only deposit eligibility, preview, idempotency, and "
                        "typed TopupIntent staging"
                    ),
                ),
                AuthorityInput(
                    name="canonical invoice direct-transfer intent protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only invoice intent creation, explicit replacement, "
                        "idempotency, metadata, status, and event staging"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "execute_owner_command admits a transaction-free session, "
                    "resolves current policy, stages exactly one deposit or invoice "
                    "intent path plus events, and commits or rolls back once."
                ),
                locking=(
                    "Invoice creation locks the customer account then exact invoice "
                    "and pending direct-transfer attempts. Deposit creation uses the "
                    "account-credit owner's canonical account lock."
                ),
                idempotency=(
                    "A stable account-scoped digest of supplied idempotency evidence "
                    "replays the matching intent. Without a caller key, command_id "
                    "provides one-attempt identity; invoice replacement is explicit."
                ),
                retries=(
                    "Adapters retry only the complete command after rollback using "
                    "the same context/idempotency evidence. Domain conflicts fail "
                    "closed and are not partially retried."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.direct_transfer_intent_commands.unavailable",
                    "financial.direct_transfer_intent_commands.created_by_required",
                    "financial.direct_transfer_intent_commands.configuration_invalid",
                    "financial.direct_transfer_intent_commands.invoice_not_found",
                    "financial.direct_transfer_intent_commands.invoice_not_payable",
                    "financial.direct_transfer_intent_commands.currency_unsupported",
                    "financial.direct_transfer_intent_commands.amount_invalid",
                    "financial.direct_transfer_intent_commands.deposit_rejected",
                    "financial.direct_transfer_intent_commands.intent_conflict",
                    "financial.direct_transfer_intent_commands.record_invalid",
                    "financial.direct_transfer_intent_commands.invalid_command_context",
                    "financial.direct_transfer_intent_commands.command_contract_violation",
                    "financial.direct_transfer_intent_commands.nested_owner_command",
                    "financial.direct_transfer_intent_commands.active_caller_transaction",
                    "financial.direct_transfer_intent_commands.nested_transaction_completion",
                ),
                mapping_owner="customer portal and self-service API adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "disabled feature or no enabled configured bank",
                    "invalid lifetime, amount, or WHT rate policy",
                    "missing, wrong-account, draft, terminal, zero-balance, or "
                    "unsupported-currency invoice",
                    "missing customer WHT policy, missing authoritative VAT-exclusive "
                    "invoice basis, partial settlement, or inconsistent invoice tax "
                    "evidence for an enabled WHT customer",
                    "deposit policy rejection or concurrent intent conflict",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "topup_intent.direct_transfer_created",
                    "topup_intent.direct_transfer_canceled",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries canonical intent/account/flow identifiers, "
                    "replacement evidence, and command correlation only."
                ),
                replay=(
                    "Creation/cancellation events replay to projections only and "
                    "never re-enter the creation command."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "customer portal amount/limit/TTL decisions, direct TopupIntent "
                    "construction, pending replacement, and branch-local commits"
                ),
                new_owner="financial.direct_transfer_intent_commands",
                verification=(
                    "Configuration, deposit/invoice creation, idempotent replay, "
                    "replacement, rollback, event, portal, manifest, and "
                    "architecture tests."
                ),
                cutover_gate=(
                    "Customer portal direct-transfer creation constructs only a "
                    "typed command on a transaction-free session."
                ),
                fallback_retirement=(
                    "Portal feature/limit/TTL decisions, direct constructors, "
                    "replacement loops, and creation commits are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_direct_transfer_intents.py",
                "tests/test_customer_portal_topup_flow.py",
                "tests/architecture/test_topup_intent_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.topup_intent_proof_reconciliation",
        module="app.services.topup_intent_proof_reconciliation",
        owns=("submitted intent terminal-proof reconciliation",),
        depends_on=(
            "financial.payment_proofs",
            "financial.payments",
            "financial.topup_intents",
        ),
        notes=(
            "The read-only preview discovers only exact PaymentProof identities "
            "persisted in submitted direct-transfer intent metadata. One bounded "
            "owner command locks and rechecks the terminal proof, then composes "
            "the canonical intent participant. Rejected proofs cancel; verified "
            "proofs complete only from a current active succeeded Payment. "
            "Reversed/missing payment evidence remains quarantined."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="submitted intent terminal-proof reconciliation",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "canonical payment-proof review evidence",
                        "canonical direct-transfer top-up intent",
                        "canonical succeeded payment evidence",
                        "canonical reviewed-proof intent projection protocol",
                    ),
                    canonical_writer=("financial.topup_intent_proof_reconciliation"),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical payment-proof review evidence",
                    owner="financial.payment_proofs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked PaymentProof identity, account, reference, terminal "
                        "verified/rejected status, and optional resulting Payment link"
                    ),
                ),
                AuthorityInput(
                    name="canonical direct-transfer top-up intent",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked submitted TopupIntent identity, account, reference, "
                        "provider, exact metadata proof link, and current projection"
                    ),
                ),
                AuthorityInput(
                    name="canonical succeeded payment evidence",
                    owner="financial.payments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active succeeded Payment linked by the verified proof with "
                        "matching account, currency, provider, amount, and provenance"
                    ),
                ),
                AuthorityInput(
                    name="canonical reviewed-proof intent projection protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only exact-link completion/rejection, idempotency, "
                        "late-payment recovery, metadata, and event contract"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The operator adapter previews read-only, releases its session, "
                    "then each selected exact candidate enters one owner command on "
                    "a transaction-free session and composes only the canonical "
                    "top-up intent participant."
                ),
                locking=(
                    "The command locks PaymentProof first; the participant then locks "
                    "the canonical account scope, TopupIntent, and any succeeded "
                    "Payment before rechecking exact link and outcome evidence."
                ),
                idempotency=(
                    "Stable intent/proof/outcome command evidence replays the same "
                    "terminal projection without a second transition or event. "
                    "Changed proof status, payment link, or intent link fails closed."
                ),
                retries=(
                    "Transient owner-transaction failures may retry the same exact "
                    "candidate. Missing, reversed, inactive, changed, or ambiguous "
                    "evidence remains requires_review and is never guessed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.topup_intent_proof_reconciliation.active_caller_transaction",
                    "financial.topup_intent_proof_reconciliation.command_contract_violation",
                    "financial.topup_intent_proof_reconciliation.intent_not_found",
                    "financial.topup_intent_proof_reconciliation.invalid_command_context",
                    "financial.topup_intent_proof_reconciliation.limit_invalid",
                    "financial.topup_intent_proof_reconciliation.nested_owner_command",
                    "financial.topup_intent_proof_reconciliation.nested_transaction_completion",
                    "financial.topup_intent_proof_reconciliation.proof_intent_scope_mismatch",
                    "financial.topup_intent_proof_reconciliation.proof_not_found",
                    "financial.topup_intent_proof_reconciliation.proof_not_terminal",
                    "financial.topup_intent_proof_reconciliation.proof_payment_changed",
                    "financial.topup_intent_proof_reconciliation.proof_status_changed",
                    "financial.topup_intent_proof_reconciliation.scan_limit_invalid",
                ),
                mapping_owner=("scripts.one_off.reconcile_topup_intent_proofs"),
                retryable_codes=(),
                fail_closed_on=(
                    "missing or changed proof, intent, or payment identity",
                    "non-terminal proof or non-exact intent proof link",
                    "verified proof without a current active succeeded Payment",
                    "scope, currency, provider, outcome, or projection conflict",
                ),
            ),
            events=EventContract(
                event_types=(
                    "topup_intent.completed",
                    "topup_intent.direct_transfer_proof_rejected",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "The canonical intent events retain their existing additive "
                    "schema and identify reconciliation as the named source."
                ),
                replay=(
                    "Event replay never re-enters reconciliation; the exact proof "
                    "and intent records deterministically rebuild the projection."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="terminal proof to top-up intent projection",
                    input_names=(
                        "canonical payment-proof review evidence",
                        "canonical direct-transfer top-up intent",
                        "canonical succeeded payment evidence",
                        "canonical reviewed-proof intent projection protocol",
                    ),
                    writer=("financial.topup_intents"),
                    freshness=(
                        "Synchronous at proof review; the drift preview is suitable "
                        "for scheduled and operator invariant monitoring."
                    ),
                    stale_behavior=(
                        "Exact terminal-proof drift is reported and repairable; "
                        "missing/reversed payment or absent proof-link evidence stays "
                        "quarantined without changing money or intent state."
                    ),
                    drift_signal=(
                        "A direct-transfer TopupIntent remains submitted while its "
                        "exact metadata-linked PaymentProof is verified or rejected."
                    ),
                    rebuild_operation=(
                        "Run reconcile_topup_intent_proofs in dry-run mode, then "
                        "apply reviewed exact candidates through the owner command."
                    ),
                    repair_owner=("financial.topup_intent_proof_reconciliation"),
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUT_OVER,
                old_owner=(
                    "no canonical writer after payment-proof review plus ad hoc "
                    "status analysis or manual intent mutation"
                ),
                new_owner="financial.topup_intent_proof_reconciliation",
                verification=(
                    "Exact-link preview, verified/rejected repair, quarantine, "
                    "rollback, idempotency, live review, event, and architecture tests."
                ),
                cutover_gate=(
                    "New proof reviews synchronously terminalize exact linked intents "
                    "and the reviewed repair cohort contains no unexplained candidate."
                ),
                fallback_retirement=(
                    "Direct SQL status repair and account-level proof inference are "
                    "forbidden; only explicit requires_review exceptions may remain."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/ACCOUNT_CREDIT_DEPOSITS.md",
            ),
            test_refs=(
                "tests/test_topup_intent_proof_reconciliation.py",
                "tests/test_payment_proofs.py",
                "tests/architecture/test_topup_intent_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.customer_tax_policies",
        module="app.services.customer_tax_policies",
        owns=(
            "customer withholding-tax eligibility policy",
            "customer VAT exemption policy",
        ),
        depends_on=("customer.accounts", "events.dispatcher"),
        notes=(
            "This owner persists per-customer WHT eligibility as an audited "
            "financial policy. It does not own the global WHT rate, invoice tax "
            "basis, payment intent snapshot, or WHT lifecycle."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="customer withholding-tax eligibility policy",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "customer WHT policy command context",
                        "canonical customer account",
                        "canonical customer WHT policy record",
                    ),
                    canonical_writer="financial.customer_tax_policies",
                ),
                ConcernContract(
                    name="customer VAT exemption policy",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "customer VAT exemption command context",
                        "canonical customer account",
                        "canonical customer VAT exemption record",
                    ),
                    canonical_writer="financial.customer_tax_policies",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="customer WHT policy command context",
                    owner="financial.customer_tax_policies",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed actor, scope, reason, command, correlation, and "
                        "idempotency evidence for a customer WHT policy change"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer account",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked customer account identity and existence",
                ),
                AuthorityInput(
                    name="canonical customer WHT policy record",
                    owner="financial.customer_tax_policies",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique per-account WHT enablement flag, version, actor, and "
                        "updated timestamp"
                    ),
                ),
                AuthorityInput(
                    name="customer VAT exemption command context",
                    owner="financial.customer_tax_policies",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed actor, scope, reason, command, correlation, and "
                        "idempotency evidence for a customer VAT exemption change"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer VAT exemption record",
                    owner="financial.customer_tax_policies",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique per-account VAT exemption flag, shared policy "
                        "version, actor, and updated timestamp"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "set_customer_withholding_tax_policy or "
                    "set_customer_vat_exemption_policy enters "
                    "execute_owner_command once on a transaction-free session, "
                    "locks the target customer and policy row, stages one "
                    "versioned update, and commits or rolls back once."
                ),
                locking=(
                    "The owner locks the target Subscriber first, then the unique "
                    "CustomerTaxPolicy row for that account."
                ),
                idempotency=(
                    "Repeated commands with the same target policy state replay the "
                    "existing policy version. State changes increment the policy "
                    "version exactly once."
                ),
                retries=(
                    "Adapters retry only the full command after rollback with the "
                    "same context and idempotency evidence."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.customer_tax_policies.actor_required",
                    "financial.customer_tax_policies.account_not_found",
                    "financial.customer_tax_policies.invalid_command_context",
                    "financial.customer_tax_policies.command_contract_violation",
                    "financial.customer_tax_policies.nested_owner_command",
                    "financial.customer_tax_policies.active_caller_transaction",
                    "financial.customer_tax_policies.nested_transaction_completion",
                ),
                mapping_owner="admin customer billing adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "missing actor, missing customer account, or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=("customer_tax_policy.updated",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries account identity, changed policy state, "
                    "policy version, and actor provenance only."
                ),
                replay=(
                    "Replay may refresh projections or audit consumers only; it "
                    "never re-enters the policy command."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="financial.customer_tax_policies",
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/FRONTEND_SPEC.md",
                "docs/ACCOUNT_CREDIT_DEPOSITS.md",
            ),
            test_refs=(
                "tests/test_subscriber_billing_config.py",
                "tests/test_direct_transfer_intents.py",
                "tests/test_customer_wht_policy_migration.py",
            ),
        ),
    ),
    SOTService(
        name="financial.gateway_topup_intent_commands",
        module="app.services.gateway_topup_intents",
        owns=(
            "customer gateway top-up intent creation coordination",
            "reseller gateway top-up intent creation coordination",
            "saved-card charge failure coordination",
        ),
        depends_on=(
            "control.settings_spec",
            "customer.accounts",
            "financial.account_credit_deposits",
            "financial.billing_accounts",
            "financial.invoices",
            "financial.topup_intents",
            "integration.installations",
        ),
        notes=(
            "This coordinator admits typed customer or reseller gateway "
            "creation commands, resolves canonical amount/lifetime policy, and "
            "commits one intent participant. A failed saved-card transport call "
            "re-enters a separate command that atomically fails the intent and "
            "releases any unused retry reservation."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="customer gateway top-up intent creation coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated customer gateway creation command",
                        "canonical payable invoice",
                        "canonical gateway lifetime and amount policy",
                        "canonical deposit intent protocol",
                        "canonical gateway intent protocol",
                        "enabled checkout capability binding",
                    ),
                ),
                ConcernContract(
                    name="reseller gateway top-up intent creation coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated reseller gateway creation command",
                        "canonical reseller billing account",
                        "canonical gateway lifetime and amount policy",
                        "canonical gateway intent protocol",
                        "enabled checkout capability binding",
                    ),
                ),
                ConcernContract(
                    name="saved-card charge failure coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "typed saved-card failure command",
                        "canonical gateway intent protocol",
                        "canonical saved-card retry reservation",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated customer gateway creation command",
                    owner="financial.gateway_topup_intent_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed customer account, flow, invoice or amount, provider, "
                        "reference, actor, command, correlation, and idempotency evidence"
                    ),
                ),
                AuthorityInput(
                    name="authenticated reseller gateway creation command",
                    owner="financial.gateway_topup_intent_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed reseller and billing-account identities, amount, "
                        "provider, reference, card choices, actor, and correlation"
                    ),
                ),
                AuthorityInput(
                    name="typed saved-card failure command",
                    owner="financial.gateway_topup_intent_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed intent and optional reservation identities plus "
                        "named reservation scope and correlated command context"
                    ),
                ),
                AuthorityInput(
                    name="canonical payable invoice",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked invoice account, lifecycle status, currency, and "
                        "current outstanding balance"
                    ),
                ),
                AuthorityInput(
                    name="canonical reseller billing account",
                    owner="financial.billing_accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active billing-account identity, reseller owner, "
                        "status, and currency"
                    ),
                ),
                AuthorityInput(
                    name="canonical gateway lifetime and amount policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "bounded gateway intent lifetime and account-credit "
                        "minimum/maximum settings"
                    ),
                ),
                AuthorityInput(
                    name="canonical deposit intent protocol",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only deposit eligibility, preview, idempotency, and "
                        "typed intent staging"
                    ),
                ),
                AuthorityInput(
                    name="canonical gateway intent protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only creation, replay, status, failure, metadata, "
                        "scope lock, and event staging"
                    ),
                ),
                AuthorityInput(
                    name="enabled checkout capability binding",
                    owner="integration.installations",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "enabled payments.intent.v1 binding and enabled parent "
                        "installation whose connector key matches the selected provider"
                    ),
                ),
                AuthorityInput(
                    name="canonical saved-card retry reservation",
                    owner="financial.gateway_topup_intent_commands",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked IdempotencyKey identity, scope, customer account, "
                        "and unbound result evidence"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Each execute_owner_command call admits a transaction-free "
                    "session and commits or rolls back one creation or failure "
                    "operation exactly once. External gateway transport is outside "
                    "the database command."
                ),
                locking=(
                    "Customer creation locks account then invoice/intent; reseller "
                    "creation locks billing account then intent reference. Failure "
                    "locks intent scope and intent before an optional retry reservation."
                ),
                idempotency=(
                    "Deposit creation derives a stable account-scoped digest; "
                    "invoice/reseller references replay only identical evidence. "
                    "A repeated failure is a no-op and an already-removed retry "
                    "reservation remains released."
                ),
                retries=(
                    "Adapters retry the complete database command with the same "
                    "context evidence. Gateway transport is never retried inside "
                    "the owner transaction."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.gateway_topup_intent_commands.configuration_invalid",
                    "financial.gateway_topup_intent_commands.amount_invalid",
                    "financial.gateway_topup_intent_commands.created_by_required",
                    "financial.gateway_topup_intent_commands.flow_evidence_invalid",
                    "financial.gateway_topup_intent_commands.invoice_not_found",
                    "financial.gateway_topup_intent_commands.invoice_not_payable",
                    "financial.gateway_topup_intent_commands.checkout_binding_unavailable",
                    "financial.gateway_topup_intent_commands.deposit_rejected",
                    "financial.gateway_topup_intent_commands.intent_conflict",
                    "financial.gateway_topup_intent_commands.record_invalid",
                    "financial.gateway_topup_intent_commands.billing_account_unavailable",
                    "financial.gateway_topup_intent_commands.reservation_mismatch",
                    "financial.gateway_topup_intent_commands.invalid_command_context",
                    "financial.gateway_topup_intent_commands.command_contract_violation",
                    "financial.gateway_topup_intent_commands.nested_owner_command",
                    "financial.gateway_topup_intent_commands.active_caller_transaction",
                    "financial.gateway_topup_intent_commands.nested_transaction_completion",
                ),
                mapping_owner="customer and reseller portal/API adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "invalid amount, flow, lifetime, account, invoice, provider, or "
                    "reference evidence",
                    "missing, disabled, or provider-mismatched checkout binding",
                    "inactive or wrong-reseller billing account",
                    "deposit policy rejection or concurrent creation conflict",
                    "mismatched or already-bound saved-card retry reservation",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "topup_intent.gateway_created",
                    "topup_intent.failed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries canonical intent/scope/flow/provider "
                    "identities and command correlation; failure adds a named "
                    "source and non-sensitive reason code."
                ),
                replay=(
                    "Events replay to projections only and never charge a card or "
                    "re-enter creation/failure commands."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "customer and reseller portals constructing TopupIntent rows, "
                    "hard-coding lifetime, and separately failing intents/releasing keys"
                ),
                new_owner="financial.gateway_topup_intent_commands",
                verification=(
                    "Customer invoice/deposit, reseller consolidated, failure, "
                    "rollback, settings, manifest, and canonical-writer tests."
                ),
                cutover_gate=(
                    "Portal adapters construct only typed commands on "
                    "transaction-free sessions and retain gateway transport only."
                ),
                fallback_retirement=(
                    "Portal TopupIntent constructors, local 30-minute constants, "
                    "and split saved-card failure/retry-release commits are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_gateway_topup_intents.py",
                "tests/test_customer_portal_topup_flow.py",
                "tests/architecture/test_topup_intent_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.account_credit_deposits",
        module="app.services.account_credit_deposits",
        owns=(
            "Deposit Account Credit eligibility and preview",
            "typed deposit intent lifecycle and provider correlation",
            "verified Deposit Account Credit settlement command",
            "deposit-to-payment evidence link",
            "post-application funding-change outbox event",
        ),
        depends_on=(
            "customer.accounts",
            "events.dispatcher",
            "financial.account_credit_applications",
            "financial.prepaid_service_renewals",
            "financial.access_resolution",
            "financial.invoices",
            "financial.payments",
            "financial.topup_intents",
            "observability.audit_log",
        ),
        notes=(
            "A deposit preview may include current eligible invoices and "
            "the exact oldest-debt application before any checkout starts. "
            "The same policy owner supplies the customer-facing active-request "
            "phase, observation/expiry facts, and closed next-action hint so "
            "portal adapters do not reinterpret pending intent state. "
            "The deposit first records the whole confirmed receipt as "
            "unallocated account credit, grants no service duration, and "
            "then asks the canonical applicator to settle eligible debt. "
            "Only after that application completes does its chained event "
            "request due-service renewal before access reconciliation. "
            "Customer verification and reconciliation enter the typed public "
            "command; webhook and proof owners compose the same flush-only "
            "participant inside their wider evidence transactions."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="Deposit Account Credit eligibility and preview",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical deposit customer account",
                        "canonical payable invoice set",
                        "canonical payment-backed account credit",
                        "canonical deposit eligibility policy",
                    ),
                ),
                ConcernContract(
                    name="typed deposit intent lifecycle and provider correlation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed deposit intent creation evidence",
                        "canonical deposit customer account",
                        "canonical deposit eligibility policy",
                    ),
                    canonical_writer="financial.account_credit_deposits",
                ),
                ConcernContract(
                    name="verified Deposit Account Credit settlement command",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "typed verified deposit settlement evidence",
                        "canonical typed deposit intent",
                        "canonical subscriber payment settlement protocol",
                        "canonical account-credit application protocol",
                        "canonical top-up intent completion protocol",
                    ),
                    canonical_writer="financial.account_credit_deposits",
                ),
                ConcernContract(
                    name="deposit-to-payment evidence link",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical typed deposit intent",
                        "canonical subscriber payment settlement protocol",
                    ),
                    canonical_writer="financial.account_credit_deposits",
                ),
                ConcernContract(
                    name="post-application funding-change outbox event",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=(
                        "typed verified deposit settlement evidence",
                        "canonical typed deposit intent",
                        "canonical subscriber payment settlement protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical deposit customer account",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=("locked active subscriber identity and lifecycle state"),
                ),
                AuthorityInput(
                    name="canonical payable invoice set",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "current active same-currency issued, partially paid, or "
                        "overdue invoice balances"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment-backed account credit",
                    owner="financial.payments",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "ledger-derived unconsumed succeeded-payment settlement "
                        "credit with exact structural evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical deposit eligibility policy",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed purpose, allocation/application policy, version, "
                        "supported currency, amount bounds, reviewed-preview rule, "
                        "pending-intent rule, and typed customer next-action read model"
                    ),
                ),
                AuthorityInput(
                    name="typed deposit intent creation evidence",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed coordinator-admitted account, amount, provider, "
                        "reference, expiry, channel, actor, reviewed preview "
                        "fingerprint, and idempotency evidence"
                    ),
                ),
                AuthorityInput(
                    name="typed verified deposit settlement evidence",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "typed intent/provider/external-transaction identities, "
                        "amount, currency, named source, and correlated CommandContext"
                    ),
                ),
                AuthorityInput(
                    name="canonical typed deposit intent",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked TopupIntent account, purpose, policies, provider, "
                        "amount, currency, reference, and completion link"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscriber payment settlement protocol",
                    owner="financial.payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only succeeded account-credit payment creation, "
                        "idempotency, settlement, and ledger evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical account-credit application protocol",
                    owner="financial.account_credit_applications",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "locked deterministic eligible-invoice application from "
                        "exact payment-backed credit"
                    ),
                ),
                AuthorityInput(
                    name="canonical top-up intent completion protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only completion projection derived from canonical "
                        "succeeded Payment evidence"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "settle_verified admits a transaction-free session through "
                    "execute_owner_command and commits or rolls back payment, credit "
                    "application, intent projection, audit, and event once. "
                    "stage_intent and stage_verified_settlement never complete a "
                    "transaction and are callable only inside named coordinator/owner "
                    "transactions."
                ),
                locking=(
                    "Intent creation and settlement lock the subscriber account "
                    "first; preview and creation share that account-scoped "
                    "invoice-order snapshot; settlement then locks the exact "
                    "intent before composing payment and oldest-debt application "
                    "owners."
                ),
                idempotency=(
                    "Intent creation uses one account/purpose/key identity. Settlement "
                    "uses the intent-scoped payment key plus provider external identity; "
                    "a completed intent replays the same Payment without a second event."
                ),
                retries=(
                    "Adapters retry the complete public command with the same evidence. "
                    "Webhook/proof owners retry their wider command; staging helpers "
                    "never retry or commit independently."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "deposit_account_not_found",
                    "deposit_account_inactive",
                    "deposit_currency_unsupported",
                    "deposit_amount_below_minimum",
                    "deposit_amount_above_maximum",
                    "deposit_preview_stale",
                    "deposit_intent_already_pending",
                    "deposit_idempotency_invalid",
                    "deposit_idempotency_conflict",
                    "deposit_intent_not_found",
                    "deposit_contract_invalid",
                    "deposit_amount_mismatch",
                    "deposit_provider_fee_invalid",
                    "deposit_currency_mismatch",
                    "deposit_provider_identity_invalid",
                    "deposit_provider_mismatch",
                    "deposit_provider_correlation_mismatch",
                    "deposit_settlement_incomplete",
                    "deposit_provider_reference_conflict",
                    "financial.account_credit_deposits.invalid_command_context",
                    "financial.account_credit_deposits.command_contract_violation",
                    "financial.account_credit_deposits.nested_owner_command",
                    "financial.account_credit_deposits.active_caller_transaction",
                    "financial.account_credit_deposits.nested_transaction_completion",
                ),
                mapping_owner=(
                    "customer, webhook, reconciliation, and payment-proof adapters"
                ),
                retryable_codes=(),
                fail_closed_on=(
                    "inactive or missing account, stale reviewed preview, pending "
                    "deposit intent, or invalid amount/currency policy",
                    "missing, untyped, wrong-provider, wrong-amount, wrong-currency, or "
                    "uncorrelated intent/receipt evidence",
                    "conflicting provider transaction or incomplete completed-payment link",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=("account_credit.deposited",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact intent/payment/account amount, currency, "
                    "application allocations, named source, and command correlation."
                ),
                replay=(
                    "The event projects committed evidence only and never creates money "
                    "or re-enters settlement."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "portal, webhook, proof, and reconciliation callers selecting "
                    "commit behavior and passing transport-shaped gateway transactions"
                ),
                new_owner="financial.account_credit_deposits",
                verification=(
                    "Root/participant settlement, replay, mismatch, rollback, caller, "
                    "manifest, and canonical-transaction tests."
                ),
                cutover_gate=(
                    "Customer/reconciliation callers use the public typed command; "
                    "webhook/proof owners use only typed flush-only staging."
                ),
                fallback_retirement=(
                    "The create_intent transaction wrapper, settlement commit flag, "
                    "PaymentGatewayTransaction domain input, and caller settlement "
                    "commits are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/ACCOUNT_CREDIT_DEPOSITS.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_account_credit_deposits.py",
                "tests/test_customer_portal_topup_flow.py",
                "tests/test_payment_webhook_settlement.py",
                "tests/test_payment_proofs.py",
                "tests/architecture/test_account_credit_deposit_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.collection_accounts",
        module="app.services.billing.collection_accounts",
        owns=(
            "collection-account identity and lifecycle",
            "Dotmac receiving-account payment details",
            "derived receiving-account last-four projection",
            "customer bank-destination presentment order",
            "external collection-account accounting mapping",
        ),
        notes=(
            "Portal, reseller, API, invoice, settings, proof, and "
            "attribution adapters carry this identity and never maintain "
            "parallel bank-detail copies. Legacy settings are only a "
            "frozen rollback snapshot during A1 verification."
        ),
    ),
    SOTService(
        name="financial.payment_configuration_staff_actions",
        module="app.services.payment_configuration_staff_actions",
        owns=("reviewed payment configuration lifecycle and audit coordination",),
        depends_on=(
            "financial.collection_accounts",
            "financial.payment_routing",
            "observability.audit_log",
        ),
        notes=(
            "Settings adapters preview and submit only. This coordinator "
            "locks and rechecks collection-account, payment-channel, and "
            "channel-mapping state, applies lifecycle/default changes, and "
            "stages the decision audit atomically. It never selects a "
            "customer checkout gateway."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=(
                        "reviewed payment configuration lifecycle and "
                        "audit coordination"
                    ),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "payment configuration staff command",
                        "canonical collection-account state",
                        "canonical settlement-attribution state",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="payment configuration staff command",
                    owner="financial.payment_configuration_staff_actions",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed resource, action, actor, permission scope, "
                        "review fingerprint, confirmation, and command context"
                    ),
                ),
                AuthorityInput(
                    name="canonical collection-account state",
                    owner="financial.collection_accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked CollectionAccount identity, currency, "
                        "presentment priority, and lifecycle"
                    ),
                ),
                AuthorityInput(
                    name="canonical settlement-attribution state",
                    owner="financial.payment_configuration_staff_actions",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked PaymentChannel and PaymentChannelAccount "
                        "identity, provider, currency, priority, default, "
                        "and lifecycle facts"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "confirm_payment_configuration_staff_action enters "
                    "execute_owner_command exactly once on a transaction-free "
                    "session and commits configuration plus audit atomically."
                ),
                locking=(
                    "The target and affected same-provider, same-currency, "
                    "collection-account, and mapping rows are locked before "
                    "the preview fingerprint is rechecked."
                ),
                idempotency=(
                    "The command context carries the resource, action, and "
                    "review fingerprint; a stale fingerprint fails closed."
                ),
                retries=(
                    "Contention or stale-preview failures return to review; "
                    "adapters do not replay an unreviewed decision."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "financial.payment_configuration_staff_actions"
                    ),
                    "financial.payment_configuration_staff_actions.not_found",
                    "financial.payment_configuration_staff_actions.invalid_action",
                    "financial.payment_configuration_staff_actions.invalid_mapping",
                    "financial.payment_configuration_staff_actions.invalid_scope",
                    "financial.payment_configuration_staff_actions.invalid_actor",
                    "financial.payment_configuration_staff_actions.confirmation_required",
                    "financial.payment_configuration_staff_actions.stale_preview",
                    "financial.payment_configuration_staff_actions.action_not_available",
                ),
                mapping_owner="Settings payment-configuration web adapter",
                retryable_codes=(
                    "financial.payment_configuration_staff_actions.stale_preview",
                ),
                fail_closed_on=(
                    "missing actor or scope",
                    "stale reviewed state",
                    "last customer transfer destination",
                    "inactive mapping dependencies",
                    "default mapping replacement not selected",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "Billing templates, form booleans, direct toggle routes, "
                    "and PaymentChannel.default_collection_account_id"
                ),
                new_owner="financial.payment_configuration_staff_actions",
                verification=(
                    "Owner behavior, stale-preview, route, template, migration, "
                    "and architecture tests."
                ),
                cutover_gate=(
                    "Canonical Settings routes use reviewed actions and "
                    "payment_channel_accounts is the sole channel-to-account map."
                ),
                fallback_retirement=(
                    "Old Billing routes, browser confirmation toggles, lifecycle "
                    "form fields, and the duplicate default pointer are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/PAYMENT_CONFIGURATION_SETTINGS_SAFE_ACTIONS.md",
            ),
            test_refs=(
                "tests/test_payment_configuration_staff_actions.py",
                "tests/test_payment_configuration_settings_ui.py",
            ),
        ),
    ),
    SOTService(
        name="financial.payment_routing",
        module="app.services.payment_routing",
        owns=(
            "installation-backed customer gateway eligibility",
            "ordered customer gateway presentment policy",
            "checkout provider and binding selection",
        ),
        depends_on=(
            "financial.payment_gateway_finance",
            "integration.installations",
        ),
        notes=(
            "Enabled payments.intent.v1 bindings are the only online "
            "gateway control plane. Payment channels classify recorded "
            "settlement and never route checkout."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="installation-backed customer gateway eligibility",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "enabled payment capability installation bundle",
                        "canonical gateway finance identity",
                    ),
                ),
                ConcernContract(
                    name="ordered customer gateway presentment policy",
                    role=OwnerRole.POLICY,
                    input_names=("enabled payment capability installation bundle",),
                ),
                ConcernContract(
                    name="checkout provider and binding selection",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "enabled payment capability installation bundle",
                        "canonical gateway finance identity",
                        "customer checkout provider request",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="enabled payment capability installation bundle",
                    owner="integration.installations",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "enabled connector installation and complete enabled "
                        "payments intent, webhook, reconciliation, and refund "
                        "bindings; intent policy carries presentment priority"
                    ),
                ),
                AuthorityInput(
                    name="canonical gateway finance identity",
                    owner="financial.payment_gateway_finance",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "exactly one canonical PaymentProvider identity for "
                        "the connector type, established during connector setup"
                    ),
                ),
                AuthorityInput(
                    name="customer checkout provider request",
                    owner="financial.payment_routing",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "optional requested supported provider type from a "
                        "customer or reseller checkout adapter"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Eligibility, ordering, health, and selection are computed "
                    "from one caller-owned read session and write no state."
                ),
                locking=(
                    "No locks are taken; a subsequent intent command revalidates "
                    "the selected binding inside its owned transaction."
                ),
                idempotency=(
                    "The same committed installation, binding policy, and finance "
                    "identity rows produce the same ordered options."
                ),
                retries=(
                    "Adapters may repeat the complete read after concurrent "
                    "configuration changes; intent admission remains authoritative."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="customer, reseller, and payment admin adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "missing or incomplete capability bundle",
                    "disabled intent binding or installation",
                    "missing or duplicate finance identity",
                    "unsupported or unavailable requested provider",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "billing primary, secondary, and failover settings plus "
                    "template and route-level Paystack defaults"
                ),
                new_owner="financial.payment_routing",
                verification=(
                    "Routing, customer portal, connector setup, intent "
                    "provenance, and shrink-only architecture tests."
                ),
                cutover_gate=(
                    "Every new checkout option and selection comes from an "
                    "enabled payments.intent.v1 binding."
                ),
                fallback_retirement=(
                    "Routing settings, provider fallback readers, and "
                    "template or route-level Paystack defaults are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_payment_routing.py",
                "tests/test_customer_portal_billing_routes.py",
                "tests/architecture/test_payment_gateway_control_plane.py",
            ),
        ),
    ),
)
