"""financial_access SOT declarations: sales funding."""

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
        name="sales.order_funding",
        module="app.services.sales_order_funding",
        owns=(
            "finite order-obligation funding set",
            "exact funding-gate transition evidence",
        ),
        depends_on=(
            "billing.obligations",
            "events.owner_outputs",
            "sales.orders",
        ),
        notes=(
            "ADR 0007 Phase 6. The order funding gate consumes exact "
            "obligation-resolution outputs for its registered finite "
            "set only. Partial funding never advances it, full finite "
            "funding advances it exactly once, and recurring "
            "obligations on the subscription contract cannot reopen "
            "or inflate the historical order result. "
            "SalesOrder.amount_paid remains provenance during shadow. "
            "Coverage is DERIVED, never asserted by an operator: "
            "payment_status, amount_paid and paid_at are refused on the "
            "generic sales-order edit and on the admin form "
            "(sales_orders.FUNDING_CONTROLLED_FIELDS), so only a caller "
            "holding a sales_orders.FundingAuthority — recorded settlement, "
            "verified deposit evidence, or this gate — can cross the funding "
            "edge that stages sales_order.funding_satisfied."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="finite order-obligation funding set",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "exact obligation resolution outputs",
                        "recorded funding gates",
                    ),
                    canonical_writer="sales.order_funding",
                ),
                ConcernContract(
                    name="exact funding-gate transition evidence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "exact obligation resolution outputs",
                        "recorded funding gates",
                    ),
                    canonical_writer="sales.order_funding",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="exact obligation resolution outputs",
                    owner="billing.obligations",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "billing.obligation.resolved outputs with their "
                        "resolution kind and event identity"
                    ),
                ),
                AuthorityInput(
                    name="recorded funding gates",
                    owner="sales.order_funding",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "sales_order_funding_gates and "
                        "sales_order_funding_obligations rows"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "register_finite_obligations and "
                    "record_obligation_resolution each enter "
                    "execute_owner_command once on a transaction-free "
                    "session."
                ),
                locking=(
                    "The gate row is locked FOR UPDATE before "
                    "registration or resolution, so concurrent "
                    "resolutions serialise and the gate advances once."
                ),
                idempotency=(
                    "Registration and resolution are idempotent per "
                    "obligation; a funded gate refuses set changes."
                ),
                retries=(
                    "The complete command retries. The funded output is "
                    "staged atomically with the gate transition."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "sales.order_funding.active_caller_transaction",
                    "sales.order_funding.command_contract_violation",
                    "sales.order_funding.empty_finite_obligation_set",
                    "sales.order_funding.funding_gate_not_found",
                    "sales.order_funding.gate_already_funded",
                    "sales.order_funding.invalid_command_context",
                    "sales.order_funding.invalid_resolution_instant",
                    "sales.order_funding.nested_owner_command",
                    "sales.order_funding.nested_transaction_completion",
                    "sales.order_funding.obligation_not_in_finite_set",
                ),
                mapping_owner="sales and billing adapters",
                fail_closed_on=(
                    "a resolution for an unregistered obligation",
                    "changing the finite set of a funded gate",
                ),
            ),
            events=EventContract(
                event_types=("sales.order_funding.completed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive; the output names the order "
                    "and its finite obligation count."
                ),
                replay=(
                    "The funded output redelivers at least once; "
                    "fulfillment receipts it via events.owner_outputs."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "SalesOrder.amount_paid arithmetic and metadata "
                    "payment-origin joins"
                ),
                new_owner="sales.order_funding",
                verification=(
                    "Partial-funding, funded-once, unregistered-"
                    "obligation, and replay tests plus the ADR 0007 "
                    "guards."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 6 gate: shadow funding gates match "
                    "current SalesOrder funding projections for the "
                    "cohort, partial funding never releases service, "
                    "and recurring obligations never touch the gate."
                ),
                fallback_retirement=(
                    "SalesOrder.amount_paid authority and metadata "
                    "payment-origin joins are removed after cutover."
                ),
            ),
            steward="sales and billing operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_sales_order_funding.py",
                "tests/test_sales_order_funding_authority.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
)
