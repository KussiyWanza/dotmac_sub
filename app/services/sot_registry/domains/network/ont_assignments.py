"""network SOT declarations: ont assignments."""

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
        name="network.ont_topology_observations",
        module="app.services.network.ont_topology_observations",
        owns=(
            "durable allowlisted electronic-topology observations",
            "non-destructive initialization of empty ONT OLT/PON edges",
            "exact observed PON inventory initialization with provenance",
            "observation agreement and manual-review evidence",
        ),
        depends_on=("network.fiber_topology",),
        notes=(
            "UISP collectors submit exact OLT and numeric PON evidence; "
            "Huawei authorization submits exact modeled F/S/P evidence. "
            "Only UISP numeric evidence may initialize missing PON "
            "inventory. Both sources may fill empty ONT edges, but never "
            "overwrite or merge an existing identity edge. Conflicts "
            "remain durable review evidence for "
            "network.ont_assignment_identity."
        ),
    ),
    SOTService(
        name="network.ont_assignment_commands",
        module="app.services.network.ont_assignment_commands",
        owns=(
            "normal explicit ONT-to-subscription assignments",
            "normal assignment release transitions",
            "verified physical PON move projections",
            "exact normal assignment audit results",
        ),
        depends_on=(
            "network.identity",
            "network.ont_topology_observations",
        ),
        notes=(
            "Normal provisioning requires an exact ONT, subscription, "
            "and modeled PON. The subscriber is derived only through the "
            "subscription bridge. MAC, name, address, work-order, map, "
            "and registration inference cannot select identity. Existing "
            "disagreements fail closed into reviewed repair."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="normal explicit ONT-to-subscription assignments",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical ONT inventory identity",
                        "active subscriber account",
                        "active subscription lifecycle",
                        "active ONT service assignment",
                    ),
                    canonical_writer="network.ont_assignment_commands",
                ),
                ConcernContract(
                    name="normal assignment release transitions",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical ONT inventory identity",
                        "active ONT service assignment",
                    ),
                    canonical_writer="network.ont_assignment_commands",
                ),
                ConcernContract(
                    name="verified physical PON move projections",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "canonical ONT inventory identity",
                        "modeled PON and OLT identity",
                        "active ONT service assignment",
                    ),
                    canonical_writer="network.ont_assignment_commands",
                ),
                ConcernContract(
                    name="exact normal assignment audit results",
                    role=OwnerRole.PROJECTION_WRITER,
                    input_names=(
                        "canonical ONT inventory identity",
                        "active ONT service assignment",
                    ),
                    canonical_writer="network.ont_assignment_commands",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical ONT inventory identity",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="OntUnit exact serial, MAC, active state, OLT, and PON identity",
                ),
                AuthorityInput(
                    name="modeled PON and OLT identity",
                    owner="network.ont_topology_observations",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="PonPort and OLTDevice modeled identity accepted by topology owners",
                ),
                AuthorityInput(
                    name="active subscriber account",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Subscriber active lifecycle state",
                ),
                AuthorityInput(
                    name="active subscription lifecycle",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Subscription owner and active lifecycle state",
                ),
                AuthorityInput(
                    name="active ONT service assignment",
                    owner="network.ont_assignment_commands",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="OntAssignment active row and assignment history",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Public typed commands enter execute_owner_command once; "
                    "nested assignment/release helpers use flush and the owner "
                    "boundary commits or rolls back the complete transition."
                ),
                locking=(
                    "Lock subscriber, subscription, current assignment, current "
                    "ONT, target ONT, and active assignment rows before deciding."
                ),
                idempotency=(
                    "Repeated identical reassignment returns the existing target "
                    "assignment when the supplied current assignment was already "
                    "released by the same transition."
                ),
                retries="Fail closed on stale or ambiguous state; callers may retry after refresh.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes(
                        "network.ont_assignment_commands"
                    ),
                    "network.ont_assignment_commands.invalid_identity",
                    "network.ont_assignment_commands.not_found",
                    "network.ont_assignment_commands.conflict",
                    "network.ont_assignment_commands.stale_assignment",
                ),
                mapping_owner="web/API adapters",
                fail_closed_on=(
                    "missing subscriber, inactive subscription, stale assignment, "
                    "assigned target ONT, missing OLT/PON identity",
                ),
            ),
            events=EventContract(
                event_types=(
                    "network.ont_assignment.assigned",
                    "network.ont_assignment.released",
                    "network.ont_assignment.reassigned",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="Audit metadata preserves exact result evidence for current consumers.",
                replay="Commands are idempotent for unchanged exact input; outbox/audit replay is evidence-only.",
            ),
            projections=(
                ProjectionContract(
                    name="verified physical PON move projections",
                    input_names=(
                        "canonical ONT inventory identity",
                        "modeled PON and OLT identity",
                        "active ONT service assignment",
                    ),
                    writer="network.ont_assignment_commands",
                    freshness="Current at owner-command commit.",
                    stale_behavior="Stale supplied assignment identity fails closed.",
                    drift_signal="Assignment cutover audit and access-path gaps.",
                    rebuild_operation="Repeat exact command or use reviewed identity repair for conflicts.",
                    repair_owner="network.ont_assignment_commands",
                ),
                ProjectionContract(
                    name="exact normal assignment audit results",
                    input_names=(
                        "canonical ONT inventory identity",
                        "active ONT service assignment",
                    ),
                    writer="network.ont_assignment_commands",
                    freshness="Transactionally staged with assignment transition.",
                    stale_behavior="Missing evidence is detected by audit review.",
                    drift_signal="Audit result mismatch against active assignment state.",
                    rebuild_operation="Replay idempotent command or reviewed repair.",
                    repair_owner="network.ont_assignment_commands",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.ont_assignment_commands",
            ),
            steward="network operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
            ),
            test_refs=(
                "tests/test_ont_assignment_commands.py",
                "tests/architecture/test_ont_reassignment_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="network.ont_assignment_identity",
        module="app.services.network.ont_assignment_identity",
        owns=(
            "reviewed exceptional ONT-to-subscription identity repairs",
            "exact subscription/subscriber and ONT/PON/OLT repair projection",
            "duplicate assignment deactivation audit evidence",
            "exact electronic identity repair result evidence",
        ),
        depends_on=(
            "network.fiber_topology",
            "network.ont_topology_observations",
            "network.ont_assignment_commands",
        ),
        notes=(
            "Repairs bind exact assignment, subscription, PON, OLT, and "
            "conflict IDs. Subscriber, address, name, and registration "
            "inference are forbidden. Preview is read-only, review is "
            "independent, and execution revalidates under lock. This "
            "does not replace normal explicit service provisioning."
        ),
    ),
    SOTService(
        name="network.ont_assignment_cutover",
        module="app.services.network.ont_assignment_cutover",
        owns=(
            "read-only active ONT assignment invariant audit",
            "stable exact assignment blocker evidence",
            "assignment database-constraint cutover readiness gate",
        ),
        depends_on=(
            "network.ont_assignment_commands",
            "network.ont_assignment_identity",
        ),
        notes=(
            "The exhaustive audit reports persisted identifiers and "
            "routes every repair to independent identity review. It "
            "never chooses replacement identity, mutates assignments, "
            "or enables constraints. A clean report is necessary but "
            "does not itself authorize cutover."
        ),
    ),
    SOTService(
        name="network.ont_assignment_cutover_batches",
        module="app.services.network.ont_assignment_cutover_batches",
        owns=(
            "immutable operator-selected assignment cleanup manifests",
            "exact cutover report and finding evidence binding",
            "atomic independent batch review attestations",
            "delegated identity decision staging provenance",
        ),
        depends_on=(
            "network.ont_assignment_cutover",
            "network.ont_assignment_identity",
        ),
        notes=(
            "A batch binds one complete audit digest and each selected "
            "finding digest to explicit actions, targets, and conflict "
            "IDs. It stages and reviews identity-owner decisions "
            "atomically but has no execution operation; approved repairs "
            "remain individual locked identity commands."
        ),
    ),
    SOTService(
        name="network.ont_assignment_cutover_verification",
        module=("app.services.network.ont_assignment_cutover_verification"),
        owns=(
            "immutable post-execution cleanup verification attestations",
            "exact terminal identity-decision result snapshots",
            "fresh exhaustive assignment audit evidence binding",
            "batch-scope residual and global cutover-readiness evidence",
        ),
        depends_on=(
            "network.ont_assignment_cutover",
            "network.ont_assignment_cutover_batches",
            "network.ont_assignment_identity",
        ),
        notes=(
            "Verification copies exact terminal result payloads and "
            "hashes into an immutable evidence snapshot, then binds a "
            "fresh exhaustive audit. Pending decisions cannot be "
            "attested. The owner never executes repairs, mutates "
            "assignments, or enables constraints."
        ),
    ),
    SOTService(
        name="network.ont_assignment_cutover_coverage",
        module="app.services.network.ont_assignment_cutover_coverage",
        owns=(
            "read-only current cleanup-finding lineage reconciliation",
            "exact, superseded, and overlapping coverage classification",
            "current decision-result and verification-drift projection",
            "constraint-authorization review readiness evidence",
        ),
        depends_on=(
            "network.ont_assignment_cutover",
            "network.ont_assignment_cutover_batches",
            "network.ont_assignment_cutover_verification",
            "network.ont_assignment_identity",
        ),
        notes=(
            "One repeatable snapshot joins every current finding to all "
            "immutable proposal, review, result, and verification "
            "evidence. It keeps decision, current-scope, and verification "
            "state separate. Readiness is conservative evidence for a "
            "separate authorization review; this owner cannot execute "
            "repairs or authorize or enable constraints."
        ),
    ),
    SOTService(
        name="network.ont_assignment_constraint_authorization",
        module=("app.services.network.ont_assignment_constraint_authorization"),
        owns=(
            "immutable constraint-cutover authorization requests",
            "independent approve or decline authorization attestations",
            "authorization expiry and current-evidence projection",
            "exact target, coverage, and cutover evidence binding",
        ),
        depends_on=("network.ont_assignment_cutover_coverage",),
        notes=(
            "A request stores an explicitly named target, caller-chosen "
            "expiry, and the complete clean coverage snapshot. A different "
            "actor reviews the unchanged request. Approval becomes stale "
            "or expired by derivation and is only evidence for a separate "
            "reviewed DDL change; this owner has no DDL authority."
        ),
    ),
    SOTService(
        name="network.ont_inventory_release",
        module="app.services.network.ont_inventory_release",
        owns=(
            "return-to-inventory electronic identity release transition",
            "closure and de-identification of all ONT assignments",
            "post-cleanup ONT OLT/PON/F/S/P identity clearing",
        ),
        depends_on=(
            "network.ont_assignment_commands",
            "network.ont_assignment_identity",
            "network.ont_topology_observations",
        ),
        notes=(
            "The broader inventory orchestrator must complete external "
            "OLT/ACS cleanup first. This narrow owner locks the ONT and "
            "every assignment, selects no replacement, closes active "
            "assignments, and clears customer/subscription and electronic "
            "identity in one local transaction."
        ),
    ),
    SOTService(
        name="network.fiber_access_attachments",
        module="app.services.network.fiber_access_attachments",
        owns=(
            "reviewed PON-to-splitter input attachments",
            "reviewed splitter-output-to-downstream-input cascade links",
            "reviewed ONT-to-splitter output attachments",
            "canonical ONT splitter parent projection",
            "splitter stage and cumulative optical-loss evidence",
            "fiber access attachment result evidence",
        ),
        depends_on=(
            "network.fiber_topology",
            "network.fiber_connectivity_decisions",
            "network.ont_assignment_commands",
            "network.ont_assignment_identity",
        ),
        notes=(
            "Only exact directed ports in one rooted, acyclic splitter "
            "tree with agreeing ONT/PON/OLT identity can be attached. "
            "Cascade construction is root-first, removal is leaf-first, "
            "and each participating splitter has explicit insertion loss. "
            "Preview is read-only, review is independent, execution "
            "revalidates under lock, and stale inputs close without "
            "mutation. Geometry, cabinets, ratios, names, and legacy "
            "assignments never create an access edge."
        ),
    ),
    SOTService(
        name="network.access_path",
        module="app.services.network.access_path",
        owns=(
            "subscription access path",
            "last-mile path summary",
            "composed ONT-to-passive-fiber-to-NAS-to-core/border path",
            "typed cross-domain path gaps and combined evidence hash",
            "distinct provisioning-NAS and live-session-NAS evidence",
        ),
        depends_on=(
            "network.identity",
            "network.fiber_topology",
            "network.ont_assignment_commands",
            "network.ont_assignment_identity",
            "network.fiber_access_attachments",
            "network.fiber_physical_continuity",
            "network.forwarding_topology",
        ),
    ),
)
