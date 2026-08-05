"""sales_referrals SOT declarations: customer handoff."""

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
        name="customer.experience_handoff",
        module="app.services.customer_experience_handoffs",
        owns=(
            "implementation-to-customer-experience readiness decision",
            "CX acceptance and needs-attention lifecycle",
            "durable CX actor, time, reason, and event evidence",
        ),
        depends_on=(
            "auth.permission_gate",
            "sales.orders",
            "sales.fulfillment",
            "operations.service_order_lifecycle",
            "access.subscription_lifecycle",
            "events.dispatcher",
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("implementation-to-customer-experience readiness decision"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical sales fulfilment state",
                        "canonical ServiceOrder completion state",
                        "canonical subscription access state",
                        "customer-experience transition protocol",
                    ),
                    canonical_writer="customer.experience_handoff",
                ),
                ConcernContract(
                    name="CX acceptance and needs-attention lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical CX handoff state",
                        "reviewed CX transition command",
                        "canonical SalesOrder state",
                    ),
                    canonical_writer="customer.experience_handoff",
                ),
                ConcernContract(
                    name="durable CX actor, time, reason, and event evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical CX handoff state",
                        "reviewed CX transition command",
                    ),
                    canonical_writer="customer.experience_handoff",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical sales fulfilment state",
                    owner="sales.fulfillment",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "structurally linked completed Project, verified "
                        "InstallationProject, and exact verification evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical ServiceOrder completion state",
                    owner="operations.service_order_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active structurally linked ServiceOrder and committed "
                        "successful provisioning result"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription access state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="the exact linked active Subscription",
                ),
                AuthorityInput(
                    name="canonical SalesOrder state",
                    owner="sales.orders",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="the exact fully paid or fulfilled SalesOrder",
                ),
                AuthorityInput(
                    name="canonical CX handoff state",
                    owner="customer.experience_handoff",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked CustomerExperienceHandoff and append-only "
                        "CustomerExperienceHandoffEvent history"
                    ),
                ),
                AuthorityInput(
                    name="customer-experience transition protocol",
                    owner="customer.experience_handoff",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "policy version 1 readiness facts and pending, ready, "
                        "accepted, needs-attention, and canceled state graph"
                    ),
                ),
                AuthorityInput(
                    name="reviewed CX transition command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "authenticated actor type/id, action, and bounded reason "
                        "from the customer-experience adapter"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Readiness may flush into the committed service-completion "
                    "projection transaction; staff acceptance/attention owns one "
                    "handoff, evidence, SalesOrder consequence, and event transaction."
                ),
                locking=(
                    "The exact ServiceOrder or CustomerExperienceHandoff is selected "
                    "FOR UPDATE; the SalesOrder owner locks its own root."
                ),
                idempotency=(
                    "Unique Subscription and ServiceOrder bindings plus terminal "
                    "status checks make identical readiness/acceptance replay a no-op."
                ),
                retries=(
                    "Committed service completion and staff commands replay exact "
                    "root identifiers through the same owner."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "customer.experience_handoff.active_caller_transaction",
                    "customer.experience_handoff.command_contract_violation",
                    "customer.experience_handoff.invalid_command_context",
                    "customer.experience_handoff.nested_owner_command",
                    "customer.experience_handoff.nested_transaction_completion",
                    "service_order_not_found",
                    "incomplete_handoff_context",
                    "handoff_context_mismatch",
                    "actor_required",
                    "handoff_not_found",
                    "handoff_not_ready",
                    "reason_required",
                    "attention_reason_conflict",
                    "handoff_terminal",
                    "invalid_status",
                ),
                mapping_owner="customer-experience API and event adapters",
                fail_closed_on=(
                    "missing structural lifecycle roots",
                    "funding or readiness disagreement",
                    "conflicting handoff binding",
                    "invalid or terminal transition",
                ),
            ),
            events=EventContract(
                event_types=(
                    "customer_experience.ready",
                    "customer_experience.accepted",
                    "customer_experience.needs_attention",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact lifecycle roots, source and target "
                    "status, policy version, actor, reason, and readiness evidence."
                ),
                replay=(
                    "The handoff root and append-only event rows reconstruct status; "
                    "missing readiness is repaired from authoritative linked facts."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="prose onboarding checklist without durable acceptance state",
                new_owner="customer.experience_handoff",
                verification=(
                    "Readiness, incomplete context, acceptance, attention, evidence "
                    "immutability, SalesOrder fulfilment, and lifecycle tests."
                ),
                cutover_gate=(
                    "A sales-origin active ServiceOrder creates one structurally "
                    "linked handoff; only reviewed CX acceptance fulfils the order."
                ),
                fallback_retirement=(
                    "Template-derived onboarding completion and direct SalesOrder "
                    "fulfilment writes are absent."
                ),
            ),
            steward="customer experience",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_sales_to_service_lifecycle.py",
                "tests/test_sales_orders_services.py",
                "tests/architecture/test_service_http_boundary.py",
            ),
        ),
    ),
)
