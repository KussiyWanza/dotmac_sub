"""financial_access SOT declarations: provider payments."""

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
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="financial.provider_payment_settlements",
        module="app.services.provider_payment_settlements",
        owns=(
            "verified invoice-payment cash-first orchestration",
            "post-settlement invoice-allocation request",
            "allocation-failure exception handoff",
            "idempotent post-allocation funding-change outbox event",
        ),
        depends_on=(
            "financial.payments",
            "financial.invoices",
            "financial.prepaid_service_renewals",
        ),
    ),
    SOTService(
        name="financial.payment_provider_events",
        module="app.services.payment_provider_events",
        owns=(
            "payment-provider event ingestion",
            "normalized provider monetary observations",
            "provider-event idempotency",
            "incomplete provider settlement resumption",
        ),
        depends_on=(
            "events.dispatcher",
            "financial.consolidated_payments",
            "financial.invoices",
            "financial.payments",
            "financial.payment_routing",
            "financial.provider_payment_settlements",
            "observability.audit_log",
        ),
        notes=(
            "Signature verification and gateway verification remain external "
            "observation admission boundaries. This owner persists the exact "
            "normalized identity, source, digest, status, money, processing "
            "result, audit, and event before delegating financial consequences "
            "to named participants. Administrative observations cannot change "
            "payment state. Provider fee policy is intentionally not decided "
            "here; the unresolved route policy remains with financial.payments."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="payment-provider event ingestion",
                    role=OwnerRole.OBSERVATION_COLLECTOR,
                    input_names=(
                        "verified external provider observation",
                        "administrative provider observation",
                        "active provider identity",
                        "provider-event command context",
                    ),
                    canonical_writer="financial.payment_provider_events",
                ),
                ConcernContract(
                    name="normalized provider monetary observations",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "verified external provider observation",
                        "active provider identity",
                    ),
                    canonical_writer="financial.payment_provider_events",
                ),
                ConcernContract(
                    name="provider-event idempotency",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical provider-event record",
                        "provider-event command context",
                    ),
                    canonical_writer="financial.payment_provider_events",
                ),
                ConcernContract(
                    name="incomplete provider settlement resumption",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical provider-event record",
                        "canonical payment participant protocol",
                        "canonical consolidated-payment participant protocol",
                        "canonical invoice-settlement participant protocol",
                        "provider-event command context",
                    ),
                    canonical_writer="financial.payment_provider_events",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="verified external provider observation",
                    owner="external:payment_provider",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "signature-verified webhook or gateway-verified transaction "
                        "identity, event type, gross, fee, net observation, currency, "
                        "provider reference, status, and bounded raw payload"
                    ),
                ),
                AuthorityInput(
                    name="administrative provider observation",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "authenticated operator-supplied informational event with no "
                        "payment-state or refund/reversal authority"
                    ),
                ),
                AuthorityInput(
                    name="active provider identity",
                    owner="financial.payment_routing",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "configured provider identity and active eligibility admitted "
                        "for the verified receipt or reconciliation path"
                    ),
                ),
                AuthorityInput(
                    name="canonical provider-event record",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked provider/event identity, source, observation digest, "
                        "normalized money/status/effect, processing state, error code, "
                        "and linked payment/invoice"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment participant protocol",
                    owner="financial.payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only settlement, fee observation, payment status, "
                        "allocation, refund, and reversal participants"
                    ),
                ),
                AuthorityInput(
                    name="canonical consolidated-payment participant protocol",
                    owner="financial.consolidated_payments",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only consolidated settlement, refund, and reversal "
                        "participants"
                    ),
                ),
                AuthorityInput(
                    name="canonical invoice-settlement participant protocol",
                    owner="financial.provider_payment_settlements",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "cash-first verified invoice-payment and allocation-exception "
                        "participant"
                    ),
                ),
                AuthorityInput(
                    name="provider-event command context",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed actor, trust scope, reason, command, idempotency, "
                        "correlation, and causation evidence"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The administrative command enters execute_owner_command once and "
                    "can record informational observations only. Verified webhook and "
                    "reconciliation methods are flush-only participants of their named "
                    "coordinator transactions; all record, payment, audit, event, and "
                    "processed-receipt consequences commit or roll back together."
                ),
                locking=(
                    "After the wider coordinator locks its receipt or top-up intent, "
                    "this owner locks the exact provider row, then matching provider "
                    "event, invoice/payment, and downstream participant scopes. The "
                    "provider lock serializes first insert for both unique identities."
                ),
                idempotency=(
                    "Provider plus idempotency key or external transaction identifies "
                    "one row. A canonical SHA-256 digest includes the admission trust "
                    "class and all normalized decision evidence. Signature-verified "
                    "webhook and gateway-verified observations may converge only when "
                    "those normalized fields match exactly; administrative evidence "
                    "cannot converge with either. A legacy incomplete row may resume "
                    "once from verified evidence and receives canonical provenance."
                ),
                retries=(
                    "Retry the complete owning coordinator with the same normalized "
                    "observation and context. Conflicting identity evidence never "
                    "retries as the existing event and no participant commits alone."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.payment_provider_events.currency_invalid",
                    "financial.payment_provider_events.currency_required",
                    "financial.payment_provider_events.event_not_found",
                    "financial.payment_provider_events.financial_consequence_rejected",
                    "financial.payment_provider_events.financial_effect_conflict",
                    "financial.payment_provider_events.financial_effect_required",
                    "financial.payment_provider_events.identity_collision",
                    "financial.payment_provider_events.identity_required",
                    "financial.payment_provider_events.invoice_account_mismatch",
                    "financial.payment_provider_events.invoice_not_found",
                    "financial.payment_provider_events.money_invalid",
                    "financial.payment_provider_events.observation_invalid",
                    "financial.payment_provider_events.pagination_invalid",
                    "financial.payment_provider_events.payment_not_found",
                    "financial.payment_provider_events.provider_not_found",
                    "financial.payment_provider_events.replay_conflict",
                    "financial.payment_provider_events.status_conflict",
                    "financial.payment_provider_events.untrusted_financial_effect",
                    "financial.payment_provider_events.untrusted_financial_observation",
                    "financial.payment_provider_events.invalid_command_context",
                    "financial.payment_provider_events.command_contract_violation",
                    "financial.payment_provider_events.command_scope_mismatch",
                    "financial.payment_provider_events.nested_owner_command",
                    "financial.payment_provider_events.active_caller_transaction",
                    "financial.payment_provider_events.nested_transaction_completion",
                    "financial.payment_provider_events.net_amount_required",
                ),
                mapping_owner=(
                    "billing API, payment-webhook coordinator, and payment-"
                    "reconciliation coordinator adapters"
                ),
                retryable_codes=(
                    "financial.payment_provider_events.financial_consequence_rejected",
                ),
                fail_closed_on=(
                    "missing verified identity, malformed money or currency, and "
                    "contradictory event/status/effect evidence",
                    "identity reuse with a different trust class or normalized digest",
                    "administrative attempts to change payment, refund, or reversal state",
                    "admission source invoked with the wrong command scope",
                    "invoice settlement without an explicit normalized net amount",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "payment_provider_event.processed",
                    "payment_provider_event.failed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 exposes canonical event/payment/invoice/provider "
                    "identities, trust source, normalized status/effect, processing "
                    "state, stable error code, and command lineage without raw provider "
                    "payloads, credentials, or customer PII."
                ),
                replay=(
                    "Consumers project committed observation facts only and never "
                    "re-enter settlement, refund, reversal, or allocation commands."
                ),
            ),
            projections=(),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "billing.providers mixed provider configuration with a mutable "
                    "event helper, caller-selected trust boolean, helper commit, "
                    "non-exact replay, and administrative financial consequences"
                ),
                new_owner="financial.payment_provider_events",
                verification=(
                    "Typed admission, exact replay/conflict, provenance, rollback, "
                    "audit/event, adapter-boundary, manifest, and PostgreSQL concurrent "
                    "identity tests."
                ),
                cutover_gate=(
                    "Only payment_webhooks and payment_reconciliation call verified "
                    "participants with typed context; the API root admits only "
                    "non-financial administrative observations."
                ),
                fallback_retirement=(
                    "The trusted_financial_effects boolean, event helper commit, "
                    "billing_adapter gateway ingress, mutable transport payload, "
                    "source-free record, and return-on-identity-without-proof paths "
                    "are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_payment_provider_events.py",
                "tests/test_api_billing_webhooks.py",
                "tests/test_payment_webhook_settlement.py",
                "tests/architecture/test_payment_provider_event_ownership.py",
                "tests/architecture/test_payment_settlement_participants.py",
                "tests/integration/test_payment_provider_event_concurrency.py",
            ),
        ),
    ),
    SOTService(
        name="financial.payment_webhooks",
        module="app.services.payment_webhook_commands",
        owns=(
            "verified payment webhook projection",
            "billing consequence submission from verified receipts",
        ),
        depends_on=(
            "integration.inbox",
            "financial.account_credit_deposits",
            "financial.payment_provider_events",
            "financial.topup_intents",
        ),
        notes=(
            "Signature adapters persist and claim the verified receipt through "
            "integration.inbox, then submit only its typed identity. This "
            "coordinator normalizes the stored external observation and commits "
            "all billing consequences with processed-receipt evidence once."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="verified payment webhook projection",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "claimed signature-verified payment receipt",
                        "external provider payment observation",
                        "canonical provider-event settlement protocol",
                    ),
                ),
                ConcernContract(
                    name=("billing consequence submission from verified receipts"),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "claimed signature-verified payment receipt",
                        "canonical provider-event settlement protocol",
                        "canonical account-credit deposit protocol",
                        "canonical top-up completion protocol",
                        "canonical inbox consequence protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="claimed signature-verified payment receipt",
                    owner="integration.inbox",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked IntegrationInbox identity, verified payload digest, "
                        "provider header, processing state, and attempt evidence"
                    ),
                ),
                AuthorityInput(
                    name="external provider payment observation",
                    owner="external:payment_provider",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "signature-verified Paystack or Flutterwave event type, "
                        "transaction identity, amount, fee, currency, status, and "
                        "provider-reflected checkout metadata"
                    ),
                ),
                AuthorityInput(
                    name="canonical provider-event settlement protocol",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only idempotent provider-event, payment, allocation, "
                        "refund, reversal, status, and exception participants"
                    ),
                ),
                AuthorityInput(
                    name="canonical account-credit deposit protocol",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only typed deposit correlation, settlement, credit "
                        "application, intent projection, audit, and event protocol"
                    ),
                ),
                AuthorityInput(
                    name="canonical top-up completion protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only locked payment-to-intent completion projection"
                    ),
                ),
                AuthorityInput(
                    name="canonical inbox consequence protocol",
                    owner="integration.inbox",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only processed consequence projection and separate "
                        "owner-managed failure/dead-letter recording"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "After integration.inbox durably records and claims the receipt, "
                    "execute_owner_command admits a transaction-free session and "
                    "commits or rolls back provider event, money, allocation, intent, "
                    "audit/event, and processed-receipt evidence exactly once."
                ),
                locking=(
                    "The coordinator locks the exact claimed inbox receipt first; "
                    "participants then use their canonical account, invoice, payment, "
                    "billing-account, and top-up intent lock order."
                ),
                idempotency=(
                    "IntegrationInbox binding/provider-event identity owns receipt "
                    "replay. Provider-event and monetary participants reuse that "
                    "identity or exact provider transaction evidence; a processed "
                    "receipt returns its stored consequence."
                ),
                retries=(
                    "Retryable failure rolls back the complete billing consequence, "
                    "then the adapter separately records inbox retry evidence. "
                    "Deterministic payload/correlation rejection is dead-lettered."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.payment_webhooks.payload_invalid",
                    "financial.payment_webhooks.receipt_not_found",
                    "financial.payment_webhooks.receipt_not_claimed",
                    "financial.payment_webhooks.receipt_provider_mismatch",
                    "financial.payment_webhooks.topup_intent_mismatch",
                    "financial.payment_webhooks.provider_not_configured",
                    "financial.payment_webhooks.deposit_rejected",
                    "financial.payment_webhooks.provider_event_rejected",
                    "financial.payment_webhooks.settlement_unlinked",
                    "financial.payment_webhooks.topup_projection_rejected",
                    "financial.payment_webhooks.invalid_command_context",
                    "financial.payment_webhooks.command_contract_violation",
                    "financial.payment_webhooks.nested_owner_command",
                    "financial.payment_webhooks.active_caller_transaction",
                    "financial.payment_webhooks.nested_transaction_completion",
                ),
                mapping_owner="Paystack and Flutterwave webhook HTTP adapters",
                retryable_codes=(
                    "financial.payment_webhooks.provider_not_configured",
                    "financial.payment_webhooks.settlement_unlinked",
                    "financial.payment_webhooks.topup_projection_rejected",
                ),
                fail_closed_on=(
                    "unclaimed, missing, provider-mismatched, or malformed receipt",
                    "invalid amount, fee, currency, top-up, invoice, or deposit "
                    "correlation evidence",
                    "provider success without structurally linked payment evidence",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.api_billing_webhooks mixed signature transport, "
                    "provider normalization, direct receipt/payment/intent writes, "
                    "savepoints, commits, rollbacks, and best-effort access decisions"
                ),
                new_owner="financial.payment_webhooks",
                verification=(
                    "Signature, identity collision, provider normalization, invoice, "
                    "deposit, top-up, fee, replay, dead-letter, atomic rollback, "
                    "manifest, and adapter-boundary tests."
                ),
                cutover_gate=(
                    "Adapters verify signatures, serialize HTTP, and invoke one typed "
                    "coordinator on a transaction-free session; every business write "
                    "uses a named flush-only participant."
                ),
                fallback_retirement=(
                    "Adapter-owned ORM writes, provider mappings, savepoints, "
                    "commit/rollback calls, direct provider-fee edits, and synchronous "
                    "service-restoration fallback are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_api_billing_webhooks.py",
                "tests/test_payment_webhook_settlement.py",
                "tests/architecture/test_payment_webhook_ownership.py",
                "tests/architecture/test_payment_settlement_participants.py",
            ),
        ),
    ),
    SOTService(
        name="financial.payment_reconciliation",
        module="app.services.payment_reconciliation",
        owns=(
            "stranded top-up reconciliation",
            "scheduled top-up reconciliation execution",
            "verified provider settlement then allocation orchestration",
            "top-up reconciliation backlog projection",
        ),
        depends_on=(
            "control.settings_spec",
            "integration.runtime",
            "financial.account_credit_deposits",
            "financial.payments",
            "financial.payment_provider_events",
            "financial.topup_intents",
        ),
        notes=(
            "The bounded sweep selects immutable candidates, releases its read "
            "transaction, and treats gateway verification as an external fact. "
            "Each consequence is a separate typed coordinator transaction that "
            "composes canonical financial participants."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="stranded top-up reconciliation",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical pending top-up intent",
                        "external gateway verification observation",
                        "canonical top-up expiry protocol",
                    ),
                ),
                ConcernContract(
                    name="scheduled top-up reconciliation execution",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical pending top-up intent",
                        "external gateway verification observation",
                    ),
                ),
                ConcernContract(
                    name=("verified provider settlement then allocation orchestration"),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical pending top-up intent",
                        "external gateway verification observation",
                        "canonical account-credit deposit protocol",
                        "canonical provider-event settlement protocol",
                        "canonical top-up completion protocol",
                    ),
                ),
                ConcernContract(
                    name="top-up reconciliation backlog projection",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical pending top-up intent",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical top-up reconciliation policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed stale window, maximum age, expiry grace, and batch "
                        "size settings with bounded defaults"
                    ),
                ),
                AuthorityInput(
                    name="canonical pending top-up intent",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked intent identity, account scope, provider, reference, "
                        "purpose, currency, invoice instruction, and completion state"
                    ),
                ),
                AuthorityInput(
                    name="external gateway verification observation",
                    owner="external:payment_provider",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "normalized Paystack or Flutterwave transaction identity, "
                        "gross amount, provider fee, currency, and outcome"
                    ),
                ),
                AuthorityInput(
                    name="canonical account-credit deposit protocol",
                    owner="financial.account_credit_deposits",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only typed deposit correlation, payment, credit "
                        "application, audit, and event participant"
                    ),
                ),
                AuthorityInput(
                    name="canonical provider-event settlement protocol",
                    owner="financial.payment_provider_events",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only idempotent provider-event, payment, fee, invoice, "
                        "consolidated settlement, and allocation participants"
                    ),
                ),
                AuthorityInput(
                    name="canonical top-up completion protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="flush-only locked payment-to-intent completion projection",
                ),
                AuthorityInput(
                    name="canonical top-up expiry protocol",
                    owner="financial.topup_intents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="flush-only locked expiry projection from canonical time",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Candidate selection completes its read transaction before any "
                    "gateway call. Each definitive observation enters exactly one "
                    "execute_owner_command root that commits or rolls back settlement, "
                    "provider event, intent projection, audit, and domain events."
                ),
                locking=(
                    "The coordinator locks the canonical subscriber or billing-account "
                    "scope and top-up intent first; named participants then use their "
                    "account, invoice, payment, and settlement lock order."
                ),
                idempotency=(
                    "Provider type plus intent reference reuses the webhook provider-"
                    "event identity. Provider transaction identity and canonical "
                    "participant keys prevent duplicate cash, allocation, and intent "
                    "consequences."
                ),
                retries=(
                    "Provider unavailability leaves the intent pending. Each candidate "
                    "is an independent transaction, so one rejection cannot roll back "
                    "or repeat another candidate's completed consequence."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.payment_reconciliation.policy_missing",
                    "financial.payment_reconciliation.provider_mismatch",
                    "financial.payment_reconciliation.reference_mismatch",
                    "financial.payment_reconciliation.transaction_identity_invalid",
                    "financial.payment_reconciliation.amount_invalid",
                    "financial.payment_reconciliation.provider_fee_invalid",
                    "financial.payment_reconciliation.currency_invalid",
                    "financial.payment_reconciliation.currency_mismatch",
                    "financial.payment_reconciliation.invoice_correlation_invalid",
                    "financial.payment_reconciliation.completion_conflict",
                    "financial.payment_reconciliation.provider_not_configured",
                    "financial.payment_reconciliation.provider_configuration_mismatch",
                    "financial.payment_reconciliation.deposit_rejected",
                    "financial.payment_reconciliation.provider_event_rejected",
                    "financial.payment_reconciliation.settlement_unlinked",
                    "financial.payment_reconciliation.topup_projection_rejected",
                    "financial.payment_reconciliation.outcome_invalid",
                    "financial.payment_reconciliation.observation_incomplete",
                    "financial.payment_reconciliation.invalid_command_context",
                    "financial.payment_reconciliation.command_contract_violation",
                    "financial.payment_reconciliation.nested_owner_command",
                    "financial.payment_reconciliation.active_caller_transaction",
                    "financial.payment_reconciliation.nested_transaction_completion",
                ),
                mapping_owner="scheduled payment reconciliation task adapter",
                retryable_codes=(
                    "financial.payment_reconciliation.provider_not_configured",
                    "financial.payment_reconciliation.settlement_unlinked",
                    "financial.payment_reconciliation.topup_projection_rejected",
                ),
                fail_closed_on=(
                    "provider, reference, transaction, amount, fee, or currency mismatch",
                    "missing or invalid explicit invoice instruction",
                    "successful observation without linked payment evidence",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            projections=(
                ProjectionContract(
                    name="top-up reconciliation backlog projection",
                    input_names=(
                        "canonical top-up reconciliation policy",
                        "canonical pending top-up intent",
                    ),
                    writer="financial.payment_reconciliation",
                    freshness=(
                        "Recomputed on read at an explicit observation time "
                        "from pending intent creation times and the effective "
                        "stale and maximum-age policy."
                    ),
                    stale_behavior=(
                        "Separates currently eligible intents from intents "
                        "outside the automatic repair window; it never treats "
                        "absence of a successful runner heartbeat as an empty "
                        "backlog."
                    ),
                    drift_signal=(
                        "Pending intent counts disagree with the bounded "
                        "eligible and outside-window partition."
                    ),
                    rebuild_operation=(
                        "Re-run topup_reconciliation_backlog from canonical "
                        "pending intents and effective reconciliation policy."
                    ),
                    repair_owner="financial.payment_reconciliation",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.payment_reconciliation mixed scheduled session "
                    "lifecycle, gateway calls, direct payment decisions, commits, "
                    "rollbacks, private provider lookup, allocation fallback, prepaid "
                    "invoice settlement, and synchronous access restoration"
                ),
                new_owner="financial.payment_reconciliation",
                verification=(
                    "Gateway outcome, deposit, invoice, consolidated, existing-payment, "
                    "expiry, replay, transaction-boundary, manifest, and task-adapter "
                    "tests."
                ),
                cutover_gate=(
                    "The task owns only session lifecycle and serialization; the sweep "
                    "submits typed immutable evidence to per-intent coordinator roots."
                ),
                fallback_retirement=(
                    "Service commit/rollback/session creation, direct payment creation, "
                    "private provider lookup, guessed invoice allocation, synchronous "
                    "prepaid settlement, and access restoration are absent."
                ),
            ),
            steward="finance operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                "docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md",
            ),
            test_refs=(
                "tests/test_payment_webhook_settlement.py",
                "tests/test_reconcile_honours_invoice_intent.py",
                "tests/architecture/test_payment_reconciliation_ownership.py",
                "tests/architecture/test_payment_settlement_participants.py",
            ),
        ),
    ),
)
