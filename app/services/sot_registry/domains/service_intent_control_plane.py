"""Canonical SOT declarations for the service_intent_control_plane domain."""

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
    domain="service_intent_control_plane",
    services=(
        SOTService(
            name="service_intent.catalog_policy",
            module="app.services.catalog.policies",
            owns=("catalog policy lookup", "offer policy interpretation"),
        ),
        SOTService(
            name="service_intent.catalog_validation",
            module="app.services.catalog.validation",
            owns=("catalog mutation validation", "offer/profile consistency"),
            depends_on=("service_intent.catalog_policy",),
        ),
        SOTService(
            name="service_intent.catalog_billing_governance",
            module="app.services.catalog_billing_governance",
            owns=(
                "billing-critical catalog mutation policy",
                "live pricing and cadence immutability",
                "billing catalog audit and operator alerting",
            ),
            depends_on=(
                "service_intent.catalog_validation",
                "auth.permission_gate",
                "observability.recording",
            ),
        ),
        SOTService(
            name="service_intent.subscription_nas_assignment",
            module="app.services.catalog.subscriptions",
            owns=(
                "subscription provisioning NAS assignment",
                "nonterminal services grouped by NAS",
            ),
            depends_on=("service_intent.catalog_policy",),
        ),
        SOTService(
            name="service_intent.subscription_billing_cadence",
            module="app.services.catalog.subscriptions",
            owns=(
                "subscription billing cadence",
                "subscription cadence resolution "
                "(subscription -> offer price -> monthly)",
                "next-billing anchor computation",
            ),
            depends_on=("service_intent.catalog_policy",),
            notes=(
                "The subscription is the source of truth for a customer's "
                "contracted billing cadence, captured from the sales-order "
                "line and read by billing_automation. The offer/version "
                "price cadence is fallback-only when the subscription's is "
                "unset. Catalog offer-cadence immutability stays with "
                "service_intent.catalog_billing_governance."
            ),
        ),
        SOTService(
            name="service_intent.subscription_lifecycle",
            module="app.services.subscription_lifecycle",
            owns=(
                "subscription lifecycle state projection",
                "subscription command eligibility and preview",
                "billing and access impact projection",
                "service-change delivery-mode decision",
                "service-address qualification and field-fee preview",
                "vacation-hold duration, annual-limit, cooldown, and resume policy",
                "subscription command and outcome contracts",
            ),
            depends_on=(
                "service_intent.catalog_policy",
                "control.settings_spec",
                "financial.access_resolution",
                "financial.prepaid_plan_change",
                "access.radius_state",
            ),
            notes=(
                "Service-change preview classifies commercial-only, remote, "
                "and field delivery from access and provisionable network "
                "facts; plan family is never delivery evidence. A service-"
                "address change is always field delivery. Fixed-wireless/radio "
                "relocation fails closed unless the target address qualifies "
                "and the configured catalog offer supplies a nonzero one-time "
                "field fee. "
                "Execution remains with the established billing, account "
                "lifecycle, catalog, and RADIUS owners. UI, API, scheduled, "
                "and bulk callers consume this preview before execution."
            ),
        ),
        SOTService(
            name="service_intent.subscription_lifecycle_execution",
            module="app.services.subscription_lifecycle_commands",
            owns=(
                "single-subscription command orchestration",
                "subscription command locking and reviewed-head enforcement",
                "subscription command idempotent replay",
                "structured subscription command outcomes",
                "persisted relocation qualification and fee evidence",
                "vacation-hold and exact customer-lock resume orchestration",
                "independently committed subscription command batches",
            ),
            depends_on=(
                "service_intent.subscription_lifecycle",
                "service_intent.catalog_policy",
                "financial.prepaid_plan_change",
                "access.radius_state",
            ),
            notes=(
                "Confirmed commercial-only changes apply immediately. Remote "
                "and field changes persist reviewed intent until their delivery "
                "owner supplies verification; no support ticket is created. A "
                "priced field relocation remains awaiting_payment and leaves "
                "the current offer and service address unchanged. "
                "Delegates mutations and side effects to the established "
                "account lifecycle, catalog, billing, scheduler, and RADIUS "
                "owners. Renewal execution remains billing-owned and fails "
                "closed. Deferred status execution is owned by "
                "service_intent.subscription_lifecycle_scheduling. Admin "
                "single and bulk adapters delegate here instead of writing "
                "subscription lifecycle fields directly."
                " Customer, admin, and automatic vacation-hold adapters all "
                "delegate customer_hold lock creation/resolution here."
            ),
        ),
        SOTService(
            name="service_intent.subscription_lifecycle_scheduling",
            module="app.services.subscription_lifecycle_schedules",
            owns=(
                "durable deferred subscription status intent",
                "deferred command execution leases and bounded retry",
                "scheduled lifecycle cancellation",
                "deferred lifecycle execution evidence",
            ),
            depends_on=(
                "service_intent.subscription_lifecycle",
                "service_intent.subscription_lifecycle_execution",
                "scheduler.registry",
            ),
            notes=(
                "Revalidates the reviewed subscription head at execution "
                "time and delegates every mutation to the canonical command "
                "executor. Plan scheduling remains with the catalog change "
                "request owner."
            ),
        ),
        SOTService(
            name="service_intent.subscription_change_execution",
            module="app.services.subscription_change_execution",
            owns=(
                "relocation charge evidence and settlement admission",
                "paid relocation fulfillment release",
                "remote provisioning price confirmation and failure recovery",
                "remote reprovision verification",
                "verified service-change finalization",
                "interrupted execution-chain reconciliation",
            ),
            depends_on=(
                "service_intent.subscription_lifecycle_execution",
                "financial.invoices",
                "financial.payments",
                "operations.service_order_lifecycle",
                "operations.work_order_commands",
                "operations.provisioning_lifecycle",
                "access.radius_state",
                "financial.prepaid_plan_change",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="relocation charge evidence and settlement admission",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "confirmed relocation quote evidence",
                            "canonical invoice and payment allocation evidence",
                        ),
                    ),
                    ConcernContract(
                        name="paid relocation fulfillment release",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "canonical invoice and payment allocation evidence",
                            "canonical subscription-change execution state",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "remote provisioning price confirmation and failure "
                            "recovery"
                        ),
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "canonical prepaid plan-change decision",
                            "canonical RADIUS profile observation",
                            "canonical subscription-change execution state",
                        ),
                    ),
                    ConcernContract(
                        name="remote reprovision verification",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "catalog-linked target RADIUS profile",
                            "canonical RADIUS profile observation",
                            "canonical subscription-change execution state",
                        ),
                    ),
                    ConcernContract(
                        name="verified service-change finalization",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "canonical provisioning-readiness decision",
                            "canonical subscription-change execution state",
                        ),
                    ),
                    ConcernContract(
                        name="interrupted execution-chain reconciliation",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "canonical invoice and payment allocation evidence",
                            "canonical RADIUS profile observation",
                            "canonical subscription-change execution state",
                            "canonical provisioning-readiness decision",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="confirmed relocation quote evidence",
                        owner="service_intent.subscription_lifecycle_execution",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked SubscriptionChangeRequest target address, "
                            "qualification, exact fee, currency, and quote fingerprint"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical invoice and payment allocation evidence",
                        owner="financial.payments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "exact issued Invoice, succeeded Payment, active "
                            "PaymentAllocation, and paid invoice state"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical prepaid plan-change decision",
                        owner="financial.prepaid_plan_change",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "one frozen-effective-time plan-change decision with "
                            "required amount, currency, funding, shortfall, "
                            "eligibility, and exact human-review fingerprint"
                        ),
                    ),
                    AuthorityInput(
                        name="catalog-linked target RADIUS profile",
                        owner="service_intent.subscription_lifecycle_execution",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "the single OfferRadiusProfile linked to the confirmed "
                            "target offer"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical RADIUS profile observation",
                        owner="access.radius_state",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "exact subscription-scoped RadiusUser profile and "
                            "post-request last_sync_at watermark"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscription-change execution state",
                        owner="service_intent.subscription_change_execution",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked SubscriptionChangeRequest execution state and "
                            "structural invoice, payment, service-order, work-order, "
                            "and readiness-decision links"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical provisioning-readiness decision",
                        owner="operations.provisioning_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "activated ProvisioningReadinessDecision for the exact "
                            "linked ServiceOrder"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "Each event admission locks one change request. Remote "
                        "execution durably records any changed-price review before "
                        "network I/O; confirmed execution coordinates the RADIUS "
                        "projection and commercial owner, then compensates the "
                        "external projection when commercial finalization fails."
                    ),
                    locking="The exact SubscriptionChangeRequest is locked first.",
                    idempotency=(
                        "Unique structural links and deterministic service/work-order "
                        "keys replay the original outcome."
                    ),
                    retries=(
                        "Unsettled, unverified, stale-price, or billing-blocked "
                        "requests fail closed and remain retryable after their "
                        "authoritative evidence changes."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "service_intent.subscription_change_execution.service_change_not_found",
                        "service_intent.subscription_change_execution.relocation_fee_not_settled",
                        "service_intent.subscription_change_execution.provisioning_verification_missing",
                        "service_intent.subscription_change_execution.remote_radius_profile_ambiguous",
                        "service_intent.subscription_change_execution.remote_access_credential_ambiguous",
                        "service_intent.subscription_change_execution.remote_reprovision_verification_missing",
                        "service_intent.subscription_change_execution.remote_reprovision_compensation_failed",
                        "service_intent.subscription_change_execution.service_change_not_finalizable",
                        "service_intent.subscription_change_execution.reconciliation_head_invalid",
                        "service_intent.subscription_change_execution.reconciliation_head_stale",
                        "service_intent.subscription_change_execution.reconciliation_key_invalid",
                        "service_intent.subscription_change_execution.reconciliation_key_conflict",
                        "service_intent.subscription_change_execution.reconciliation_reason_invalid",
                        "service_intent.subscription_change_execution.reconciliation_not_repairable",
                        "service_intent.subscription_change_execution.invalid_command_context",
                        "service_intent.subscription_change_execution.command_contract_violation",
                        "service_intent.subscription_change_execution.nested_owner_command",
                        "service_intent.subscription_change_execution.active_caller_transaction",
                        "service_intent.subscription_change_execution.nested_transaction_completion",
                    ),
                    mapping_owner="event and service-change adapters",
                    retryable_codes=(
                        "service_intent.subscription_change_execution.relocation_fee_not_settled",
                        "service_intent.subscription_change_execution.provisioning_verification_missing",
                        "service_intent.subscription_change_execution.remote_reprovision_verification_missing",
                        "service_intent.subscription_change_execution.remote_reprovision_compensation_failed",
                    ),
                    fail_closed_on=(
                        "missing or mismatched fee, currency, invoice, allocation, or payment",
                        "missing field-work or provisioning verification",
                        "missing, stale, ambiguous, or mismatched RADIUS profile evidence",
                        "changed or unaffordable upgrade pricing not explicitly reconfirmed",
                        "mismatched service-order scope",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "invoice.created",
                        "service_order.created",
                        "service_order.completed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Events carry exact request, invoice, payment, service-order, "
                        "and readiness identifiers where applicable."
                    ),
                    replay=(
                        "Structural request links and canonical owner records rebuild "
                        "the execution chain without memo or status inference."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    new_owner="service_intent.subscription_change_execution",
                    old_owner=(
                        "unimplemented handoff after awaiting_payment with no "
                        "structural settlement, fulfillment, verification, or "
                        "finalization evidence chain"
                    ),
                    verification=(
                        "Focused tests cover charge creation, exact settlement, "
                        "fulfillment release, RADIUS and field verification gates, "
                        "and replay."
                        " Reviewed operator repair covers interrupted states,"
                        " stale-head rejection, and durable idempotent replay."
                    ),
                    cutover_gate=(
                        "Migrations 401-402 backfill deferred execution state; every "
                        "new priced relocation or remote reprovision receives "
                        "structural evidence."
                    ),
                    fallback_retirement=(
                        "No support-ticket, memo lookup, invoice-status-only, or "
                        "work-order completion shortcut is retained."
                    ),
                ),
                steward="customer service delivery, billing, and network operations",
                design_refs=(
                    "docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md",
                    "docs/designs/PROVISIONING_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_customer_plan_change_prepaid.py",
                    "tests/architecture/test_provisioning_lifecycle_sot.py",
                ),
            ),
        ),
        SOTService(
            name="service_intent.ont",
            module="app.services.network.ont_service_intent",
            owns=("ONT service intent projection",),
        ),
    ),
    entrypoints=(
        "app.services.provisioning_*",
        "app.tasks.tr069.*",
        "app.web.admin.catalog",
        "app.web.admin.provisioning",
    ),
    rule="Catalog policy and subscription services define commercial intent; "
    "network owners project configured intent without a parallel adapter.",
)
