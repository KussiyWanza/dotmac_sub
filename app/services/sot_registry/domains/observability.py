"""Canonical SOT declarations for the observability domain."""

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
