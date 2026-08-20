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
    setting_domains=(
        "catalog",
        "lifecycle",
    ),
    services=(
        SOTService(
            name="service_intent.catalog_policy",
            module="app.services.catalog.policies",
            owns=("catalog policy lookup", "offer policy interpretation"),
        ),
        SOTService(
            name="service_intent.ip_block_catalog",
            module="app.services.catalog.ip_block_choices",
            owns=(
                "active catalog IPv4 block-size choices",
                "subscriber IPv4 block entitlement resolution",
            ),
            depends_on=(
                "service_intent.catalog_policy",
                "service_intent.subscription_lifecycle",
            ),
            notes=(
                "Interprets the existing typed plan markers on active CatalogOffer "
                "records and de-duplicates them by prefix. Manual ONT LAN block "
                "configuration is network-owned and does not consume this resolver."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="active catalog IPv4 block-size choices",
                        role=OwnerRole.RESOLVER,
                        input_names=("active canonical IP-address offers",),
                    ),
                    ConcernContract(
                        name="subscriber IPv4 block entitlement resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "active canonical IP-address offers",
                            "active subscriber subscriptions",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="active canonical IP-address offers",
                        owner="service_intent.catalog_policy",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active CatalogOffer rows with plan_kind=ip_address and "
                            "a supported ip_block_size marker"
                        ),
                    ),
                    AuthorityInput(
                        name="active subscriber subscriptions",
                        owner="service_intent.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active Subscription rows joined to canonical offers",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="Callers own the read-only session and transaction lifetime.",
                    locking="No locks; results describe committed catalog and lifecycle rows.",
                    idempotency="The same committed offers and subscriptions resolve identically.",
                    retries="Read-only resolution is safe to retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner=(
                        "ONT configuration context and service-configuration owner"
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="catalog plan-marker readers without a typed resolver",
                    new_owner="service_intent.ip_block_catalog",
                    verification=("Catalog choice and entitlement resolver tests"),
                    cutover_gate=(
                        "Catalog IP block size readers call this typed resolver"
                    ),
                    fallback_retirement=(
                        "Catalog IP block readers contain no copied prefix parsing."
                    ),
                ),
                steward="commercial and network operations",
                design_refs=(
                    "docs/designs/ONT_UI_SERVICE_CONFIGURATION_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_catalog_services.py",
                    "tests/test_ont_config_ui_contract.py",
                    "tests/test_ont_service_configuration.py",
                ),
            ),
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
            name="service_intent.plan_family_catalogues",
            module="app.services.catalog.plan_family_catalogues",
            owns=(
                "approved plan-family catalogue publication",
                "configured plan-family catalogue vocabulary",
                "current and historical public catalogue resolution",
            ),
            depends_on=(
                "control.settings_spec",
                "auth.permission_gate",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Owns approved marketing PDF versions, not the commercial offer "
                "configuration inside those brochures. Settings and Inbox routes are "
                "adapters; superseded public versions remain readable so previously "
                "sent links are stable, while withdrawn versions fail closed."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="approved plan-family catalogue publication",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "authenticated catalogue publication command",
                            "configured plan-family vocabulary",
                            "validated catalogue PDF storage record",
                        ),
                        canonical_writer="service_intent.plan_family_catalogues",
                    ),
                    ConcernContract(
                        name="configured plan-family catalogue vocabulary",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "authenticated catalogue vocabulary command",
                            "approved catalogue version records",
                        ),
                        canonical_writer="service_intent.plan_family_catalogues",
                    ),
                    ConcernContract(
                        name="current and historical public catalogue resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "approved catalogue version records",
                            "validated catalogue PDF storage record",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated catalogue publication command",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Typed plan family, approved display metadata, PDF bytes, "
                            "staff principal, reason, correlation, and idempotency key."
                        ),
                    ),
                    AuthorityInput(
                        name="configured plan-family vocabulary",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Catalog plan_families setting with built-in defaults.",
                    ),
                    AuthorityInput(
                        name="authenticated catalogue vocabulary command",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Typed complete plan-family vocabulary, staff principal, "
                            "reason, correlation, and idempotency key."
                        ),
                    ),
                    AuthorityInput(
                        name="validated catalogue PDF storage record",
                        owner="service_intent.plan_family_catalogues",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "PDF-only object uploaded before the owner's first database "
                            "operation; immutable StoredFile metadata is staged by the "
                            "owner through the private object-storage participant."
                        ),
                    ),
                    AuthorityInput(
                        name="approved catalogue version records",
                        owner="service_intent.plan_family_catalogues",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "PlanFamilyCatalogue family, version, publication status, "
                            "file identity, staff provenance, and lifecycle timestamps."
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "publish_catalogue enters one root owner transaction; external "
                        "object upload completes before its first database operation, then "
                        "file metadata, prior-version supersession, audit, and event are "
                        "staged before the boundary commits."
                    ),
                    locking=(
                        "Publication locks every existing version for the selected family; "
                        "a partial unique index permits only one published version. "
                        "Vocabulary updates retain every family with a published catalogue."
                    ),
                    idempotency=(
                        "A retry with the current PDF SHA-256 returns that version; "
                        "object keys are content-addressed."
                    ),
                    retries=(
                        "Validation failures do not retry; concurrent publication retries "
                        "from a fresh read after the unique/locking conflict."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "service_intent.plan_family_catalogues.active_caller_transaction",
                        "service_intent.plan_family_catalogues.command_contract_violation",
                        "service_intent.plan_family_catalogues.invalid_command_context",
                        "service_intent.plan_family_catalogues.nested_owner_command",
                        "service_intent.plan_family_catalogues.nested_transaction_completion",
                        "service_intent.plan_family_catalogues.invalid_plan_family",
                        "service_intent.plan_family_catalogues.plan_family_required",
                        "service_intent.plan_family_catalogues.duplicate_plan_family",
                        "service_intent.plan_family_catalogues.published_plan_family_removal",
                        "service_intent.plan_family_catalogues.display_name_required",
                        "service_intent.plan_family_catalogues.display_name_too_long",
                        "service_intent.plan_family_catalogues.file_required",
                        "service_intent.plan_family_catalogues.invalid_file",
                        "service_intent.plan_family_catalogues.catalogue_unavailable",
                        "service_intent.plan_family_catalogues.actor_not_eligible",
                    ),
                    mapping_owner=(
                        "app.web.admin.catalog_settings and app.web.public.catalogues"
                    ),
                    fail_closed_on=(
                        "unconfigured family",
                        "missing or non-PDF payload",
                        "withdrawn or missing object",
                    ),
                ),
                events=EventContract(
                    event_types=("catalog.plan_family_catalogue_published",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "The event carries stable catalogue, family, version, and stored "
                        "file identifiers; consumers read the owner for display metadata."
                    ),
                    replay=(
                        "Replay is informational and never republishes or changes the "
                        "current version."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="service_intent.plan_family_catalogues",
                    verification=(
                        "Focused owner, web adapter, public delivery, migration, and "
                        "architecture tests."
                    ),
                    cutover_gate=(
                        "Inbox exposes only owner-resolved published PDFs and Settings "
                        "publishes only through the owner command."
                    ),
                    fallback_retirement=(
                        "No generic setting stores file paths or independently selects a "
                        "current brochure."
                    ),
                ),
                steward="commercial operations",
                design_refs=(
                    "docs/designs/INBOX_PLAN_CATALOGUE_SHARING.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_plan_family_catalogues.py",
                    "tests/test_admin_inbox_catalogue_sharing.py",
                    "tests/architecture/test_plan_family_catalogue_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="service_intent.subscription_nas_assignment",
            module="app.services.subscription_nas_assignment",
            owns=(
                "subscription provisioning NAS assignment",
                "nonterminal services grouped by NAS",
                "reviewed subscription service-access move",
            ),
            depends_on=(
                "service_intent.catalog_policy",
                "service_intent.subscription_lifecycle",
                "network.nas_inventory",
                "network.ip_assignment_lifecycle",
                "access.radius_projection",
                "access.session_enforcement",
                "sessions.radius_reconciliation",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Owns the reviewed decision to move one exact subscription to a "
                "different NAS and NAS-linked IPv4 pool. The coordinator writes "
                "the subscription NAS binding, invokes required flush-only IPv4 "
                "lifecycle participants in the same transaction, and relies on "
                "the staged served-projection event for RADIUS-first session "
                "convergence. Billing and commercial state are excluded."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="subscription provisioning NAS assignment",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "canonical subscription identity",
                            "canonical NAS inventory",
                        ),
                        canonical_writer="service_intent.subscription_nas_assignment",
                    ),
                    ConcernContract(
                        name="nonterminal services grouped by NAS",
                        role=OwnerRole.RESOLVER,
                        input_names=("canonical subscription NAS binding",),
                    ),
                    ConcernContract(
                        name="reviewed subscription service-access move",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authenticated service-access move command",
                            "canonical subscription identity",
                            "canonical subscription NAS binding",
                            "canonical NAS inventory",
                            "canonical active IPv4 assignment",
                            "serviceable NAS-linked IPv4 pool inventory",
                            "observed RADIUS IPv4 projection",
                            "active RADIUS session observation",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated service-access move command",
                        owner="service_intent.subscription_nas_assignment",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "exact subscription, target NAS, target pool, target "
                            "IPv4, preview SHA-256, actor, reason, and idempotency key"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscription identity",
                        owner="service_intent.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="exact active Subscription and Subscriber identity",
                    ),
                    AuthorityInput(
                        name="canonical subscription NAS binding",
                        owner="service_intent.subscription_nas_assignment",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Subscription.provisioning_nas_device_id",
                    ),
                    AuthorityInput(
                        name="canonical NAS inventory",
                        owner="network.nas_inventory",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active NasDevice identity and lifecycle state",
                    ),
                    AuthorityInput(
                        name="canonical active IPv4 assignment",
                        owner="network.ip_assignment_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="exact active primary IPAssignment for the subscription",
                    ),
                    AuthorityInput(
                        name="serviceable NAS-linked IPv4 pool inventory",
                        owner="network.ip_assignment_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active legacy IpPool.nas_device_id or NAS radius_pool "
                            "configuration, IPv4Address safety, and assignment "
                            "availability evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="observed RADIUS IPv4 projection",
                        owner="access.radius_projection",
                        kind=AuthorityKind.OBSERVATION,
                        source="external RADIUS Framed-IP-Address observation",
                    ),
                    AuthorityInput(
                        name="active RADIUS session observation",
                        owner="sessions.radius_reconciliation",
                        kind=AuthorityKind.OBSERVATION,
                        source="fresh exact-subscription RADIUS session evidence",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "move_subscription_service_access enters one root owner "
                        "transaction; IPv4 participants are required and flush-only"
                    ),
                    locking=(
                        "Lock the subscription, source and target NAS, target pool "
                        "and address, and relevant assignments; IPv4 safety inventory "
                        "uses the lifecycle owner's PostgreSQL locks"
                    ),
                    idempotency=(
                        "A caller-supplied key is bound to the reviewed fingerprint "
                        "and durable audit outcome"
                    ),
                    retries=(
                        "Stale previews fail closed; exact-key replay returns the "
                        "recorded outcome; durable projection delivery retries after commit"
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "service_intent.subscription_nas_assignment.active_caller_transaction",
                        "service_intent.subscription_nas_assignment.command_contract_violation",
                        "service_intent.subscription_nas_assignment.idempotency_conflict",
                        "service_intent.subscription_nas_assignment.incomplete_outcome",
                        "service_intent.subscription_nas_assignment.incomplete_preview",
                        "service_intent.subscription_nas_assignment.invalid_command_context",
                        "service_intent.subscription_nas_assignment.missing_idempotency_key",
                        "service_intent.subscription_nas_assignment.nested_owner_command",
                        "service_intent.subscription_nas_assignment.nested_transaction_completion",
                        "service_intent.subscription_nas_assignment.projection_not_ready",
                        "service_intent.subscription_nas_assignment.stale_preview",
                        "service_intent.subscription_nas_assignment.subscription_not_found",
                        "service_intent.subscription_nas_assignment.unsafe_move",
                    ),
                    mapping_owner="app.web.admin.catalog",
                    retryable_codes=(
                        "service_intent.subscription_nas_assignment.stale_preview",
                    ),
                    fail_closed_on=(
                        "inactive or unchanged target NAS",
                        "target pool not linked to target NAS",
                        "ambiguous current IPv4 ownership",
                        "unaligned RADIUS or session evidence",
                        "stale reviewed fingerprint",
                    ),
                ),
                events=EventContract(
                    event_types=("ip_assignment.served_projection_repaired",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "The existing IPv4 projection event carries exact subscription, "
                        "assignment, previous and desired address evidence; the committed "
                        "subscription already contains the new NAS binding."
                    ),
                    replay=(
                        "Replay revalidates the committed served IPv4 before requesting "
                        "RADIUS projection and old-address session disconnection."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    new_owner="service_intent.subscription_nas_assignment",
                    old_owner=(
                        "generic admin subscription edit and web provisioning "
                        "migration NAS/IP writers"
                    ),
                    verification=(
                        "Focused owner, route, billing-isolation, stale-preview, "
                        "rollback, concurrency, and architecture tests"
                    ),
                    cutover_gate=(
                        "Individual UI calls only this coordinator and legacy bulk "
                        "NAS/IP targets are refused until they submit exact commands"
                    ),
                    fallback_retirement=(
                        "Generic edits cannot mutate NAS/IP and no migration helper "
                        "may repoint IPv4Address.pool_id"
                    ),
                ),
                steward="network operations",
                design_refs=(
                    "docs/designs/SERVICE_ACCESS_MOVE_SOT.md",
                    "docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_subscription_nas_assignment.py",
                    "tests/test_web_catalog_subscriptions.py",
                    "tests/architecture/test_subscription_service_access_boundary.py",
                ),
            ),
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
