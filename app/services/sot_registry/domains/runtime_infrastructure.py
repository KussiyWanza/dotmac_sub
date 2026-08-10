"""Canonical SOT declarations for the runtime_infrastructure domain."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="runtime_infrastructure",
    services=(
        SOTService(
            name="runtime.realtime_projection",
            module="app.services.realtime_platform",
            owns=(
                "versioned real-time event envelope",
                "Redis topic naming and best-effort publication",
                "shared WebSocket and SSE delivery semantics",
                "reconnect and no-replay refresh contract",
            ),
            depends_on=("auth.permission_gate",),
            notes=(
                "Real-time events are non-durable projections after an "
                "owning domain commits state. Redis pub/sub is at-most-once; "
                "clients refetch canonical read models after reconnect or "
                "reset. Client-selected topics are authorized by "
                "app.services.realtime_subscriptions; workqueue topics are "
                "derived by its scope owner. WebSocket and SSE modules are "
                "transport adapters only."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="versioned real-time event envelope",
                        role=OwnerRole.POLICY,
                        input_names=("real-time schema contract",),
                    ),
                    ConcernContract(
                        name="Redis topic naming and best-effort publication",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "real-time schema contract",
                            "committed projection request",
                            "Redis delivery availability observation",
                        ),
                    ),
                    ConcernContract(
                        name="shared WebSocket and SSE delivery semantics",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "real-time schema contract",
                            "authorized subscription topics",
                            "Redis delivery availability observation",
                        ),
                    ),
                    ConcernContract(
                        name="reconnect and no-replay refresh contract",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "real-time schema contract",
                            "Redis delivery availability observation",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="real-time schema contract",
                        owner="runtime.realtime_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "RealtimeEvent schema version, event/topic validation, "
                            "and refresh_required semantics"
                        ),
                    ),
                    AuthorityInput(
                        name="committed projection request",
                        owner="runtime.realtime_projection",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "best-effort invalidation request accepted only after the "
                            "calling domain owner commits durable state"
                        ),
                    ),
                    AuthorityInput(
                        name="authorized subscription topics",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "object-level conversation or operation topic decision "
                            "from app.services.realtime_subscriptions"
                        ),
                    ),
                    AuthorityInput(
                        name="Redis delivery availability observation",
                        owner="external:redis",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Redis publish and pub/sub connection outcome; never a "
                            "durable delivery receipt"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "The platform never opens or completes a domain database "
                        "transaction; callers publish only after durable state commits."
                    ),
                    locking=(
                        "Immutable envelopes and topic strings require no database "
                        "lock; Redis pub/sub provides no durable ordering lock."
                    ),
                    idempotency=(
                        "Event identifiers distinguish repeated invalidations, while "
                        "clients converge by refetching the canonical read model."
                    ),
                    retries=(
                        "Publication failure returns false and is not retried inside "
                        "the domain transaction; reconnecting clients refetch state."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="WebSocket and SSE transport adapters",
                    fail_closed_on=(
                        "invalid event envelope",
                        "unauthorized client-selected topic",
                        "channel and envelope topic mismatch",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.websocket.realtime and transport-specific Redis "
                        "publication helpers"
                    ),
                    new_owner="runtime.realtime_projection",
                    verification=(
                        "Shared envelope, broker, subscription authorization, and "
                        "transport-boundary tests."
                    ),
                    cutover_gate=(
                        "All application publishers use realtime_platform and no "
                        "domain service imports app.websocket transports."
                    ),
                    fallback_retirement=(
                        "The legacy inbox_ws broker prefix and app.websocket.realtime "
                        "publication module are removed."
                    ),
                ),
                steward="platform runtime",
                design_refs=(
                    "docs/REALTIME_PLATFORM.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_realtime_platform.py",
                    "tests/test_realtime_subscriptions.py",
                    "tests/architecture/test_realtime_platform_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="runtime.db_sessions",
            module="app.services.db_session_adapter",
            owns=(
                "background DB session lifecycle",
                "read/write task session boundaries",
                "Postgres advisory lock ownership",
            ),
        ),
        SOTService(
            name="runtime.task_idempotency",
            module="app.services.task_idempotency",
            owns=("task idempotency keys", "duplicate task suppression"),
            depends_on=("runtime.db_sessions",),
        ),
        SOTService(
            name="runtime.task_heartbeat",
            module="app.services.task_heartbeat",
            owns=("task success heartbeat", "single-flight skip streaks"),
            depends_on=("observability.recording",),
        ),
        SOTService(
            name="runtime.infrastructure_polling",
            module="app.services.infrastructure_polling",
            owns=(
                "shared native reachability poll observations",
                "generic network-device pollable predicate",
                "poll heartbeat result counters",
            ),
            depends_on=("runtime.db_sessions",),
            notes=(
                "Polling and topology warming use reserved monitoring-queue "
                "capacity. Bulk ingestion, including independently bounded "
                "per-OLT MAC harvests, does not share that worker; queue "
                "placement does not change observation ownership."
            ),
        ),
        SOTService(
            name="runtime.infrastructure_health",
            module="app.services.infrastructure_health",
            owns=(
                "dependency health checks",
                "Postgres/Redis/VM/Celery infrastructure status",
                "scheduled bounded dependency health snapshot",
            ),
            depends_on=("runtime.db_sessions", "control.settings_spec"),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="dependency health checks",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "dependency probe observations",
                            "health probe configuration",
                        ),
                    ),
                    ConcernContract(
                        name="Postgres/Redis/VM/Celery infrastructure status",
                        role=OwnerRole.RESOLVER,
                        input_names=("dependency probe observations",),
                    ),
                    ConcernContract(
                        name="scheduled bounded dependency health snapshot",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "resolved dependency health status",
                            "scheduled probe cadence",
                            "Redis projection availability",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="dependency probe observations",
                        owner="external:runtime_dependencies",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Bounded PostgreSQL, Redis, VictoriaMetrics, GenieACS, "
                            "RADIUS, MinIO, Celery, and Nominatim probe responses."
                        ),
                    ),
                    AuthorityInput(
                        name="health probe configuration",
                        owner="runtime.infrastructure_health",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Checked-in probe set, timeouts, queue expectations, "
                            "and validated disabled-check configuration."
                        ),
                    ),
                    AuthorityInput(
                        name="resolved dependency health status",
                        owner="runtime.infrastructure_health",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "Typed ServiceStatus results produced by the complete "
                            "bounded scheduled probe run."
                        ),
                    ),
                    AuthorityInput(
                        name="scheduled probe cadence",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "stale_infrastructure_check_enabled and the validated "
                            "stale_infrastructure_check_interval_seconds setting."
                        ),
                    ),
                    AuthorityInput(
                        name="Redis projection availability",
                        owner="external:redis",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Shared application-cache write/read outcome; Redis is a "
                            "projection transport and never health authority."
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The task adapter owns a bounded read session for the "
                        "PostgreSQL probe and closes it before publishing the shared "
                        "cache projection; dashboard requests only read the snapshot."
                    ),
                    locking=(
                        "No business rows are mutated or locked; the latest cache key "
                        "is atomically replaced by one complete scheduled result."
                    ),
                    idempotency=(
                        "Repeating a probe run replaces the latest projection with the "
                        "same typed service cohort and a new observed timestamp."
                    ),
                    retries=(
                        "The scheduler retries on its next bounded cadence; individual "
                        "dependency failures become status facts and never trigger an "
                        "unbounded in-task retry."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner=(
                        "monitoring task and admin dashboard transport adapters"
                    ),
                    fail_closed_on=(
                        "missing snapshot",
                        "malformed snapshot",
                        "unsupported snapshot schema",
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="scheduled bounded dependency health snapshot",
                        input_names=(
                            "resolved dependency health status",
                            "scheduled probe cadence",
                            "Redis projection availability",
                        ),
                        writer="runtime.infrastructure_health",
                        freshness=(
                            "Observed at completion of the scheduled probe; fresh for "
                            "ten minutes against the default five-minute cadence."
                        ),
                        stale_behavior=(
                            "Dashboard labels last-known values stale after ten minutes; "
                            "missing or malformed data renders unavailable and never "
                            "runs probes in the request."
                        ),
                        drift_signal=(
                            "Snapshot age, cache publication failure, malformed schema, "
                            "or a missing scheduled-task heartbeat."
                        ),
                        rebuild_operation=(
                            "Run check_stale_infrastructure to execute the bounded probe "
                            "cohort and atomically republish the latest snapshot."
                        ),
                        repair_owner="runtime.infrastructure_health",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "dashboard request-time check_all_services calls and each "
                        "web worker's short-lived process cache"
                    ),
                    new_owner="runtime.infrastructure_health",
                    verification=(
                        "Snapshot contract, task publication, dashboard no-probe, "
                        "freshness UI, and architecture manifest tests."
                    ),
                    cutover_gate=(
                        "Dashboard infrastructure routes load only the scheduled "
                        "shared snapshot."
                    ),
                    fallback_retirement=(
                        "Request-time dependency probes and the per-process dashboard "
                        "infrastructure cache are removed."
                    ),
                ),
                steward="platform runtime",
                design_refs=(
                    "docs/designs/OPERATIONS_MEASUREMENT_STRATEGY.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_infrastructure_health.py",
                    "tests/test_database_pressure_metrics.py",
                    "tests/test_web_admin_dashboard_infrastructure.py",
                    "tests/architecture/test_dashboard_infrastructure_snapshot_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.tasks.*",
        "app.main",
        "app.websocket.*",
        "app.api.workqueue",
        "app.services.scheduler_config",
        "app.web.admin.system",
    ),
    rule="Real-time delivery projects already-committed state and never "
    "becomes a decision owner or durable event log. Infrastructure "
    "tasks use shared DB/session/lock and heartbeat helpers; polling "
    "writes observations while network/device resolvers interpret state.",
)
