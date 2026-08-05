"""financial_access SOT declarations: erp billing."""

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
        name="integration.dotmac_erp_billing_adapter",
        module="app.services.dotmac_erp.billing_adapter",
        owns=(
            "versioned ERP billing payload staging",
            "durable ERP delivery and acknowledgement evidence",
        ),
        depends_on=("events.owner_outputs",),
        notes=(
            "ADR 0007 Phase 7. A transport, not a decision system: it "
            "maps committed Sub owner outputs into versioned "
            "idempotent ERP payloads and keeps durable delivery and "
            "acknowledgement evidence. Dotmac ERP keeps the chart of "
            "accounts, TaxCode mappings, journals, returns, and "
            "statements, and fails closed on anything missing or "
            "ambiguous. ERP downtime leaves exports pending and never "
            "rolls back Sub cash, documents, entitlement, or access."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="versioned ERP billing payload staging",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "committed billing owner outputs",
                        "recorded ERP exports",
                    ),
                    canonical_writer=("integration.dotmac_erp_billing_adapter"),
                ),
                ConcernContract(
                    name=("durable ERP delivery and acknowledgement evidence"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("recorded ERP exports",),
                    canonical_writer=("integration.dotmac_erp_billing_adapter"),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="committed billing owner outputs",
                    owner="events.owner_outputs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "committed document, payment, and posting owner "
                        "outputs with their envelopes"
                    ),
                ),
                AuthorityInput(
                    name="recorded ERP exports",
                    owner="integration.dotmac_erp_billing_adapter",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="erp_billing_exports rows",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "stage_export is a flush-only participant inside "
                    "the committing owner's command; "
                    "record_delivery_outcome enters "
                    "execute_owner_command once on a transaction-free "
                    "session."
                ),
                locking=(
                    "The export row is locked FOR UPDATE before an "
                    "outcome is recorded; the idempotency unique "
                    "constraint serialises duplicate staging."
                ),
                idempotency=(
                    "One export per business idempotency key; a replayed "
                    "terminal outcome is a no-op and a conflicting one "
                    "fails closed."
                ),
                retries=(
                    "Delivery attempts are durable and repeatable; a "
                    "pending export survives ERP downtime untouched."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "integration.dotmac_erp_billing_adapter.active_caller_transaction",
                    "integration.dotmac_erp_billing_adapter.command_contract_violation",
                    "integration.dotmac_erp_billing_adapter.conflicting_export_outcome",
                    "integration.dotmac_erp_billing_adapter.export_not_found",
                    "integration.dotmac_erp_billing_adapter.export_requires_owner_command",
                    "integration.dotmac_erp_billing_adapter.incomplete_export_payload",
                    "integration.dotmac_erp_billing_adapter.invalid_command_context",
                    "integration.dotmac_erp_billing_adapter.invalid_outcome",
                    "integration.dotmac_erp_billing_adapter.invalid_outcome_instant",
                    "integration.dotmac_erp_billing_adapter.missing_erp_reference",
                    "integration.dotmac_erp_billing_adapter.missing_idempotency_key",
                    "integration.dotmac_erp_billing_adapter.missing_rejection_evidence",
                    "integration.dotmac_erp_billing_adapter.nested_owner_command",
                    "integration.dotmac_erp_billing_adapter.nested_transaction_completion",
                ),
                mapping_owner="the ERP delivery task and finance adapters",
                fail_closed_on=(
                    "an incomplete payload",
                    "an acknowledgement without ERP's reference",
                    "a rejection without reviewable evidence",
                ),
            ),
            events=EventContract(
                event_types=("erp_billing_export.status_changed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive; payload versions are pinned per export row."
                ),
                replay=(
                    "Exports rebuild from committed owner outputs; "
                    "delivery replays are idempotent by key."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "ad hoc ERP billing pushes without durable acknowledgement evidence"
                ),
                new_owner="integration.dotmac_erp_billing_adapter",
                verification=(
                    "Idempotent staging, fail-closed payload, outcome "
                    "conflict, and replay tests plus the ADR 0007 "
                    "guards."
                ),
                cutover_gate=(
                    "ADR 0007 Phase 7 gate: every flow has stable "
                    "idempotency and replay, ERP outage cannot roll "
                    "back Sub facts, identities are structurally "
                    "recorded, and finance approves accounting parity."
                ),
                fallback_retirement=(
                    "Fallback ERP push paths and obsolete columns are "
                    "removed in the Phase 7 contract step."
                ),
            ),
            steward="finance and platform operations",
            design_refs=(
                "docs/adr/0007-end-to-end-billing-target-architecture.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_erp_billing_adapter.py",
                "tests/architecture/test_billing_target_architecture.py",
            ),
        ),
    ),
)
