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
            ),
            depends_on=("runtime.db_sessions",),
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
