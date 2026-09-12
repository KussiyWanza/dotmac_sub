"""provisioning_operations SOT declarations: core."""

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
        name="operations.provisioning_context",
        module="app.services.provisioning_context",
        owns=("subscriber provisioning context", "ONT/CPE service link"),
        depends_on=("customer.identity_scope", "network.access_path"),
    ),
    SOTService(
        name="operations.provisioning_workflow",
        module="app.services.provisioning_managers",
        owns=("provisioning workflow execution", "provisioning step state"),
        depends_on=("operations.provisioning_context",),
    ),
    SOTService(
        name="operations.service_order_lifecycle",
        module="app.services.service_order_lifecycle",
        owns=(
            "service-order status transition and recovery lifecycle",
            "verified-implementation provisioning release",
            "successful-provisioning activation consequence",
        ),
        depends_on=(
            "operations.provisioning_workflow",
            "operations.project_lifecycle",
            "access.subscription_lifecycle",
            "events.dispatcher",
        ),
        notes=(
            "Routes, managers, billing callbacks, and event handlers do "
            "not write ServiceOrder status directly. Domain errors are "
            "transport-neutral and HTTP translation stays in adapters."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("service-order status transition and recovery lifecycle"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical service-order state",
                        "service-order transition protocol",
                        "recorded administrative recovery evidence",
                    ),
                    canonical_writer="operations.service_order_lifecycle",
                ),
                ConcernContract(
                    name="verified-implementation provisioning release",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical service-order state",
                        "verified implementation evidence",
                        "canonical project lifecycle state",
                    ),
                    canonical_writer="operations.service_order_lifecycle",
                ),
                ConcernContract(
                    name="successful-provisioning activation consequence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical service-order state",
                        "canonical provisioning result",
                        "canonical subscription lifecycle state",
                    ),
                    canonical_writer="operations.service_order_lifecycle",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical service-order state",
                    owner="operations.service_order_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked ServiceOrder identity, status, structural sales, "
                        "project, installation, and subscription references"
                    ),
                ),
                AuthorityInput(
                    name="service-order transition protocol",
                    owner="operations.service_order_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "versioned ServiceOrderStatus graph and implementation "
                        "readiness gates"
                    ),
                ),
                AuthorityInput(
                    name="recorded administrative recovery evidence",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "exact subscriber recovery snapshot, authenticated actor, "
                        "and named recovery reason"
                    ),
                ),
                AuthorityInput(
                    name="verified implementation evidence",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "append-only vendor verification event id and verified "
                        "InstallationProject status"
                    ),
                ),
                AuthorityInput(
                    name="canonical project lifecycle state",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked structurally linked completed Project",
                ),
                AuthorityInput(
                    name="canonical provisioning result",
                    owner="operations.provisioning_workflow",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "durable successful or failed ProvisioningRun outcome for "
                        "the exact ServiceOrder"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription lifecycle state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="the exact linked pending or active Subscription",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Public transitions own one transaction; nested release and "
                    "provisioning-result calls flush into their event coordinator's "
                    "transaction. Recovery tooling calls the same locked writer."
                ),
                locking="Every existing ServiceOrder is selected FOR UPDATE.",
                idempotency=(
                    "Equivalent status and identical verification or recovery "
                    "evidence are no-ops; conflicting evidence fails closed."
                ),
                retries=(
                    "Outbox consumers and recovery commands replay the exact "
                    "identifier and evidence through this owner."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.service_order_lifecycle.active_caller_transaction",
                    "operations.service_order_lifecycle.command_contract_violation",
                    "operations.service_order_lifecycle.invalid_command_context",
                    "operations.service_order_lifecycle.nested_owner_command",
                    "operations.service_order_lifecycle.nested_transaction_completion",
                    "service_order_not_found",
                    "invalid_status",
                    "invalid_transition",
                    "implementation_not_ready",
                    "verification_evidence_conflict",
                    "provisioning_result_required",
                    "subscription_not_activatable",
                    "actor_required",
                    "reason_required",
                ),
                mapping_owner="HTTP, event, and administrative recovery adapters",
                fail_closed_on=(
                    "missing structural implementation evidence",
                    "invalid transition",
                    "conflicting verification evidence",
                    "non-success provisioning activation",
                ),
            ),
            events=EventContract(
                event_types=(
                    "service_order.released",
                    "service_order.assigned",
                    "service_order.completed",
                    "service_order.recovered",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact service, subscription, project, "
                    "status, actor, reason, and verification identifiers."
                ),
                replay=(
                    "ServiceOrder state and EventStore evidence reconstruct each "
                    "outcome; duplicate consequences re-enter the locked owner."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "provisioning managers, subscription event handlers, and "
                    "system-recovery tooling with parallel status assignments"
                ),
                new_owner="operations.service_order_lifecycle",
                verification=(
                    "Transition, implementation, provisioning, activation, "
                    "recovery, event, and sole-writer architecture tests."
                ),
                cutover_gate=(
                    "All runtime and recovery status mutations call the named "
                    "owner; raw writer detection is green."
                ),
                fallback_retirement=(
                    "Direct manager, event-handler, and recovery status writes "
                    "are removed."
                ),
            ),
            steward="service delivery",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_sales_to_service_lifecycle.py",
                "tests/test_provisioning_services.py",
                "tests/architecture/test_service_order_status_writers.py",
                "tests/architecture/test_service_http_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.provisioning_lifecycle",
        module="app.services.provisioning_lifecycle",
        owns=(
            "provisioning readiness and activation request decisions",
            "service-order activation confirmation",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "events.dispatcher",
            "operations.project_lifecycle",
            "operations.provisioning_context",
            "operations.provisioning_workflow",
            "operations.service_order_lifecycle",
            "operations.work_orders",
        ),
        notes=(
            "Provisioning runs, project tasks, field work, and active IP "
            "assignments are facts. This coordinator persists the readiness "
            "decision and asks the service-order lifecycle owner to perform "
            "terminal status transitions. Connectivity systems remain "
            "projection transports."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("provisioning readiness and activation request decisions"),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical service-order state",
                        "canonical provisioning-run outcome",
                        "native project activation scope",
                        "native field-work completion evidence",
                        "active IP-assignment fact",
                        "canonical subscription lifecycle state",
                    ),
                ),
                ConcernContract(
                    name="service-order activation confirmation",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical provisioning-readiness decision",
                        "canonical subscription lifecycle state",
                        "connectivity projection success observation",
                        "service-order transition protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical service-order state",
                    owner="operations.service_order_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked ServiceOrder identity, scope, and status",
                ),
                AuthorityInput(
                    name="canonical provisioning-run outcome",
                    owner="operations.provisioning_workflow",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="terminal ProvisioningRun for the exact ServiceOrder",
                ),
                AuthorityInput(
                    name="canonical provisioning-readiness decision",
                    owner="operations.provisioning_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "append-only ProvisioningReadinessDecision and "
                        "ProvisioningReadinessCheck rows"
                    ),
                ),
                AuthorityInput(
                    name="native project activation scope",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "ServiceOrder.project_id, activation_project_task_id, "
                        "and completed ProjectTask state"
                    ),
                ),
                AuthorityInput(
                    name="native field-work completion evidence",
                    owner="operations.work_orders",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "zero or many native WorkOrders linked through "
                        "WorkOrder.project_task_id"
                    ),
                ),
                AuthorityInput(
                    name="active IP-assignment fact",
                    owner="operations.provisioning_context",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "active IPAssignment linked to the exact subscription; "
                        "read only and not a connectivity authority cutover"
                    ),
                ),
                AuthorityInput(
                    name="canonical subscription lifecycle state",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Subscription pending or active lifecycle state",
                ),
                AuthorityInput(
                    name="connectivity projection success observation",
                    owner="operations.provisioning_context",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "successful completion of the existing IP, RADIUS, "
                        "and NAS activation event handler"
                    ),
                ),
                AuthorityInput(
                    name="service-order transition protocol",
                    owner="operations.service_order_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "canonical ServiceOrderStatus graph and implementation "
                        "readiness guard"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Each public command enters execute_owner_command once on "
                    "a transaction-free adapter session and atomically records "
                    "one decision, owner transition request, and outbox event."
                ),
                locking=(
                    "Readiness locks the exact service order and provisioning "
                    "run; confirmation locks the exact service order."
                ),
                idempotency=(
                    "CommandContext.command_id is unique on append-only decision "
                    "evidence and equivalent event retries replay it."
                ),
                retries=(
                    "Event delivery retries use the original event id; blocked "
                    "facts require a new evaluation command after source repair."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.provisioning_lifecycle.service_order_not_found",
                    "operations.provisioning_lifecycle.run_scope_mismatch",
                    "operations.provisioning_lifecycle.run_not_terminal",
                    "operations.provisioning_lifecycle.invalid_order_state",
                    "operations.provisioning_lifecycle.subscription_required",
                    "operations.provisioning_lifecycle.subscription_scope_mismatch",
                    "operations.provisioning_lifecycle.subscription_not_activatable",
                    "operations.provisioning_lifecycle.activation_not_requested",
                    "operations.provisioning_lifecycle.activation_projection_incomplete",
                    "operations.provisioning_lifecycle.invalid_command_context",
                    "operations.provisioning_lifecycle.command_contract_violation",
                    "operations.provisioning_lifecycle.nested_owner_command",
                    "operations.provisioning_lifecycle.active_caller_transaction",
                    "operations.provisioning_lifecycle.nested_transaction_completion",
                ),
                mapping_owner="provisioning event and customer portal adapters",
                retryable_codes=(
                    "operations.provisioning_lifecycle.activation_projection_incomplete",
                ),
                fail_closed_on=(
                    "ambiguous or missing native project scope",
                    "incomplete project task or field work",
                    "missing active IP assignment",
                    "mismatched run, subscription, or service-order scope",
                ),
            ),
            events=EventContract(
                event_types=(
                    "service_order.activation_requested",
                    "subscription.activated",
                    "service_order.completed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "The request, completion, and subscription events carry the "
                    "exact service_order_id and readiness_decision_id."
                ),
                replay=(
                    "Append-only decisions plus current project, field, IP, and "
                    "subscription facts rebuild customer readiness views."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="customer provisioning readiness view",
                    input_names=(
                        "canonical provisioning-readiness decision",
                        "native project activation scope",
                    ),
                    writer="customer.experience_lifecycle",
                    freshness="read through on each customer project request",
                    stale_behavior="show the latest persisted decision and time",
                    drift_signal=(
                        "service order project/task scope does not resolve in the "
                        "customer project graph"
                    ),
                    rebuild_operation=(
                        "re-read append-only decisions and native project links"
                    ),
                    repair_owner="operations.provisioning_lifecycle",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner="operations.provisioning_lifecycle",
                old_owner=(
                    "provisioning event handlers deriving readiness and writing "
                    "terminal service-order state directly"
                ),
                verification=(
                    "Architecture tests enforce one status writer and behavior "
                    "tests cover blocked, requested, and confirmed states."
                ),
                cutover_gate=(
                    "Migration 390 backfills only unambiguous project/task links; "
                    "all unresolved new installs fail closed."
                ),
                fallback_retirement=(
                    "Direct run-completion and subscription-wide service-order "
                    "activation paths are removed."
                ),
            ),
            steward="service delivery and network operations",
            design_refs=(
                "docs/designs/PROVISIONING_LIFECYCLE_SOT.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_provisioning_lifecycle.py",
                "tests/architecture/test_provisioning_lifecycle_sot.py",
            ),
        ),
    ),
    SOTService(
        name="operations.work_order_status",
        module="app.services.field.work_order_status",
        owns=(
            "persisted work-order status vocabulary",
            "open, assignable, and terminal work-order status sets",
        ),
    ),
    SOTService(
        name="operations.work_order_commands",
        module="app.services.work_order_commands",
        owns=(
            "native work-order creation and header commands",
            "native work-order project binding",
            "native work-order project-task binding",
            "work-order as-built evidence requirement",
            "work-order assignment decisions and projection",
            "work-order assignment-queue transitions",
            "work-order staff/team tag notification consequence",
        ),
        depends_on=(
            "customer.identity_scope",
            "communications.staff_notifications",
            "operations.work_order_status",
            "observability.audit_log",
        ),
        notes=(
            "Dispatch API/web and field-manager adapters authorize and "
            "delegate here. The owner validates a read-only assignment "
            "preview, locks the work order, changes queue and assignee "
            "projection atomically, records exact actor audit evidence, "
            "and treats equivalent retries as replays. Retained CRM ids are "
            "provenance only; field execution statuses remain "
            "owned by operations.field_completion. Native project-binding "
            "and evidence-policy rejections are transport-neutral "
            "WorkOrderCommandError values mapped only by the app boundary. "
            "Staff/team tag tokens stage in-app notifications through "
            "communications.staff_notifications; ordinary descriptive tags "
            "remain local work-order metadata."
        ),
    ),
    SOTService(
        name="operations.work_orders",
        module="app.services.work_order_views",
        owns=("work-order read models", "customer work-order linkage"),
        depends_on=(
            "customer.identity_scope",
            "operations.work_order_status",
        ),
        notes=(
            "This registration owns reads only. Native mutations delegate "
            "to operations.work_order_commands; retained CRM identifiers "
            "are provenance and never become native command authority."
        ),
    ),
    SOTService(
        name="operations.field_notes",
        module="app.services.field.note_commands",
        owns=(
            "native field work-order note creation",
            "authorized staff field-note related-context projection",
        ),
        depends_on=(
            "auth.permission_gate",
            "events.owner_outputs",
            "operations.work_order_commands",
            "operations.work_orders",
        ),
        notes=(
            "The typed owner creates technician-scoped native notes and stages "
            "their durable output. A stable mobile client reference is the "
            "idempotency key; the field API and offline outbox are adapters. "
            "Authorized staff pages compose the same canonical rows through "
            "native work-order, project-task, and origin-ticket bindings."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="native field work-order note creation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "authenticated technician identity",
                        "assigned work-order state",
                        "field-note client request identity",
                    ),
                    canonical_writer="operations.field_notes",
                ),
                ConcernContract(
                    name="authorized staff field-note related-context projection",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical field work-order note state",
                        "native work-order relationship bindings",
                        "staff field-note authorization evidence",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated technician identity",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "Authenticated SystemUser resolved to one active "
                        "TechnicianProfile"
                    ),
                ),
                AuthorityInput(
                    name="assigned work-order state",
                    owner="operations.work_orders",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Active WorkOrder and current technician or vendor "
                        "assignment scope"
                    ),
                ),
                AuthorityInput(
                    name="field-note client request identity",
                    owner="operations.field_notes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Stable client_ref plus the normalized work-order, body, "
                        "visibility, and attachment fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="canonical field work-order note state",
                    owner="operations.field_notes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "FieldWorkOrderNote rows and their active field attachment links"
                    ),
                ),
                AuthorityInput(
                    name="native work-order relationship bindings",
                    owner="operations.work_order_commands",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "WorkOrder public identity, project_task_id, and "
                        "origin_ticket_id foreign keys"
                    ),
                ),
                AuthorityInput(
                    name="staff field-note authorization evidence",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "Exact admin work-order scope and operations:dispatch:read "
                        "permission evaluated by the web adapter"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "The note, attachment links, provenance fingerprint, and "
                    "owner output commit in one owner-managed transaction."
                ),
                locking=(
                    "Lock the authenticated SystemUser before replay lookup, then "
                    "lock the assigned WorkOrder before note creation."
                ),
                idempotency=(
                    "The unique author_system_user_id/client_ref pair replays only "
                    "when the normalized command fingerprint is identical."
                ),
                retries=(
                    "Equivalent offline retries return the original note; a reused "
                    "key with changed inputs fails closed as a conflict."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.field_notes.attachment_forbidden",
                    "operations.field_notes.attachment_not_found",
                    "operations.field_notes.idempotency_conflict",
                    "operations.field_notes.invalid_request",
                    "operations.field_notes.requester_not_found",
                    "operations.field_notes.work_order_not_found",
                    *owner_command_boundary_error_codes("operations.field_notes"),
                ),
                mapping_owner="field work-order note API and admin web adapters",
                retryable_codes=(),
                fail_closed_on=(
                    "unknown or inactive technician identity",
                    "unassigned or inaccessible work order",
                    "foreign, missing, or already-linked attachment",
                    "missing or unrelated staff work-order read scope",
                    "client-reference fingerprint conflict",
                ),
            ),
            events=EventContract(
                event_types=("field_work_order_note.created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and excludes the private note body; it "
                    "carries stable note, work-order, actor, and command evidence."
                ),
                replay=(
                    "The canonical note row, attachment links, and command "
                    "fingerprint rebuild the creation output."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                old_owner="app.services.field.notes.FieldNotes.create",
                new_owner="operations.field_notes",
                verification=(
                    "Owner-command, exact replay, conflict, mobile queued/failed "
                    "projection, and predecessor-to-head migration tests pass."
                ),
                cutover_gate=(
                    "Revision 590 is applied before a mobile build that sends "
                    "field-note client references is released."
                ),
                fallback_retirement=(
                    "The adapter-generated reference remains only for older mobile "
                    "clients and can be made required after their support window."
                ),
            ),
            steward="field operations",
            design_refs=(
                "docs/designs/FIELD_WORK_ORDER_NOTES.md",
                "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
            ),
            test_refs=(
                "tests/test_field_notes.py",
                "tests/test_field_note_staff_projection.py",
                "tests/architecture/test_field_note_delivery_contract.py",
                "tests/integration/test_field_note_delivery_migration.py",
                "field_mobile/test/execution_test.dart",
                "field_mobile/test/jobs_screens_test.dart",
            ),
        ),
    ),
    SOTService(
        name="operations.field_completion",
        module="app.services.field.transitions",
        owns=(
            "field job completion eligibility",
            "field completion evidence requirements",
            "field job completion transitions",
        ),
        depends_on=(
            "operations.work_orders",
            "operations.work_order_status",
            "control.domain_settings",
        ),
        notes=(
            "Authenticated field job detail projects the same completion "
            "requirements consumed by transition validation. Field clients "
            "do not reconstruct this policy."
        ),
    ),
    SOTService(
        name="operations.material_catalog",
        module="app.services.field.material_catalog",
        owns=(
            "ERP material catalogue and warehouse projection",
            "field material request eligibility",
        ),
        notes="ERP owns catalogue facts; Sub owns only field-request eligibility.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="ERP material catalogue and warehouse projection",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=("ERP inventory catalogue observation",),
                    canonical_writer="operations.material_catalog",
                ),
                ConcernContract(
                    name="field material request eligibility",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("ERP inventory catalogue observation",),
                    canonical_writer="operations.material_catalog",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="ERP inventory catalogue observation",
                    owner="external:dotmac_erp",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source="ERP item and warehouse APIs via the inventory capability",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary="One catalogue observation or eligibility decision per owner transaction.",
                locking="Projected rows lock by stable ERP source identity.",
                idempotency="Repeated complete observations converge by ERP identity.",
                retries="Transport retries remain outside the owner transaction.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.material_catalog.invalid_identity",
                    "operations.material_catalog.duplicate_item",
                    "operations.material_catalog.duplicate_warehouse",
                    "operations.material_catalog.suspicious_shrink",
                    "operations.material_catalog.item_not_found",
                    "operations.material_catalog.inactive_item",
                    "operations.material_catalog.naive_observation",
                    *owner_command_boundary_error_codes("operations.material_catalog"),
                ),
                mapping_owner="material catalogue web and scheduler adapters",
                retryable_codes=(),
                fail_closed_on=("invalid or suspicious ERP observations",),
            ),
            events=EventContract(
                event_types=(
                    "field_material_catalog.projected",
                    "field_material_catalog.eligibility_updated",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Version 1 is additive and preserves ERP source identities.",
                replay="The latest complete ERP observation plus Sub eligibility decisions rebuild the projection.",
            ),
            projections=(
                ProjectionContract(
                    name="ERP field-material catalogue projection",
                    input_names=("ERP inventory catalogue observation",),
                    writer="operations.material_catalog",
                    freshness="Refreshed every 24 hours or by an explicit operator import.",
                    stale_behavior="Retain the last good catalogue and display its observation time.",
                    drift_signal="The catalogue observation is stale or a complete scan reports suspicious shrinkage.",
                    rebuild_operation="Run a complete ERP item and warehouse catalogue import.",
                    repair_owner="operations.material_catalog",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                old_owner="legacy CRM item projection",
                new_owner="operations.material_catalog",
                verification="ERP identity, deactivation guard, and eligibility owner tests.",
                cutover_gate="Validated ERP inventory capability and initial complete scan.",
                fallback_retirement="Retire CRM item imports after ERP scan verification.",
            ),
            steward="field operations",
            design_refs=("docs/designs/MATERIALS_VENDOR_ERP_CHAIN.md",),
            test_refs=("tests/test_admin_material_requests.py",),
        ),
    ),
    SOTService(
        name="operations.expense_categories",
        module="app.services.field.expense_categories",
        owns=("ERP expense category query",),
        depends_on=("integration.installations", "integration.runtime"),
        notes=(
            "ERP owns expense-category facts. This resolver returns a typed live "
            "observation and keeps unavailable distinct from an authoritative "
            "empty category list."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="ERP expense category query",
                    role=OwnerRole.RESOLVER,
                    input_names=("ERP expense category observation",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="ERP expense category observation",
                    owner="external:dotmac_erp",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "Authenticated GET /api/v1/sync/sub/expense-categories "
                        "through the version-pinned ERP inventory capability"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "The API adapter owns session lifecycle; the resolver performs "
                    "no business write or transaction completion."
                ),
                locking="No business rows are locked for this live ERP read.",
                idempotency="Repeated reads return the current ERP observation.",
                retries="The connector runtime classifies bounded transport retries.",
            ),
            errors=ErrorContract(
                domain_codes=("operations.expense_categories.erp_unavailable",),
                mapping_owner="field expense category API adapter",
                retryable_codes=("operations.expense_categories.erp_unavailable",),
                fail_closed_on=(
                    "missing capability binding",
                    "retryable or rejected ERP response",
                    "malformed ERP category identity or amount",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                old_owner=None,
                new_owner="operations.expense_categories",
                verification=(
                    "Typed normalization, authoritative-empty, and unavailable "
                    "response tests pass."
                ),
                cutover_gate="The ERP inventory capability binding is enabled.",
                fallback_retirement="No empty-list fallback is permitted.",
            ),
            steward="field operations and finance",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=("tests/test_field_expense_categories.py",),
        ),
    ),
    SOTService(
        name="operations.expense_requests",
        module="app.services.field.expense_requests",
        owns=(
            "field expense request submission",
            "field expense request cancellation",
            "field expense ERP form context",
            "expense receipt staging for submitted claims",
            "field expense approval and ERP delivery staging",
            "dead expense delivery recovery",
            "field expense payment initiation and ERP delivery staging",
            "field expense vendor picker",
            "requester-owned field expense history",
            "field expense requester-identity repair",
        ),
        depends_on=(
            "auth.permission_gate",
            "integration.backoffice_adapter",
            "operations.expense_categories",
            "operations.work_orders",
        ),
        notes=(
            "One typed command creates and submits an expense request atomically. "
            "Field clients retain assigned-technician scope; the staff web adapter "
            "supplies exact RBAC-authorized work-order evidence and derives the actor "
            "from the authenticated session. Every submission requires current "
            "technician-assignment evidence. Receipt metadata is staged flush-only "
            "inside the same command. Submission never stages ERP delivery. Manager "
            "approval is the sole ERP release point and atomically stages one event "
            "that creates or reuses a draft, uploads missing receipts, then approves. "
            "Rejection remains local. Explicit previewed recovery appends a linked "
            "replacement without changing the original dead event. A "
            "separately authorized payment command stages reimbursement initiation, "
            "while ERP remains the payment and settlement authority. The client reference and "
            "normalized fingerprint make retries safe. The vendor picker remains "
            "read-only and projects active vendor labels for expense entry. ERP "
            "owns eligible approvers, bank identity, account verification, and the "
            "opaque claim-bound destination token. Sub stores no raw account number; "
            "it owns the selected local approver link and masked expense snapshot. "
            "ERP delivery failures retain only typed allowlisted diagnostic codes, "
            "HTTP status, and request identifiers alongside partial-delivery progress."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="field expense request submission",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical service work-order state",
                        "authenticated requester and work-order access evidence",
                        "ERP-owned expense category rules",
                        "ERP-owned eligible approver observation",
                        "ERP-verified payment destination token",
                        "validated receipt content",
                    ),
                    canonical_writer="operations.expense_requests",
                ),
                ConcernContract(
                    name="field expense request cancellation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical field expense request state",
                        "authenticated requester and work-order access evidence",
                    ),
                    canonical_writer="operations.expense_requests",
                ),
                ConcernContract(
                    name="field expense ERP form context",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "authenticated requester and work-order access evidence",
                        "ERP-owned eligible approver observation",
                        "ERP-verified payment destination token",
                    ),
                ),
                ConcernContract(
                    name="expense receipt staging for submitted claims",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical service work-order state",
                        "authenticated requester and work-order access evidence",
                        "validated receipt content",
                    ),
                    canonical_writer="operations.expense_requests",
                ),
                ConcernContract(
                    name="field expense approval and ERP delivery staging",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical submitted expense request",
                        "selected expense approver",
                        "ERP-owned expense category rules",
                        "validated receipt content",
                        "expense ERP delivery cutover control",
                    ),
                    canonical_writer="operations.expense_requests",
                ),
                ConcernContract(
                    name="dead expense delivery recovery",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical approved expense request",
                        "ERP-owned expense category rules",
                        "validated receipt content",
                        "expense ERP delivery cutover control",
                    ),
                    canonical_writer="operations.expense_requests",
                ),
                ConcernContract(
                    name="field expense payment initiation and ERP delivery staging",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical approved expense request",
                        "expense ERP delivery cutover control",
                    ),
                    canonical_writer="operations.expense_requests",
                ),
                ConcernContract(
                    name="field expense vendor picker",
                    role=OwnerRole.RESOLVER,
                    input_names=("legacy active vendor identity observation",),
                ),
                ConcernContract(
                    name="requester-owned field expense history",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical field expense request state",
                        "authenticated requester and work-order access evidence",
                    ),
                ),
                ConcernContract(
                    name="field expense requester-identity repair",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "canonical field expense request state",
                        "authenticated requester and work-order access evidence",
                    ),
                    canonical_writer="operations.expense_requests",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical service work-order state",
                    owner="operations.work_orders",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Active WorkOrder identity and current technician-assignment "
                        "evidence; assigned-technician scope for field clients or exact "
                        "route-authorized work-order identity for staff"
                    ),
                ),
                AuthorityInput(
                    name="authenticated requester and work-order access evidence",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "Authenticated system-user identity and global, reseller, or "
                        "region operations:dispatch:read access resolved for the exact "
                        "work order"
                    ),
                ),
                AuthorityInput(
                    name="ERP-owned expense category rules",
                    owner="operations.expense_categories",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "Live ERP category identity, receipt requirement, and "
                        "per-claim maximum observed before submission"
                    ),
                ),
                AuthorityInput(
                    name="validated receipt content",
                    owner="operations.expense_requests",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Validated private receipt bytes and content-addressed stored-file "
                        "metadata staged in the expense owner transaction"
                    ),
                ),
                AuthorityInput(
                    name="ERP-owned eligible approver observation",
                    owner="external:dotmac_erp",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "Active ERP employees with an expense-approval permission, "
                        "matched to one active Sub SystemUser by normalized email"
                    ),
                ),
                AuthorityInput(
                    name="ERP-verified payment destination token",
                    owner="external:dotmac_erp",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "Short-lived claim-bound opaque token plus bank name, account "
                        "last four digits, verified beneficiary, and verification time; "
                        "the raw account number never crosses into Sub persistence"
                    ),
                ),
                AuthorityInput(
                    name="selected expense approver",
                    owner="operations.expense_requests",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "ERP employee identity and exact active Sub SystemUser/email "
                        "selected when the expense is submitted"
                    ),
                ),
                AuthorityInput(
                    name="canonical submitted expense request",
                    owner="operations.expense_requests",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Locked active FieldExpenseRequest, item rows, requester "
                        "identity, and submitted lifecycle evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical approved expense request",
                    owner="operations.expense_requests",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Locked active FieldExpenseRequest with manager approval "
                        "evidence and no active or unresolved payment command"
                    ),
                ),
                AuthorityInput(
                    name="canonical field expense request state",
                    owner="operations.expense_requests",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Active FieldExpenseRequest, item rows, and requester "
                        "technician, Person Party, and SystemUser identity evidence"
                    ),
                ),
                AuthorityInput(
                    name="expense ERP delivery cutover control",
                    owner="integration.backoffice_adapter",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "expense_claim single-writer ownership and the durable "
                        "provider-neutral ERP outbox contract"
                    ),
                ),
                AuthorityInput(
                    name="legacy active vendor identity observation",
                    owner="external:legacy_vendor_registry",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "Active native Vendor rows carrying CRM-UUID-keyed vendor "
                        "identity and display names"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Create, submit, optional receipt metadata, and work-order activity "
                    "marking complete in one owner transaction without ERP staging. "
                    "Receipt storage is a flush-only participant. Manager approval "
                    "requires the selected approver, locks the verified payment "
                    "snapshot, and stages the sole ERP release intent in the "
                    "same transaction. Rejection and cancellation remain local; payment "
                    "stages a later ordered intent. Previewed recovery locks and "
                    "revalidates before appending linked replacement evidence. "
                    "The vendor picker performs a "
                    "read-only session-scoped query. Requester-history reads are "
                    "side-effect free; ERP form-context and destination-verification "
                    "queries are also side-effect free. "
                    "Revision 584 performs the bounded, idempotent "
                    "identity repair during schema migration."
                ),
                locking=(
                    "Submission locks the scoped active work order; approval, "
                    "rejection, and payment initiation lock the active expense "
                    "request before their transitions."
                ),
                idempotency=(
                    "A unique client reference replays only when the normalized "
                    "submission fingerprint is identical. An already-approved "
                    "request returns its current delivery outcome; the first "
                    "transition records its command id. Approval and receipt upload "
                    "have stable versioned delivery keys; each payment command id "
                    "has one stable key and one ERP payment intent."
                ),
                retries=(
                    "Identical submission retries return the committed request. "
                    "A staging failure rolls back the manager action for safe command "
                    "retry. Ordered ERP transport retries from the durable outbox; an "
                    "indeterminate provider outcome blocks another payment command."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.expense_requests.invalid_request",
                    "operations.expense_requests.approver_invalid",
                    "operations.expense_requests.approver_mismatch",
                    "operations.expense_requests.destination_expired",
                    "operations.expense_requests.destination_invalid",
                    "operations.expense_requests.destination_unavailable",
                    "operations.expense_requests.form_context_unavailable",
                    "operations.expense_requests.form_context_required",
                    "operations.expense_requests.idempotency_conflict",
                    "operations.expense_requests.erp_delivery_not_configured",
                    "operations.expense_requests.erp_staging_failed",
                    "operations.expense_requests.incomplete_approval",
                    "operations.expense_requests.invalid_transition",
                    "operations.expense_requests.manager_email_required",
                    "operations.expense_requests.payment_already_active",
                    "operations.expense_requests.requester_not_found",
                    "operations.expense_requests.request_not_found",
                    "operations.expense_requests.receipt_required",
                    "operations.expense_requests.receipt_url_invalid",
                    "operations.expense_requests.recovery_not_available",
                    "operations.expense_requests.recovery_state_invalid",
                    "operations.expense_requests.recovery_erp_unavailable",
                    "operations.expense_requests.recovery_ambiguous",
                    "operations.expense_requests.recovery_preview_stale",
                    "operations.expense_requests.work_order_not_found",
                    "operations.expense_requests.work_order_unauthorized",
                    "operations.expense_requests.work_order_unassigned",
                    *owner_command_boundary_error_codes("operations.expense_requests"),
                ),
                mapping_owner="field expense request API adapter",
                retryable_codes=(
                    "operations.expense_requests.destination_unavailable",
                    "operations.expense_requests.erp_delivery_not_configured",
                    "operations.expense_requests.erp_staging_failed",
                    "operations.expense_requests.form_context_unavailable",
                ),
                fail_closed_on=(
                    "unknown requester or work order",
                    "missing exact staff work-order authorization evidence",
                    "work order without a current technician assignment",
                    "unavailable or invalid ERP category rules",
                    "unavailable or mismatched ERP approver identity",
                    "missing, expired, or invalid ERP payment-destination token",
                    "invalid receipt evidence",
                    "ambiguous ERP state during dead-event recovery",
                    "client-reference fingerprint conflict",
                    "expense-flow ownership not assigned to Sub",
                    "ERP outbox staging failure after expense-flow cutover",
                ),
            ),
            events=EventContract(
                event_types=("field_expense_request.submitted",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and identifies the expense request, "
                    "work order, requester, client reference, and submission time."
                ),
                replay=(
                    "The canonical expense request, item rows, command fingerprint, "
                    "approval command evidence, and ERP outbox evidence rebuild "
                    "submission and delivery consequences."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="field expense ERP payment projection",
                    input_names=(
                        "canonical approved expense request",
                        "expense ERP delivery cutover control",
                    ),
                    writer="operations.expense_requests",
                    freshness=(
                        "Updated after each accepted payment command response and by "
                        "scheduled ERP claim-status reconciliation."
                    ),
                    stale_behavior=(
                        "The last observed payment status remains visible with its "
                        "timestamp; unknown outcomes remain blocking, never failed."
                    ),
                    drift_signal=(
                        "ERP reports a payment intent or paid claim state that differs "
                        "from the request's erp_payment metadata projection."
                    ),
                    rebuild_operation=(
                        "Poll the ERP expense-claim status endpoint by the stable Sub "
                        "request id and reapply the typed claim/payment response."
                    ),
                    repair_owner="operations.expense_requests",
                ),
                ProjectionContract(
                    name="field expense requester identity bridge",
                    input_names=(
                        "canonical field expense request state",
                        "authenticated requester and work-order access evidence",
                    ),
                    writer="operations.expense_requests",
                    freshness=(
                        "Written with each native submission and repaired once by "
                        "revision 587 for exact legacy identity matches."
                    ),
                    stale_behavior=(
                        "Ambiguous legacy rows remain hidden from requester history "
                        "until their identity evidence is resolved."
                    ),
                    drift_signal=(
                        "An expense request lacks requested_by_system_user_id even "
                        "though its technician or Person identity has one exact "
                        "SystemUser match."
                    ),
                    rebuild_operation=(
                        "Apply the idempotent requester-identity repair from Alembic "
                        "revision 587 to the bounded drift cohort."
                    ),
                    repair_owner="operations.expense_requests",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                old_owner="separate field expense draft creation and submit calls",
                new_owner="operations.expense_requests",
                verification=(
                    "Work-order-bound submission, approval-only release, private receipt "
                    "delivery, partial-failure replay, explicit dead-event recovery, "
                    "conflict, requester-history, and identity-repair tests pass."
                ),
                cutover_gate=(
                    "Mobile clients use the typed submit, decision, and payment "
                    "endpoints, ERP delivery "
                    "capabilities are enabled, and expense_claim ownership moves to "
                    "Sub before approving a new production expense. Historical expense "
                    "events are not delivered or backfilled; exact requester-"
                    "identity repair remains a separate local migration."
                ),
                fallback_retirement=(
                    "Generic create and create-then-submit writers return 410; all new "
                    "expense creation uses the work-order-bound owner command."
                ),
            ),
            steward="field operations and finance",
            design_refs=(
                "docs/designs/WORK_ORDER_EXPENSE_ENTRY.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/runbooks/EXPENSE_CLAIM_ERP_CUTOVER.md",
            ),
            test_refs=(
                "tests/test_field_expense_requests.py",
                "tests/test_work_order_expense_web.py",
                "tests/test_dotmac_erp_expense_sync.py",
                "tests/playwright/e2e/test_work_order_expense_disclosure.py",
                "tests/architecture/test_field_expense_request_writer_boundary.py",
                "tests/architecture/test_field_request_history_identity.py",
                "tests/integration/test_field_request_requester_history_migration.py",
            ),
        ),
    ),
    SOTService(
        name="operations.material_dependencies",
        module="app.services.field.material_requests",
        owns=(
            "contextual material need and ERP submission",
            "service-work-order material need and operational approval",
            "ERP material status observation",
            "backoffice material-outcome projection into the service workflow",
            "work-order material allocation after confirmed external issue",
            "committed material output consumption",
            "requester-owned material request history",
            "material request requester-identity repair",
        ),
        depends_on=(
            "auth.permission_gate",
            "control.settings_spec",
            "events.dispatcher",
            "operations.work_orders",
            "operations.work_order_status",
        ),
        notes=(
            "The configured backoffice system owns warehouse, stock, "
            "serial, and issue decisions. This owner records the service "
            "dependency and applies an idempotent observed outcome; it "
            "never posts inventory. Backoffice unavailability never "
            "reverses a valid Sub approval. Local "
            "issue/fulfil transitions are compatibility-only and fail "
            "closed after the material flow is cut over to Sub."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("contextual material need and ERP submission"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical service work-order state",
                        "material dependency transition protocol",
                        "material-support cutover controls",
                    ),
                    canonical_writer="operations.material_dependencies",
                ),
                ConcernContract(
                    name="service-work-order material need and operational approval",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("canonical service work-order state",),
                    canonical_writer="operations.material_dependencies",
                ),
                ConcernContract(
                    name="ERP material status observation",
                    role=OwnerRole.RECONCILER,
                    input_names=("ERP material-support outcome observation",),
                    canonical_writer="operations.material_dependencies",
                ),
                ConcernContract(
                    name=(
                        "backoffice material-outcome projection into the "
                        "service workflow"
                    ),
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "canonical material dependency state",
                        "ERP material-support outcome observation",
                        "material dependency transition protocol",
                    ),
                    canonical_writer="operations.material_dependencies",
                ),
                ConcernContract(
                    name="committed material output consumption",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical material dependency state",
                        "material dependency transition protocol",
                    ),
                    canonical_writer="operations.material_dependencies",
                ),
                ConcernContract(
                    name=(
                        "work-order material allocation after confirmed external issue"
                    ),
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "canonical material dependency state",
                        "ERP material-support outcome observation",
                    ),
                    canonical_writer="operations.material_dependencies",
                ),
                ConcernContract(
                    name="requester-owned material request history",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical material dependency state",
                        "authenticated field requester identity",
                    ),
                ),
                ConcernContract(
                    name="material request requester-identity repair",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "canonical material dependency state",
                        "authenticated field requester identity",
                    ),
                    canonical_writer="operations.material_dependencies",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical service work-order state",
                    owner="operations.work_orders",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active WorkOrder identity, field assignment, and native "
                        "ticket, project, project-task service-workflow linkage"
                    ),
                ),
                AuthorityInput(
                    name="canonical material dependency state",
                    owner="operations.material_dependencies",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "FieldMaterialRequest, request items, and active "
                        "FieldWorkOrderMaterial allocation rows"
                    ),
                ),
                AuthorityInput(
                    name="authenticated field requester identity",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "Authenticated SystemUser plus exact technician-profile and "
                        "Person Party bindings"
                    ),
                ),
                AuthorityInput(
                    name="material dependency transition protocol",
                    owner="operations.work_order_status",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "draft, submission, approval, refusal, issued, and "
                        "fulfilled transition invariants"
                    ),
                ),
                AuthorityInput(
                    name="material-support cutover controls",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "validated ERP capability bindings and material_request "
                        "flow ownership gate"
                    ),
                ),
                AuthorityInput(
                    name="ERP material-support outcome observation",
                    owner="external:dotmac_erp",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "ERP material-request identity and normalized issue or "
                        "refusal status received by the integration transport"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each material command owns the request, workflow, allocation, "
                    "event evidence, and ERP outbox transaction; reconciled ERP "
                    "outcomes commit one locked request at a time. Requester-history "
                    "reads are side-effect free; revision 587 performs the bounded, "
                    "idempotent identity repair during schema migration."
                ),
                locking=(
                    "Commands resolve one active request and its work-order "
                    "aggregate; reconciliation rejects a changed ERP identity and "
                    "applies equivalent outcomes as no-ops."
                ),
                idempotency=(
                    "Stable request identity plus normalized ERP identity/status "
                    "makes repeated delivery and scheduled reconciliation converge."
                ),
                retries=(
                    "Transport failures retry outside the owner transaction; "
                    "policy or identity conflicts require corrected source state."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.material_dependencies.invalid_transition",
                    "operations.material_dependencies.request_not_found",
                    "operations.material_dependencies.work_order_not_found",
                    "operations.material_dependencies.assignment_required",
                    "operations.material_dependencies.material_item_not_found",
                    "operations.material_dependencies.requester_required",
                    "operations.material_dependencies.invalid_request",
                    "operations.material_dependencies.idempotency_conflict",
                    "operations.material_dependencies.erp_identity_conflict",
                    "operations.material_dependencies.sync_unavailable",
                    "operations.material_dependencies.invalid_command_context",
                    "operations.material_dependencies.command_contract_violation",
                    "operations.material_dependencies.nested_owner_command",
                    "operations.material_dependencies.active_caller_transaction",
                    "operations.material_dependencies.nested_transaction_completion",
                ),
                mapping_owner=(
                    "field material API/web adapters and "
                    "integration.dotmac_erp_material_support_adapter"
                ),
                retryable_codes=("operations.material_dependencies.sync_unavailable",),
                fail_closed_on=(
                    "invalid request or work-order scope",
                    "changed ERP request identity",
                    "active ERP ownership with disabled delivery",
                ),
            ),
            events=EventContract(
                event_types=(
                    "field_material_request.approved",
                    "field_material_request.fulfilled",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and identifies the request, work order, "
                    "transition, actor or ERP source, and occurrence time."
                ),
                replay=(
                    "Canonical material-request, item, allocation, ERP mirror, and "
                    "manager-event evidence rebuild the current workflow state."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="ERP-confirmed work-order material allocation",
                    input_names=(
                        "canonical material dependency state",
                        "ERP material-support outcome observation",
                    ),
                    writer="operations.material_dependencies",
                    freshness=(
                        "Current after each accepted delivery response or scheduled "
                        "ERP status reconciliation."
                    ),
                    stale_behavior=(
                        "Keep the service dependency pending; never infer issue or "
                        "allocate stock locally after cutover."
                    ),
                    drift_signal=(
                        "An approved ERP-linked request remains without a terminal "
                        "outcome or its allocation disagrees with request items."
                    ),
                    rebuild_operation=(
                        "Refresh the ERP request status and reapply the idempotent "
                        "backoffice outcome."
                    ),
                    repair_owner="operations.material_dependencies",
                ),
                ProjectionContract(
                    name="material request requester identity bridge",
                    input_names=(
                        "canonical material dependency state",
                        "authenticated field requester identity",
                    ),
                    writer="operations.material_dependencies",
                    freshness=(
                        "Written with each native submission and repaired once by "
                        "revision 587 for exact legacy identity matches."
                    ),
                    stale_behavior=(
                        "Ambiguous legacy rows remain hidden from requester history "
                        "until their identity evidence is resolved."
                    ),
                    drift_signal=(
                        "A material request lacks requested_by_system_user_id even "
                        "though its technician or Person identity has one exact "
                        "SystemUser match."
                    ),
                    rebuild_operation=(
                        "Apply the idempotent requester-identity repair from Alembic "
                        "revision 587 to the bounded drift cohort."
                    ),
                    repair_owner="operations.material_dependencies",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUTOVER_READY,
                old_owner=(
                    "CRM material fulfilment and local issue/fulfil actions that "
                    "could decide warehouse outcomes independently of ERP"
                ),
                new_owner="operations.material_dependencies",
                verification=(
                    "Approval/outbox atomicity, cutover gate, fail-closed local "
                    "fulfilment, idempotent outcome, allocation, requester-history, "
                    "identity-repair, and retry tests."
                ),
                cutover_gate=(
                    "The material_request flow is assigned to Sub and the validated "
                    "ERP outbox capability is enabled before delivery can begin."
                ),
                fallback_retirement=(
                    "Remove compatibility issue/fulfil actions after the Sub-owned "
                    "flow is verified and CRM delivery is retired."
                ),
            ),
            steward="field operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                "docs/designs/MATERIALS_VENDOR_ERP_CHAIN.md",
            ),
            test_refs=(
                "tests/test_field_material_requests.py",
                "tests/test_dotmac_erp_material_sync.py",
                "tests/test_admin_material_requests.py",
                "tests/architecture/test_field_request_history_identity.py",
                "tests/integration/test_field_request_requester_history_migration.py",
            ),
        ),
    ),
    SOTService(
        name="operations.material_consumption",
        module="app.services.field.materials",
        owns=("field material consumption evidence",),
        depends_on=(
            "operations.material_dependencies",
            "events.dispatcher",
        ),
        notes=(
            "Technicians record monotonic, allocation-capped material "
            "consumption on their scoped work orders. Each recording "
            "stages the consumption-evidence output atomically for "
            "downstream reconcilers; ERP inventory outcomes remain "
            "ERP-owned observations."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="field material consumption evidence",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("allocated work-order materials",),
                    canonical_writer="operations.material_consumption",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="allocated work-order materials",
                    owner="operations.material_dependencies",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "FieldWorkOrderMaterial allocation rows synced "
                        "from the ERP material-support outcome"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "consume commits once per technician submission on "
                    "the scoped work order."
                ),
                locking=(
                    "Each FieldWorkOrderMaterial row is selected FOR "
                    "UPDATE before the monotonic consumption write."
                ),
                idempotency=(
                    "Consumption is monotonic and capped at the "
                    "allocation; replays cannot reduce or exceed it."
                ),
                retries=(
                    "A failed submission changes nothing; the client "
                    "resubmits the same absolute quantities."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.material_consumption.active_caller_transaction",
                    "operations.material_consumption.command_contract_violation",
                    "operations.material_consumption.invalid_command_context",
                    "operations.material_consumption.nested_owner_command",
                    "operations.material_consumption.nested_transaction_completion",
                ),
                mapping_owner="field API adapters",
                fail_closed_on=(
                    "consumption above the allocated quantity",
                    "an unscoped work order",
                ),
            ),
            events=EventContract(
                event_types=("field_material.consumption_recorded",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries work-order identity and per-item "
                    "allocated/consumed quantities."
                ),
                replay=(
                    "Allocation rows are the durable state; the output "
                    "is evidence and replays are additive no-ops."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="unregistered writer on the SOT baseline",
                new_owner="operations.material_consumption",
                verification=(
                    "Field materials consumption tests and the materials "
                    "chain boundary test."
                ),
                cutover_gate=(
                    "The module leaves the shrink-only unregistered writer baseline."
                ),
                fallback_retirement=("No parallel consumption writer exists."),
            ),
            steward="field operations",
            design_refs=(
                "docs/designs/MATERIALS_VENDOR_ERP_CHAIN.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_field_materials.py",
                "tests/architecture/test_materials_lifecycle_chain_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.project_lifecycle",
        module="app.services.projects",
        owns=(
            "Project and ProjectTask identity and lifecycle",
            "project customer-account eligibility",
            "project infrastructure relationship",
            "project creation customer email consequence",
            "project and task status-change customer notification consequence",
            "project completion finance email consequence",
            "project and task allowed status transitions",
            "project-task relationship integrity and completion readiness",
            "project and task assignment and scheduling",
            "project manager assistant manager service-team and task-assignee changes",
            "project and task staff assignment notification consequence",
            "project and task staff/team tag notification consequence",
            "Project-to-ProjectTask and project/task-to-work-order relationships",
            "project audit records and transactional domain events",
            "project derived-state reconciliation",
        ),
        depends_on=(
            "auth.permission_gate",
            "auth.staff_provisioning",
            "communications.intents",
            "customer.accounts",
            "events.dispatcher",
            "communications.notification_service",
            "communications.staff_notifications",
            "communications.nextcloud_talk_staff",
            "operations.work_order_commands",
            "network.infrastructure_catalogue",
        ),
        notes=(
            "Customer and reseller reads consume the read-only "
            "customer.experience_lifecycle projection. There is no CRM "
            "project mirror, read-flip fallback, or connector operation "
            "that can read project/work-order authority."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="project infrastructure relationship",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical project aggregate",
                        "native infrastructure selection",
                    ),
                    canonical_writer="operations.project_lifecycle",
                ),
                ConcernContract(
                    name="Project and ProjectTask identity and lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical project aggregate",
                        "authorized project command",
                    ),
                    canonical_writer="operations.project_lifecycle",
                ),
                ConcernContract(
                    name="project customer-account eligibility",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical project aggregate",
                        "canonical customer account state",
                        "authorized project command",
                    ),
                ),
                ConcernContract(
                    name="project creation customer email consequence",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=(
                        "canonical project aggregate",
                        "customer communication delivery intent",
                    ),
                ),
                ConcernContract(
                    name="project and task status-change customer notification consequence",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=(
                        "canonical project aggregate",
                        "project transition protocol",
                        "customer communication delivery intent",
                    ),
                ),
                ConcernContract(
                    name="project completion finance email consequence",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=(
                        "canonical project aggregate",
                        "project transition protocol",
                        "project completion finance notification policy",
                        "staff notification delivery queue",
                    ),
                ),
                ConcernContract(
                    name="project and task allowed status transitions",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical project aggregate",
                        "project transition protocol",
                    ),
                ),
                ConcernContract(
                    name="project-task relationship integrity and completion readiness",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical project aggregate",
                        "project transition protocol",
                        "authorized project command",
                    ),
                ),
                ConcernContract(
                    name="project and task assignment and scheduling",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical project aggregate",
                        "project assignment decision",
                        "authorized project command",
                    ),
                    canonical_writer="operations.project_lifecycle",
                ),
                ConcernContract(
                    name="project manager assistant manager service-team and task-assignee changes",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical project aggregate",
                        "project assignment decision",
                        "authorized project command",
                    ),
                    canonical_writer="operations.project_lifecycle",
                ),
                ConcernContract(
                    name="project and task staff assignment notification consequence",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=(
                        "canonical project aggregate",
                        "active project assignment audience",
                        "staff notification delivery queue",
                    ),
                ),
                ConcernContract(
                    name="project and task staff/team tag notification consequence",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=(
                        "canonical project aggregate",
                        "staff notification delivery queue",
                    ),
                ),
                ConcernContract(
                    name="Project-to-ProjectTask and project/task-to-work-order relationships",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical project aggregate",
                        "canonical work-order relationship",
                    ),
                    canonical_writer="operations.project_lifecycle",
                ),
                ConcernContract(
                    name="project audit records and transactional domain events",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical project aggregate",
                        "authorized project command",
                    ),
                    canonical_writer="operations.project_lifecycle",
                ),
                ConcernContract(
                    name="project derived-state reconciliation",
                    role=OwnerRole.RECONCILER,
                    input_names=("canonical project aggregate",),
                    canonical_writer="operations.project_lifecycle",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="native infrastructure selection",
                    owner="network.infrastructure_catalogue",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="Typed exact inventory reference, validated before structural project link writes",
                ),
                AuthorityInput(
                    name="canonical project aggregate",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked native Project, ProjectTask, ProjectTaskAssignee, dependency, comment, and SLA records keyed only by native UUIDs",
                ),
                AuthorityInput(
                    name="project transition protocol",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="typed ProjectStatus and ProjectTaskStatus transition tables",
                ),
                AuthorityInput(
                    name="authorized project command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="authenticated actor, scope, reason, correlation id, and idempotency key",
                ),
                AuthorityInput(
                    name="canonical customer account state",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "native Subscriber UUID and active state selected for a new "
                        "or changed Project customer relationship"
                    ),
                ),
                AuthorityInput(
                    name="project assignment decision",
                    owner="operations.project_assignment_policy",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="typed rule match and candidate decision; it never mutates a Project aggregate",
                ),
                AuthorityInput(
                    name="canonical work-order relationship",
                    owner="operations.work_order_commands",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="native WorkOrder.project_id and WorkOrder.project_task_id foreign keys",
                ),
                AuthorityInput(
                    name="active project assignment audience",
                    owner="auth.staff_provisioning",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active SystemUser identity and email resolved from direct "
                        "Project or ProjectTask SystemUser/canonical Person assignments "
                        "and active assigned Service Team membership"
                    ),
                ),
                AuthorityInput(
                    name="staff notification delivery queue",
                    owner="communications.notification_service",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "durable queued Notification rows and post-commit delivery state"
                    ),
                ),
                AuthorityInput(
                    name="customer communication delivery intent",
                    owner="communications.intents",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "deduplicated customer email intent and durable Notification "
                        "delivery state"
                    ),
                ),
                AuthorityInput(
                    name="project completion finance notification policy",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "projects-domain enablement, explicit finance recipient list, "
                        "and permission-key audience fallback"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary="Every public state-changing command enters execute_owner_command once on a transaction-free adapter session; all nested helpers are flush-only.",
                locking="Lock Project before its tasks, then tasks by UUID; assignment and relationship changes re-read locked rows in that order. Customer eligibility is locked and read by native Subscriber UUID inside the owner command.",
                idempotency="CommandContext idempotency keys identify externally retryable commands; identical completed intent replays its stable typed outcome and changed-state no-ops are safe.",
                retries="Adapters retry serialization failures, deadlocks, and lock timeouts as a complete command; validation, stale-state, authorization, and idempotency conflicts are not retryable.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.project_lifecycle.not_found",
                    "operations.project_lifecycle.invalid_input",
                    "operations.project_lifecycle.invalid_transition",
                    "operations.project_lifecycle.stale_state",
                    "operations.project_lifecycle.idempotency_conflict",
                    "operations.project_lifecycle.relationship_conflict",
                    *owner_command_boundary_error_codes("operations.project_lifecycle"),
                ),
                mapping_owner="project API, admin-web, job, and assignment adapters",
                retryable_codes=("operations.project_lifecycle.stale_state",),
                fail_closed_on=(
                    "unknown native identity",
                    "missing or inactive customer selected for a new or changed project relationship",
                    "stale transition evidence",
                    "ambiguous assignment target",
                    "external identifier without native relationship",
                ),
            ),
            events=EventContract(
                event_types=(
                    "project.created",
                    "project.updated",
                    "project.completed",
                    "project.canceled",
                    "project_task.created",
                    "project_task.updated",
                    "project_task.completed",
                    "project_task.dependencies_replaced",
                    "project.assignment_changed",
                    "project.infrastructure_changed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Version 1 payloads are additive and preserve native aggregate identifiers; CRM identifiers are optional provenance only.",
                replay="Rebuild consequences from the durable event/audit record; replay never re-runs lifecycle eligibility.",
            ),
            projections=(
                ProjectionContract(
                    name="project SLA and assignment projections",
                    input_names=("canonical project aggregate",),
                    writer="operations.project_lifecycle",
                    freshness="synchronous in the owner transaction",
                    stale_behavior="fail closed for action eligibility and expose drift to operators",
                    drift_signal="reconciliation reports missing, duplicate, or mismatched SLA clocks and task-assignee rows",
                    rebuild_operation="reconcile_project_projection(project_id) deterministically rebuilds derived rows from the locked native aggregate",
                    repair_owner="operations.project_lifecycle",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner="operations.project_lifecycle",
                old_owner="legacy Projects/web_projects/ticket_assignment direct writers",
                verification="Projects contract, adapter-boundary, assignment ownership, projection parity, and reconciliation tests",
                cutover_gate="all project and task mutations delegate to the owner command and architecture guards reject direct adapter writes",
                fallback_retirement="HTTP-coupled domain errors, adapter commits, and assignment-engine Project writes removed",
            ),
            steward="service delivery",
            design_refs=(
                "docs/designs/PROJECTS_SOT_COMPLETION.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_projects_service.py",
                "tests/test_project_assignment_engine.py",
                "tests/architecture/test_projects_sot_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.project_assignment_policy",
        module="app.services.ticket_assignment.rules",
        owns=("project assignment-rule evaluation",),
        depends_on=("operations.project_lifecycle", "control.settings_spec"),
        notes="Evaluates typed rules and candidates only. It cannot mutate Project, ProjectTask, or assignee rows; the lifecycle owner applies its decision under lock.",
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="project assignment-rule evaluation",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical project assignment facts",
                        "configured assignment rules",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical project assignment facts",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="native project type, region, service team, and current assignment state",
                ),
                AuthorityInput(
                    name="configured assignment rules",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="validated active assignment rule configuration and ordering",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="Pure rule evaluation inside the lifecycle owner's transaction or a read-only preview.",
                locking="No locks and no writes; the lifecycle owner locks and revalidates before applying a decision.",
                idempotency="Same normalized facts and rule version produce the same ordered decision.",
                retries="Read availability failures may be retried; invalid or ambiguous rules fail closed.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.project_assignment_policy.invalid_rule",
                    "operations.project_assignment_policy.ambiguous_target",
                ),
                mapping_owner="operations.project_lifecycle and assignment preview adapters",
                fail_closed_on=(
                    "invalid rule",
                    "ambiguous target",
                    "unknown candidate identity",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner="operations.project_assignment_policy",
                old_owner="ticket_assignment engine direct Project writer",
                verification="assignment policy and architecture boundary tests",
                cutover_gate="engine delegates Project writes to operations.project_lifecycle",
                fallback_retirement="direct Project and ProjectTask assignment writes removed from ticket_assignment.engine",
            ),
            steward="service delivery",
            design_refs=("docs/designs/PROJECTS_SOT_COMPLETION.md",),
            test_refs=(
                "tests/test_project_assignment_engine.py",
                "tests/architecture/test_projects_sot_boundary.py",
            ),
        ),
    ),
)
