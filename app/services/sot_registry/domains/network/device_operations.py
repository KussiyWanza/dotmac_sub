"""network SOT declarations: device operations."""

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
        name="network.operation_ledger",
        module="app.services.network_operations",
        owns=(
            "tracked device operation lifecycle and status vocabulary",
            "operation terminal-transition guard",
            "correlation-key duplicate suppression",
            "stale-active operation reclamation",
            "parent/child operation status rollup",
            "device operation re-execution eligibility",
            "immutable redrive lineage and reviewed-head evidence",
            "typed recovery eligibility and retry limits",
            "customer subscription/device command scope and typed outcomes",
        ),
        depends_on=("network.identity",),
        notes=(
            "Owns whether a tracked device operation may run, resume, or "
            "be re-executed. Celery tasks are transport adapters that "
            "report progress through this ledger; they do not decide "
            "retry eligibility. app.services.task_reliability declares "
            "each task's contract and is a projection of this owner, not "
            "a parallel authority — a task whose contract claims operator "
            "redrive requires a redrive path here first. Failed attempts "
            "remain immutable; approved retries create linked operations "
            "through app.services.network_operation_recovery. Unregistered "
            "device writes fail closed."
            " Customer reboot and Wi-Fi adapters delegate subscription,"
            " subscriber, and active assignment scope to"
            " app.services.customer_device_commands; its stable outcome"
            " carries the exact subscription, device, and operation IDs."
        ),
    ),
    SOTService(
        name="network.operation_dispatch",
        module="app.services.network_operation_dispatch",
        owns=(
            "transactional network command outbox",
            "typed operation-to-task command registry",
            "broker publication attempts and acknowledgement state",
            "single-admission worker execution claims",
            "unknown-delivery and interrupted-execution classification",
        ),
        depends_on=("network.operation_ledger",),
        notes=(
            "Stages the exact registered command in the same transaction "
            "as its operation. A scheduled publisher is the only broker "
            "writer for managed commands, and a worker envelope claims the "
            "dispatch row before entering device code. Operation status "
            "remains the device/business outcome; transport uncertainty is "
            "preserved separately and fails closed for reviewed recovery."
        ),
    ),
    SOTService(
        name="network.tr069_commands",
        module="app.services.network.tr069_job_commands",
        owns=(
            "TR-069 command admission coordination",
            "TR-069 command execution coordination",
            "TR-069 command outcome coordination",
        ),
        depends_on=(
            "auth.permission_gate",
            "control.feature_registry",
            "network.identity",
            "network.operation_ledger",
            "network.operation_dispatch",
            "events.dispatcher",
        ),
        notes=(
            "Owns the complete command lifecycle boundary. Adapters submit "
            "typed intent; the operation ledger owns lifecycle, the durable "
            "dispatch owns broker delivery, GenieACS supplies observations, "
            "and tr069_jobs is a read-only operator projection. Disabling "
            "admission never stops accepted-work drainage."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="TR-069 command admission coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated TR-069 command evidence",
                        "canonical TR-069 device and ACS binding",
                        "TR-069 command admission capability",
                        "canonical network operation lifecycle",
                        "durable network command dispatch",
                    ),
                ),
                ConcernContract(
                    name="TR-069 command execution coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical TR-069 device and ACS binding",
                        "canonical network operation lifecycle",
                        "durable network command dispatch",
                    ),
                ),
                ConcernContract(
                    name="TR-069 command outcome coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical network operation lifecycle",
                        "durable network command dispatch",
                        "normalized GenieACS command observation",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated TR-069 command evidence",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "admin authorization plus typed actor, scope, reason, "
                        "command, correlation, and idempotency context"
                    ),
                ),
                AuthorityInput(
                    name="canonical TR-069 device and ACS binding",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active Tr069CpeDevice identity, GenieACS "
                        "device id, and active Tr069AcsServer binding"
                    ),
                ),
                AuthorityInput(
                    name="TR-069 command admission capability",
                    owner="control.feature_registry",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="network.tr069_command_admission",
                ),
                AuthorityInput(
                    name="canonical network operation lifecycle",
                    owner="network.operation_ledger",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "cpe_tr069_command NetworkOperation identity and "
                        "guarded lifecycle state"
                    ),
                ),
                AuthorityInput(
                    name="durable network command dispatch",
                    owner="network.operation_dispatch",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "cpe_tr069_command.v1 outbox row, publication "
                        "attempts, and worker claim"
                    ),
                ),
                AuthorityInput(
                    name="normalized GenieACS command observation",
                    owner="external:genieacs",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "accepted task ids, pending task inventory, faults, "
                        "and absence after acceptance"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Each public admission, execution claim, or observation "
                    "enters execute_owner_command once on a transaction-free "
                    "session; the operation, dispatch, encrypted payload, job "
                    "projection, and event evidence commit atomically."
                ),
                locking=(
                    "Admission locks the target device; execution and outcome "
                    "lock the operation and job projection. Unique active "
                    "correlation and one-job-per-operation constraints arbitrate "
                    "concurrent requests."
                ),
                idempotency=(
                    "A device, command kind, and encrypted-payload fingerprint "
                    "return the active operation. Dispatch claims permit one "
                    "queued-to-running transition and terminal observations do "
                    "not regress."
                ),
                retries=(
                    "Broker publication may retry before worker claim. ACS "
                    "submission is never automatically replayed; interrupted "
                    "or ambiguous delivery becomes unverified and requires "
                    "review of current device state."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.tr069_commands.invalid_parameter",
                    "network.tr069_commands.invalid_parameter_type",
                    "network.tr069_commands.invalid_parameter_value",
                    "network.tr069_commands.invalid_download",
                    "network.tr069_commands.invalid_command",
                    "network.tr069_commands.invalid_refresh_root",
                    "network.tr069_commands.admission_disabled",
                    "network.tr069_commands.device_not_found",
                    "network.tr069_commands.device_inactive",
                    "network.tr069_commands.device_not_registered",
                    "network.tr069_commands.acs_unavailable",
                    "network.tr069_commands.operation_projection_missing",
                    "network.tr069_commands.invalid_observation",
                    "network.tr069_commands.device_command_in_progress",
                    "network.tr069_commands.device_state_review_required",
                    "network.tr069_commands.concurrent_admission",
                    *owner_command_boundary_error_codes("network.tr069_commands"),
                ),
                mapping_owner=(
                    "app.services.web_network_tr069 and app.tasks.tr069 adapters"
                ),
                retryable_codes=(),
                fail_closed_on=(
                    "disabled admission",
                    "inactive or unregistered target",
                    "missing ACS binding",
                    "operation/projection mismatch",
                    "ambiguous ACS delivery",
                    "active caller transaction",
                    "manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=(
                    "tr069_job.accepted",
                    "tr069_job.completed",
                    "tr069_job.failed",
                    "tr069_job.unverified",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Additive payload evolution within version 1; no event "
                    "contains command values, firmware URLs, or credentials."
                ),
                replay=(
                    "The event store retries delivery by event identity; "
                    "consumers treat job and operation ids as immutable "
                    "lifecycle evidence."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="tr069_jobs operator lifecycle projection",
                    input_names=(
                        "canonical TR-069 device and ACS binding",
                        "canonical network operation lifecycle",
                        "durable network command dispatch",
                        "normalized GenieACS command observation",
                    ),
                    writer="network.tr069_commands",
                    freshness=(
                        "Admission and normalized observations update the "
                        "projection in the owning transaction; the permanent "
                        "resolver checks active ACS tasks every scheduler pass."
                    ),
                    stale_behavior=(
                        "Readers show the last committed state and "
                        "last_observed_at. Missing or ambiguous confirmation "
                        "becomes unverified rather than inferred success."
                    ),
                    drift_signal=(
                        "Operation/job status disagreement, an unlinked active "
                        "job, or an expired running/pending confirmation window."
                    ),
                    rebuild_operation=("app.tasks.tr069.reconcile_command_outcomes"),
                    repair_owner="network.tr069_commands",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner="network.tr069_commands",
                old_owner=(
                    "app.services.tr069.JobProjections mutation methods and "
                    "app.tasks.tr069.execute_bulk_action"
                ),
                verification=(
                    "Focused lifecycle and architecture tests prove atomic "
                    "admission, guarded execution, terminal ambiguity, "
                    "permanent drainage, secret redaction, and absence of old "
                    "producers."
                ),
                cutover_gate=(
                    "Migration 409 terminalizes every unlinked executable row "
                    "and moves the old flag value to the admission-only control."
                ),
                fallback_retirement=(
                    "Legacy create/update/delete/execute/cancel methods, bulk "
                    "task, runtime adoption, and old control alias are removed."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/TR069_COMMAND_LIFECYCLE.md",
                "docs/runbooks/TR069_COMMAND_CUTOVER.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/CODING_STANDARD.md",
            ),
            test_refs=(
                "tests/test_tr069_job_commands.py",
                "tests/architecture/test_tr069_job_lifecycle_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="network.ont_provisioning_commands",
        module="app.services.network.ont_provisioning_commands",
        owns=(
            "ONT authorization and baseline-repair command acceptance",
            "provisioning operation and dispatch atomicity",
            "bootstrap child-operation and delayed-attempt staging",
            "provisioning command duplicate responses",
        ),
        depends_on=(
            "network.identity",
            "network.operation_ledger",
            "network.operation_dispatch",
        ),
        notes=(
            "Assigned authorization adapters submit only "
            "RequestAssignedOntAuthorization with CommandContext and an exact "
            "UUID/OLT/F/S/P/serial value-object target; admission returns "
            "OntAuthorizationAdmission. The owner evaluates the active "
            "assignment and exact PON before staging the operation and typed "
            "dispatch. Admin, API, and bulk adapters never publish provisioning "
            "device tasks directly, and workers never create their own operation "
            "after broker delivery."
        ),
    ),
    SOTService(
        name="network.ont_provisioning_defaults",
        module="app.services.network.ont_provisioning_defaults",
        owns=("approved ONT provisioning layout defaults",),
        depends_on=(),
        notes=(
            "Owns only reviewed device-layout defaults that may become "
            "executable desired values. It is deliberately separate "
            "from the stateful provisioning executor: declaring the "
            "first TR-069 WANPPPConnection instance is a pure policy "
            "decision, not an execution transition."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="approved ONT provisioning layout defaults",
                    role=OwnerRole.POLICY,
                    input_names=("approved Huawei provisioning layout",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="approved Huawei provisioning layout",
                    owner="network.ont_provisioning_defaults",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "PppoeInstanceLayout declaration reviewed with "
                        "the ONT rollout eligibility cohort"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.NOT_APPLICABLE,
                boundary="Pure typed policy values; no session or I/O.",
                locking="None: immutable module-level declarations.",
                idempotency=(
                    "Repeated resolution returns the same immutable layout value."
                ),
                retries="Not applicable: resolution cannot fail transiently.",
            ),
            errors=ErrorContract(
                domain_codes=("ont_provisioning_default_unsupported_layout",),
                mapping_owner="app.services.network.ont_provisioning_defaults",
                fail_closed_on=(
                    "a device layout without an approved typed declaration",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="raw literal fallback in effective ONT configuration",
                new_owner="network.ont_provisioning_defaults",
                verification=(
                    "tests/test_reconcile_sentinels.py pins the declared "
                    "owner, executable value, and absence of authority debt."
                ),
                cutover_gate=(
                    "The composer obtains the instance index from this owner."
                ),
                fallback_retirement=(
                    "The raw literal fallback is removed from composer and adapter."
                ),
            ),
            steward="network",
            design_refs=(
                "docs/designs/ONT_RECONCILE_ELIGIBILITY_SOT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_reconcile_sentinels.py",
                "tests/architecture/test_control_plane_desired_value_policy.py",
            ),
        ),
    ),
    SOTService(
        name="network.ont_provisioning_execution",
        module="app.services.network.ont_provisioning_execution",
        owns=(
            "tracked ONT authorization execution transitions",
            "tracked baseline-repair execution transitions",
            "DB-only ONT baseline preview execution",
            "TR-069 bootstrap verification and retry policy",
            "bootstrap parent and bulk-item outcome projection",
        ),
        depends_on=(
            "network.ont_provisioning_commands",
            "network.identity",
            "network.operation_ledger",
        ),
        notes=(
            "Celery tasks claim a durable dispatch, reconstruct "
            "ExecuteAssignedOntAuthorization, and delegate here. The execution "
            "owner repeats the exact assignment/PON decision immediately before "
            "device I/O and returns OntAuthorizationExecutionOutcome; stale "
            "assignment fails closed without an OLT write. Inform-driven "
            "confirmation and scheduled verification share the same parent/child "
            "completion projection. A pre-cutover broker envelope may only "
            "re-submit typed assigned intent to the command owner and cannot "
            "enter device code."
        ),
    ),
    SOTService(
        name="network.ont_commissioning",
        module="app.services.network.ont_commissioning",
        owns=(
            "temporary ONT commissioning intent lifecycle",
            "assignment-free management-only commissioning coordination",
            "commissioning expiry and assignment reconciliation",
        ),
        depends_on=(
            "auth.permission_gate",
            "network.identity",
            "network.ont_assignment_commands",
            "network.ont_provisioning_execution",
            "network.operation_ledger",
            "network.operation_dispatch",
            "events.dispatcher",
        ),
        notes=(
            "Owns the explicit alternative to raw assignment-free "
            "authorization. Each intent binds one live autofind serial, "
            "OLT, and F/S/P, expires after a bounded interval, and permits "
            "only OLT registration plus management VLAN/IPHOST/TR-069. "
            "Model-aware live reads use global autofind plus exact parsed "
            "F/S/P filtering where scoped Huawei syntax is unsupported. "
            "Its dependency audit includes only registration and management "
            "profiles; customer traffic-table and WAN inventories remain "
            "normal authorization dependencies. Separate named capabilities "
            "replace a public provisioning switch: only this owner may request "
            "commissioning registration, while assigned authorization and "
            "reauthorization enter through the exact-assignment command owner. "
            "It never creates an assignment or applies internet, PPPoE, "
            "WAN, LAN, or Wi-Fi intent. Assignment converts a "
            "management-ready intent; expiry without assignment stages "
            "idempotent return-to-inventory cleanup."
            " Slow OLT calls consume detached immutable connection values "
            "after the preceding database transaction closes; a fresh "
            "reliability session records partial external success. The "
            "permanent reconciler admits bounded management-only recovery "
            "only from matching intent, operation-ledger, dispatch, local "
            "inventory, and live OLT registration evidence."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="temporary ONT commissioning intent lifecycle",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated commissioning intent",
                        "exact live OLT autofind observation",
                        "canonical ONT inventory identity",
                        "active ONT service assignment",
                        "durable network operation lifecycle",
                        "durable network command dispatch",
                    ),
                ),
                ConcernContract(
                    name=("assignment-free management-only commissioning coordination"),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "exact live OLT autofind observation",
                        "canonical ONT inventory identity",
                        "active ONT service assignment",
                        "effective OLT management configuration",
                        "durable network operation lifecycle",
                        "durable network command dispatch",
                    ),
                ),
                ConcernContract(
                    name="commissioning expiry and assignment reconciliation",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical ONT inventory identity",
                        "active ONT service assignment",
                        "durable network operation lifecycle",
                        "durable network command dispatch",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated commissioning intent",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "network:ont:commission permission plus typed actor, "
                        "reason, reference, correlation, and exact candidate"
                    ),
                ),
                AuthorityInput(
                    name="exact live OLT autofind observation",
                    owner="external:huawei_olt",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "model-supported display ont autofind read immediately "
                        "before the first commissioning device write, filtered "
                        "to the exact parsed OLT/F/S/P/serial"
                    ),
                ),
                AuthorityInput(
                    name="canonical ONT inventory identity",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "OltAutofindCandidate, OLTDevice, PonPort, and "
                        "OntUnit exact serial/OLT/F/S/P identity"
                    ),
                ),
                AuthorityInput(
                    name="active ONT service assignment",
                    owner="network.ont_assignment_commands",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="the exact active OntAssignment for the ONT",
                ),
                AuthorityInput(
                    name="effective OLT management configuration",
                    owner="network.ont_provisioning_execution",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "effective OLT config pack management VLAN, imported "
                        "GEM/priority, management IPAM, ACS, and TR-069 profile"
                    ),
                ),
                AuthorityInput(
                    name="durable network operation lifecycle",
                    owner="network.operation_ledger",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="NetworkOperation status and landed-device evidence",
                ),
                AuthorityInput(
                    name="durable network command dispatch",
                    owner="network.operation_dispatch",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="versioned commission, verify, and cleanup dispatches",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Admission and scheduled reconciliation each enter "
                    "execute_owner_command once on a transaction-free session; "
                    "intent, operation, dispatch, audit, and event stage "
                    "atomically. Device workers close the database transaction "
                    "before live OLT or management I/O, use a detached immutable "
                    "typed execution plan, and persist external-write evidence "
                    "in a fresh transaction before subsequent coordination. "
                    "Finalization locks and revalidates the exact intent and "
                    "operation after device I/O."
                ),
                locking=(
                    "Admission locks the exact autofind candidate and active "
                    "serial intent; reconciliation locks active intents; "
                    "cleanup and assignment both lock the OntUnit and recheck "
                    "assignment/commissioning state before mutation."
                ),
                idempotency=(
                    "One active intent per canonical serial and operation "
                    "correlation suppress duplicate admission; versioned "
                    "dispatch keys make bounded ACS checks and cleanup "
                    "replay-safe. Interrupted management redrives are linked to "
                    "one reviewed operation/evidence fingerprint and carry "
                    "authorization_reissue_allowed=false."
                ),
                retries=(
                    "Device authorization reuses durable landed-write evidence; "
                    "management recovery is bounded by the operation retry "
                    "budget; ACS checks use five delayed attempts; later "
                    "reconciliation repairs assignment and expiry drift."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.ont_commissioning.invalid_target",
                    "network.ont_commissioning.candidate_not_active",
                    "network.ont_commissioning.stale_target",
                    "network.ont_commissioning.reason_required",
                    "network.ont_commissioning.invalid_expiry",
                    "network.ont_commissioning.assignment_exists",
                    "network.ont_commissioning.intent_conflict",
                    "network.ont_commissioning.concurrent_admission",
                    "network.ont_commissioning.intent_not_found",
                    "network.ont_commissioning.live_autofind_mismatch",
                    "network.ont_commissioning.olt_unavailable",
                    "network.ont_commissioning.local_inventory_failed",
                    "network.ont_commissioning.authorization_failed",
                    "network.ont_commissioning.inventory_missing",
                    "network.ont_commissioning.olt_ont_id_missing",
                    "network.ont_commissioning.config_pack_missing",
                    "network.ont_commissioning.management_prerequisite_missing",
                    "network.ont_commissioning.management_ip_incomplete",
                    "network.ont_commissioning.management_priority_missing",
                    "network.ont_commissioning.management_apply_failed",
                    "network.ont_commissioning.service_config_forbidden",
                    "network.ont_commissioning.unsafe_external_transaction",
                    "network.ont_commissioning.external_write_reconciliation_required",
                    "network.ont_commissioning.registration_not_confirmed",
                    "network.ont_commissioning.execution_conflict",
                    "network.ont_commissioning.interrupted_execution_review_required",
                    "network.ont_commissioning.management_recovery_exhausted",
                    "network.ont_commissioning.operation_missing",
                    "network.ont_commissioning.acs_not_ready",
                    "network.ont_commissioning.cleanup_target_missing",
                    "network.ont_commissioning.cleanup_identity_mismatch",
                    "network.ont_commissioning.cleanup_failed",
                    *owner_command_boundary_error_codes("network.ont_commissioning"),
                ),
                mapping_owner=(
                    "admin ONT commissioning web adapter and "
                    "app.tasks.ont_commissioning"
                ),
                retryable_codes=("network.ont_commissioning.management_apply_failed",),
                fail_closed_on=(
                    "missing permission or reason",
                    "stale or mismatched live autofind target",
                    "existing active assignment",
                    "registration on a different F/S/P",
                    "missing registration or management-only prerequisites",
                    "any internet, WAN, PPPoE, LAN, or Wi-Fi command",
                    "missing durable landed-authorization recovery evidence",
                    "changed live registration before management recovery",
                    "an active database transaction at any device-I/O boundary",
                    "identity drift before cleanup",
                ),
            ),
            events=EventContract(
                event_types=(
                    "ont.commissioning_requested",
                    "ont.commissioning_state_changed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 payloads evolve additively; the exact intent, "
                    "OLT, F/S/P, serial, state, and expiry remain stable."
                ),
                replay=(
                    "Consumers key side effects by event_id; the intent row "
                    "remains the authoritative current lifecycle."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="ont_commissioning_intents lifecycle",
                    input_names=(
                        "exact live OLT autofind observation",
                        "canonical ONT inventory identity",
                        "active ONT service assignment",
                        "durable network operation lifecycle",
                    ),
                    writer="network.ont_commissioning",
                    freshness=(
                        "Device workers update after each phase and the "
                        "permanent reconciler targets a 60-second interval."
                    ),
                    stale_behavior=(
                        "Stale intent state never grants service. Expired "
                        "unassigned device state is cleanup-eligible only "
                        "after exact locked revalidation. An interrupted "
                        "authorizing intent either receives bounded, exact-live "
                        "management recovery from durable landed evidence or "
                        "moves to a terminal failed state for review."
                    ),
                    drift_signal=(
                        "last_reconciled_at, expiry, assignment state, "
                        "operation status, and cleanup failure evidence"
                    ),
                    rebuild_operation=(
                        "reconcile_ont_commissioning recomputes assignment "
                        "conversion, repairs interrupted management execution, "
                        "and stages safe expiry cleanup"
                    ),
                    repair_owner="network.ont_commissioning",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                new_owner="network.ont_commissioning",
                old_owner=("raw assignment-free authorize ONT web/task workflow"),
                verification=(
                    "Focused behavior and architecture tests prove assigned "
                    "authorization admission and management-only commissioning."
                ),
                cutover_gate=(
                    "Raw authorization rejects requests without an exact "
                    "active assignment and UI routes those candidates to "
                    "Commission ONT."
                ),
                fallback_retirement=(
                    "No raw assignment-free authorize adapter remains."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/ONT_COMMISSIONING_INTENT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/OLT_ONT_ACS_ARCHITECTURE.md",
                "docs/PROVISIONING_OPERATIONS_GUIDE.md",
            ),
            test_refs=(
                "tests/test_ont_commissioning.py",
                "tests/architecture/test_ont_commissioning_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="network.cpe_dialer_credential",
        module="app.services.cpe_dialer_credential_reconcile",
        owns=(
            "derived CPE PPPoE dialer credential projection",
            "CPE dialer credential fingerprint comparison and readback",
        ),
        depends_on=(
            "network.identity",
            "access.radius_projection",
            "secrets.credential_crypto",
            "events.dispatcher",
            "runtime.db_sessions",
        ),
        notes=(
            "AccessCredential/RadiusUser stays the authoritative access "
            "credential and access.radius_projection stays the only "
            "writer of the RADIUS auth tables. What the CPE dials with "
            "is a DERIVED projection of that credential onto "
            "OntUnit.desired_config wan.pppoe_username/pppoe_password, "
            "and this reconciler is its single canonical writer. "
            "Operator-typed dialer values are repaired back to the "
            "authoritative credential; they never flow the other way "
            "and never reach RADIUS. Delivery to the physical CPE "
            "remains with the ONT reconciler. Comparison is by keyed "
            "fingerprint only — credential values are never logged, "
            "returned, or stored in drift records."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="derived CPE PPPoE dialer credential projection",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "authoritative subscriber access credential",
                        "active ONT-to-subscriber assignment",
                        "derived CPE dialer projection",
                        "credential fingerprint key",
                    ),
                    canonical_writer="network.cpe_dialer_credential",
                ),
                ConcernContract(
                    name=("CPE dialer credential fingerprint comparison and readback"),
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "authoritative subscriber access credential",
                        "derived CPE dialer projection",
                        "ACS-reported PPPoE dialer username",
                        "credential fingerprint key",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authoritative subscriber access credential",
                    owner="access.radius_projection",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active AccessCredential username and encrypted "
                        "secret for the ONT's assigned subscriber; the "
                        "same record that decides RADIUS authentication"
                    ),
                ),
                AuthorityInput(
                    name="active ONT-to-subscriber assignment",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active OntAssignment subscriber binding for an active OntUnit"
                    ),
                ),
                AuthorityInput(
                    name="ACS-reported PPPoE dialer username",
                    owner="external:genieacs",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "last GenieACS-reported WANPPPConnection Username, "
                        "cached on OntObservation by the ONT reconciler; "
                        "the dialer password is never readable from a CPE"
                    ),
                ),
                AuthorityInput(
                    name="derived CPE dialer projection",
                    owner="network.cpe_dialer_credential",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "OntUnit.desired_config wan.pppoe_username and "
                        "wan.pppoe_password plus the recorded dialer "
                        "fingerprint under delivery"
                    ),
                ),
                AuthorityInput(
                    name="credential fingerprint key",
                    owner="secrets.credential_crypto",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "configured credential-encryption key, used to key "
                        "the HMAC-SHA256 dialer fingerprint so comparison "
                        "never handles or exposes the credential value"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "One pass flushes each ONT's projection into the "
                    "caller's session; the scheduling adapter's session "
                    "context owns the commit."
                ),
                locking=(
                    "No cross-row lock is taken. Each ONT is repaired "
                    "independently from its own authoritative credential, "
                    "so a concurrent pass converges on the same value."
                ),
                idempotency=(
                    "The keyed fingerprint is the idempotency key: a pass "
                    "whose desired and observed fingerprints already agree "
                    "performs no write."
                ),
                retries=(
                    "Safe to re-run at any time; a partially applied pass "
                    "is completed by the next one."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.cpe_dialer_credential.fingerprint_key_unavailable",
                    "network.cpe_dialer_credential.unreadable_stored_credential",
                ),
                mapping_owner=("ONT reconcile sweep task and administrative adapters"),
                retryable_codes=(
                    "network.cpe_dialer_credential.fingerprint_key_unavailable",
                ),
                fail_closed_on=(
                    "missing credential-encryption key",
                    "credential with no usable secret",
                    "ONT with no active access credential",
                ),
            ),
            events=EventContract(
                event_types=("ont.dialer_credential_reconciled",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries ONT identity, repair reason, and "
                    "truncated credential fingerprints only. It never "
                    "carries a dialer username or secret."
                ),
                replay=(
                    "Re-running the reconciler reproduces the outcome from "
                    "the authoritative credential; the recorded fingerprint "
                    "on the ONT reconstructs which projection was applied."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="CPE PPPoE dialer credential projection",
                    input_names=(
                        "authoritative subscriber access credential",
                        "active ONT-to-subscriber assignment",
                        "derived CPE dialer projection",
                        "ACS-reported PPPoE dialer username",
                        "credential fingerprint key",
                    ),
                    writer="network.cpe_dialer_credential",
                    freshness=(
                        "Current while the recorded dialer fingerprint "
                        "equals the authoritative credential's fingerprint "
                        "and the last ACS-reported username matches it."
                    ),
                    stale_behavior=(
                        "A stale projection is rewritten from the "
                        "authoritative credential; a correct projection the "
                        "CPE has not taken is re-flagged for delivery, not "
                        "rewritten. Operator-typed values never win."
                    ),
                    drift_signal=(
                        "Keyed HMAC-SHA256 fingerprint over the "
                        "(username, secret) pair, plus username-only device "
                        "readback. pppoe_health CATEGORY_CREDENTIAL_MISMATCH "
                        "is the weaker read-side detector."
                    ),
                    rebuild_operation=(
                        "Re-run reconcile_cpe_dialer_credentials; it is "
                        "fully derivable from the authoritative credential."
                    ),
                    repair_owner="network.cpe_dialer_credential",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                new_owner="network.cpe_dialer_credential",
                old_owner=(
                    "operator-typed ONT dialer values written directly by "
                    "web_network_ont_actions.config_setters."
                    "set_pppoe_credentials and update_ont_config"
                ),
                verification=(
                    "Each sweep audits every assigned ONT by fingerprint "
                    "and reports drift before repairing; the audit mode "
                    "reports exactly what a repair pass would change."
                ),
                cutover_gate=(
                    "Fleet-wide dialer drift stays at zero across "
                    "consecutive sweeps, and the ONT configuration UI no "
                    "longer presents the dialer fields as an "
                    "authentication fix."
                ),
                fallback_retirement=(
                    "The manual dialer action is retained only as a CPE "
                    "repair tool and is documented as non-authoritative; "
                    "its values are re-converged by this owner."
                ),
            ),
            steward="network operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=("tests/test_cpe_dialer_credential_reconcile.py",),
        ),
    ),
    SOTService(
        name="network.control_plane_intent",
        module="app.services.control_plane_intent",
        owns=(
            "shared desired-state delivery lifecycle",
            "control-plane target and revision identity",
            "vendor status projections and transition guards",
            "unset desired-value admissibility policy",
        ),
        depends_on=("network.identity",),
        notes=(
            "Vendor adapters retain native persistence models but project "
            "through one desired-to-readback lifecycle. Verified always "
            "requires device evidence for the current intent revision. "
            "Missing or provenance-unknown desired state stays typed as "
            "unknown and cannot become an executable device value unless "
            "a named owner explicitly declares that default. Execution "
            "authority is separate from review progress and review "
            "progress never grants it; providers register their own "
            "sentinel tables, enforce the ruling on every delivery "
            "path, and hold any default that still executes without a "
            "declaration on a shrink-only debt baseline."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="shared desired-state delivery lifecycle",
                    role=OwnerRole.POLICY,
                    input_names=("vendor delivery status",),
                ),
                ConcernContract(
                    name="control-plane target and revision identity",
                    role=OwnerRole.POLICY,
                    input_names=("control-plane target identity",),
                ),
                ConcernContract(
                    name="vendor status projections and transition guards",
                    role=OwnerRole.POLICY,
                    input_names=("vendor delivery status",),
                ),
                ConcernContract(
                    name="unset desired-value admissibility policy",
                    role=OwnerRole.POLICY,
                    input_names=("provider unset-sentinel declaration",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="vendor delivery status",
                    owner="network.control_plane_intent",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "Native vendor states passed in by their owning "
                        "adapter: NetworkOperation, UISP intent, Huawei "
                        "reconcile sync_status, RouterOS push and push "
                        "result, ProvisioningRun."
                    ),
                ),
                AuthorityInput(
                    name="control-plane target identity",
                    owner="network.control_plane_intent",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "Provider, target type, target id, and desired "
                        "revision supplied by the calling owner; the "
                        "correlation key is derived, never stored here."
                    ),
                ),
                AuthorityInput(
                    name="provider unset-sentinel declaration",
                    owner="network.control_plane_intent",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "Per-field sentinel value, execution authority, "
                        "and review status declared by the provider's "
                        "table, e.g. "
                        "app.services.network.reconcile.sentinels.RULES "
                        "for Huawei ONTs."
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.NOT_APPLICABLE,
                boundary=(
                    "Pure semantic policy. The module opens no session, "
                    "reads no table, and writes no state; callers apply "
                    "its rulings inside their own transactions."
                ),
                locking=(
                    "None. Revision conflicts are detected by "
                    "assert_intent_head against a current revision the "
                    "caller has already locked."
                ),
                idempotency=(
                    "Every function is a pure function of its "
                    "arguments. Same-phase transitions are explicitly "
                    "allowed so a retried write re-derives the same "
                    "ruling."
                ),
                retries=(
                    "Safe to re-evaluate without side effects; a retry "
                    "that carries a superseded revision is rejected by "
                    "assert_intent_head rather than silently applied."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.control_plane_intent.contract_error",
                    "network.control_plane_intent.transition_error",
                    "network.control_plane_intent.head_conflict",
                ),
                mapping_owner=(
                    "Calling delivery owners translate these to their own "
                    "transport outcomes; this module raises typed errors "
                    "and never HTTP."
                ),
                fail_closed_on=(
                    "unknown vendor status",
                    "impossible phase transition",
                    "superseded intent revision",
                    "undeclared unset desired value",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                new_owner="network.control_plane_intent",
                old_owner=(
                    "Per-vendor delivery status vocabularies and, for "
                    "unset desired values, undeclared per-call defaults "
                    "scattered across composer, adapter, and planner "
                    "emission sites."
                ),
                verification=(
                    "Providers register every substitution and an audit "
                    "fails when a new default is added without one; the "
                    "read-only blast-radius detector reports the live "
                    "population per rule before any disposition changes."
                ),
                cutover_gate=(
                    "The provider authority-debt baselines are empty: "
                    "every substituted default is either a declared "
                    "owner default, refused here, or delegated to a "
                    "named refusing owner, with the detector reporting "
                    "a measured count behind each decision."
                ),
                fallback_retirement=(
                    "Provider-local default substitution is retired as "
                    "each field's disposition is declared; the registry "
                    "remains the only place a default may be named."
                ),
            ),
            steward="network operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=(
                "tests/architecture/test_control_plane_desired_value_policy.py",
                "tests/test_reconcile_sentinels.py",
            ),
        ),
    ),
)
