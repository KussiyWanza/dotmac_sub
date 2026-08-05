"""financial_access SOT declarations: collections."""

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
        name="collections.postpaid_policy",
        module="app.services.collections.postpaid_policy",
        owns=("typed overdue-receivable decision",),
        depends_on=("billing.obligations",),
        notes=(
            "ADR 0007 Phase 5. Read-only planner over one exact "
            "overdue collectible receivable obligation. Returns a "
            "typed proposal for collections.lifecycle; decides no "
            "consequence and mutates nothing."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="typed overdue-receivable decision",
                    role=OwnerRole.POLICY,
                    input_names=("recorded billing obligations",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="recorded billing obligations",
                    owner="billing.obligations",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="exact obligation state, due time, and amounts",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller owns the session; the planner reads one "
                    "obligation and completes no transaction."
                ),
                locking=(
                    "No read lock. collections.lifecycle locks its case "
                    "before acting on the proposal."
                ),
                idempotency=(
                    "Deterministic: identical obligation state and "
                    "instant produce an identical proposal or None."
                ),
                retries="Reads may be retried without side effects.",
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="collections adapters",
                fail_closed_on=("a naive evaluation instant",),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner="dunning rule evaluation inside dunning tasks",
                new_owner="collections.postpaid_policy",
                verification=(
                    "Overdue, partial-settlement, and non-receivable planner tests."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 5 gate: planner proposals match "
                    "current dunning outcomes for the candidate cohort."
                ),
                fallback_retirement=(
                    "Inline dunning rule evaluation is removed after cutover."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_collections_target_lifecycle.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="collections.prepaid_policy",
        module="app.services.collections.prepaid_policy",
        owns=("typed uncovered-service decision",),
        depends_on=(
            "billing.obligations",
            "financial.customer_subledger",
            "financial.prepaid_funding_reconstruction",
        ),
        notes=(
            "ADR 0007 Phase 5. Read-only planner over one exact "
            "uncovered prepaid obligation and the typed per-currency "
            "funding position. No receivable is created for "
            "enforcement convenience."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="typed uncovered-service decision",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "recorded billing obligations",
                        "typed per-currency subledger position",
                        "prepaid opening-position quarantine",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="recorded billing obligations",
                    owner="billing.obligations",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="exact obligation state, period, and amounts",
                ),
                AuthorityInput(
                    name="typed per-currency subledger position",
                    owner="financial.customer_subledger",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "prepaid funding and unapplied credit lanes for "
                        "the obligation's account and currency"
                    ),
                ),
                AuthorityInput(
                    name="prepaid opening-position quarantine",
                    owner="financial.prepaid_funding_reconstruction",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "evidence-bound accounts without an approved active "
                        "prepaid funding baseline and their owned finance work items"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Caller owns the session; the planner reads exact "
                    "facts and completes no transaction."
                ),
                locking=(
                    "No read lock. collections.lifecycle locks its case "
                    "before acting on the proposal."
                ),
                idempotency=(
                    "Deterministic: identical obligation, position, and "
                    "instant produce an identical proposal or None."
                ),
                retries="Reads may be retried without side effects.",
            ),
            errors=ErrorContract(
                domain_codes=("collections.prepaid_policy.opening_source_incomplete",),
                mapping_owner="collections adapters",
                fail_closed_on=(
                    "a naive evaluation instant",
                    "an unresolved prepaid opening-position quarantine",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=("prepaid balance sweep threshold evaluation"),
                new_owner="collections.prepaid_policy",
                verification=(
                    "Underfunded, covered, and not-yet-started planner tests."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 5 gate: planner proposals match "
                    "prepaid enforcement outcomes for the candidate "
                    "cohort."
                ),
                fallback_retirement=(
                    "Sweep threshold evaluation is removed after cutover."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_collections_target_lifecycle.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
    SOTService(
        name="collections.lifecycle",
        module="app.services.collections.lifecycle",
        owns=(
            "reason-scoped collections case workflow",
            "collections case close and reopen evidence",
        ),
        depends_on=(
            "collections.postpaid_policy",
            "collections.prepaid_policy",
            "events.owner_outputs",
            "runtime.durable_timers",
        ),
        notes=(
            "ADR 0007 Phase 5. One case per account/subscription/"
            "reason with warning and escalation states, exact durable "
            "timers, and idempotent consequence requests. It never "
            "mutates subscription or RADIUS state: only "
            "access.subscription_lifecycle applies or removes the "
            "matching reason-scoped restriction."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="reason-scoped collections case workflow",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "typed mode-policy proposals",
                        "recorded collections cases",
                    ),
                    canonical_writer="collections.lifecycle",
                ),
                ConcernContract(
                    name="collections case close and reopen evidence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("recorded collections cases",),
                    canonical_writer="collections.lifecycle",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="typed mode-policy proposals",
                    owner="collections.postpaid_policy",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "CollectionsProposal values from the postpaid "
                        "and prepaid planners"
                    ),
                ),
                AuthorityInput(
                    name="recorded collections cases",
                    owner="collections.lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="collections_cases rows",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "advance and close each enter execute_owner_command "
                    "once on a transaction-free session; timers and "
                    "consequence outputs are staged as flush-only "
                    "participants in the same transaction."
                ),
                locking=(
                    "The live case row is locked FOR UPDATE before any "
                    "transition; the partial unique index enforces one "
                    "live case per (account, subscription, reason)."
                ),
                idempotency=(
                    "Advancing a terminal case is a no-op; the "
                    "consequence idempotency key is unique so access "
                    "applies at most one restriction per request."
                ),
                retries=(
                    "The complete command retries; a failed advance "
                    "leaves the case, timer, and output unstaged."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "collections.lifecycle.active_caller_transaction",
                    "collections.lifecycle.command_contract_violation",
                    "collections.lifecycle.invalid_case_instant",
                    "collections.lifecycle.invalid_command_context",
                    "collections.lifecycle.missing_close_reason",
                    "collections.lifecycle.nested_owner_command",
                    "collections.lifecycle.nested_transaction_completion",
                ),
                mapping_owner="collections adapters and the timer runner",
                fail_closed_on=(
                    "a naive case instant",
                    "closing without close evidence",
                ),
            ),
            events=EventContract(
                event_types=(
                    "collections.consequence_requested",
                    "collections.case_closed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive; consequence requests carry "
                    "their reason and idempotency key."
                ),
                replay=(
                    "Outputs redeliver at least once; the access owner "
                    "receipts them via events.owner_outputs."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "postpaid dunning workflow state and prepaid "
                    "enforcement timer/notice fields"
                ),
                new_owner="collections.lifecycle",
                verification=(
                    "Case ladder, consequence idempotency, close/"
                    "restore, and timer replacement tests plus the ADR "
                    "0007 guards."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 5 gate: shadow cases produce the "
                    "same or explicitly approved outcomes as current "
                    "dunning and prepaid enforcement for the full "
                    "candidate cohort without duplicate consequences."
                ),
                fallback_retirement=(
                    "dunning_runner, prepaid_balance_sweep, duplicate "
                    "notice/timer fields, and parallel access actions "
                    "are removed after cutover."
                ),
            ),
            steward="billing operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_collections_target_lifecycle.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
)
