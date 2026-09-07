"""Shared native infrastructure selection contract."""

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

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="network.infrastructure_catalogue",
        module="app.services.infrastructure_catalogue",
        owns=("native infrastructure selection and exact-reference resolution",),
        depends_on=("network.identity",),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="native infrastructure selection and exact-reference resolution",
                    role=OwnerRole.RESOLVER,
                    input_names=("native inventory identities",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="native inventory identities",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Native NAS, POP, AP, OLT, PON and FDH records; role and active flags qualify new selections",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary="Bounded typed search and exact-reference query in the caller's read or project-owner transaction",
                locking="No inventory writes; project foreign keys protect persisted referents",
                idempotency="Identical queries over identical inventory return identical ordered results",
                retries="Caller may retry transient database read failures",
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="customer and project adapters",
                fail_closed_on=("invalid query or reference",),
            ),
            projections=(
                ProjectionContract(
                    name="infrastructure picker options",
                    input_names=("native inventory identities",),
                    writer="network.infrastructure_catalogue",
                    freshness="Direct database read, no cached inventory",
                    stale_behavior="New selections reject inactive or role-mismatched records; historical labels may include inactive records",
                    drift_signal="Exact reference cannot resolve to its declared inventory kind",
                    rebuild_operation="Repeat the typed catalogue query",
                    repair_owner="network.infrastructure_catalogue",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="customer-page inventory search SQL",
                new_owner="network.infrastructure_catalogue",
                verification="Customer and project selector parity and unavailable-reference tests",
                cutover_gate="Both search adapters delegate to the shared query",
                fallback_retirement="Customer-specific search SQL removed",
            ),
            steward="network inventory and service delivery",
            design_refs=("docs/designs/PROJECT_INFRASTRUCTURE_SCOPE.md",),
            test_refs=(
                "tests/test_project_infrastructure.py",
                "tests/architecture/test_project_infrastructure_boundary.py",
            ),
        ),
    ),
)
