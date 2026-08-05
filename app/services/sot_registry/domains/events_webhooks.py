"""Canonical SOT declarations for the events_webhooks domain."""

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
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="events_webhooks",
    services=(
        SOTService(
            name="events.dispatcher",
            module="app.services.events.dispatcher",
            owns=("event routing", "handler orchestration"),
            depends_on=("control.relationships",),
        ),
        SOTService(
            name="events.store",
            module="app.services.event_store",
            owns=("event persistence", "handler attempt tracking"),
            depends_on=("events.dispatcher",),
        ),
        SOTService(
            name="events.owner_outputs",
            module="app.services.events.owner_outputs",
            owns=(
                "versioned owner-output envelope",
                "durable owner-output consumer receipts",
            ),
            depends_on=(
                "events.dispatcher",
                "events.store",
            ),
            notes=(
                "ADR 0007 Phase 4. The guaranteed owner-output protocol: "
                "a producer stages its versioned output inside its own "
                "owner command (state and output commit atomically), and a "
                "consumer commits its business effect and its unique "
                "(consumer, event_id) receipt atomically. Redelivery is "
                "harmless, a retryable failure stays durably pending, and "
                "a terminal failure is recorded with reviewable evidence "
                "instead of becoming a success log line."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="versioned owner-output envelope",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "producing owner command evidence",
                            "staged outbox events",
                        ),
                    ),
                    ConcernContract(
                        name="durable owner-output consumer receipts",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "producing owner command evidence",
                            "recorded consumer receipts",
                        ),
                        canonical_writer="events.owner_outputs",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="producing owner command evidence",
                        owner="events.dispatcher",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "the calling owner's active command context "
                            "(command, correlation, causation, idempotency)"
                        ),
                    ),
                    AuthorityInput(
                        name="staged outbox events",
                        owner="events.store",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="event_store rows staged transactionally",
                    ),
                    AuthorityInput(
                        name="recorded consumer receipts",
                        owner="events.owner_outputs",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="owner_output_receipts rows",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "stage_owner_output, consume_owner_output, and "
                        "record_terminal_failure run only inside the calling "
                        "owner's active execute_owner_command transaction, "
                        "use flush only, and never commit or roll back."
                    ),
                    locking=(
                        "The calling owner holds its canonical locks; the "
                        "(consumer, event_id) unique constraint serialises "
                        "duplicate deliveries."
                    ),
                    idempotency=(
                        "One receipt per (consumer, event_id). A redelivered "
                        "event returns the recorded outcome without running "
                        "the effect again."
                    ),
                    retries=(
                        "A raised consumer error leaves no receipt, so the "
                        "outbox keeps the delivery durably retryable. Only an "
                        "explicit terminal failure ends retrying."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "events.owner_outputs.missing_failure_reason",
                        "events.owner_outputs.missing_idempotency_key",
                        "events.owner_outputs.missing_required_payload",
                        "events.owner_outputs.invalid_required_payload",
                        "events.owner_outputs.output_requires_owner_command",
                        "events.owner_outputs.receipt_already_recorded",
                        "events.owner_outputs.receipt_requires_owner_command",
                    ),
                    mapping_owner="producing and consuming owners' adapters",
                    fail_closed_on=(
                        "staging or receipting outside an owner command",
                        "a second outcome for one (consumer, event_id)",
                        "a required consumer identity missing from an output",
                        "a required consumer input having the wrong shape or type",
                        "a terminal failure without reviewable evidence",
                    ),
                ),
                events=EventContract(
                    event_types=("owner_output.terminal_failure_recorded",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive. The envelope adds fields, "
                        "never repurposes them."
                    ),
                    replay=(
                        "Receipts rebuild from owner_output_receipts; the "
                        "outbox redelivers unreceipted events at least once."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner=(
                        "best-effort handler completion and logged-only "
                        "failure in the legacy dispatcher path"
                    ),
                    new_owner="events.owner_outputs",
                    verification=(
                        "Producer atomicity, replay-once, retryable-failure, "
                        "and terminal-failure tests plus the ADR 0007 guards."
                    ),
                    cutover_gate=(
                        "ADR 0007 Phase 4 gate: state transitions cannot "
                        "commit without their required output, replay "
                        "produces one business effect, injected failures "
                        "remain retryable, and every event has a named "
                        "terminal consumer outcome."
                    ),
                    fallback_retirement=(
                        "Direct cross-owner calls that independently commit "
                        "and logged-only failure paths are removed after "
                        "cutover."
                    ),
                ),
                steward="platform and billing operations",
                design_refs=(
                    "docs/adr/0007-end-to-end-billing-target-architecture.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_owner_outputs.py",
                    "tests/architecture/test_billing_target_architecture.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.services.events.handlers.*",
        "app.tasks.integration_delivery",
        "app.web.admin.integrations",
    ),
    rule="Handlers orchestrate; event persistence stays in events.store and "
    "external delivery is requested from integration.delivery.",
)
