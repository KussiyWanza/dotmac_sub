"""financial_access SOT declarations: collection operations."""

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
        name="financial.addon_purchases",
        module="app.services.customer_portal_flow_addons",
        owns=(
            "customer add-on purchase eligibility and preview",
            "add-on price and subscription-state confirmation",
            "add-on purchase idempotency and audit evidence",
            "exact add-on entitlement-to-adjustment link",
            "canonical recurring add-on billing-terms output",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "events.dispatcher",
            "events.owner_outputs",
            "financial.account_adjustments",
            "customer.financial_position",
            "observability.audit_log",
        ),
        notes=(
            "Paid purchases request one exact debit from the adjustment "
            "owner. Free add-ons explicitly produce no ledger transaction. "
            "A recurring purchase stages its exact accepted term output in "
            "the same owner transaction. billing.contracts receipts that "
            "output into a next-boundary draft and durable timer; purchase "
            "never writes contract rows. Cancellation is a later migration "
            "slice and remains outside this command."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="customer add-on purchase eligibility and preview",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical subscription state",
                        "offered add-on commercial terms",
                        "current customer financial position",
                    ),
                ),
                ConcernContract(
                    name="add-on price and subscription-state confirmation",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical subscription state",
                        "offered add-on commercial terms",
                        "current customer financial position",
                    ),
                ),
                ConcernContract(
                    name="add-on purchase idempotency and audit evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "recorded add-on purchase evidence",
                        "canonical audit participant",
                    ),
                    canonical_writer="financial.addon_purchases",
                ),
                ConcernContract(
                    name="exact add-on entitlement-to-adjustment link",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "confirmed account adjustment",
                        "recorded add-on purchase evidence",
                    ),
                    canonical_writer="financial.addon_purchases",
                ),
                ConcernContract(
                    name="canonical recurring add-on billing-terms output",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical subscription state",
                        "offered add-on commercial terms",
                        "recorded add-on purchase evidence",
                    ),
                    canonical_writer="financial.addon_purchases",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical subscription state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "owned Subscription account, offer identity, and "
                        "lifecycle state"
                    ),
                ),
                AuthorityInput(
                    name="offered add-on commercial terms",
                    owner="financial.addon_purchases",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "OfferAddOn authorization plus exact AddOn and active "
                        "AddOnPrice identity, amount, currency, and cadence"
                    ),
                ),
                AuthorityInput(
                    name="current customer financial position",
                    owner="customer.financial_position",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "typed prepaid funding, postpaid receivables, "
                        "collection-blocking balance, and shortfall"
                    ),
                ),
                AuthorityInput(
                    name="confirmed account adjustment",
                    owner="financial.account_adjustments",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "flush-only confirmed add-on-purchase adjustment "
                        "and exact ledger-entry identity"
                    ),
                ),
                AuthorityInput(
                    name="canonical audit participant",
                    owner="observability.audit_log",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="flush-only customer confirmation audit protocol",
                ),
                AuthorityInput(
                    name="recorded add-on purchase evidence",
                    owner="financial.addon_purchases",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "SubscriptionAddOn, purchase preview fingerprint, "
                        "business idempotency key, and adjustment link"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The typed confirmation enters execute_owner_command "
                    "once on a transaction-free session; entitlement, exact "
                    "adjustment, usage grant, idempotency, audit, and recurring "
                    "terms output commit or roll back together."
                ),
                locking=(
                    "The account is locked before the owned Subscription and "
                    "preview are re-read; all account debits follow the same "
                    "account-first lock order."
                ),
                idempotency=(
                    "The caller key is unique in addon_purchase scope and "
                    "replays the exact SubscriptionAddOn without a second "
                    "debit or owner output."
                ),
                retries=(
                    "Retry the complete command with the same key. A changed "
                    "price, subscription, or funding fingerprint requires a "
                    "new preview."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes("financial.addon_purchases"),
                    "financial.addon_purchases.addon_not_available",
                    "financial.addon_purchases.idempotency_conflict",
                    ("financial.addon_purchases.incomplete_idempotency_evidence"),
                    "financial.addon_purchases.invalid_preview_fingerprint",
                    "financial.addon_purchases.missing_idempotency_key",
                    "financial.addon_purchases.service_not_found",
                    "financial.addon_purchases.stale_preview",
                ),
                mapping_owner="customer API and web adapters",
                fail_closed_on=(
                    "stale price, service, or financial preview",
                    "ambiguous active recurring price",
                    "missing or conflicting business idempotency",
                ),
            ),
            events=EventContract(
                event_types=("billing.contract_terms.recurring_addon_added",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact account, subscription, "
                    "SubscriptionAddOn, AddOn, AddOnPrice, quantity, price, "
                    "currency, cadence, and purchase-time identities."
                ),
                replay=(
                    "The purchase row and output commit atomically; "
                    "billing.contracts receipts each event exactly once."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUT_OVER,
                old_owner=(
                    "customer portal helper-owned commit and rollback with "
                    "no canonical billing-terms consequence"
                ),
                new_owner="financial.addon_purchases",
                verification=(
                    "Typed command-boundary, stale preview, adjustment link, "
                    "replay, owner-output, draft, timer, activation, "
                    "obligation, and architecture tests."
                ),
                cutover_gate=(
                    "The live customer purchase adapter uses only the typed "
                    "owner command and recurring purchases complete the "
                    "receipted shadow chain."
                ),
                fallback_retirement=(
                    "The helper-level purchase commit path and unreceipted "
                    "recurring purchase path are removed in this slice."
                ),
            ),
            steward="billing and finance operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_api_me_addons.py",
                "tests/test_billing_addon_contract_backfill.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="financial.payment_arrangements",
        module="app.services.payment_arrangements",
        owns=(
            "payment-arrangement eligibility and lifecycle",
            "installment schedule and payment application",
            "active-arrangement collection shield state",
        ),
        depends_on=(
            "customer.financial_position",
            "financial.invoices",
            "financial.payments",
        ),
    ),
    SOTService(
        name="financial.payment_arrangement_staff_actions",
        module="app.services.payment_arrangement_staff_actions",
        owns=("atomic staff arrangement transition and audit coordination",),
        depends_on=(
            "financial.payment_arrangements",
            "auth.permission_gate",
            "observability.audit_log",
        ),
        notes=(
            "The arrangement owner supplies eligibility, impact facts, "
            "fingerprints, locking, and transition participants. This "
            "coordinator binds explicit staff confirmation to that preview "
            "and stages audit evidence in the same transaction."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("atomic staff arrangement transition and audit coordination"),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical payment-arrangement action preview",
                        "authorized staff command context",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical payment-arrangement action preview",
                    owner="financial.payment_arrangements",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "locked arrangement lifecycle, installment schedule, "
                        "collection-shield consequence, and preview fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="authorized staff command context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "billing:arrangement:write principal, command identity, "
                        "scope, reason, and explicit impact confirmation"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "confirm_staff_action enters execute_owner_command once; "
                    "the arrangement and audit owners only stage and flush."
                ),
                locking=(
                    "The coordinator locks the arrangement and its active "
                    "installments, then recomputes eligibility and impact."
                ),
                idempotency=(
                    "The preview fingerprint binds the exact action, lifecycle "
                    "state, schedule state, and target installment; duplicate "
                    "or changed submissions fail closed and require a new preview."
                ),
                retries=(
                    "Adapters retry only after complete rollback and must obtain "
                    "a fresh owner-authored preview after any stale-state result."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "financial.payment_arrangement_staff_actions"
                    ),
                    "financial.payment_arrangement_staff_actions.invalid_scope",
                    "financial.payment_arrangement_staff_actions.invalid_actor",
                    "financial.payment_arrangement_staff_actions.confirmation_required",
                    "financial.payment_arrangement_staff_actions.invalid_note",
                    "financial.payment_arrangement_staff_actions.stale_preview",
                    "financial.payment_arrangements.not_found",
                    "financial.payment_arrangements.action_not_available",
                    "financial.payment_arrangements.incomplete_evidence",
                ),
                mapping_owner="admin payment-arrangement adapter",
                retryable_codes=(
                    "financial.payment_arrangement_staff_actions.stale_preview",
                ),
                fail_closed_on=(
                    "missing explicit confirmation",
                    "changed arrangement or installment state",
                    "missing authorized actor",
                    "missing target installment evidence",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "payment-arrangement admin routes, web helpers, Jinja status "
                    "branches, browser confirmation dialogs, and post-commit audit"
                ),
                new_owner="financial.payment_arrangement_staff_actions",
                verification=(
                    "Owner preview, stale-state, atomic audit, adapter, shared "
                    "action-form, accessibility, and architecture tests."
                ),
                cutover_gate=(
                    "Every staff approve, cancel, and manual installment action "
                    "submits an exact preview fingerprint and explicit confirmation."
                ),
                fallback_retirement=(
                    "Direct admin lifecycle commits, post-commit audit, raw action "
                    "forms, and browser confirmation dialogs are removed."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/designs/PAYMENT_ARRANGEMENT_SAFE_ACTIONS.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_payment_arrangement_safe_actions.py",
                "tests/test_payment_arrangements.py",
                "tests/architecture/test_action_form_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.access_resolution",
        module="app.services.access_resolution",
        owns=(
            "billable service classification",
            "RADIUS access decision",
            "financial suspension/restoration eligibility",
            "currency-bound prepaid funding decision",
        ),
        depends_on=(
            "financial.billing_profile",
            "financial.prepaid_currency",
            "financial.prepaid_threshold",
            "customer.financial_position",
            "access.subscription_lifecycle",
            "access.walled_garden_policy",
        ),
        notes=(
            "One read-only policy owner resolves customer-impact, billing, "
            "prepaid funding, and RADIUS answers from the same account and "
            "subscription evidence."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="billable service classification",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical subscriber account state",
                        "canonical subscription lifecycle state",
                        "canonical billing profile",
                    ),
                ),
                ConcernContract(
                    name="RADIUS access decision",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical subscriber account state",
                        "canonical subscription lifecycle state",
                        "canonical access restriction intent",
                    ),
                ),
                ConcernContract(
                    name="financial suspension/restoration eligibility",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical subscriber account state",
                        "canonical subscription lifecycle state",
                        "canonical billing profile",
                        "currency-bound customer financial position",
                        "canonical prepaid threshold",
                    ),
                ),
                ConcernContract(
                    name="currency-bound prepaid funding decision",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "currency-bound customer financial position",
                        "canonical prepaid threshold",
                        "prepaid enforcement currency setting",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical subscriber account state",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Subscriber identity, lifecycle, active, billing-enabled, "
                        "and billing-mode fields"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription lifecycle state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=("Subscription identity, status, account, and billing mode"),
                ),
                AuthorityInput(
                    name="canonical access restriction intent",
                    owner="access.walled_garden_policy",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "effective hard-reject or captive restriction resolved "
                        "from canonical enforcement locks and readiness"
                    ),
                ),
                AuthorityInput(
                    name="canonical billing profile",
                    owner="financial.billing_profile",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=("resolved prepaid/postpaid mode and automation safety"),
                ),
                AuthorityInput(
                    name="currency-bound customer financial position",
                    owner="customer.financial_position",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "native prepaid funding position in the requested currency"
                    ),
                ),
                AuthorityInput(
                    name="canonical prepaid threshold",
                    owner="financial.prepaid_threshold",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "currency-matched minimum balance and unfunded renewal "
                        "requirement"
                    ),
                ),
                AuthorityInput(
                    name="prepaid enforcement currency setting",
                    owner="financial.prepaid_currency",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="validated prepaid enforcement currency code",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller creates and closes the session. Resolution reads "
                    "canonical rows and settings, or resolves an already-loaded "
                    "typed subscription, without writes or transaction completion."
                ),
                locking=(
                    "No row lock; each decision reflects the authoritative input "
                    "snapshot visible to the caller transaction."
                ),
                idempotency=(
                    "The same account, subscription, restriction, currency, and "
                    "visible financial snapshot produce the same outcome."
                ),
                retries=(
                    "Callers may retry transient reads. Invalid currency evidence "
                    "is terminal until the canonical setting or request is fixed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner=(
                    "billing, lifecycle, RADIUS, reporting, and task adapters"
                ),
                fail_closed_on=(
                    "missing or invalid prepaid enforcement currency",
                    "missing, inactive, blocked, or ambiguous account state",
                    "currency mismatch in funding inputs",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "duplicate access.control_resolution registry alias plus "
                    "customer_service_state billing/RADIUS implementation"
                ),
                new_owner="financial.access_resolution",
                verification=(
                    "Billing, prepaid, lifecycle, RADIUS, SQL-filter parity, "
                    "invalid-currency, and architecture tests."
                ),
                cutover_gate=(
                    "All application decision and cohort callers import the "
                    "canonical owner; customer_service_state retains only outage "
                    "and support observations."
                ),
                fallback_retirement=(
                    "The duplicate registry service, re-export facade, untyped "
                    "account identifier, and second decision implementation are "
                    "removed. Currency failures are delegated to the dedicated "
                    "prepaid-currency policy owner."
                ),
            ),
            steward="billing and network access",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/audits/BILLING_SOT_AUDIT_2026-07-12.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
            ),
            test_refs=(
                "tests/test_access_resolution.py",
                "tests/test_customer_service_state.py",
                "tests/test_prepaid_threshold_resolver.py",
                "tests/architecture/test_access_resolution_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="financial.dunning",
        module="app.services.collections._core",
        owns=(
            "postpaid collection lifecycle",
            "dunning action execution",
            "financial access consequence preview and confirmation",
            "financial suspension and restoration idempotency and audit",
            "exact enforcement-lock, throttle, and case evidence",
            "financial access restoration reconciliation",
            "per-account dunning transaction isolation and failure evidence",
        ),
        depends_on=(
            "financial.access_resolution",
            "financial.ledger",
            "financial.payment_arrangements",
            "financial.billing_health",
            "financial.prepaid_enforcement_state",
            "access.subscription_lifecycle",
            "access.walled_garden_policy",
        ),
        notes=(
            "The scheduled cohort read is observational. Each account then owns "
            "one independent decision/consequence transaction; a failure rolls "
            "back only that account, records bounded durable audit evidence in a "
            "new transaction, increments dunning_errors, and continues. Clean-"
            "account restoration uses the same per-account boundary and no nested "
            "participant savepoint."
        ),
    ),
    SOTService(
        name="financial.dunning_staff_actions",
        module="app.services.dunning_staff_actions",
        owns=("atomic staff dunning-case transition and audit coordination",),
        depends_on=(
            "financial.dunning",
            "auth.permission_gate",
            "observability.audit_log",
        ),
        notes=(
            "The dunning owner supplies exact selected-scope eligibility, "
            "receivable impact, fingerprints, locked transitions, action-log "
            "evidence, and account projection updates. This coordinator binds "
            "staff confirmation and audit to that owner result."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=(
                        "atomic staff dunning-case transition and audit coordination"
                    ),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical dunning staff-action impact",
                        "authorized dunning staff command context",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical dunning staff-action impact",
                    owner="financial.dunning",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "exact selected case membership, current lifecycle "
                        "state, canonical collectible receivables, eligibility, "
                        "resulting state, and deterministic fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="authorized dunning staff command context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "billing:dunning:write principal, command identity, "
                        "scope, reason, explicit selected IDs, and confirmation"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "confirm_staff_action enters execute_owner_command once; "
                    "dunning lifecycle, action-log, event, account projection, "
                    "and audit participants only stage or flush."
                ),
                locking=(
                    "Selected cases and their subscriber accounts are locked in "
                    "stable UUID order before eligibility and receivables are "
                    "recomputed."
                ),
                idempotency=(
                    "The fingerprint binds action, exact selected membership, "
                    "case lifecycle versions, eligible/skipped results, and "
                    "close-time receivables. A replay after transition fails "
                    "closed and requires a new preview."
                ),
                retries=(
                    "Adapters retry only after complete rollback and obtain a "
                    "fresh preview after any membership, state, or eligibility "
                    "conflict."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "financial.dunning_staff_actions"
                    ),
                    "financial.dunning_staff_actions.invalid_selection",
                    "financial.dunning_staff_actions.invalid_scope",
                    "financial.dunning_staff_actions.invalid_actor",
                    "financial.dunning_staff_actions.confirmation_required",
                    "financial.dunning_staff_actions.stale_preview",
                    "financial.dunning_staff_actions.no_eligible_cases",
                ),
                mapping_owner="admin dunning adapter",
                retryable_codes=("financial.dunning_staff_actions.stale_preview",),
                fail_closed_on=(
                    "empty, invalid, or oversized selected scope",
                    "missing explicit confirmation",
                    "changed membership, lifecycle, or receivable eligibility",
                    "no eligible selected case",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "dunning admin routes, web helpers, raw Jinja forms, "
                    "browser dialogs, per-case commits, swallowed bulk failures, "
                    "and post-commit audit"
                ),
                new_owner="financial.dunning_staff_actions",
                verification=(
                    "Individual and bulk preview, exact scope, stale-state, "
                    "atomic rollback/audit, adapter, shared form, UI, and "
                    "architecture tests."
                ),
                cutover_gate=(
                    "Every staff pause, resume, or close confirmation carries "
                    "the exact owner preview fingerprint and explicit selected "
                    "membership."
                ),
                fallback_retirement=(
                    "Direct web mutations, per-case bulk commits, generic "
                    "exception swallowing, post-commit audit, and browser "
                    "confirmation dialogs are removed."
                ),
            ),
            steward="collections operations",
            design_refs=(
                "docs/designs/DUNNING_STAFF_SAFE_ACTIONS.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_dunning_staff_safe_actions.py",
                "tests/test_web_billing_dunning.py",
                "tests/architecture/test_action_form_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.billing_automation",
        module="app.services.billing_automation",
        owns=(
            "postpaid recurring charge preview",
            "postpaid invoice batch execution",
            "durable billing-run lifecycle and retry lineage",
            "billing-run audit projection and repair",
        ),
        depends_on=(
            "financial.invoices",
            "financial.prepaid_service_renewals",
            "financial.billing_accounts",
            "observability.audit_log",
        ),
        notes=(
            "Scheduled execution may compose the independently owned prepaid "
            "renewal pass. Confirmed manual invoice batches disable it. "
            "BillingRun is authoritative operational evidence for the "
            "resumable workflow; invoice period keys make retries convergent. "
            "The typed per-subscription recurring-charge preview is read-only "
            "and reuses the exact current period, price, discount, proration, "
            "recurring add-on, route-cap, and tax helpers for Phase 2 "
            "migration evidence. Its component total is the complete current "
            "postpaid cycle; skipped, ambiguous, or route-capped add-ons are "
            "typed issues rather than silent parity."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="postpaid recurring charge preview",
                    role=OwnerRole.RESOLVER,
                    input_names=("canonical billable subscription facts",),
                ),
                ConcernContract(
                    name="postpaid invoice batch execution",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical billable subscription facts",
                        "confirmed staff batch evidence",
                    ),
                    canonical_writer="financial.billing_automation",
                ),
                ConcernContract(
                    name="durable billing-run lifecycle and retry lineage",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=("confirmed staff batch evidence",),
                    canonical_writer="financial.billing_automation",
                ),
                ConcernContract(
                    name="billing-run audit projection and repair",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=("canonical billing-run record",),
                    canonical_writer="financial.billing_automation",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical billable subscription facts",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "native active or pending postpaid Subscription rows, "
                        "billing anchors, offer and recurring add-on prices, "
                        "SubscriptionAddOn intervals and quantities, route-cap "
                        "inputs, billing treatments, account state, and "
                        "existing canonical invoice lines"
                    ),
                ),
                AuthorityInput(
                    name="confirmed staff batch evidence",
                    owner="ui.invoice_batch_action_projection",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "normalized cycle/date, exact current fingerprint, "
                        "staff principal, explicit confirmation, and optional "
                        "failed source run"
                    ),
                ),
                AuthorityInput(
                    name="canonical billing-run record",
                    owner="financial.billing_automation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "BillingRun lifecycle, counters, launch kind, actor, "
                        "preview fingerprint, failure, and retry lineage"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The recurring-charge preview is read-only in the caller's "
                    "session. Batch execution persists a running launch before "
                    "work, commits canonical invoice-owner results, and records "
                    "the terminal run state. This is a durable resumable "
                    "workflow, not one database transaction."
                ),
                locking=(
                    "Invoice and subscription period idempotency keys prevent "
                    "duplicate documents; account and invoice participants "
                    "apply their own canonical locks."
                ),
                idempotency=(
                    "Exact subscription/period invoice-line keys and owner "
                    "checks make a failed-run retry converge without duplicate "
                    "billing. Retry creates a new BillingRun linked to its "
                    "failed source."
                ),
                retries=(
                    "Only failed or abandoned runs are eligible for reviewed "
                    "retry. Transient database retries rerun owner checks; a "
                    "changed staff preview requires new confirmation."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes("financial.billing_automation"),
                    "financial.billing_automation.invalid_cycle",
                    "financial.billing_automation.invalid_date",
                    "financial.billing_automation.execution_failed",
                    "financial.billing_automation.account_not_billable",
                    "financial.billing_automation.retry_ineligible",
                    "financial.billing_automation.audit_projection_failed",
                    "financial.billing_automation.missing_price",
                    "financial.billing_automation.mode_not_postpaid",
                    "financial.billing_automation.service_ended",
                    "financial.billing_automation.subscription_not_billable",
                    "financial.billing_automation.subscription_not_found",
                    "financial.billing_automation.zero_amount",
                ),
                mapping_owner=("scheduled billing and administrative batch adapters"),
                retryable_codes=(
                    "financial.billing_automation.execution_failed",
                    "financial.billing_automation.audit_projection_failed",
                ),
                fail_closed_on=(
                    "invalid cycle/date",
                    "changed confirmed membership",
                    "non-failed retry source",
                    "missing canonical price or treatment authority",
                ),
            ),
            events=EventContract(
                event_types=("invoice_created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 identifies the canonical invoice, account, "
                    "amount, currency, period, due date, and billing-run ID."
                ),
                replay=(
                    "Replay rebuilds invoice-created delivery projections; "
                    "invoice and BillingRun rows remain authoritative."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="billing-run audit projection",
                    input_names=("canonical billing-run record",),
                    writer="financial.billing_automation",
                    freshness=(
                        "Written after terminal BillingRun state; a missing "
                        "row never changes the authoritative run outcome."
                    ),
                    stale_behavior=(
                        "BillingRun history remains visible and explicitly "
                        "identifies the missing secondary audit projection."
                    ),
                    drift_signal=(
                        "A terminal BillingRun has no billing_run AuditEvent "
                        "with the same run identifier."
                    ),
                    rebuild_operation=(
                        "Re-run reconcile_billing_run_audit for the canonical "
                        "BillingRun identifier."
                    ),
                    repair_owner="financial.billing_automation",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "batch-page forms and JavaScript launched mutable work "
                    "without exact evidence; retry was available for every "
                    "historical status and carried no lineage; an unused "
                    "BillingRunSchedule plus shadow DomainSetting falsely "
                    "presented itself as scheduler configuration"
                ),
                new_owner="financial.billing_automation",
                verification=(
                    "dry-run scope, fingerprint drift, launch evidence, "
                    "failed-only retry, lineage, idempotency, audit repair, "
                    "adapter, UI, and architecture tests"
                ),
                cutover_gate=(
                    "Manual and retry launches persist exact preview and actor "
                    "evidence; only failed runs can be retry sources."
                ),
                fallback_retirement=(
                    "Unpreviewed manual launch, implicit prepaid renewal, "
                    "unlinked retry, browser confirmation, and the unwired "
                    "schedule facade are absent. scheduler.registry remains "
                    "the only cadence and enablement owner."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_billing_invoice_batch_web.py",
                "tests/test_billing_automation_services.py",
                "tests/architecture/test_action_form_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="financial.billing_health",
        module="app.services.billing_health",
        owns=(
            "billing health snapshot",
            "billing anomaly classification",
            "bounded billing health observations",
            "account-credit invariant observation publication",
        ),
        depends_on=(
            "customer.financial_position",
            "financial.access_resolution",
            "financial.billing_profile",
            "financial.account_credit_applications",
        ),
        notes=(
            "Billing health is monitoring evidence, never a financial "
            "balance owner or direct suspension/restoration decision. "
            "The frequent snapshot consumes typed aggregate counts; "
            "record-level forensic inspection stays with the financial "
            "owner and is not used merely to calculate a metric count."
        ),
    ),
    SOTService(
        name="financial.billing_reporting",
        module="app.services.billing.reporting",
        owns=(
            "billing statistics and dashboard report read models",
            "admin revenue report figure definitions",
            "payments-basis revenue definitions",
            "subscription movement and per-offer report counts",
        ),
        depends_on=("financial.invoices", "financial.payments"),
        notes=(
            "Read owner only: aggregates invoice/payment/subscription "
            "facts for dashboards and the admin reports. It decides no "
            "financial consequences."
        ),
    ),
    SOTService(
        name="financial.billing_scheduled",
        module="app.services.billing.scheduled",
        owns=(
            "scheduled invoice and overdue execution",
            "billing health and audit execution",
            "scheduled billing notification execution",
        ),
        depends_on=(
            "financial.ledger",
            "financial.access_resolution",
            "financial.billing_health",
        ),
    ),
    SOTService(
        name="financial.collections_scheduled",
        module="app.services.collections.scheduled",
        owns=(
            "scheduled billing enforcement execution",
            "scheduled prepaid balance enforcement execution",
            "scheduled prepaid coverage-evidence repair execution",
            "scheduled bundle-state reconciliation execution",
        ),
        depends_on=(
            "financial.dunning",
            "financial.access_resolution",
            "financial.prepaid_enforcement",
            "financial.prepaid_enforcement_state",
            "financial.prepaid_service_coverage_reconciliation",
        ),
        notes=(
            "The coverage-evidence repair pass is transitional ADR "
            "0007 debt: it drains entitlement gaps historical forward "
            "billing could commit. It retires with the prepaid "
            "balance sweep at the Phase 5 collections cutover, once "
            "activation and renewal cannot commit without contract, "
            "obligation, and timer."
        ),
    ),
)
