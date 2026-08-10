"""financial_access SOT declarations: prepaid."""

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
        name="financial.prepaid_currency",
        module="app.services.prepaid_currency",
        owns=("prepaid enforcement currency policy",),
        depends_on=("control.settings_spec",),
        notes=(
            "Normalizes and validates the sole currency used to compare prepaid "
            "funding, thresholds, and access consequences."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid enforcement currency policy",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "prepaid enforcement currency setting",
                        "prepaid currency protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="prepaid enforcement currency setting",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="typed billing.prepaid_enforcement_currency value",
                ),
                AuthorityInput(
                    name="prepaid currency protocol",
                    owner="financial.prepaid_currency",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=("uppercase three-letter ASCII currency-code invariant"),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller owns the session; resolution reads one canonical "
                    "setting and performs no writes or transaction completion."
                ),
                locking="No row lock; the decision uses the visible setting value.",
                idempotency=(
                    "The same explicit value or visible canonical setting produces "
                    "the same normalized currency or stable failure."
                ),
                retries=(
                    "Transient setting reads may be retried. Invalid currency "
                    "evidence is terminal until corrected."
                ),
            ),
            errors=ErrorContract(
                domain_codes=("financial.prepaid_currency.invalid_currency",),
                mapping_owner=(
                    "billing, funding, enforcement, reporting, and task adapters"
                ),
                fail_closed_on=(
                    "missing prepaid enforcement currency",
                    "non-ASCII or non-three-letter currency evidence",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "financial.access_resolution local currency normalizer plus "
                    "caller-local strip/uppercase handling"
                ),
                new_owner="financial.prepaid_currency",
                verification=(
                    "Normalization, invalid-setting, funding-decision, threshold, "
                    "caller-import, and architecture tests."
                ),
                cutover_gate=(
                    "Access, threshold, funding-position, enforcement-plan, and "
                    "readiness callers import the dedicated currency owner."
                ),
                fallback_retirement=(
                    "The access-resolution implementation, re-export, dynamic error "
                    "code, and funding caller-local normalization are removed."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/audits/BILLING_SOT_AUDIT_2026-07-12.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_access_resolution.py",
                "tests/test_prepaid_threshold_resolver.py",
                "tests/architecture/test_prepaid_threshold_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.subscription_billing_treatments",
        module="app.services.subscription_billing_treatments",
        owns=(
            "subscription billing-treatment lifecycle",
            "effective subscription customer-billing treatment",
            "billing-treatment offer and value authorization",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "auth.permission_gate",
            "control.settings_spec",
            "events.dispatcher",
            "service_intent.catalog_policy",
        ),
        notes=(
            "The offer and contracted price retain service value. One "
            "effective-dated subscription approval records why its customer "
            "is not billed; every approval is finite and offer changes "
            "require revoke and reapproval."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="subscription billing-treatment lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "authenticated billing-treatment command",
                        "canonical subscription contract",
                        "canonical recurring service value",
                        "canonical billing-treatment approval policy",
                        "current billing-treatment records",
                    ),
                    canonical_writer="financial.subscription_billing_treatments",
                ),
                ConcernContract(
                    name="effective subscription customer-billing treatment",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical subscription contract",
                        "canonical recurring service value",
                        "current billing-treatment records",
                        "evaluation time",
                    ),
                ),
                ConcernContract(
                    name="billing-treatment offer and value authorization",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical subscription contract",
                        "canonical recurring service value",
                        "canonical billing-treatment approval policy",
                        "current billing-treatment records",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated billing-treatment command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "billing:treatment:write principal plus command, "
                        "correlation, idempotency, actor, scope, and reason"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription contract",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscription account, offer, lifecycle, billing mode, "
                        "cadence, price terms, and billing anchor"
                    ),
                ),
                AuthorityInput(
                    name="canonical recurring service value",
                    owner="service_intent.catalog_policy",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="positive recurring price, currency, and cadence",
                ),
                AuthorityInput(
                    name="canonical billing-treatment approval policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "registered billing setting "
                        "subscription_billing_treatment_max_days"
                    ),
                ),
                AuthorityInput(
                    name="current billing-treatment records",
                    owner="financial.subscription_billing_treatments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "effective-dated arrangement approval, authorized offer, "
                        "value ceiling, cadence, approval-policy snapshot, and "
                        "revocation evidence"
                    ),
                ),
                AuthorityInput(
                    name="evaluation time",
                    owner="external:system_clock",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source="UTC policy evaluation time",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Create and revoke commands enter the owner executor once; "
                    "preview and treatment resolution are read-only."
                ),
                locking=(
                    "The subscription locks before overlap, offer, value, cadence, "
                    "and preview evidence are rechecked."
                ),
                idempotency=(
                    "Unique hashed create and revoke keys replay only the same "
                    "fingerprinted decision."
                ),
                retries=(
                    "Transient conflicts retry with the same key; evidence drift "
                    "requires a new preview."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.subscription_billing_treatments.active_caller_transaction",
                    "financial.subscription_billing_treatments.approval_horizon_exceeded",
                    "financial.subscription_billing_treatments.arrangement_not_found",
                    "financial.subscription_billing_treatments.command_contract_violation",
                    "financial.subscription_billing_treatments.idempotency_conflict",
                    "financial.subscription_billing_treatments.finite_period_required",
                    "financial.subscription_billing_treatments.invalid_approval_policy",
                    "financial.subscription_billing_treatments.invalid_command",
                    "financial.subscription_billing_treatments.invalid_command_context",
                    "financial.subscription_billing_treatments.invalid_currency",
                    "financial.subscription_billing_treatments.invalid_period",
                    "financial.subscription_billing_treatments.invalid_scope",
                    "financial.subscription_billing_treatments.invalid_transition",
                    "financial.subscription_billing_treatments.invalid_treatment",
                    "financial.subscription_billing_treatments.missing_billing_anchor",
                    "financial.subscription_billing_treatments.missing_contract_price",
                    "financial.subscription_billing_treatments.missing_sponsor_evidence",
                    "financial.subscription_billing_treatments.nested_owner_command",
                    "financial.subscription_billing_treatments.nested_transaction_completion",
                    "financial.subscription_billing_treatments.overlapping_treatment",
                    "financial.subscription_billing_treatments.retroactive_treatment",
                    "financial.subscription_billing_treatments.stale_preview",
                    "financial.subscription_billing_treatments.subscription_not_collectible",
                    "financial.subscription_billing_treatments.subscription_not_found",
                    "financial.subscription_billing_treatments.unaligned_period",
                    "financial.subscription_billing_treatments.unaligned_start",
                ),
                mapping_owner="billing-treatment administrative adapters",
                fail_closed_on=(
                    "missing or zero contracted service value",
                    "missing end or approval beyond the registered horizon",
                    "overlap or account, offer, price, currency, or cadence drift",
                    "sponsored service without funding-party evidence",
                ),
            ),
            events=EventContract(
                event_types=("subscription_billing_treatment.changed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Additive payload evolution within schema version 1.",
                replay="Consumers deduplicate by event and arrangement command id.",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                old_owner=(
                    "zero prices, zero-price offer switching, billing flags, "
                    "long grace, and manual restore"
                ),
                new_owner="financial.subscription_billing_treatments",
                verification=(
                    "Lifecycle, price, plan-change, billing, threshold, API, "
                    "migration, and architecture tests."
                ),
                cutover_gate=(
                    "Every recurring billing and prepaid adverse-decision path "
                    "consumes the resolved treatment."
                ),
                fallback_retirement=(
                    "Zero price, billing flags, anchors, and grace are not "
                    "complimentary authority."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_subscription_billing_treatments.py",
                "tests/test_subscription_billing_treatment_api.py",
                "tests/architecture/test_subscription_billing_treatment_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.subscription_billing_grants",
        module="app.services.subscription_billing_grants",
        owns=(
            "exact non-cash subscription service-period grant",
            "non-cash grant entitlement and billing-anchor projection",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "events.dispatcher",
            "financial.subscription_billing_treatments",
            "service_intent.catalog_policy",
        ),
        notes=(
            "This flush-only participant writes an immutable grant, matching "
            "entitlement, and anchor without customer money consequences."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="exact non-cash subscription service-period grant",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "effective subscription billing treatment",
                        "canonical subscription contract",
                        "canonical recurring service value",
                        "requested service period",
                    ),
                    canonical_writer="financial.subscription_billing_grants",
                ),
                ConcernContract(
                    name="non-cash grant entitlement and billing-anchor projection",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "exact non-cash service grant",
                        "canonical subscription contract",
                    ),
                    canonical_writer="financial.subscription_billing_grants",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="effective subscription billing treatment",
                    owner="financial.subscription_billing_treatments",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="effective arrangement and approved boundaries",
                ),
                AuthorityInput(
                    name="canonical subscription contract",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Subscription account, offer, cadence, and anchor",
                ),
                AuthorityInput(
                    name="canonical recurring service value",
                    owner="service_intent.catalog_policy",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="positive contracted recurring amount and currency",
                ),
                AuthorityInput(
                    name="requested service period",
                    owner="financial.subscription_billing_grants",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="complete recurring period requested by its owner",
                ),
                AuthorityInput(
                    name="exact non-cash service grant",
                    owner="financial.subscription_billing_grants",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="append-only SubscriptionBillingGrant evidence",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary="Flush-only participant; never commits or rolls back.",
                locking=(
                    "Locks the subscription and arrangement before revalidating "
                    "the grant evidence."
                ),
                idempotency=(
                    "Arrangement, subscription, and exact interval form a "
                    "deterministic key."
                ),
                retries="The caller retries its complete transaction.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.subscription_billing_grants.approved_value_exceeded",
                    "financial.subscription_billing_grants.arrangement_not_effective",
                    "financial.subscription_billing_grants.entitlement_conflict",
                    "financial.subscription_billing_grants.grant_blocked",
                    "financial.subscription_billing_grants.grant_outside_arrangement",
                    "financial.subscription_billing_grants.idempotency_conflict",
                    "financial.subscription_billing_grants.invalid_grant_period",
                    "financial.subscription_billing_grants.invalid_reference_amount",
                    "financial.subscription_billing_grants.subscription_not_found",
                    "financial.subscription_billing_treatments.missing_contract_price",
                ),
                mapping_owner="prepaid and postpaid recurring-period owners",
                fail_closed_on=(
                    "invalid treatment evidence",
                    "offer, account, value, currency, cadence, or interval drift",
                ),
            ),
            events=EventContract(
                event_types=("subscription_service.granted",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Additive payload evolution within schema version 1.",
                replay="Consumers deduplicate by event and deterministic grant id.",
            ),
            projections=(
                ProjectionContract(
                    name="non-cash service entitlement and paid-through projection",
                    input_names=(
                        "exact non-cash service grant",
                        "canonical subscription contract",
                    ),
                    writer="financial.subscription_billing_grants",
                    freshness="Atomic with the exact service grant.",
                    stale_behavior="Billing is suppressed and drift is reported.",
                    drift_signal="Grant without exact entitlement or anchor.",
                    rebuild_operation="Replay stage_subscription_billing_grant.",
                    repair_owner="financial.subscription_billing_grants",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                old_owner="zero-price skips and manually advanced anchors",
                new_owner="financial.subscription_billing_grants",
                verification="Grant, entitlement, no-money, and architecture tests.",
                cutover_gate="Recurring owners grant before skipping customer money.",
                fallback_retirement="Future anchors are not grant evidence.",
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_subscription_billing_treatments.py",
                "tests/architecture/test_subscription_billing_treatment_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.service_extensions",
        module="app.services.service_extensions",
        owns=(
            "service-extension lifecycle and exact grant intervals",
            "immutable applied service-extension entry evidence",
            "immutable service-extension reversal evidence",
            "service-extension billing-anchor projection",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "auth.permission_gate",
            "control.settings_spec",
            "customer.accounts",
            "events.dispatcher",
            "observability.audit_log",
        ),
        notes=(
            "Typed create, apply, cancel, reverse, and anchor-repair commands are "
            "the "
            "only lifecycle writers. They stage immutable extension evidence, "
            "exact entity-linked audit records, and aggregate/per-subscription "
            "domain events in the same owner transaction. The owner records one "
            "immutable grant interval per affected subscription, starting at the "
            "later of the existing billing anchor and application time; "
            "next_billing_at, coverage, enforcement shielding, audit, events, "
            "and UI projections consume that interval rather than maintaining "
            "parallel clocks. A fingerprint-gated historical repair collapses "
            "exact duplicate rows and preserves an approved chained interval as "
            "a separately audited corrective extension without shortening "
            "customer service. Reversal preserves every original grant row, "
            "invalidates coverage through an explicit terminal status, restores "
            "only an unchanged extension-owned anchor, and records why later, "
            "lower, or terminal anchors were preserved. Access restoration "
            "remains a request to "
            "access.subscription_lifecycle."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="service-extension lifecycle and exact grant intervals",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "authenticated extension command",
                        "canonical service-extension aggregate",
                        "canonical subscriber scope",
                        "canonical subscription lifecycle and billing anchor",
                        "service-extension duration policy",
                        "reviewed service-extension reversal command",
                        "reviewed historical duplicate reconciliation command",
                    ),
                    canonical_writer="financial.service_extensions",
                ),
                ConcernContract(
                    name="immutable applied service-extension entry evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical service-extension aggregate",
                        "canonical subscription lifecycle and billing anchor",
                    ),
                    canonical_writer="financial.service_extensions",
                ),
                ConcernContract(
                    name="immutable service-extension reversal evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical service-extension aggregate",
                        "immutable applied service-extension entry evidence",
                        "canonical subscription lifecycle and billing anchor",
                        "reviewed service-extension reversal command",
                    ),
                    canonical_writer="financial.service_extensions",
                ),
                ConcernContract(
                    name="service-extension billing-anchor projection",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "canonical service-extension aggregate",
                        "canonical subscription lifecycle and billing anchor",
                        "immutable applied service-extension entry evidence",
                        "immutable service-extension reversal evidence",
                    ),
                    canonical_writer="financial.service_extensions",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated extension command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "CreateServiceExtensionCommand, "
                        "ApplyServiceExtensionCommand, "
                        "CancelServiceExtensionCommand, or "
                        "ReverseServiceExtensionCommand, "
                        "RepairServiceExtensionAnchorProjectionCommand with "
                        "CommandContext actor and reason"
                    ),
                ),
                AuthorityInput(
                    name="canonical service-extension aggregate",
                    owner="financial.service_extensions",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="service_extensions lifecycle and command evidence",
                ),
                AuthorityInput(
                    name="canonical subscriber scope",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "validated subscriber, site, zone, or network scope "
                        "resolved at command execution"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription lifecycle and billing anchor",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked Subscription identity, lifecycle status, "
                        "enforcement locks, and next_billing_at before the "
                        "extension consequence"
                    ),
                ),
                AuthorityInput(
                    name="service-extension duration policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="billing.service_extension_max_days",
                ),
                AuthorityInput(
                    name="reviewed service-extension reversal command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "billing:extension:reverse permission, normalized reason, "
                        "exact impact fingerprint, command identity, and UUID "
                        "idempotency key"
                    ),
                ),
                AuthorityInput(
                    name=("reviewed historical duplicate reconciliation command"),
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "exact cohort fingerprint, actor, reason, timestamp, "
                        "idempotency key, and chained-entitlement decision"
                    ),
                ),
                AuthorityInput(
                    name="immutable applied service-extension entry evidence",
                    owner="financial.service_extensions",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique ServiceExtensionEntry grant_starts_at, "
                        "grant_ends_at, and anchor_basis with its previous and "
                        "resulting billing-anchor interval"
                    ),
                ),
                AuthorityInput(
                    name="immutable service-extension reversal evidence",
                    owner="financial.service_extensions",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique ServiceExtensionReversal and "
                        "ServiceExtensionReversalEntry rows linking the original "
                        "grant entry to observed and resulting anchors plus a "
                        "typed restoration or preservation disposition"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each public create, apply, cancel, reverse, anchor-repair, or "
                    "historical "
                    "duplicate reconciliation command enters execute_owner_command "
                    "exactly once on a transaction-free session. Internal mutation, "
                    "access-restoration, audit, and event helpers are flush-only."
                ),
                locking=(
                    "Apply, cancel, and reverse select the extension FOR UPDATE. "
                    "Reverse also locks ordered immutable entries and affected "
                    "subscriptions before re-fingerprinting the preview. Apply "
                    "locks its resolved subscriptions in stable UUID order, and "
                    "database primary and unique keys arbitrate create and entry "
                    "races so a duplicate extension/subscription grant is "
                    "database-rejected. Historical repair locks and re-fingerprints "
                    "the complete duplicate cohort before changing evidence."
                ),
                idempotency=(
                    "Create derives its extension UUID from the form key and "
                    "compares a complete material-input fingerprint. Apply and "
                    "cancel and reverse persist command evidence and replay the stable "
                    "outcome without duplicate entries, audits, or events. "
                    "Historical repair reserves one bounded idempotency key "
                    "against the reviewed cohort fingerprint."
                ),
                retries=(
                    "Adapters retry only after complete rollback. A reused key "
                    "with changed evidence or an incompatible terminal transition "
                    "fails closed rather than granting service twice. A repair "
                    "retry returns its recorded counts and never creates another "
                    "corrective extension."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.service_extensions.access_restoration_failed",
                    "financial.service_extensions.ambiguous_customer_identifier",
                    "financial.service_extensions.blank_customer_identifier",
                    "financial.service_extensions.customer_not_found",
                    "financial.service_extensions.duplicate_reconciliation_empty_cohort",
                    "financial.service_extensions."
                    "duplicate_reconciliation_idempotency_conflict",
                    "financial.service_extensions.duplicate_reconciliation_manual_review",
                    "financial.service_extensions."
                    "duplicate_reconciliation_missing_idempotency_key",
                    "financial.service_extensions."
                    "duplicate_reconciliation_resolution_required",
                    "financial.service_extensions.duplicate_reconciliation_stale_preview",
                    "financial.service_extensions.empty_subscriber_scope",
                    "financial.service_extensions.extension_not_found",
                    "financial.service_extensions.idempotency_conflict",
                    "financial.service_extensions.invalid_customer_identifier",
                    "financial.service_extensions.invalid_days",
                    "financial.service_extensions.invalid_extension_id",
                    "financial.service_extensions.invalid_idempotency_key",
                    "financial.service_extensions.invalid_scope",
                    "financial.service_extensions.invalid_transition_action",
                    "financial.service_extensions.invalid_window",
                    "financial.service_extensions.invalid_reversal_preview",
                    "financial.service_extensions.missing_idempotency_key",
                    "financial.service_extensions.missing_reason",
                    "financial.service_extensions.missing_reversal_reason",
                    "financial.service_extensions.missing_scope_id",
                    "financial.service_extensions.network_scope_retired",
                    "financial.service_extensions.reversal_evidence_conflict",
                    "financial.service_extensions.reversal_evidence_incomplete",
                    "financial.service_extensions.reversal_reason_too_long",
                    "financial.service_extensions.self_approval_forbidden",
                    "financial.service_extensions.stale_reversal_preview",
                    "financial.service_extensions.transition_conflict",
                    "financial.service_extensions.write_conflict",
                    *owner_command_boundary_error_codes("financial.service_extensions"),
                ),
                mapping_owner="admin billing and CRM service-extension adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "changed idempotency evidence",
                    "stale or incompatible lifecycle transition",
                    "ambiguous subscriber scope",
                    "retired whole-network creation scope",
                    "failed access-restoration consequence",
                    "stale, referenced, or unsupported historical duplicates",
                ),
            ),
            events=EventContract(
                event_types=(
                    "billing.service_extension_created",
                    "billing.service_extension_applied",
                    "billing.service_extension_canceled",
                    "billing.service_extension_reversed",
                    "billing.service_extension_anchor_repaired",
                    "billing.service_extended",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries stable extension, reversal, command, correlation, "
                    "scope, status, and bounded outcome evidence without customer "
                    "contact data or full subscriber lists. Grant interval and "
                    "anchor-basis fields are additive within schema version 1."
                ),
                replay=(
                    "Consumers deduplicate aggregate events by deterministic "
                    "extension action ID and subscription consequences by "
                    "extension-entry ID."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="service-extension billing-anchor projection",
                    input_names=(
                        "canonical service-extension aggregate",
                        "canonical subscription lifecycle and billing anchor",
                        "immutable applied service-extension entry evidence",
                        "immutable service-extension reversal evidence",
                    ),
                    writer="financial.service_extensions",
                    freshness=(
                        "Atomic with each immutable ServiceExtensionEntry and "
                        "the applied aggregate transition."
                    ),
                    stale_behavior=(
                        "Access restoration fails closed and the entire owner "
                        "transaction rolls back when the anchor consequence "
                        "cannot be completed. Coverage and enforcement trust the "
                        "exact interval and report anchor drift."
                    ),
                    drift_signal=(
                        "An applied entry whose grant end or resulting anchor "
                        "differs from the visible subscription billing anchor."
                    ),
                    rebuild_operation=(
                        "Run the bounded financial.service_extensions anchor "
                        "repair command over ordered immutable applied grant "
                        "evidence. Reversal restores only anchors still equal to "
                        "the exact extension result and retains a per-subscription "
                        "terminal disposition for every preserved anchor."
                    ),
                    repair_owner="financial.service_extensions",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.service_extensions internal commits, deferred "
                    "apply auditing, overloaded applied_by cancellation evidence, "
                    "route/template lifecycle presentation, stale-anchor "
                    "addition, and the created_at-based enforcement shield"
                ),
                new_owner="financial.service_extensions",
                verification=(
                    "Lifecycle atomicity, idempotency, concurrency, "
                    "effective-interval behavior, exact coverage, CRM, "
                    "projection, audit provenance, route delegation, migration, "
                    "historical duplicate preview/apply, candidate preflight, "
                    "and architecture boundary tests."
                ),
                cutover_gate=(
                    "All create, apply, cancel, reverse, and repair adapters invoke "
                    "typed "
                    "owner commands, detail reads use the registered UI "
                    "projection, and all grant consumers read "
                    "grant_starts_at/grant_ends_at."
                ),
                fallback_retirement=(
                    "Internal commits, deferred lifecycle audit, path-based "
                    "history, and the legacy writer baseline entry are absent. "
                    "previous_next_billing_at plus days and created_at plus days "
                    "are historical evidence only, never current decisions."
                ),
            ),
            steward="billing and customer operations",
            design_refs=(
                "docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md",
                "docs/designs/SERVICE_EXTENSION_EFFECTIVE_INTERVALS.md",
                "docs/runbooks/SERVICE_EXTENSION_ACTIVITY_CUTOVER.md",
                "docs/runbooks/SERVICE_EXTENSION_REVERSAL.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_service_extensions.py",
                "tests/test_web_billing_service_extensions.py",
                "tests/test_service_extension_reversal_migration.py",
                "tests/test_prepaid_service_coverage.py",
                "tests/integration/test_service_extension_concurrency.py",
                "tests/architecture/test_service_extension_sot_boundary.py",
                "tests/architecture/test_service_extension_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_service_coverage",
        module="app.services.prepaid_service_coverage",
        owns=(
            "current prepaid service coverage classification",
            "unresolved paid-through projection classification",
            "period-scoped prepaid service coverage history",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "financial.prepaid_service_renewals",
            "financial.service_extensions",
            "financial.subscription_billing_grants",
        ),
        notes=(
            "Exact entitlement and granted-service intervals are positive "
            "coverage evidence. A future next_billing_at without evidence "
            "is a reconciliation blocker, never restoration or suspension "
            "authority. Paid invoices must first be projected into exact "
            "entitlements by the reconciliation owner."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="current prepaid service coverage classification",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical subscription projection",
                        "funded service entitlement intervals",
                        "non-cash grant service intervals",
                        "explicit granted-service intervals",
                    ),
                ),
                ConcernContract(
                    name="period-scoped prepaid service coverage history",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical subscription projection",
                        "funded service entitlement intervals",
                        "non-cash grant service intervals",
                        "explicit granted-service intervals",
                    ),
                ),
                ConcernContract(
                    name="unresolved paid-through projection classification",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical subscription projection",
                        "funded service entitlement intervals",
                        "non-cash grant service intervals",
                        "explicit granted-service intervals",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical subscription projection",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscription identity, account, lifecycle, and "
                        "diagnostic next_billing_at projection"
                    ),
                ),
                AuthorityInput(
                    name="funded service entitlement intervals",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active ServiceEntitlement interval for the exact subscription"
                    ),
                ),
                AuthorityInput(
                    name="non-cash grant service intervals",
                    owner="financial.subscription_billing_grants",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="grant-linked ServiceEntitlement exact interval",
                ),
                AuthorityInput(
                    name="explicit granted-service intervals",
                    owner="financial.service_extensions",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "applied ServiceExtensionEntry exact grant_starts_at and "
                        "grant_ends_at interval"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller owns the session; the bounded batch resolver reads "
                    "coverage evidence without writes or transaction completion."
                ),
                locking=(
                    "No read lock. State-changing access commands re-resolve "
                    "coverage after taking their canonical account lock."
                ),
                idempotency=(
                    "The same subscription cohort, as-of time, and visible "
                    "evidence produce identical typed classifications."
                ),
                retries=(
                    "Transient reads may be retried; missing evidence remains a "
                    "deterministic unresolved or uncovered classification."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner=(
                    "threshold, access, enforcement, reconciliation, and "
                    "reporting adapters"
                ),
                fail_closed_on=(
                    "future paid-through projection without current evidence",
                    "missing or contradictory coverage evidence",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "prepaid threshold direct entitlement/invoice lookups and "
                    "future billing-anchor operational inference"
                ),
                new_owner="financial.prepaid_service_coverage",
                verification=(
                    "Coverage precedence, extension interval, unresolved anchor, "
                    "threshold, access, enforcement, and architecture tests."
                ),
                cutover_gate=(
                    "Every prepaid threshold and access consequence consumes the "
                    "typed coverage decision."
                ),
                fallback_retirement=(
                    "Direct caller coverage queries and the paid-invoice read-time "
                    "fallback are removed."
                ),
            ),
            steward="billing and network access",
            design_refs=(
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_prepaid_service_coverage.py",
                "tests/test_prepaid_threshold_resolver.py",
                "tests/test_prepaid_balance_sweep.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_service_coverage_reconciliation",
        module="app.services.prepaid_coverage_reconciliation",
        owns=("exact prepaid coverage evidence reconciliation",),
        depends_on=(
            "access.subscription_lifecycle",
            "financial.account_adjustments",
            "financial.invoices",
            "financial.prepaid_service_coverage",
            "financial.prepaid_service_renewals",
            "financial.service_extensions",
        ),
        notes=(
            "A preview classifies the complete or selected prepaid cohort from "
            "structural evidence. The owner locks and rechecks the preview, "
            "creates only missing exact entitlements, and persists append-only "
            "run/item evidence; ambiguity remains quarantined."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="exact prepaid coverage evidence reconciliation",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "canonical prepaid subscription and account state",
                        "funded service entitlement intervals",
                        "exact paid invoice line periods",
                        "exact prepaid renewal adjustments",
                        "explicit granted-service intervals",
                    ),
                    canonical_writer=(
                        "financial.prepaid_service_coverage_reconciliation"
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical prepaid subscription and account state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "collectible prepaid Subscription identity, account, "
                        "lifecycle, and diagnostic paid-through projection"
                    ),
                ),
                AuthorityInput(
                    name="funded service entitlement intervals",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active ServiceEntitlement rows and exact source links",
                ),
                AuthorityInput(
                    name="exact paid invoice line periods",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active fully paid invoice, positive subscription line, "
                        "currency, and ordered billing period"
                    ),
                ),
                AuthorityInput(
                    name="exact prepaid renewal adjustments",
                    owner="financial.account_adjustments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unreversed prepaid_service_renewal adjustment, linked "
                        "active debit, and structured origin period"
                    ),
                ),
                AuthorityInput(
                    name="explicit granted-service intervals",
                    owner="financial.service_extensions",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "applied ServiceExtensionEntry exact grant_starts_at and "
                        "grant_ends_at interval"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The public reconciliation command enters the owner executor "
                    "once; preview remains read-only and adapters own only the "
                    "session lifecycle."
                ),
                locking=(
                    "Accounts, subscriptions, invoice lines/invoices, and "
                    "adjustments/ledger rows lock in deterministic identifier order "
                    "before the final fingerprint check."
                ),
                idempotency=(
                    "A unique operator idempotency key replays the immutable run "
                    "only when its preview fingerprint matches; source constraints "
                    "prevent duplicate active entitlements."
                ),
                retries=(
                    "Serialization and deadlock failures may be retried with the "
                    "same key; stale or ambiguous evidence requires a new preview."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.prepaid_service_coverage_reconciliation.active_caller_transaction",
                    "financial.prepaid_service_coverage_reconciliation.command_contract_violation",
                    "financial.prepaid_service_coverage_reconciliation.idempotency_conflict",
                    "financial.prepaid_service_coverage_reconciliation.incomplete_repair",
                    "financial.prepaid_service_coverage_reconciliation.invalid_command_context",
                    "financial.prepaid_service_coverage_reconciliation.invalid_reason",
                    "financial.prepaid_service_coverage_reconciliation.missing_idempotency_key",
                    "financial.prepaid_service_coverage_reconciliation.nested_owner_command",
                    "financial.prepaid_service_coverage_reconciliation.nested_transaction_completion",
                    "financial.prepaid_service_coverage_reconciliation.source_changed",
                    "financial.prepaid_service_coverage_reconciliation.stale_preview",
                    "financial.prepaid_service_coverage_reconciliation.subscription_not_found",
                ),
                mapping_owner="operator CLI and future admin adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "missing, malformed, duplicate, or contradictory evidence",
                    "preview drift or idempotency-key reuse with new evidence",
                    "failure to create the exact reviewed entitlement",
                ),
            ),
            events=EventContract(
                event_types=("prepaid_coverage.reconciled",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Additive payload evolution within schema version 1.",
                replay=("Consumers deduplicate by event id and the immutable run id."),
            ),
            projections=(
                ProjectionContract(
                    name="prepaid coverage reconciliation evidence",
                    input_names=(
                        "canonical prepaid subscription and account state",
                        "funded service entitlement intervals",
                        "exact paid invoice line periods",
                        "exact prepaid renewal adjustments",
                        "explicit granted-service intervals",
                    ),
                    writer=("financial.prepaid_service_coverage_reconciliation"),
                    freshness=(
                        "On-demand before repair and continuously re-evaluated by "
                        "the account-scoped prepaid quarantine."
                    ),
                    stale_behavior=(
                        "A stale fingerprint rejects confirmation; current gaps are "
                        "quarantined per account from adverse enforcement."
                    ),
                    drift_signal=(
                        "The full-cohort preview reports repairable and quarantined "
                        "counts plus a deterministic evidence hash."
                    ),
                    rebuild_operation=(
                        "preview_prepaid_coverage_reconciliation followed by the "
                        "fingerprint-bound reconcile_prepaid_service_coverage command"
                    ),
                    repair_owner=("financial.prepaid_service_coverage_reconciliation"),
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "manual SQL/one-off entitlement repair and paid-invoice "
                    "read-time coverage fallback"
                ),
                new_owner=("financial.prepaid_service_coverage_reconciliation"),
                verification=(
                    "Exact-source, ambiguity, stale preview, idempotency, event, "
                    "readiness, and architecture tests."
                ),
                cutover_gate=(
                    "Accounts with repairable or quarantined coverage evidence "
                    "remain excluded until reconciled."
                ),
                fallback_retirement=(
                    "Paid invoice rows are no longer treated as coverage at read "
                    "time; the activation timestamp setting is retired."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_prepaid_coverage_reconciliation.py",
                "tests/architecture/test_prepaid_threshold_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_threshold",
        module="app.services.prepaid_threshold",
        owns=(
            "prepaid enforcement threshold",
            "unfunded prepaid renewal requirement",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "control.settings_spec",
            "customer.accounts",
            "financial.prepaid_currency",
            "financial.prepaid_service_coverage",
            "financial.prepaid_service_coverage_reconciliation",
            "financial.prepaid_service_renewals",
            "financial.subscription_billing_treatments",
        ),
        notes=(
            "Returns typed minimum and unfunded-renewal provenance. Renewal "
            "and enforcement consume one exact taxed contract charge. Uncovered "
            "services with exact or malformed financial coverage evidence and "
            "services with missing renewal terms produce typed protected outcomes; "
            "missing accounts, invalid minimums, and cross-currency evidence fail "
            "closed."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid enforcement threshold",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical account minimum balance",
                        "prepaid default minimum setting",
                        "canonical prepaid currency",
                        "prepaid threshold protocol",
                    ),
                ),
                ConcernContract(
                    name="unfunded prepaid renewal requirement",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical collectible prepaid subscriptions",
                        "canonical current service coverage",
                        "prepaid financial coverage evidence guard",
                        "effective subscription billing treatment",
                        "exact taxed contracted renewal charge",
                        "canonical prepaid currency",
                        "prepaid threshold protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical account minimum balance",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Subscriber min_balance override for the exact account",
                ),
                AuthorityInput(
                    name="prepaid default minimum setting",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="typed billing.prepaid_default_min_balance value",
                ),
                AuthorityInput(
                    name="canonical collectible prepaid subscriptions",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "prepaid Subscription identity, lifecycle status, offer, "
                        "unit-price, and discount evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical current service coverage",
                    owner="financial.prepaid_service_coverage",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "typed covered, due-uncovered, or unresolved-projection "
                        "decision for each collectible prepaid subscription"
                    ),
                ),
                AuthorityInput(
                    name="prepaid financial coverage evidence guard",
                    owner=("financial.prepaid_service_coverage_reconciliation"),
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "typed exact or malformed invoice/renewal evidence blocker "
                        "for each uncovered collectible prepaid subscription"
                    ),
                ),
                AuthorityInput(
                    name="effective subscription billing treatment",
                    owner="financial.subscription_billing_treatments",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "standard, effective non-cash, or protected-drift "
                        "decision for each collectible subscription"
                    ),
                ),
                AuthorityInput(
                    name="exact taxed contracted renewal charge",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "positive Subscription unit_price with effective discount, "
                        "tax precedence, currency, and monthly cadence"
                    ),
                ),
                AuthorityInput(
                    name="canonical prepaid currency",
                    owner="financial.prepaid_currency",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="validated prepaid enforcement currency code",
                ),
                AuthorityInput(
                    name="prepaid threshold protocol",
                    owner="financial.prepaid_threshold",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "collectible status set, coverage and financial-evidence "
                        "guard precedence, typed missing-renewal protection, current-"
                        "coverage precedence, and due-only max(minimum, renewal) rule"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller creates and closes the session; the batched resolver "
                    "reads canonical account, service, coverage, exact renewal "
                    "charge, and setting evidence without writes or transaction "
                    "completion."
                ),
                locking=(
                    "No row lock for projections. State-changing enforcement "
                    "re-resolves the threshold inside its canonical command flow."
                ),
                idempotency=(
                    "The same account cohort, as-of time, currency, and visible "
                    "evidence produce identical typed threshold decisions."
                ),
                retries=(
                    "Transient reads may be retried. Missing renewal terms remain a "
                    "deterministic protected outcome; invalid or cross-currency "
                    "evidence remains a deterministic failure."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.prepaid_threshold.account_not_found",
                    "financial.prepaid_threshold.currency_mismatch",
                    "financial.prepaid_threshold.invalid_minimum_balance",
                ),
                mapping_owner=(
                    "access, enforcement, status, audit, and reporting adapters"
                ),
                fail_closed_on=(
                    "missing requested account",
                    "negative, non-finite, or malformed minimum balance",
                    "exact or malformed financial coverage evidence without a "
                    "current coverage projection",
                    "unfunded collectible subscription without exact renewal terms",
                    "price and enforcement currency mismatch",
                    "missing or invalid canonical prepaid currency",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "service_status scalar coverage/price implementation, primitive "
                    "threshold-only outcomes, ValueError currency mismatch, and "
                    "silent missing-price fallback"
                ),
                new_owner="financial.prepaid_threshold",
                verification=(
                    "Scalar/batch parity, query budget, renewal-charge/tax parity, "
                    "missing-term protection, paid coverage, financial-evidence "
                    "guard, provenance, caller, and architecture tests."
                ),
                cutover_gate=(
                    "Access consumes the typed threshold decision; service status "
                    "and the audit use thin scalar/batch projections of that owner."
                ),
                fallback_retirement=(
                    "The duplicate service-status derivation, untyped Any price "
                    "selection, generic ValueError, and missing-price continue path "
                    "are removed."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/audits/BILLING_SOT_AUDIT_2026-07-12.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_prepaid_threshold_resolver.py",
                "tests/test_access_resolution.py",
                "tests/architecture/test_prepaid_threshold_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.grace_policy",
        module="app.services.collections.grace_policy",
        owns=(
            "account/policy/billing-default grace precedence",
            "grace provenance and deadline",
            "post-grace elapsed-day decision",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "control.settings_spec",
            "customer.accounts",
            "customer.identity_scope",
            "financial.billing_profile",
            "service_intent.catalog_policy",
        ),
        notes=(
            "Resolves account, reseller, offer/version, policy-set, and "
            "billing-mode defaults with typed provenance. Invalid identifiers or "
            "day values halt consequences instead of becoming zero-day grace."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="account/policy/billing-default grace precedence",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical billing profile",
                        "canonical account grace configuration",
                        "canonical reseller policy assignment",
                        "canonical service policy assignments",
                        "canonical policy-set configuration",
                        "canonical grace settings",
                        "grace policy protocol",
                    ),
                ),
                ConcernContract(
                    name="grace provenance and deadline",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical billing profile",
                        "canonical account grace configuration",
                        "canonical reseller policy assignment",
                        "canonical service policy assignments",
                        "canonical policy-set configuration",
                        "canonical grace settings",
                        "grace policy protocol",
                        "evaluation time",
                    ),
                ),
                ConcernContract(
                    name="post-grace elapsed-day decision",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "grace policy protocol",
                        "evaluation time",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical billing profile",
                    owner="financial.billing_profile",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="valid effective prepaid or postpaid billing mode",
                ),
                AuthorityInput(
                    name="canonical account grace configuration",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=("Subscriber grace_period_days and policy_set_id overrides"),
                ),
                AuthorityInput(
                    name="canonical reseller policy assignment",
                    owner="customer.identity_scope",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active account reseller relationship and policy_set_id",
                ),
                AuthorityInput(
                    name="canonical service policy assignments",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "collectible Subscription lifecycle plus offer-version and "
                        "offer policy-set references"
                    ),
                ),
                AuthorityInput(
                    name="canonical policy-set configuration",
                    owner="service_intent.catalog_policy",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active PolicySet identity and grace_days",
                ),
                AuthorityInput(
                    name="canonical grace settings",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed default prepaid/postpaid policy-set identifiers and "
                        "billing-mode grace-day defaults"
                    ),
                ),
                AuthorityInput(
                    name="grace policy protocol",
                    owner="financial.grace_policy",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed provenance and phase vocabularies, service priority, "
                        "precedence order, date boundary, and non-negative-day rule"
                    ),
                ),
                AuthorityInput(
                    name="evaluation time",
                    owner="external:system_clock",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source="explicit as-of time or current UTC system time",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller owns the session; grace resolution reads canonical "
                    "account, service, policy, and setting evidence without writes "
                    "or transaction completion."
                ),
                locking=(
                    "No row lock for projections. State-changing collections and "
                    "enforcement commands re-resolve against their visible snapshot."
                ),
                idempotency=(
                    "The same account, policy override, start time, as-of time, and "
                    "visible configuration produce the same typed decision."
                ),
                retries=(
                    "Transient reads may be retried. Invalid policy identifiers, "
                    "billing profiles, or grace-day evidence remain terminal until "
                    "the authoritative input is corrected."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.grace_policy.invalid_grace_days",
                    "financial.grace_policy.invalid_policy_set_id",
                ),
                mapping_owner=(
                    "collections, prepaid enforcement, customer status, and admin "
                    "adapters"
                ),
                fail_closed_on=(
                    "missing or invalid canonical billing profile",
                    "invalid default policy-set identifier",
                    "negative, malformed, or ambiguous grace days",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "raw DomainSetting queries, primitive source/phase strings, "
                    "silent invalid policy identifiers, and invalid-day zero clamp"
                ),
                new_owner="financial.grace_policy",
                verification=(
                    "Precedence, provenance, zero-day, date-boundary, invalid-setting, "
                    "UTC-normalization, caller, and architecture tests."
                ),
                cutover_gate=(
                    "Dunning, prepaid enforcement, customer status, and admin "
                    "projections consume the shared grace decision or policy."
                ),
                fallback_retirement=(
                    "Raw setting reads, untyped provenance/phase values, invalid UUID "
                    "fallback, negative-day clamping, and invalid-setting immediate "
                    "action are removed."
                ),
            ),
            steward="collections operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/audits/BILLING_SOT_AUDIT_2026-07-12.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_grace_policy_sot.py",
                "tests/test_prepaid_balance_sweep.py",
                "tests/test_service_status.py",
                "tests/architecture/test_grace_policy_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_enforcement",
        module="app.services.prepaid_enforcement_planner",
        owns=(
            "prepaid enforcement candidate cohort",
            "prepaid warn/suspend/restore planning",
            "prepaid policy projection consumed by dry-run and execution",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "communications.customer_policy",
            "control.settings_spec",
            "customer.accounts",
            "financial.prepaid_funding_reconstruction",
            "financial.access_resolution",
            "financial.billing_profile",
            "financial.dunning",
            "financial.prepaid_currency",
            "financial.prepaid_enforcement_state",
            "financial.prepaid_threshold",
            "financial.grace_policy",
            "service_intent.catalog_policy",
        ),
        notes=(
            "The production sweep, dry-run, and audit consume one "
            "typed cohort and account plan. Planning is read-only; execution and "
            "timer/access mutation remain with their canonical writers."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid enforcement candidate cohort",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical account eligibility",
                        "canonical subscription lifecycle state",
                        "canonical prepaid enforcement locks and timers",
                        "prepaid enforcement protocol",
                    ),
                ),
                ConcernContract(
                    name="prepaid warn/suspend/restore planning",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical billing profile",
                        "canonical prepaid funding decision",
                        "canonical grace decision",
                        "canonical financial shields",
                        "canonical communication suppression",
                        "canonical service bundle policy",
                        "canonical prepaid policy settings",
                        "prepaid enforcement protocol",
                        "evaluation time",
                    ),
                ),
                ConcernContract(
                    name="prepaid policy projection consumed by dry-run and execution",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical prepaid policy settings",
                        "prepaid enforcement protocol",
                        "evaluation time",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical account eligibility",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscriber identity, lifecycle status, active, billing "
                        "enabled, billing mode, and prepaid timer fields"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription lifecycle state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "collectible Subscription identity, status, billing mode, "
                        "and account relationship"
                    ),
                ),
                AuthorityInput(
                    name="canonical prepaid enforcement locks and timers",
                    owner="financial.prepaid_enforcement_state",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "prepaid timer fields and active prepaid EnforcementLock rows"
                    ),
                ),
                AuthorityInput(
                    name="canonical billing profile",
                    owner="financial.billing_profile",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="typed effective billing mode and automation safety",
                ),
                AuthorityInput(
                    name="canonical prepaid funding decision",
                    owner="financial.access_resolution",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=("currency-bound available balance and prepaid threshold"),
                ),
                AuthorityInput(
                    name="canonical grace decision",
                    owner="financial.grace_policy",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="typed grace provenance, deadline, phase, and elapsed days",
                ),
                AuthorityInput(
                    name="canonical financial shields",
                    owner="financial.dunning",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "payment arrangement, proof review, dispute, outage, and "
                        "other collection-shield reasons"
                    ),
                ),
                AuthorityInput(
                    name="canonical communication suppression",
                    owner="communications.customer_policy",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=("customer-impact fault suppression for suspension notices"),
                ),
                AuthorityInput(
                    name="canonical service bundle policy",
                    owner="service_intent.catalog_policy",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="dedicated SubscriptionBundle classification",
                ),
                AuthorityInput(
                    name="canonical prepaid policy settings",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "customer communication templates plus the shared daily "
                        "time-of-day enforcement window"
                    ),
                ),
                AuthorityInput(
                    name="prepaid enforcement protocol",
                    owner="financial.prepaid_enforcement",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed action, policy-issue, and reason-source vocabularies; "
                        "cohort repair inclusion; deterministic decision precedence"
                    ),
                ),
                AuthorityInput(
                    name="evaluation time",
                    owner="external:system_clock",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source="explicit as-of time or current UTC system time",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller owns the session. Cohort and account planning read "
                    "canonical evidence and never write or complete a transaction."
                ),
                locking=(
                    "No row lock for planning. The execution owner locks and "
                    "re-resolves account, timer, lock, funding, and policy evidence "
                    "before applying a consequence."
                ),
                idempotency=(
                    "The same account selection, as-of time, and visible canonical "
                    "evidence produce the same ordered typed plan."
                ),
                retries=(
                    "Transient reads may be retried. Invalid account identifiers, "
                    "missing accounts and missing policy text remain deterministic "
                    "failures."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.prepaid_enforcement.account_not_found",
                    "financial.prepaid_enforcement.invalid_account_id",
                    "financial.prepaid_enforcement.missing_policy_text",
                ),
                mapping_owner=("prepaid sweep, audit, and operator-report adapters"),
                fail_closed_on=(
                    "missing or invalid selected account",
                    "invalid billing profile, funding, threshold, grace, or currency",
                    "missing communication policy text",
                    "active financial shield",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "primitive Any-based cohort/outcome maps, raw string policy "
                    "issues and provenance, generic ValueError selection failures, "
                    "and permissive malformed window/holiday policy"
                ),
                new_owner="financial.prepaid_enforcement",
                verification=(
                    "Cohort, repair-only, funding, grace, drift, shield, window, "
                    "sweep, failure, and architecture tests."
                ),
                cutover_gate=(
                    "Sweep, dry-run, deployment integrity, and funding audit callers "
                    "consume the canonical planner and typed outcomes."
                ),
                fallback_retirement=(
                    "Any-typed identifiers/maps, generic ValueError, untyped policy "
                    "issues/provenance and parallel calendar skip rules are removed."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_prepaid_enforcement_planner.py",
                "tests/test_prepaid_balance_sweep.py",
                "tests/architecture/test_prepaid_enforcement_policy_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_enforcement_state",
        module="app.services.prepaid_enforcement_state",
        owns=(
            "prepaid low-balance timer state",
            "prepaid deactivation timer state",
            "funded and terminal prepaid timer cleanup",
        ),
        depends_on=("events.dispatcher",),
        notes=(
            "Writes prepared timer observations and cleanup requests in "
            "the caller transaction. It owns no eligibility, threshold, "
            "grace, suspension, restoration, or commit decision."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid low-balance timer state",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "resolved prepaid enforcement transition",
                        "canonical prepaid enforcement timers",
                    ),
                    canonical_writer="financial.prepaid_enforcement_state",
                ),
                ConcernContract(
                    name="prepaid deactivation timer state",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "resolved prepaid enforcement transition",
                        "canonical prepaid enforcement timers",
                    ),
                    canonical_writer="financial.prepaid_enforcement_state",
                ),
                ConcernContract(
                    name="funded and terminal prepaid timer cleanup",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "resolved prepaid enforcement transition",
                        "resolved account lifecycle transition",
                        "canonical prepaid enforcement timers",
                    ),
                    canonical_writer="financial.prepaid_enforcement_state",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="resolved prepaid enforcement transition",
                    owner="financial.prepaid_enforcement",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "locked prepaid enforcement plan and successful "
                        "suspend, restore, or funding consequence"
                    ),
                ),
                AuthorityInput(
                    name="resolved account lifecycle transition",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="terminal account status derived from subscription facts",
                ),
                AuthorityInput(
                    name="canonical prepaid enforcement timers",
                    owner="financial.prepaid_enforcement_state",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscriber.prepaid_low_balance_at and "
                        "Subscriber.prepaid_deactivation_at"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "Only contracted enforcement and lifecycle owners call "
                    "the participant. It locks the Subscriber row and "
                    "flushes timer plus event evidence without committing."
                ),
                locking=(
                    "Every transition selects the canonical Subscriber row "
                    "FOR UPDATE before inspecting or changing timer state."
                ),
                idempotency=(
                    "Arm and deactivation preserve the first timestamp; "
                    "equivalent repeats and already-clear cleanup are no-ops."
                ),
                retries=(
                    "The surrounding owner retries its complete transaction; "
                    "the participant never retries or commits independently."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.prepaid_enforcement_state.invalid_account_id",
                    "financial.prepaid_enforcement_state.account_not_found",
                ),
                mapping_owner=(
                    "financial.prepaid_enforcement and "
                    "access.subscription_lifecycle coordinators"
                ),
                fail_closed_on=(
                    "malformed or missing canonical account",
                    "unlocked or ambiguous timer state",
                ),
            ),
            events=EventContract(
                event_types=("prepaid_enforcement.timer_changed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and contains only the account "
                    "identifier and transition vocabulary."
                ),
                replay=(
                    "Equivalent commands are no-ops. Current timer fields "
                    "are authoritative; events retain transition evidence."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "collections and account-lifecycle call sites mutating "
                    "Subscriber prepaid timer fields directly"
                ),
                new_owner="financial.prepaid_enforcement_state",
                verification=(
                    "Focused state, sweep, lifecycle, atomic-event, and "
                    "single-writer architecture tests."
                ),
                cutover_gate=(
                    "Every prepaid timer transition calls the locked, "
                    "flush-only participant from a named owner."
                ),
                fallback_retirement=(
                    "Direct prepaid timer assignments outside the owner and "
                    "silent missing-account behavior are removed."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_prepaid_enforcement_state_owner.py",
                "tests/architecture/test_prepaid_enforcement_state_boundary.py",
                "tests/test_prepaid_balance_sweep.py",
                "tests/test_account_lifecycle.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_plan_change",
        module="app.services.prepaid_plan_changes",
        owns=(
            "prepaid plan-change proration decision",
            "prepaid plan-change funding affordability",
            "human preview fingerprint and locked confirmation",
            "plan-change confirmation idempotency and actor audit",
            "exact change-request-to-financial-evidence links",
            "idempotent plan-change debit and credit staging",
        ),
        depends_on=(
            "financial.account_adjustments",
            "financial.credit_notes",
            "customer.financial_position",
        ),
        notes=(
            "Immediate changes bind the displayed owner preview to a "
            "durable change request, lock and recompute at write time, "
            "then commit the request, exact financial evidence, and "
            "subscription together. Bulk changes remain next-cycle only "
            "until they have per-subscription previews."
        ),
    ),
    SOTService(
        name="financial.prepaid_billing_calendar_reconciliation",
        module="app.services.prepaid_billing_calendar_reconciliation",
        owns=("historical prepaid billing calendar reconciliation",),
        depends_on=(
            "access.subscription_lifecycle",
            "access.fup_usage_windows",
            "financial.dunning",
            "financial.invoices",
            "financial.payments",
            "financial.prepaid_enforcement_state",
            "financial.prepaid_service_renewals",
            "observability.audit_log",
        ),
        notes=(
            "A reviewed, fingerprint-bound repair owner for the retired "
            "UTC-midnight prepaid settlement calculation and proved lapsed "
            "payments left on stale documentary coverage. It changes the exact "
            "invoice period, base-line period projection, sourced entitlement "
            "interval, and subscription anchor. A current lapsed repair may "
            "resolve only prepaid enforcement through the canonical lifecycle "
            "protocol. Money, allocation, settlement, invoice status, and "
            "ledger evidence remain unchanged. Ambiguous chains fail closed."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="historical prepaid billing calendar reconciliation",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "reviewed calendar correction command",
                        "canonical paid prepaid invoice chain",
                        "canonical settlement business calendar",
                        "rated quota period evidence",
                        "financial access restoration protocol",
                    ),
                    canonical_writer=(
                        "financial.prepaid_billing_calendar_reconciliation"
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="reviewed calendar correction command",
                    owner="financial.prepaid_billing_calendar_reconciliation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed invoice identity, signed preview fingerprint, "
                        "actor, reason, command, correlation, and idempotency "
                        "evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical paid prepaid invoice chain",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "one active paid invoice, one base-subscription line, "
                        "one succeeded allocated settlement, one sourced active "
                        "entitlement, and either the exact legacy anchor or a "
                        "strictly older stale anchor"
                    ),
                ),
                AuthorityInput(
                    name="rated quota period evidence",
                    owner="access.fup_usage_windows",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "persisted subscription QuotaBucket intervals that "
                        "would require a coordinated usage-owner correction"
                    ),
                ),
                AuthorityInput(
                    name="canonical settlement business calendar",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "typed settlement instant and cadence resolved through "
                        "Africa/Lagos local midnight and persisted as UTC instants"
                    ),
                ),
                AuthorityInput(
                    name="financial access restoration protocol",
                    owner="financial.dunning",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "reason-scoped prepaid lock resolution and subscription "
                        "restoration through the canonical lifecycle owner, with "
                        "independent enforcement and lifecycle blockers preserved"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Reviewed confirmation enters execute_owner_command once on "
                    "a transaction-free session, rechecks the exact chain under "
                    "lock, stages calendar projections, any scoped access "
                    "consequence, audit, outbox event, and idempotency evidence, "
                    "then commits or rolls back together."
                ),
                locking=(
                    "Lock account first, then invoice, subscription, invoice "
                    "line, entitlement, payment, allocation, settlement, and "
                    "active enforcement locks; "
                    "expire and re-read the full chain before re-running the "
                    "resolver and reject changed or overlapping evidence."
                ),
                idempotency=(
                    "A caller key is reserved per invoice and the invoice stores "
                    "the exact fingerprint and before/after evidence for stable "
                    "replay."
                ),
                retries=(
                    "Replay a completed identical command. Changed, overlapping, "
                    "returned, actively extended, or ambiguous evidence requires a fresh "
                    "review and is never guessed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.prepaid_billing_calendar_reconciliation.invoice_not_found",
                    "financial.prepaid_billing_calendar_reconciliation.missing_idempotency_key",
                    "financial.prepaid_billing_calendar_reconciliation.invalid_reason",
                    "financial.prepaid_billing_calendar_reconciliation.idempotency_conflict",
                    "financial.prepaid_billing_calendar_reconciliation.stale_preview",
                    "financial.prepaid_billing_calendar_reconciliation.not_actionable",
                    "financial.prepaid_billing_calendar_reconciliation.invalid_command_context",
                    "financial.prepaid_billing_calendar_reconciliation.command_contract_violation",
                    "financial.prepaid_billing_calendar_reconciliation.nested_owner_command",
                    "financial.prepaid_billing_calendar_reconciliation.active_caller_transaction",
                    "financial.prepaid_billing_calendar_reconciliation.nested_transaction_completion",
                ),
                mapping_owner="admin billing-date reconciliation adapter",
                retryable_codes=(),
                fail_closed_on=(
                    "non-paid or multi-line invoice evidence",
                    "missing or multiple succeeded settlement allocations",
                    "refund, reversal, applied extension, or overlap",
                    "an overlapping rated quota period",
                    "a period/anchor relationship that proves neither a retired "
                    "UTC signature nor a lapsed-payment correction",
                    "stale preview or active caller transaction",
                ),
            ),
            events=EventContract(
                event_types=("prepaid_billing_calendar.reconciled",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Invoice, subscription, entitlement, payment, timezone, "
                    "before/after instants, correction kind, zero economic delta, "
                    "access outcome, and fingerprint retain their meaning; "
                    "additions are backward compatible."
                ),
                replay=(
                    "Consumers may rebuild evidence views but never re-decide "
                    "or rewrite financial or service state."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="historical prepaid billing calendar reconciliation",
                    input_names=(
                        "canonical paid prepaid invoice chain",
                        "canonical settlement business calendar",
                    ),
                    writer=("financial.prepaid_billing_calendar_reconciliation"),
                    freshness="computed from the current database snapshot",
                    stale_behavior=(
                        "Confirmation rejects the changed fingerprint and "
                        "requires a fresh preview."
                    ),
                    drift_signal=(
                        "An exact retired UTC-period signature or proved lapsed-"
                        "payment period remains in the review queue until "
                        "corrected or quarantined."
                    ),
                    rebuild_operation=(
                        "preview_prepaid_billing_calendar_cohort deterministically "
                        "reclassifies the bounded paid-invoice cohort."
                    ),
                    repair_owner=("financial.prepaid_billing_calendar_reconciliation"),
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUT_OVER,
                old_owner=(
                    "historical payment settlement path that floored the "
                    "settlement instant at UTC midnight"
                ),
                new_owner=("financial.prepaid_billing_calendar_reconciliation"),
                verification=(
                    "Eligible legacy and lapsed-payment, scoped lock restoration, "
                    "independent blocker, stale, replay, refund, applied/reversed "
                    "extension, overlap, moved-anchor, UTC-boundary, UI permission, "
                    "and signed-review tests."
                ),
                cutover_gate=(
                    "Forward settlement periods resolve in Africa/Lagos and the "
                    "historical admin queue is preview-only until explicit "
                    "fingerprint-bound confirmation."
                ),
                fallback_retirement=(
                    "Retire the queue after the staging-accepted cohort is "
                    "reconciled and a verification scan reports no exact legacy "
                    "signatures or proved lapsed-payment defects."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/designs/PREPAID_BILLING_CALENDAR_RECONCILIATION.md",
            ),
            test_refs=(
                "tests/test_prepaid_billing_calendar_reconciliation.py",
                "tests/test_web_prepaid_billing_calendar_reconciliation.py",
                "tests/architecture/test_prepaid_billing_anchor_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_draft_reconciliation",
        module="app.services.prepaid_draft_reconciliation",
        owns=(
            "funded onboarding proforma documentary adoption",
            "historical paid prepaid invoice identity and coverage repair",
            "reviewed missing prepaid paid-invoice repair",
            "stranded prepaid draft classification",
            "stranded prepaid draft invoice reconciliation",
            "reviewed opening funding invoice consumption",
            "prepaid draft reconciliation exceptions and operator alerts",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "financial.account_credit_applications",
            "financial.dunning",
            "financial.invoices",
            "financial.ledger",
            "financial.payments",
            "financial.prepaid_funding_reconstruction",
            "financial.prepaid_service_renewals",
            "communications.staff_notifications",
            "observability.audit_log",
        ),
        notes=(
            "The invoice-first classifier distinguishes exact settlement-"
            "backed payments, reviewed opening funding, insufficient or "
            "unbacked funding, direct-renewal overlap, and ambiguity. "
            "Reviewed confirmation consumes payment settlements first and "
            "then records only the exact remainder as typed opening-funding "
            "consumption; opening funding is never represented as a Payment. "
            "When an active reviewed opening baseline exists, current "
            "account-credit evidence is scoped to native payment and ledger "
            "facts crossing its position timestamp; pre-boundary mirror rows "
            "are absorbed by the signed opening and cannot be reused or "
            "quarantined again. "
            "Automatic funding changes create a durable operator exception "
            "instead of silently leaving an authoritatively funded draft. "
            "The admin invoice adapter presents the same exact classifier "
            "output and submits an actor-bound, signed, fingerprinted review "
            "to this owner; it does not maintain a second settlement path. "
            "Every existing draft blocks the parallel invoice-less renewal "
            "path, and generic Restore cannot bypass an unresolved prepaid "
            "financial lock. A separate dry-run-first adoption concern can "
            "restore the documentary identity of one pristine onboarding "
            "proforma only when an operator names the matching active, "
            "unanchored prepaid subscription, its contracted base charge "
            "matches exactly, one native payment funds the full gross "
            "document, and the reviewed funding baseline is available. The "
            "sole payment timestamp and contracted cadence resolve the WAT "
            "service period; adoption has no economic effect and hands the "
            "resulting financial draft back to the ordinary reconciler. "
            "A separate reviewed historical repair accepts only one already-"
            "paid, periodless document whose sole active allocation is fully "
            "backed by a successful unreturned settlement and whose charge "
            "matches the current canonical prepaid renewal terms. It writes "
            "only missing document identity, entitlement, billing anchor, and "
            "the canonical access-restoration consequence with zero economic "
            "delta. A separate entity-scoped missing-invoice command accepts "
            "one operator-named account, subscription, and successful native "
            "payment plus exact business dates, contract total, and expected "
            "remaining credit. It creates and settles one invoice only when "
            "those fingerprinted facts remain exact and no competing document "
            "or coverage exists; fully payment-backed repair does not depend "
            "on or alter the migrated opening baseline."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="funded onboarding proforma documentary adoption",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "reviewed reconciliation command",
                        "canonical funded onboarding proforma",
                        "canonical prepaid subscription contract",
                        "canonical payment-backed account credit",
                        "reviewed opening funding",
                        "canonical settlement business calendar",
                        "invoice and payment participant protocols",
                    ),
                    canonical_writer="financial.prepaid_draft_reconciliation",
                ),
                ConcernContract(
                    name=(
                        "historical paid prepaid invoice identity and coverage repair"
                    ),
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "reviewed reconciliation command",
                        "canonical paid prepaid document gap",
                        "canonical prepaid subscription contract",
                        "canonical paid invoice allocation evidence",
                        "canonical settlement business calendar",
                        "financial access restoration protocol",
                    ),
                    canonical_writer="financial.prepaid_draft_reconciliation",
                ),
                ConcernContract(
                    name="reviewed missing prepaid paid-invoice repair",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "reviewed reconciliation command",
                        "canonical prepaid subscription contract",
                        "canonical payment-backed account credit",
                        "canonical paid invoice allocation evidence",
                        "invoice and payment participant protocols",
                    ),
                    canonical_writer="financial.prepaid_draft_reconciliation",
                ),
                ConcernContract(
                    name="stranded prepaid draft classification",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical prepaid draft invoice",
                        "canonical payment-backed account credit",
                        "canonical funded service entitlement",
                        "canonical direct-renewal debit",
                    ),
                ),
                ConcernContract(
                    name="stranded prepaid draft invoice reconciliation",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "reviewed reconciliation command",
                        "canonical prepaid draft invoice",
                        "canonical payment-backed account credit",
                        "reviewed opening funding",
                        "canonical funded service entitlement",
                        "canonical direct-renewal debit",
                        "invoice and payment participant protocols",
                    ),
                    canonical_writer="financial.prepaid_draft_reconciliation",
                ),
                ConcernContract(
                    name="reviewed opening funding invoice consumption",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "reviewed reconciliation command",
                        "canonical prepaid draft invoice",
                        "canonical payment-backed account credit",
                        "reviewed opening funding",
                    ),
                    canonical_writer="financial.prepaid_draft_reconciliation",
                ),
                ConcernContract(
                    name=(
                        "prepaid draft reconciliation exceptions and operator alerts"
                    ),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical prepaid draft invoice",
                        "canonical payment-backed account credit",
                        "reviewed opening funding",
                    ),
                    canonical_writer="financial.prepaid_draft_reconciliation",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="reviewed reconciliation command",
                    owner="financial.prepaid_draft_reconciliation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed invoice identity, exact preview fingerprint, "
                        "or exact account, subscription, payment, business "
                        "dates, total, remaining-credit expectation, actor, reason, command, "
                        "correlation, and idempotency evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical prepaid draft invoice",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active non-proforma draft, exact positive "
                        "subscription line, period, currency, totals, and "
                        "existing settlement evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical funded onboarding proforma",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked pristine active proforma, one positive "
                        "unlinked line, currency, exact subtotal, tax, gross "
                        "balance, and absence of financial activity"
                    ),
                ),
                AuthorityInput(
                    name="canonical paid prepaid document gap",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active paid non-proforma invoice with zero balance, "
                        "one positive unlinked line, missing period identity, "
                        "exact totals, and no credit-note funding"
                    ),
                ),
                AuthorityInput(
                    name="canonical prepaid subscription contract",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "operator-named matching account subscription, active "
                        "prepaid state, frozen unit price, contracted cadence, "
                        "unanchored billing state, and absence of coverage"
                    ),
                ),
                AuthorityInput(
                    name="canonical payment-backed account credit",
                    owner="financial.account_credit_applications",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "exact active succeeded settlement capacity and "
                        "account-credit facts crossing the active reviewed "
                        "opening-position boundary when present, source "
                        "payments, and shortfall; pre-boundary mirror residue "
                        "is excluded"
                    ),
                ),
                AuthorityInput(
                    name="canonical paid invoice allocation evidence",
                    owner="financial.payments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "one active full-value invoice allocation, matching "
                        "active succeeded Payment and settlement, currency, "
                        "paid-at instant, and absence of refund or reversal"
                    ),
                ),
                AuthorityInput(
                    name="reviewed opening funding",
                    owner="financial.prepaid_funding_reconstruction",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active typed reconstruction baseline or immutable "
                        "approved subledger opening, its fingerprinted source "
                        "evidence, prior immutable invoice consumptions, and "
                        "current verified prepaid funding position"
                    ),
                ),
                AuthorityInput(
                    name="canonical funded service entitlement",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active entitlement account, subscription, exact "
                        "period, funding amount, currency, and source link"
                    ),
                ),
                AuthorityInput(
                    name="canonical direct-renewal debit",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unreversed prepaid-service-renewal adjustment and "
                        "linked active ledger debit"
                    ),
                ),
                AuthorityInput(
                    name="canonical settlement business calendar",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "sole exact source payment paid-at instant resolved "
                        "through the contracted cadence in Africa/Lagos"
                    ),
                ),
                AuthorityInput(
                    name="invoice and payment participant protocols",
                    owner="financial.invoices",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "flush-only invoice/line construction, issue/void, and exact payment-"
                        "allocation confirmation protocols, including the "
                        "authoritative post-allocation invoice remainder"
                    ),
                ),
                AuthorityInput(
                    name="financial access restoration protocol",
                    owner="financial.dunning",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "fingerprint-bound prepaid restoration preview and "
                        "confirmation through the subscription lifecycle owner"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Reviewed confirmation enters execute_owner_command once "
                    "on a transaction-free session and commits or rolls back "
                    "the invoice lifecycle, exact payment applications, typed "
                    "opening-funding consumption and structural ledger link, "
                    "entitlement, billing anchor, access restoration, audit, "
                    "event, exception resolution, and idempotency evidence "
                    "together. The proforma-adoption command is a separate "
                    "owner root that commits only documentary identity, audit, "
                    "event, and idempotency evidence; it posts no money and "
                    "creates no entitlement. Its resulting valid prepaid draft "
                    "then enters the existing reviewed settlement command. The "
                    "historical paid-invoice repair locks and recomputes exact "
                    "allocation and settlement evidence, then commits document "
                    "identity, entitlement, reviewed anchor projection, access "
                    "consequence, audit, event, and idempotency evidence with "
                    "zero economic delta. The "
                    "missing-invoice repair is another owner root: it locks "
                    "the named account, subscription, and payment, rechecks "
                    "the preview, and commits document construction, issue, "
                    "exact allocation, entitlement, reviewed anchor projection, "
                    "audit, event, metadata, and idempotency evidence together. The "
                    "funding-change caller uses the same flush-only classifier "
                    "inside its existing transaction."
                ),
                locking=(
                    "Lock account first, then invoice or selected subscription "
                    "and payment, subscription when "
                    "adopting a proforma, eligible payment and "
                    "settlement records, and the opening-funding baseline; "
                    "re-read consumption, entitlement, adjustment, and "
                    "allocation evidence before writing. A multiple-draft "
                    "account is not automatically repaired."
                ),
                idempotency=(
                    "A caller-supplied key is reserved per invoice or reviewed "
                    "missing-document command and concern; "
                    "invoice "
                    "metadata, one-per-invoice opening-consumption uniqueness, "
                    "and participant idempotency keys replay the same paid or "
                    "void result and reject changed evidence."
                ),
                retries=(
                    "Retry transient database failures with the same key and "
                    "preview only after state is unchanged. Stale, short, "
                    "unbacked, overlapping, or ambiguous evidence requires a "
                    "fresh preview or manual review."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.prepaid_draft_reconciliation.invoice_not_found",
                    "financial.prepaid_draft_reconciliation.missing_idempotency_key",
                    "financial.prepaid_draft_reconciliation.idempotency_conflict",
                    "financial.prepaid_draft_reconciliation.stale_preview",
                    "financial.prepaid_draft_reconciliation.not_actionable",
                    "financial.prepaid_draft_reconciliation.participant_rejected",
                    "financial.prepaid_draft_reconciliation.incomplete_repair",
                    "financial.prepaid_draft_reconciliation.opening_funding_unavailable",
                    "financial.prepaid_draft_reconciliation.opening_funding_changed",
                    "financial.prepaid_draft_reconciliation.review_required",
                    "financial.prepaid_draft_reconciliation.invalid_command_context",
                    "financial.prepaid_draft_reconciliation.command_contract_violation",
                    "financial.prepaid_draft_reconciliation.nested_owner_command",
                    "financial.prepaid_draft_reconciliation.active_caller_transaction",
                    "financial.prepaid_draft_reconciliation.nested_transaction_completion",
                ),
                mapping_owner=(
                    "billing reconciliation CLI, admin invoice, and "
                    "funding-change adapters"
                ),
                retryable_codes=(),
                fail_closed_on=(
                    "any funding shortfall including NGN 0.50",
                    "unbacked account credit crossing the active reviewed "
                    "opening-position boundary, or any unbacked account "
                    "credit when no active baseline exists",
                    "multiple drafts or positive lines",
                    "a proforma with a period, linked or multiple lines, "
                    "contract mismatch, existing coverage, multiple payment "
                    "sources, missing payment timestamp, or anchored subscription",
                    "an already-paid invoice without one exact active full-value "
                    "allocation and successful unreturned settlement, or whose "
                    "charge differs from canonical renewal terms",
                    "a missing-invoice repair with changed contract tax, dates, "
                    "payment capacity, expected remaining credit, competing "
                    "document, or overlapping entitlement",
                    "partial or ambiguous entitlement overlap",
                    "stale preview, changed payment capacity, participant "
                    "remainder mismatch, or already consumed opening funding",
                ),
            ),
            events=EventContract(
                event_types=(
                    "prepaid_proforma.adopted",
                    "prepaid_paid_invoice.repaired",
                    "prepaid_draft.reconciled",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Additive payload fields are permitted. Adoption retains "
                    "invoice, subscription, sole payment, period, currency, "
                    "amount, and preview identity; settlement retains invoice, "
                    "action, source disposition, final status, amount, "
                    "currency, and preview fingerprint meaning."
                ),
                replay=(
                    "The event records the committed reconciliation outcome. "
                    "Consumers may replay consequences but never re-decide or "
                    "rewrite invoice, payment, or entitlement state."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="funded onboarding proforma documentary adoption",
                    input_names=(
                        "canonical funded onboarding proforma",
                        "canonical prepaid subscription contract",
                        "canonical payment-backed account credit",
                        "reviewed opening funding",
                        "canonical settlement business calendar",
                    ),
                    writer="financial.prepaid_draft_reconciliation",
                    freshness="computed from the current database snapshot",
                    stale_behavior=(
                        "Confirmation rejects a changed fingerprint and "
                        "requires a fresh preview."
                    ),
                    drift_signal=(
                        "An exact funded onboarding proforma remains "
                        "classified but unadopted, or an adopted financial "
                        "draft remains unreconciled."
                    ),
                    rebuild_operation=(
                        "preview_funded_prepaid_proforma_adoption reclassifies "
                        "one operator-named invoice and subscription pair."
                    ),
                    repair_owner="financial.prepaid_draft_reconciliation",
                ),
                ProjectionContract(
                    name="stranded prepaid draft classification",
                    input_names=(
                        "canonical prepaid draft invoice",
                        "canonical payment-backed account credit",
                        "reviewed opening funding",
                        "canonical funded service entitlement",
                        "canonical direct-renewal debit",
                    ),
                    writer="financial.prepaid_draft_reconciliation",
                    freshness="computed from the current database snapshot",
                    stale_behavior=(
                        "Confirmation rejects a changed fingerprint and requires "
                        "a fresh preview."
                    ),
                    drift_signal=(
                        "An active prepaid draft classified as exact-payment "
                        "fundable, reviewed-opening-fundable, already renewed, "
                        "legacy-unbacked, insufficient, or manual review "
                        "remains visible in the cohort; a durable open "
                        "exception signals automatic review work."
                    ),
                    rebuild_operation=(
                        "preview_prepaid_draft_cohort deterministically "
                        "reclassifies the bounded or complete active cohort."
                    ),
                    repair_owner="financial.prepaid_draft_reconciliation",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUT_OVER,
                old_owner=(
                    "billing.reconcile_unposted issue-then-return helper and "
                    "invoice-less funding-change renewal when a draft exists"
                ),
                new_owner="financial.prepaid_draft_reconciliation",
                verification=(
                    "Exact funded onboarding proforma adoption, contract and "
                    "baseline mismatch rejection, replay, documentary-only "
                    "intermediate state, subsequent draft settlement, exact "
                    "already-paid invoice identity/coverage repair, settlement "
                    "ambiguity rejection, zero economic delta, access consequence, "
                    "fee-inclusive mixed funding, partial funding, exact "
                    "nonzero shortfall, pre-boundary residue absorption, post-boundary "
                    "unbacked or reversed payment evidence, "
                    "direct-renewal overlap, multiple drafts, stale preview, "
                    "replay, concurrency, lapsed re-anchoring, opening-funding "
                    "double-spend, Restore guard, and architecture tests."
                ),
                cutover_gate=(
                    "Funding-change handling checks the authoritative draft "
                    "before direct renewal; the reviewed CLI defaults to dry-run; "
                    "the invoice page confirms only signed owner previews."
                ),
                fallback_retirement=(
                    "Remove the compatibility issue-then-return helper after "
                    "all remaining callers use the classifier and the backlog "
                    "has been reviewed. The prepaid-recovery settlement writer "
                    "and invoice-page adapter are retired."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/designs/PREPAID_DRAFT_RECONCILIATION.md",
            ),
            test_refs=(
                "tests/test_prepaid_draft_reconciliation.py",
                "tests/test_web_prepaid_draft_reconciliation.py",
                "tests/test_prepaid_service_renewals.py",
                "tests/test_subscription_lifecycle_commands.py",
                "tests/integration/test_prepaid_draft_reconciliation_concurrency.py",
                "tests/architecture/test_prepaid_draft_reconciliation_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.walled_account_healing",
        module="app.services.billing.unwall_paid_accounts",
        owns=(
            "per-account healing timer lifecycle",
            "locked zero-overdue-receivable healing decision",
            "walled-account healing operator exceptions",
        ),
        depends_on=(
            "financial.access_resolution",
            "financial.billing_profile",
            "financial.payments",
            "collections.lifecycle",
            "access.subscription_lifecycle",
            "runtime.durable_timers",
            "events.owner_outputs",
        ),
        notes=(
            "Service-state only: this owner posts, moves and forgives no "
            "money. Each committed payment or account-credit event schedules "
            "one exact durable account timer; the generic timer runtime is "
            "the only scanner. The fired trigger is receipted before this "
            "owner recomputes the exact overdue receivable under an account "
            "lock and requests the financial-access restoration owner. "
            "Application is allowed only when that recomputation proves zero "
            "overdue receivable; there is no tolerance, epsilon or de-minimis "
            "threshold, so a sub-naira residue correctly blocks the automated "
            "restore. Every ambiguous or blocked row becomes a durable, "
            "deduplicated operator exception with its recomputed evidence "
            "instead of an automated guess. Restoration reason scoping stays "
            "with the lifecycle owner: healing never lifts an admin, fraud or "
            "FUP lock and never clears a lifecycle override."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="per-account healing timer lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "settled funding-change event",
                        "durable timer runtime",
                    ),
                    canonical_writer="financial.walled_account_healing",
                ),
                ConcernContract(
                    name="locked zero-overdue-receivable healing decision",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "fired account healing trigger",
                        "canonical account access state",
                        "exact overdue receivable snapshot",
                    ),
                    canonical_writer="financial.walled_account_healing",
                ),
                ConcernContract(
                    name="walled-account healing operator exceptions",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "canonical account access state",
                        "exact overdue receivable snapshot",
                    ),
                    canonical_writer="financial.walled_account_healing",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="settled funding-change event",
                    owner="financial.payments",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "committed payment_received or "
                        "account_credit_deposited event with exact account "
                        "identity and durable event identity"
                    ),
                ),
                AuthorityInput(
                    name="durable timer runtime",
                    owner="runtime.durable_timers",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "owner, subscriber entity, purpose, generation, due "
                        "time, fired status, and fired event identity"
                    ),
                ),
                AuthorityInput(
                    name="fired account healing trigger",
                    owner="runtime.durable_timers",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "receipted durable-timer trigger carrying timer, "
                        "subscriber, purpose, and generation identity"
                    ),
                ),
                AuthorityInput(
                    name="canonical account access state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked subscriber and subscription statuses, active "
                        "reason-scoped enforcement locks, lifecycle override, "
                        "and restoration outcome"
                    ),
                ),
                AuthorityInput(
                    name="exact overdue receivable snapshot",
                    owner="collections.lifecycle",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "current collectible overdue invoices and exact "
                        "remaining receivable amounts under the account lock"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The payment-event and fired-timer adapters each open a "
                    "transaction-free owner session and enter "
                    "execute_owner_command once. Durable-timer mutation, "
                    "consumer receipt, locked restoration, alert projection, "
                    "audit, lifecycle event, and access-state changes are "
                    "flush-only participants in that command."
                ),
                locking=(
                    "Schedule locks the current owner/entity/purpose timer. "
                    "Consumption locks the fired timer, then the subscriber "
                    "account, subscriptions, enforcement locks, and exact "
                    "receivable evidence in stable account order."
                ),
                idempotency=(
                    "The funding event id is the scheduling command identity; "
                    "redelivery reuses its recorded timer. Timer generations "
                    "reject superseded delivery, and the unique consumer/event "
                    "receipt prevents a fired trigger from healing twice."
                ),
                retries=(
                    "Retry the same funding or timer event identity after "
                    "transient failure. Changed account or debt evidence is "
                    "recomputed under lock; malformed timer identity fails "
                    "closed and remains retryable."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "financial.walled_account_healing.invalid_timer_due_at",
                    "financial.walled_account_healing.invalid_timer_evidence",
                    "financial.walled_account_healing.invalid_command_context",
                    "financial.walled_account_healing.command_contract_violation",
                    "financial.walled_account_healing.nested_owner_command",
                    "financial.walled_account_healing.active_caller_transaction",
                    "financial.walled_account_healing.nested_transaction_completion",
                ),
                mapping_owner="billing lifecycle event adapter",
                retryable_codes=(
                    "financial.walled_account_healing.invalid_timer_evidence",
                ),
                fail_closed_on=(
                    "any positive overdue receivable including NGN 0.50",
                    "missing or mismatched durable timer identity",
                    "ambiguous account, lock, lifecycle override, or "
                    "restoration evidence",
                ),
            ),
            events=EventContract(
                event_types=("financial.walled_account_healing_due",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Timer, subscriber, purpose, generation, command, and "
                    "correlation identity retain their meaning; additive "
                    "payload fields are permitted."
                ),
                replay=(
                    "The fired event is receipted by this owner. Replay returns "
                    "the existing receipt without repeating restoration."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="walled-account healing operator exceptions",
                    input_names=(
                        "canonical account access state",
                        "exact overdue receivable snapshot",
                    ),
                    writer="financial.walled_account_healing",
                    freshness="recomputed when the exact account timer fires",
                    stale_behavior=(
                        "A later successful restore resolves the deduplicated "
                        "account alert; unresolved evidence remains visible."
                    ),
                    drift_signal=(
                        "An open walled_account_healing:<account_id> alert "
                        "contains the exact residue or remaining blockers."
                    ),
                    rebuild_operation=(
                        "Re-deliver the account timer event or run the reviewed "
                        "targeted unwall command for the named account."
                    ),
                    repair_owner="financial.walled_account_healing",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUT_OVER,
                old_owner=(
                    "cohort-wide stale-overdue detector with apply=False and "
                    "operator-only unwall script"
                ),
                new_owner="financial.walled_account_healing",
                verification=(
                    "Exact account timer schedule, event replay, fired "
                    "generation validation, zero-debt restore, NGN 0.50 "
                    "refusal, operator exception, and architecture tests."
                ),
                cutover_gate=(
                    "All committed payment and account-credit events schedule "
                    "the exact account timer; no new billing cohort sweep is "
                    "registered."
                ),
                fallback_retirement=(
                    "The targeted one-off command remains only for historical "
                    "rows that predate funding-event timers; it is not a "
                    "scheduled decision path."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
            ),
            test_refs=(
                "tests/test_walled_account_healing.py",
                "tests/test_restoration_outcome.py",
                "tests/architecture/test_walled_account_healing_ownership.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_service_renewals",
        module="app.services.prepaid_service_renewals",
        owns=(
            "prepaid service renewal execution",
            "due prepaid service-cycle funding preview",
            "settled-payment evidence validation and evaluation outcome",
            "WAT lapsed-settlement service-period resolution",
            "locked and idempotent prepaid renewal debit",
            "exact debit-to-entitlement evidence",
            "prepaid subscription paid-through advancement",
            "billing-anchor projection from entitlement evidence",
            "billing-anchor retraction after funding reversal",
            "stale billing-anchor drift repair",
            "canonical prepaid renewed-through outcome",
            "post-credit-application due-service consequence",
            "bounded scheduled renewal catch-up",
            "fingerprint-approved missed renewal execution",
        ),
        depends_on=(
            "billing.contracts",
            "customer.accounts",
            "financial.account_adjustments",
            "financial.customer_subledger",
            "financial.invoices",
            "financial.payments",
            "financial.prepaid_funding_reconstruction",
            "financial.subscription_billing_grants",
            "financial.subscription_billing_treatments",
            "events.dispatcher",
        ),
        notes=(
            "A payment receipt proves cash settlement, not service duration. "
            "A funding-change event is complete only when the referenced "
            "payment has canonical succeeded and settlement evidence. "
            "Each forward renewal stages prepaid_service.renewed with the "
            "exact entitlement, debit and renewed-through boundary in the "
            "same transaction; payment correlation is a trigger, not source "
            "attribution for pooled account credit. Incomplete evidence raises "
            "through the durable event-handler attempt so the permanent event "
            "redriver retries it; an explicit no-due-service outcome is success. "
            "This owner is also the single writer of the invoice-funded "
            "billing anchor: payment allocation, invoice application and draft "
            "reconciliation commit entitlement evidence and then emit the "
            "funding-change event or request "
            "project_prepaid_billing_anchor_for_invoice. That projection is a "
            "pure recomputation from surviving coverage, so replay is "
            "idempotent and a refund, chargeback or reversal retracts the "
            "anchor to the start of the period that stopped being funded "
            "instead of leaving it stale. Coverage is the union of active "
            "entitlements and applied financial.service_extensions grant "
            "intervals, and the anchor never lands below it. Above that floor "
            "the caller declares a BillingAnchorAuthority: a funding "
            "observation advances monotonically and never claws back a lead "
            "another owner granted, while an operator-confirmed reviewed "
            "reconciliation may resolve an evidence-free stale lead downward "
            "onto exact coverage. Those two values reproduce the separate "
            "anchor policies that previously lived in "
            "_finalize_invoice_payment_effects and "
            "finalize_invoice_application_for_owner. The retired inline "
            "project_paid_invoice_billing_anchors helper is gone. A lapsed "
            "settlement period first resolves the payment instant into the "
            "Africa/Lagos calendar, starts at local midnight, advances by the "
            "typed cadence, and persists the resulting boundaries as UTC "
            "instants. Payment participants consume that typed period; they do "
            "not derive a UTC calendar date independently. A missed period may "
            "be executed only from an exact read-only preview fingerprint plus "
            "a durable review reference; it uses the same debit, entitlement, "
            "anchor, renewed-outcome, and restoration transaction."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid service renewal execution",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "prepaid subscription and renewal terms",
                        "settled payment evidence",
                        "verified customer funding position",
                        "funded service entitlement evidence",
                    ),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="due prepaid service-cycle funding preview",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "prepaid subscription and renewal terms",
                        "verified customer funding position",
                        "funded service entitlement evidence",
                    ),
                ),
                ConcernContract(
                    name=("settled-payment evidence validation and evaluation outcome"),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "settled payment evidence",
                        "prepaid subscription and renewal terms",
                    ),
                ),
                ConcernContract(
                    name="WAT lapsed-settlement service-period resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "settled payment evidence",
                        "prepaid subscription and renewal terms",
                    ),
                ),
                ConcernContract(
                    name="locked and idempotent prepaid renewal debit",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "verified customer funding position",
                        "prepaid subscription and renewal terms",
                    ),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="exact debit-to-entitlement evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "verified customer funding position",
                        "prepaid subscription and renewal terms",
                    ),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="prepaid subscription paid-through advancement",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=("funded service entitlement evidence",),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="billing-anchor projection from entitlement evidence",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=("funded service entitlement evidence",),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="billing-anchor retraction after funding reversal",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "settled payment evidence",
                        "funded service entitlement evidence",
                    ),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="stale billing-anchor drift repair",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "prepaid subscription and renewal terms",
                        "funded service entitlement evidence",
                    ),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="canonical prepaid renewed-through outcome",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=(
                        "prepaid subscription and renewal terms",
                        "funded service entitlement evidence",
                    ),
                ),
                ConcernContract(
                    name="post-credit-application due-service consequence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "settled payment evidence",
                        "verified customer funding position",
                        "prepaid subscription and renewal terms",
                    ),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="bounded scheduled renewal catch-up",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "verified customer funding position",
                        "prepaid subscription and renewal terms",
                    ),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
                ConcernContract(
                    name="fingerprint-approved missed renewal execution",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "verified customer funding position",
                        "prepaid subscription and renewal terms",
                    ),
                    canonical_writer="financial.prepaid_service_renewals",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="prepaid subscription and renewal terms",
                    owner="billing.contracts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscription billing mode, status, frozen unit_price, "
                        "cadence, and next_billing_at anchor"
                    ),
                ),
                AuthorityInput(
                    name="settled payment evidence",
                    owner="financial.payments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active succeeded Payment plus its PaymentSettlement "
                        "and exact allocation evidence"
                    ),
                ),
                AuthorityInput(
                    name="verified customer funding position",
                    owner="financial.prepaid_funding_reconstruction",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "reviewed funding baseline plus canonical forward "
                        "money facts, excluding quarantined accounts"
                    ),
                ),
                AuthorityInput(
                    name="funded service entitlement evidence",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active ServiceEntitlement linked to the exact renewal "
                        "debit and service period"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Settlement-triggered, scheduled, and reviewed missed-period "
                    "public commands enter "
                    "execute_owner_command once on a transaction-free session. "
                    "Validation, draft consequence, debit, entitlement, anchor, "
                    "posting group, renewed outcome, and restoration commit or "
                    "roll back together."
                ),
                locking=(
                    "The account is locked before idempotency lookup and funding "
                    "re-preview; entitlement overlap and adjustment uniqueness "
                    "prevent a second funded result for the same period."
                ),
                idempotency=(
                    "The service period deterministically keys its adjustment; "
                    "settlement events and scheduled passes carry typed command "
                    "keys; reviewed execution additionally binds the exact preview "
                    "fingerprint and evidence reference, and replay must match the "
                    "exact debit, entitlement, period, and posting effects."
                ),
                retries=(
                    "The durable event redriver or scheduled adapter retries the "
                    "whole owner command after rollback; stale funding requires a "
                    "fresh preview and ambiguous evidence remains fail-closed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "financial.prepaid_service_renewals"
                    ),
                    "financial.prepaid_service_renewals.adjustment_rejected",
                    "financial.prepaid_service_renewals.idempotency_conflict",
                    "financial.prepaid_service_renewals.incomplete_entitlement",
                    "financial.prepaid_service_renewals.ineligible_billing_mode",
                    "financial.prepaid_service_renewals.ineligible_status",
                    "financial.prepaid_service_renewals.insufficient_funding",
                    "financial.prepaid_service_renewals.invalid_amount",
                    "financial.prepaid_service_renewals.invalid_currency",
                    "financial.prepaid_service_renewals.invalid_effective_at",
                    "financial.prepaid_service_renewals.invalid_period",
                    "financial.prepaid_service_renewals.invalid_preview_fingerprint",
                    "financial.prepaid_service_renewals.missing_anchor",
                    "financial.prepaid_service_renewals.missing_evidence_ref",
                    "financial.prepaid_service_renewals.missing_price",
                    "financial.prepaid_service_renewals.mode_not_prepaid",
                    "financial.prepaid_service_renewals.payment_account_mismatch",
                    "financial.prepaid_service_renewals.payment_not_found",
                    "financial.prepaid_service_renewals.payment_not_settled",
                    "financial.prepaid_service_renewals.period_already_funded",
                    "financial.prepaid_service_renewals.settlement_missing",
                    "financial.prepaid_service_renewals.settlement_time_missing",
                    "financial.prepaid_service_renewals.stale_anchor",
                    "financial.prepaid_service_renewals.stale_preview",
                    "financial.prepaid_service_renewals.subscription_not_eligible",
                    "financial.prepaid_service_renewals.subscription_not_found",
                    "financial.prepaid_service_renewals.unsupported_cadence",
                ),
                mapping_owner=("billing automation, durable event, and staff adapters"),
                fail_closed_on=(
                    "missing or non-canonical settlement evidence",
                    "quarantined or insufficient funding",
                    "stale preview or entitlement overlap",
                    "posting-group failure",
                ),
            ),
            events=EventContract(
                event_types=("prepaid_service.renewed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries the exact account, subscription, debit, "
                    "entitlement, funded period, amount, currency, and trigger."
                ),
                replay=(
                    "The period-keyed adjustment and posting group return the "
                    "recorded result; no second renewed outcome is staged."
                ),
            ),
            projections=(
                ProjectionContract(
                    name=("prepaid entitlement and paid-through anchor projection"),
                    input_names=(
                        "prepaid subscription and renewal terms",
                        "settled payment evidence",
                        "verified customer funding position",
                        "funded service entitlement evidence",
                    ),
                    writer="financial.prepaid_service_renewals",
                    freshness=(
                        "Atomic with the renewal debit or recomputed from exact "
                        "surviving entitlement evidence after reversal."
                    ),
                    stale_behavior=(
                        "Renewal and enforcement fail closed; stale-anchor drift "
                        "remains owned reconciliation work."
                    ),
                    drift_signal=(
                        "The subscription anchor differs from exact active "
                        "entitlement and approved grant coverage."
                    ),
                    rebuild_operation=(
                        "Run the fingerprint-bound stale billing-anchor repair "
                        "or replay the exact funding-change event."
                    ),
                    repair_owner="financial.prepaid_service_renewals",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUT_OVER,
                old_owner=(
                    "billing_automation.run_invoice_cycle and durable event "
                    "handler caller-owned transactions"
                ),
                new_owner="financial.prepaid_service_renewals",
                verification=(
                    "Scheduled and event-triggered command-boundary, atomicity, "
                    "idempotent posting, and architecture tests."
                ),
                cutover_gate=(
                    "Both live renewal entry paths invoke the typed public owner "
                    "command and each new funded period has exactly one matching "
                    "prepaid-consumption posting group."
                ),
                fallback_retirement=(
                    "Direct caller-transaction renewal writes and the generic "
                    "account-adjustment posting fallback are rejected by guards."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/runbooks/REVIEWED_MIGRATED_PREPAID_OPENING_REPAIR.md",
            ),
            test_refs=(
                "tests/test_prepaid_service_renewals.py",
                "tests/services/billing/test_payment_status_recompute.py",
                "tests/test_subledger_forward_shadow.py",
                "tests/architecture/test_prepaid_billing_anchor_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.prepaid_renewal_terms_backfill",
        module="app.services.prepaid_renewal_terms_backfill",
        owns=("prepaid renewal-terms evidence backfill",),
        depends_on=(
            "financial.invoices",
            "financial.prepaid_service_renewals",
        ),
        notes=(
            "ADR 0007 stage-3 migration only. Prepaid enforcement fails "
            "closed with renewal_terms_unresolved when an active prepaid "
            "subscription has no frozen contracted amount "
            "(Subscription.unit_price NULL or non-positive). This "
            "temporary owner restores the amount solely from the "
            "subscription's own PAID base-subscription invoice lines — "
            "never from the mutable catalog. Absent or contradictory "
            "paid evidence becomes an owned, SLA-bound finance work "
            "item and the account stays fail-closed. Retire at the "
            "ADR 0007 Phase 1 cutover when billing.contracts becomes "
            "the renewal-terms authority."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid renewal-terms evidence backfill",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "paid base-subscription invoice lines",
                        "blocked prepaid subscription state",
                    ),
                    canonical_writer=("financial.prepaid_renewal_terms_backfill"),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="paid base-subscription invoice lines",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active InvoiceLine rows with metadata kind "
                        "base_subscription on PAID invoices for the "
                        "blocked subscription"
                    ),
                ),
                AuthorityInput(
                    name="blocked prepaid subscription state",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active prepaid Subscription rows whose "
                        "unit_price is NULL or non-positive (the exact "
                        "predicate that yields "
                        "renewal_terms_unresolved)"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The capture command enters execute_owner_command "
                    "once on a transaction-free session; the "
                    "fingerprint-bound evidence re-check, unit_price "
                    "writes, and finance work-item sync commit "
                    "together."
                ),
                locking=(
                    "Each repaired Subscription row is locked FOR "
                    "UPDATE and re-checked (already-priced rows are "
                    "skipped) before its contracted amount is written."
                ),
                idempotency=(
                    "The capture is fingerprint-bound to the reviewed "
                    "preview; replay with unchanged evidence rewrites "
                    "nothing because repaired rows fail the "
                    "still-unpriced re-check."
                ),
                retries=(
                    "Retry the whole command with the same idempotency "
                    "key. Changed paid evidence requires a new "
                    "reviewed preview fingerprint."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "financial.prepaid_renewal_terms_backfill"
                    ),
                    (
                        "financial.prepaid_renewal_terms_backfill."
                        "missing_idempotency_key"
                    ),
                    ("financial.prepaid_renewal_terms_backfill.stale_preview"),
                    (
                        "financial.prepaid_renewal_terms_backfill."
                        "invalid_reviewed_amount"
                    ),
                    (
                        "financial.prepaid_renewal_terms_backfill."
                        "missing_review_reference"
                    ),
                    ("financial.prepaid_renewal_terms_backfill.subscription_not_found"),
                    ("financial.prepaid_renewal_terms_backfill.not_in_backfill_cohort"),
                    ("financial.prepaid_renewal_terms_backfill.stale_current_amount"),
                    (
                        "financial.prepaid_renewal_terms_backfill."
                        "missing_audit_fingerprint"
                    ),
                    ("financial.prepaid_renewal_terms_backfill.audit_mismatch"),
                    ("financial.prepaid_renewal_terms_backfill.invalid_audit_action"),
                ),
                mapping_owner="billing migration adapters",
                fail_closed_on=(
                    "absent paid base-subscription evidence",
                    "contradictory distinct paid amounts",
                    "a lone line without explicit full-cycle proof",
                    "currency, cadence, quantity, or proration incompatibility",
                    "stale preview fingerprint",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "manual staff corrections of Subscription.unit_price "
                    "with no evidence contract"
                ),
                new_owner="financial.prepaid_renewal_terms_backfill",
                verification=(
                    "Focused evidence-classification, fail-closed, "
                    "work-item lifecycle, and idempotent-replay tests."
                ),
                cutover_gate=(
                    "renewal_terms_unresolved is zero for the active "
                    "prepaid cohort, every remaining case carries an "
                    "owned finance work item, and ADR 0007 Phase 1 "
                    "makes billing.contracts the renewal-terms "
                    "authority."
                ),
                fallback_retirement=(
                    "Delete this temporary owner at the Phase 1 "
                    "cutover; Subscription.unit_price stops being the "
                    "renewal-charge authority."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            events=EventContract(
                event_types=(
                    "prepaid_renewal_terms.backfilled",
                    "prepaid_renewal_terms.corrected",
                    "prepaid_renewal_terms.audited",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries the account, subscription, "
                    "restored contracted amount, paid-line count, and "
                    "the reviewed preview fingerprint."
                ),
                replay=(
                    "Replay with unchanged evidence rewrites nothing: "
                    "repaired rows fail the still-unpriced re-check, "
                    "so no second event is emitted for them."
                ),
            ),
            test_refs=("tests/test_prepaid_renewal_terms_backfill.py",),
        ),
    ),
    SOTService(
        name="financial.prepaid_recovery_billing",
        module="app.services.prepaid_recovery_billing",
        owns=(
            "prepaid recovery draft eligibility and operator routing",
            "suspended prepaid replacement-cycle draft creation",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "events.dispatcher",
            "financial.access_resolution",
            "financial.invoices",
            "financial.prepaid_service_renewals",
        ),
        notes=(
            "This recovery-only coordinator creates a replacement full-cycle "
            "draft from the confirmed Bill Now instant. It never voids, settles, "
            "or restores from a prior invoice or displayed balance. The resulting "
            "draft is classified and reconciled only by "
            "financial.prepaid_draft_reconciliation."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="prepaid recovery draft eligibility and operator routing",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "locked prepaid subscription state",
                        "active prepaid enforcement lock",
                        "unresolved service-invoice evidence",
                    ),
                ),
                ConcernContract(
                    name="suspended prepaid replacement-cycle draft creation",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "locked prepaid subscription state",
                        "active prepaid enforcement lock",
                        "contracted prepaid renewal price",
                        "unresolved service-invoice evidence",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="locked prepaid subscription state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked Subscription billing mode, lifecycle state, offer, and next-billing anchor",
                ),
                AuthorityInput(
                    name="active prepaid enforcement lock",
                    owner="financial.access_resolution",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active EnforcementLock with prepaid reason for the exact subscription",
                ),
                AuthorityInput(
                    name="contracted prepaid renewal price",
                    owner="financial.prepaid_service_renewals",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="prepaid monthly charge resolver using subscription contract and tax policy",
                ),
                AuthorityInput(
                    name="unresolved service-invoice evidence",
                    owner="financial.invoices",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "every active unresolved invoice with an active positive "
                        "line for the exact subscription, including ordinary and "
                        "recovery drafts, plus financial and coverage evidence"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Draft confirmation enters execute_owner_command once on a "
                    "transaction-free session; preview is read-only and the "
                    "replacement invoice aggregate commits together."
                ),
                locking=(
                    "Account is locked first, then the exact subscription. Active "
                    "unresolved service-invoice lookup is repeated under those "
                    "locks before the draft is written."
                ),
                idempotency=(
                    "Recovery draft fingerprint identifies one period. Exact replay "
                    "returns the matching recovery draft; any other unresolved "
                    "service invoice prevents a replacement write."
                ),
                retries=(
                    "A stale price, service, or period preview is rejected for a "
                    "fresh preview; this owner performs no settlement."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "financial.prepaid_recovery_billing"
                    ),
                    "financial.prepaid_recovery_billing.subscription_not_found",
                    "financial.prepaid_recovery_billing.ineligible_billing_mode",
                    "financial.prepaid_recovery_billing.ineligible_status",
                    "financial.prepaid_recovery_billing.prepaid_lock_missing",
                    "financial.prepaid_recovery_billing.unresolved_service_invoice",
                    "financial.prepaid_recovery_billing.unsupported_cycle",
                    "financial.prepaid_recovery_billing.invalid_charge",
                    "financial.prepaid_recovery_billing.stale_preview",
                ),
                mapping_owner="admin catalog adapter",
                fail_closed_on=(
                    "missing prepaid lock or suspended service state",
                    "any unresolved invoice claiming the exact service",
                    "stale price, service, or period evidence",
                ),
            ),
            events=EventContract(
                event_types=("invoice_created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Existing invoice and subscription events carry the exact "
                    "invoice and subscription identifiers; no new transport event "
                    "is introduced by this coordinator."
                ),
                replay=(
                    "Invoice line period metadata and the draft fingerprint "
                    "reconstruct completed recovery-draft creation."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="financial.prepaid_recovery_billing",
                verification=(
                    "Focused command, typed routing, UI visibility, ordinary and "
                    "recovery draft, activity, service scope, stale-preview, "
                    "replay, price, period, and invoice-creation tests."
                ),
                cutover_gate=(
                    "Admin Bill Now invokes only this coordinator; invoice-page "
                    "reconciliation invokes only financial.prepaid_draft_reconciliation."
                ),
                fallback_retirement=(
                    "The recovery-specific settlement writer is removed. No adapter "
                    "may manufacture a recovery invoice or restore from displayed balance."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/designs/PREPAID_RECOVERY_BILLING.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_billing_invoice_templates.py",
                "tests/test_prepaid_recovery_billing.py",
                "tests/architecture/test_prepaid_recovery_billing_sot.py",
            ),
        ),
    ),
)
