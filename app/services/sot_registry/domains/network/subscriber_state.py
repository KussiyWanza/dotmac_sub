"""network SOT declarations: subscriber state."""

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
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="network.radio_signal",
        module="app.services.network.radio_signal",
        owns=("wireless radio RF signal freshness projection",),
        depends_on=(),
        notes=(
            "Read-side owner of how a stored RF observation may be "
            "presented: a value renders only alongside its freshness "
            "(fresh/stale/unavailable), and a radio UISP reports as "
            "disconnected/missing/vanished never renders a signal. "
            "The uisp_sync collector is the sole column writer "
            "(cpe_devices.rf_signal_*); it remains tracked as "
            "undeclared-writer debt in sot_writer_baseline.txt."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="wireless radio RF signal freshness projection",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "stored radio RF observation",
                        "radio signal freshness policy",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="stored radio RF observation",
                    owner="external:uisp",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "AP-side station RSSI observed by UISP NMS, "
                        "collected by app.services.topology.uisp_sync "
                        "into cpe_devices.rf_signal_dbm/source/"
                        "observed_at alongside last_uisp_status"
                    ),
                ),
                AuthorityInput(
                    name="radio signal freshness policy",
                    owner="network.radio_signal",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "thirty-minute freshness TTL (two sync runs) "
                        "and the disconnected/missing/vanished "
                        "presentation guard"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Resolves plain attributes of an already-loaded "
                    "radio row; never queries, mutates, commits, or "
                    "rolls back."
                ),
                locking="Pure projection; acquires no locks.",
                idempotency=(
                    "The same stored observation and evaluation time "
                    "produce the same effective signal and freshness."
                ),
                retries="Read-only resolution is safe to retry.",
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="calling read projection adapters",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "last_mile hardcoded rf_signal=None (wireless "
                    "link-signal rung unobservable)"
                ),
                new_owner="network.radio_signal",
                verification=(
                    "Freshness, status-guard, and stale-never-gates "
                    "tests across the resolver, diagnoser, and "
                    "Customer 360 projection."
                ),
                cutover_gate=(
                    "access_path and last_mile consumers resolve RF "
                    "exclusively through this owner."
                ),
                fallback_retirement=(
                    "No surface renders cpe_devices.rf_signal_dbm "
                    "without the freshness projection."
                ),
            ),
            steward="network operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=(
                "tests/test_radio_signal.py",
                "tests/test_access_path_endpoint_projection.py",
                "tests/services/topology/test_last_mile.py",
            ),
        ),
    ),
    SOTService(
        name="network.radius_sessions",
        module="app.services.network.radius_sessions",
        owns=(
            "online-now session state",
            "active-session NAS observation evidence",
            "bounded historical NAS evidence",
            "subscription-scoped live-session binding and freshness projection",
        ),
        depends_on=(
            "access.subscription_lifecycle",
            "network.identity",
            "sessions.radius_reconciliation",
        ),
        notes=(
            "An exact subscription binding wins. An unbound live session may "
            "represent a service only when the subscriber has exactly one "
            "operationally-current subscription; it is never copied across "
            "sibling services. Connected evidence becomes stale after the "
            "declared freshness window."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="online-now session state",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical live RADIUS observations",
                        "canonical subscription cohort",
                    ),
                ),
                ConcernContract(
                    name="active-session NAS observation evidence",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical live RADIUS observations",
                        "canonical network identities",
                    ),
                ),
                ConcernContract(
                    name="bounded historical NAS evidence",
                    role=OwnerRole.RESOLVER,
                    input_names=("canonical RADIUS history",),
                ),
                ConcernContract(
                    name=(
                        "subscription-scoped live-session binding and freshness "
                        "projection"
                    ),
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical live RADIUS observations",
                        "canonical subscription cohort",
                        "session freshness policy",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical live RADIUS observations",
                    owner="sessions.radius_reconciliation",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "active RadiusSession subscription, NAS, IP, and "
                        "last-update observations"
                    ),
                ),
                AuthorityInput(
                    name="canonical RADIUS history",
                    owner="sessions.radius_reconciliation",
                    kind=AuthorityKind.OBSERVATION,
                    source="bounded reconciled RADIUS accounting history",
                ),
                AuthorityInput(
                    name="canonical subscription cohort",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "operationally-current Subscription identity, subscriber, "
                        "and lifecycle status"
                    ),
                ),
                AuthorityInput(
                    name="canonical network identities",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="NAS and managed-device identities",
                ),
                AuthorityInput(
                    name="session freshness policy",
                    owner="network.radius_sessions",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "exact-binding precedence, single-service unbound "
                        "eligibility, and fifteen-minute freshness window"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Resolvers read on the caller session and never poll, "
                    "mutate, commit, or roll back."
                ),
                locking="Read projections acquire no mutation locks.",
                idempotency=(
                    "The same cohort, observations, and evaluation time produce "
                    "the same binding and freshness result."
                ),
                retries="Read-only resolution is safe to retry.",
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="calling read projection adapters",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "customer_portal_flow_services and web_customer_details "
                    "page-local accounting-session freshness and subscriber-wide "
                    "fallback rules"
                ),
                new_owner="network.radius_sessions",
                verification=(
                    "Exact-binding, single-service fallback, multi-service "
                    "non-leakage, freshness, and consumer contract tests."
                ),
                cutover_gate=(
                    "Portal and Customer 360 consumers request subscription "
                    "snapshots from this owner."
                ),
                fallback_retirement=(
                    "Page-local freshness thresholds and subscriber-wide session "
                    "reuse are absent."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_network_sot_services.py",
                "tests/test_portal_account_health.py",
            ),
        ),
    ),
    SOTService(
        name="network.ont_runtime_status",
        module="app.services.network.ont_runtime_status",
        owns=(
            "Huawei ONT runtime-status poll observations",
            "Huawei OLT bulk-status pollability predicate",
            "Huawei OLT bulk-status poll task admission",
        ),
        depends_on=("runtime.infrastructure_polling",),
        notes=(
            "Owns recurring and stale-read-triggered Huawei bulk status "
            "polls as retry-safe infrastructure observations. These polls "
            "do not create tracked device operations; operator-requested "
            "single-ONT commands remain owned by operation dispatch."
        ),
    ),
    SOTService(
        name="network.device_state",
        module="app.services.device_operational_status",
        owns=(
            "binary NOC-facing device operational outcome",
            "device operational status vocabulary and reason classification",
            "device verification-due, impairment, and alarm classification",
        ),
        depends_on=(
            "network.monitoring_inventory",
            "runtime.infrastructure_polling",
            "network.ont_runtime_status",
        ),
        notes=(
            "Returns exactly working or not_working. Observation age is "
            "an internal verification-due input; reason distinguishes "
            "confirmed failure, administrative lifecycle, impairment, "
            "and inability to verify without adding a public freshness "
            "state. Required verification collectors are permanent."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="binary NOC-facing device operational outcome",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "device administrative lifecycle",
                        "native reachability observations",
                        "ONT runtime observations",
                        "monitoring path observations",
                    ),
                ),
                ConcernContract(
                    name=(
                        "device operational status vocabulary and reason classification"
                    ),
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "device administrative lifecycle",
                        "native reachability observations",
                        "ONT runtime observations",
                        "monitoring path observations",
                    ),
                ),
                ConcernContract(
                    name=(
                        "device verification-due, impairment, and alarm classification"
                    ),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "device administrative lifecycle",
                        "native reachability observations",
                        "ONT runtime observations",
                        "monitoring path observations",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="device administrative lifecycle",
                    owner="network.monitoring_inventory",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "NetworkDevice, OLTDevice, OntUnit, NAS, router, "
                        "and CPE administrative lifecycle fields"
                    ),
                ),
                AuthorityInput(
                    name="native reachability observations",
                    owner="runtime.infrastructure_polling",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "timestamped ping, poll, and health observations; "
                        "live_status_at is derived transition/dwell evidence, "
                        "not an observation timestamp"
                    ),
                ),
                AuthorityInput(
                    name="ONT runtime observations",
                    owner="network.ont_runtime_status",
                    kind=AuthorityKind.OBSERVATION,
                    source=("timestamped OLT status and ACS inform observations"),
                ),
                AuthorityInput(
                    name="monitoring path observations",
                    owner="external:wireguard",
                    kind=AuthorityKind.EXTERNAL_OBSERVATION,
                    source=(
                        "timestamped WireGuard handshake and allowed-IP "
                        "routing facts normalized by monitoring_coverage"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Callers supply already-visible observation models; "
                    "the resolver performs no writes or transaction completion."
                ),
                locking=(
                    "No row locks; one resolution reflects one caller-visible "
                    "observation snapshot."
                ),
                idempotency=(
                    "The same lifecycle, observations, path coverage, and "
                    "evaluation time produce the same typed outcome."
                ),
                retries=(
                    "The resolver has no side effects; permanent collectors "
                    "retry verification and later projections repair drift."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="web, API, projection, and task adapters",
                fail_closed_on=(
                    "missing verification",
                    "expired verification",
                    "unavailable verification path",
                    "inconclusive verification",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="device UI/API read services",
                new_owner="network.device_state",
                verification=(
                    "architecture and behavior tests enforce the binary "
                    "vocabulary and permanent verifier tasks"
                ),
                cutover_gate="migration 412 backfills and constrains projections",
                fallback_retirement=(
                    "retry-pending, freshness, degraded, maintenance, and "
                    "unknown public operational branches removed"
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/DEVICE_OPERATIONAL_STATUS.md",
                "docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md",
            ),
            test_refs=(
                "tests/test_device_operational_status.py",
                "tests/test_operational_status_per_type.py",
                "tests/architecture/test_binary_device_operational_lifecycle.py",
            ),
        ),
    ),
    SOTService(
        name="network.ont_status_refresh",
        module="app.services.network.ont_status_refresh",
        owns=(
            "stale ONT runtime-status refresh admission",
            "OLT-level status refresh rate limiting",
            "safe background refresh request projection",
        ),
        depends_on=(
            "network.device_state",
            "network.ont_runtime_status",
        ),
        notes=(
            "Read surfaces may request refresh through this owner, but "
            "must not poll OLTs directly. Huawei refreshes are admitted "
            "through the infrastructure observation polling owner as "
            "bounded OLT-level jobs; UISP-managed ONTs remain owned by "
            "the UISP topology sync source."
        ),
    ),
    SOTService(
        name="network.device_projection",
        module="app.services.device_projection_reconcile",
        owns=(
            "device_projections materialised table",
            "unified cross-type device row (OLT/core/ONT/CPE)",
            "projected binary operational status and repair evidence",
            "device projection orphan pruning",
        ),
        depends_on=(
            "network.device_state",
            "network.monitoring_inventory",
            "network.identity",
        ),
        notes=(
            "Sole canonical writer of device_projections. Delegates the "
            "multi-source device derivation to collect_devices and "
            "projects one materialised row per device so the admin device "
            "list can search/filter/sort/paginate in SQL. The table is a "
            "rebuildable cache: reconcile is idempotent, stamps "
            "refreshed_at, and prunes rows whose source device is gone. "
            "Pruning follows existence, not admission: a deactivated "
            "device is still projected, marked lifecycle_state="
            "'inactive', so deactivation cannot erase it from the staff "
            "ledger. Reviewed core-device archival is projected as "
            "lifecycle_state='archived' and hidden only from the default "
            "current-device cohort. Release gate — a non-active device can never "
            "project 'working'; the reconciler normalises it and a "
            "CHECK constraint makes the violation unrepresentable. "
            "Its scheduled repair is permanent: settings and feature "
            "controls may tune cadence but cannot disable convergence. "
            "Readers never write it; they request a reconcile rather "
            "than maintaining a parallel derivation path."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="device_projections materialised table",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "canonical device identity",
                        "monitoring inventory observations",
                        "resolved operational device state",
                    ),
                    canonical_writer="network.device_projection",
                ),
                ConcernContract(
                    name="unified cross-type device row (OLT/core/ONT/CPE)",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "canonical device identity",
                        "monitoring inventory observations",
                    ),
                    canonical_writer="network.device_projection",
                ),
                ConcernContract(
                    name="projected binary operational status and repair evidence",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "resolved operational device state",
                        "monitoring inventory observations",
                    ),
                    canonical_writer="network.device_projection",
                ),
                ConcernContract(
                    name="device projection orphan pruning",
                    role=OwnerRole.RECONCILER,
                    input_names=("canonical device identity",),
                    canonical_writer="network.device_projection",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical device identity",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "OLTDevice, NetworkDevice, OntUnit, and CpeDevice "
                        "natural identities"
                    ),
                ),
                AuthorityInput(
                    name="monitoring inventory observations",
                    owner="network.monitoring_inventory",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "active device inventory, address, vendor, model, "
                        "and last-seen facts consumed by collect_devices"
                    ),
                ),
                AuthorityInput(
                    name="resolved operational device state",
                    owner="network.device_state",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=("collect_devices operational status and reason derivation"),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "reconcile_device_projections enters the verified "
                    "owner-command boundary on a transaction-free session; "
                    "the projection and outbox event commit atomically "
                    "before return."
                ),
                locking=(
                    "A PostgreSQL transaction advisory lock serializes full "
                    "rebuilds; uq_device_projection_source arbitrates each "
                    "device_type/source_id natural key."
                ),
                idempotency=(
                    "The natural-key upsert and orphan-pruning pass converges "
                    "to the authoritative input set; one Celery delivery "
                    "keeps its task-derived command/idempotency key across "
                    "retries without duplicating rows."
                ),
                retries=(
                    "The task retries SQLAlchemy OperationalError up to three "
                    "times with bounded exponential backoff and a fresh "
                    "session; a later scheduled pass repairs stale state."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.device_projection.invalid_command",
                    "network.device_projection.invalid_command_context",
                    "network.device_projection.command_contract_violation",
                    "network.device_projection.nested_owner_command",
                    "network.device_projection.active_caller_transaction",
                    "network.device_projection.nested_transaction_completion",
                ),
                mapping_owner="app.tasks.device_projection",
                fail_closed_on=(
                    "invalid command metadata",
                    "active caller transaction",
                    "nested command or transaction completion",
                    "manifest mismatch",
                ),
            ),
            events=EventContract(
                event_types=("device_projection.reconciled",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Additive payload evolution within schema version 1; "
                    "breaking changes require a new version."
                ),
                replay=(
                    "Event-store delivery is retryable; consumers key side "
                    "effects by event_id and treat reconciliation counts as "
                    "immutable evidence."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="device_projections",
                    input_names=(
                        "canonical device identity",
                        "monitoring inventory observations",
                        "resolved operational device state",
                    ),
                    writer="network.device_projection",
                    freshness=(
                        "Celery beat targets a 60-second rebuild interval; "
                        "every row carries reconciled refreshed_at."
                    ),
                    stale_behavior=(
                        "Readers keep the owner-resolved binary outcome. "
                        "Projection age may trigger repair or diagnostics "
                        "but never becomes a public device state."
                    ),
                    drift_signal=(
                        "Reconcile logs inserted, updated, and pruned counts; "
                        "latest_refreshed_at exposes projection age."
                    ),
                    rebuild_operation=(
                        "app.services.device_projection_reconcile."
                        "reconcile_device_projections"
                    ),
                    repair_owner="network.device_projection",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.device_projection",
            ),
            steward="network operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
                "docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md",
            ),
            test_refs=(
                "tests/test_owner_commands.py",
                "tests/test_device_projection_reconcile.py",
                "tests/test_device_projection_task.py",
                "tests/architecture/test_owner_command_boundary.py",
                "tests/architecture/test_scheduler_boolean_control_boundary.py",
            ),
        ),
    ),
)
