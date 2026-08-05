"""financial_access SOT declarations: durable timers."""

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
        name="runtime.durable_timers",
        module="app.services.runtime_durable_timers",
        owns=(
            "owner-bound durable timer generations",
            "due-timer trigger emission",
        ),
        depends_on=("events.dispatcher", "events.store"),
        notes=(
            "ADR 0007 Phase 5. The owning business transition stages "
            "its timer as a flush-only participant, so a transition "
            "requiring a future action cannot commit without it. The "
            "fire path scans due_at on an index with a bounded batch "
            "and emits only the declared trigger with its generation; "
            "it performs no customer, invoice, funding, or access "
            "decision. This replaces business-wide financial sweeps."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="owner-bound durable timer generations",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "owning transition command evidence",
                        "recorded durable timers",
                    ),
                    canonical_writer="runtime.durable_timers",
                ),
                ConcernContract(
                    name="due-timer trigger emission",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("recorded durable timers",),
                    canonical_writer="runtime.durable_timers",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="owning transition command evidence",
                    owner="events.dispatcher",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "the calling owner's active command context and "
                        "declared output event type"
                    ),
                ),
                AuthorityInput(
                    name="recorded durable timers",
                    owner="runtime.durable_timers",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="durable_timers rows and their generations",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "schedule_timer and cancel_timer are flush-only "
                    "participants inside the owning transition's "
                    "command; fire_due_timers enters "
                    "execute_owner_command once on a transaction-free "
                    "session."
                ),
                locking=(
                    "The current timer row is locked FOR UPDATE before "
                    "replacement; the due scan uses SKIP LOCKED so "
                    "concurrent fire runs never double-emit."
                ),
                idempotency=(
                    "One current timer per (owner, entity, purpose); "
                    "replacement bumps the generation so a stale "
                    "delivery is idempotently rejected by its consumer."
                ),
                retries=(
                    "A failed fire batch rolls back whole; timers stay "
                    "scheduled and the next run retries them."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "runtime.durable_timers.active_caller_transaction",
                    "runtime.durable_timers.command_contract_violation",
                    "runtime.durable_timers.invalid_command_context",
                    "runtime.durable_timers.invalid_timer_due_at",
                    "runtime.durable_timers.invalid_timer_output",
                    "runtime.durable_timers.nested_owner_command",
                    "runtime.durable_timers.nested_transaction_completion",
                    "runtime.durable_timers.timer_requires_owner_command",
                ),
                mapping_owner="owning transitions and the timer runner task",
                fail_closed_on=(
                    "staging a timer outside an owner command",
                    "a timer without a declared output event type",
                ),
            ),
            events=EventContract(
                event_types=("runtime.timer_due",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive; the trigger payload names "
                    "the declared output and generation."
                ),
                replay=(
                    "A fired trigger redelivers at least once; consumers "
                    "reject a stale generation."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "dunning_runner, prepaid_balance_sweep, and other "
                    "scheduled account scans in scheduler_config"
                ),
                new_owner="runtime.durable_timers",
                verification=(
                    "Generation replacement, stale rejection, bounded "
                    "due-scan, and participant boundary tests plus the "
                    "ADR 0007 sweep ratchet."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 5 gate: every open invoice, prepaid "
                    "period, grace deadline, and escalation has exactly "
                    "one current timer or a typed no-timer reason, and "
                    "timer-triggered outcomes match the sweeps for the "
                    "full candidate cohort."
                ),
                fallback_retirement=(
                    "dunning_runner and prepaid_balance_sweep scheduled "
                    "tasks are removed from scheduler_config and the "
                    "sweep baseline after cutover."
                ),
            ),
            steward="platform and billing operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_durable_timers.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
)
