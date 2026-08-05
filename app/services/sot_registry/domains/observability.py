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
