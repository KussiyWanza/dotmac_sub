"""Canonical SOT declarations for the observability domain."""

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
    domain="observability",
    setting_domains=(
        "audit",
        "bandwidth",
    ),
    services=(
        SOTService(
            name="observability.audit_log",
            module="app.services.audit",
            owns=(
                "audit event persistence and queries",
                "request audit payload redaction",
                "staged and deferred audit recording",
            ),
            notes=(
                "AuditEvents is the sole AuditEvent constructor and query owner. "
                "Kernel audit R1 keeps legacy metadata and forensic columns live "
                "while every sanctioned writer dual-populates details; migration "
                "524 adds actor_party_id, details, and created_at without an "
                "authority transfer or kernel-lineage stamp. The aggregate-only "
                "r1_parity query owns drift detection during expansion."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="audit event persistence and queries",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed audit evidence",
                            "persisted audit rows",
                        ),
                        canonical_writer="observability.audit_log",
                    ),
                    ConcernContract(
                        name="request audit payload redaction",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "request forensic observation",
                            "audit actor and redaction contract",
                        ),
                    ),
                    ConcernContract(
                        name="staged and deferred audit recording",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "typed audit evidence",
                            "audit actor and redaction contract",
                        ),
                        canonical_writer="observability.audit_log",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed audit evidence",
                        owner="observability.audit_log",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "AuditRecord and AuditEventCreate values normalized at "
                            "the one AuditEvents model-construction boundary"
                        ),
                    ),
                    AuthorityInput(
                        name="persisted audit rows",
                        owner="observability.audit_log",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="the audit_events table built by Sub's Alembic chain",
                    ),
                    AuthorityInput(
                        name="request forensic observation",
                        owner="runtime.db_sessions",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "authenticated request actor state, request id, response "
                            "status, client address, user agent, path and redacted query"
                        ),
                    ),
                    AuthorityInput(
                        name="audit actor and redaction contract",
                        owner="observability.audit_log",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "the closed system/user/api_key/service actor rules and "
                            "the request sensitive-key allow/deny policy"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "The canonical stage surface adds only to the calling "
                        "owner's transaction. Deferred record runs only after the "
                        "source transaction commits. Standalone immediate recording "
                        "is legacy shadow debt and cannot be invoked from an active "
                        "owner command."
                    ),
                    locking=(
                        "Append-only row inserts need no application lock; the audit "
                        "row UUID is the database uniqueness arbiter."
                    ),
                    idempotency=(
                        "The audit owner does not deduplicate business decisions. The "
                        "calling owner supplies stable entity/correlation evidence and "
                        "owns command idempotency."
                    ),
                    retries=(
                        "Staged writes roll back with the caller. Deferred writes run "
                        "after commit and remain independently retryable; parity drift "
                        "fails the R1 gate instead of being silently repaired."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "observability.audit_log.invalid_actor",
                        "observability.audit_log.persistence_failed",
                        "observability.audit_log.parity_drift",
                    ),
                    mapping_owner="calling owner adapters and audit R1 operators",
                    retryable_codes=("observability.audit_log.persistence_failed",),
                    fail_closed_on=(
                        "a non-system actor without a non-empty identifier",
                        "an actor type outside the kernel-owned closed taxonomy",
                        "R1 details or actor parity drift",
                    ),
                ),
                events=EventContract(
                    event_types=("audit.event.recorded",),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility=(
                        "R1 is additive: legacy columns remain readable while details, "
                        "actor_party_id and created_at are added and dual-written."
                    ),
                    replay=(
                        "The audit_events rows are the durable event record. Readers "
                        "replay from persisted rows; no transport delivery is required."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner=(
                        "legacy direct AuditEvent constructors and mixed immediate/"
                        "deferred recording surfaces"
                    ),
                    new_owner="observability.audit_log",
                    verification=(
                        "AST writer ratchet, PostgreSQL 523-to-524 rehearsal, and the "
                        "aggregate-only audit R1 parity report"
                    ),
                    cutover_gate=(
                        "released kernel a42 exact pin plus observed post-R1 rows with "
                        "zero parity mismatches"
                    ),
                    fallback_retirement=(
                        "retire the remaining standalone immediate-write compatibility "
                        "path after all adapters stage or defer through typed contracts"
                    ),
                ),
                steward="platform operations",
                design_refs=(
                    "docs/audits/AUDIT_R1_KERNEL_INTEGRATION.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/architecture/test_audit_writer_surfaces.py",
                    "tests/integration/test_audit_r1_migration.py",
                    "tests/test_audit_r1_parity.py",
                    "tests/test_transactional_audit_events.py",
                ),
            ),
        ),
        SOTService(
            name="observability.recording",
            module="app.services.observability",
            owns=(
                "task/job run recording",
                "operational findings",
                "bounded state snapshot publication",
            ),
        ),
        SOTService(
            name="observability.database_diagnostics",
            module="app.services.db_error_observability",
            owns=(
                "redacted database schema-error correlation",
                "redacted idle-transaction failure correlation",
            ),
            depends_on=("observability.recording",),
            notes=(
                "Records request ID, application caller, SQLSTATE, safe missing "
                "identifier, and a statement fingerprint. SQL text, parameters, "
                "and result data are never logged."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="redacted database schema-error correlation",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "database driver failure observation",
                            "request correlation context",
                        ),
                    ),
                    ConcernContract(
                        name="redacted idle-transaction failure correlation",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "database driver failure observation",
                            "request correlation context",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="database driver failure observation",
                        owner="runtime.db_sessions",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "SQLAlchemy handle_error context and original "
                            "driver SQLSTATE"
                        ),
                    ),
                    AuthorityInput(
                        name="request correlation context",
                        owner="observability.recording",
                        kind=AuthorityKind.OBSERVATION,
                        source="request ID context and application call stack",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "The SQLAlchemy error hook observes an already-failed "
                        "operation and writes structured logs only."
                    ),
                    locking="No application lock or database write is performed.",
                    idempotency=(
                        "Repeated failures emit independent observations with "
                        "the same stable statement fingerprint."
                    ),
                    retries=(
                        "The observer never retries database work; the owning "
                        "caller retains retry policy."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="database caller adapters and task owners",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="observability.database_diagnostics",
                    verification=(
                        "Fingerprint redaction and caller-correlation tests."
                    ),
                ),
                steward="platform operations",
                design_refs=(
                    "docs/designs/OPERATIONAL_EVIDENCE_AND_RETRY.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=("tests/test_operational_evidence_followup.py",),
            ),
        ),
        SOTService(
            name="observability.database_transaction_spans",
            module="app.services.session_hooks",
            owns=(
                "root database transaction duration observations",
                "slow database transaction alert thresholds",
            ),
            depends_on=("runtime.db_sessions", "observability.metrics"),
            notes=(
                "Measures from the first SQLAlchemy root-transaction statement "
                "through completion. Metrics have no request or customer labels; "
                "structured logs retain only the request correlation ID."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="root database transaction duration observations",
                        role=OwnerRole.RESOLVER,
                        input_names=("root transaction lifecycle observation",),
                    ),
                    ConcernContract(
                        name="slow database transaction alert thresholds",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "root transaction duration observation",
                            "fixed slow transaction thresholds",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="root transaction lifecycle observation",
                        owner="runtime.db_sessions",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "SQLAlchemy root after_begin and "
                            "after_transaction_end lifecycle events"
                        ),
                    ),
                    AuthorityInput(
                        name="root transaction duration observation",
                        owner="observability.database_transaction_spans",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "monotonic elapsed seconds exported as a bounded "
                            "Prometheus histogram and slow counter"
                        ),
                    ),
                    AuthorityInput(
                        name="fixed slow transaction thresholds",
                        owner="observability.database_transaction_spans",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "30-second application slow threshold and reviewed "
                            "Prometheus warning/critical rule expressions"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "Session lifecycle hooks observe completion and update "
                        "process-local metrics/logs without opening a database "
                        "transaction. Prometheus evaluates alerts externally."
                    ),
                    locking="No application or database lock is acquired.",
                    idempotency=(
                        "Each completed root transaction emits exactly one duration "
                        "observation and at most one slow-counter increment."
                    ),
                    retries="The observer never retries or alters caller work.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="platform monitoring rule evaluator",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="observability.database_transaction_spans",
                    verification=(
                        "Session-hook metric tests, alert-rule contract tests, and "
                        "affected read query-budget tests."
                    ),
                ),
                steward="platform operations",
                design_refs=(
                    "docs/runbooks/DATABASE_TRANSACTION_PRESSURE.md",
                    "docs/designs/OPERATIONS_MEASUREMENT_STRATEGY.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_database_pressure_metrics.py",
                    "tests/test_team_inbox_sot_completion.py",
                    "tests/test_customer_network_path.py",
                    "tests/architecture/test_customer_detail_panel_budget.py",
                    "tests/architecture/test_database_transaction_alerts.py",
                ),
            ),
        ),
        SOTService(
            name="observability.channel_health_contracts",
            module="app.services.channel_health_contracts",
            owns=(
                "sensitive channel monitoring activation",
                "channel active-window interpretation",
                "natural and synthetic silence thresholds",
                "channel alert severity contract",
            ),
            depends_on=(
                "communications.team_inbox_commands",
                "observability.recording",
            ),
        ),
        SOTService(
            name="observability.task_reliability",
            module="app.services.task_reliability",
            owns=("task reliability classification", "stale-run alerts"),
            depends_on=("observability.recording",),
        ),
        SOTService(
            name="observability.metrics",
            module="app.metrics",
            owns=(
                "runtime counters",
                "runtime gauges",
                "state snapshot scrape export",
            ),
            depends_on=("observability.recording",),
        ),
    ),
    entrypoints=("app.tasks.*", "app.main", "app.services.*"),
    rule="Tasks and service loops record lifecycle through observability "
    "helpers instead of writing heartbeat/run state directly. Metrics "
    "collectors read counters or bounded snapshots; unbounded business "
    "queries run only in scheduled single-flight producers.",
)
