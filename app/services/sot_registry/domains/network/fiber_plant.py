"""network SOT declarations: fiber plant."""

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
        name="network.fiber_source_staging",
        module="app.services.network.fiber_topology_staging",
        owns=(
            "immutable fiber source manifests",
            "normalized staged fiber source facts",
            "non-authoritative duplicate and canonical-match suggestions",
        ),
        depends_on=("gis.spatial_sync",),
        notes=(
            "Staged map rows are observations with provenance. Match "
            "suggestions never mutate or retire canonical assets."
        ),
    ),
    SOTService(
        name="network.fiber_cost_items",
        module="app.services.fiber_cost_items",
        owns=(
            "fiber drop-cost components and their prices",
            "whether a drop estimate can be produced, and what it totals",
        ),
        notes=(
            "Currency is the deployment's own `billing/default_currency`, read "
            "as a setting rather than declared as a dependency: one estimate "
            "mixing currencies is meaningless, and the screen already labels "
            "the whole estimate with one. "
            "The components were four hardcoded settings, each restated in a "
            "spec, a service reader and the map template's JavaScript — so a "
            "new one meant editing three layers, and no layer owned the cost "
            "model. They are rows now. The estimate is computed here rather "
            "than in the browser, so the breakdown a user reads has one "
            "implementation. An active component with no price does not "
            "contribute and makes the estimate incomplete: a total built from "
            "only the priced components would be a number nobody chose, which "
            "is how the retired defaults came to quote NGN 85 for an ONT."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="fiber drop-cost components and their prices",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("operator-priced fiber cost components",),
                    canonical_writer="network.fiber_cost_items",
                ),
                ConcernContract(
                    name=(
                        "whether a drop estimate can be produced, and what it totals"
                    ),
                    role=OwnerRole.RESOLVER,
                    input_names=("operator-priced fiber cost components",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="operator-priced fiber cost components",
                    owner="network.fiber_cost_items",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "FiberCostItem rows: the component, how its amount is "
                        "applied, and whether it has been priced at all"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "create_item and update_item each enter execute_owner_command "
                    "once on a transaction-free session. The row, immutable "
                    "before/after audit evidence and durable change event stage "
                    "inside that transaction and commit together. Estimation is "
                    "a pure read over committed state and writes nothing."
                ),
                locking=(
                    "Updates lock the exact FiberCostItem row and compare the "
                    "submitted version before replacing any values. The unique "
                    "code constraint arbitrates concurrent creates."
                ),
                idempotency=(
                    "A create is refused on its stable unique code. An update is "
                    "bound to one expected row version, so a replay or stale form "
                    "cannot quietly reprice a component. An estimate is "
                    "deterministic for identical committed inputs."
                ),
                retries=(
                    "A duplicate create and a stale update fail with stable domain "
                    "codes. The operator must reload current evidence before "
                    "submitting a replacement decision."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes("network.fiber_cost_items"),
                    "network.fiber_cost_items.code_required",
                    "network.fiber_cost_items.invalid_code",
                    "network.fiber_cost_items.label_required",
                    "network.fiber_cost_items.label_too_long",
                    "network.fiber_cost_items.duplicate_code",
                    "network.fiber_cost_items.unknown_unit",
                    "network.fiber_cost_items.invalid_amount",
                    "network.fiber_cost_items.negative_amount",
                    "network.fiber_cost_items.amount_too_large",
                    "network.fiber_cost_items.description_too_long",
                    "network.fiber_cost_items.invalid_sort_order",
                    "network.fiber_cost_items.invalid_distance",
                    "network.fiber_cost_items.invalid_scope",
                    "network.fiber_cost_items.invalid_actor",
                    "network.fiber_cost_items.invalid_version",
                    "network.fiber_cost_items.stale_version",
                    "network.fiber_cost_items.not_found",
                ),
                mapping_owner="app.web.admin.network_fiber_costs",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.fiber_cost_items",
                # No `old_owner`: native authority has none by definition, and
                # the four settings this replaced were never an OWNER — they
                # were the same fact restated in three layers, which is why
                # nothing could be pointed at when the estimate went wrong.
                verification=(
                    "Per-metre and flat components sum correctly; an unpriced "
                    "active component makes the estimate incomplete rather "
                    "than contributing zero; an inactive unpriced component "
                    "does not; zero is a price and empty is not."
                ),
                fallback_retirement=(
                    "The four settings and their seed entries are removed in "
                    "the same change, so no parallel price source survives."
                ),
            ),
            events=EventContract(
                event_types=("fiber.cost_item_changed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "The AMOUNT is deliberately absent from the payload. A "
                    "subscriber that needs it asks for an estimate, which "
                    "keeps one reader of the price and stops what an install "
                    "costs travelling through a delivery pipeline with its own "
                    "retention and logging."
                ),
                replay=(
                    "Safe to replay: the event names what changed, not what it "
                    "became, and an estimate is recomputed from committed rows "
                    "on every request."
                ),
            ),
            steward="network operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=(
                "tests/test_fiber_cost_items.py",
                "tests/architecture/test_fiber_cost_items_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="network.fiber_topology",
        module="app.services.fiber_topology",
        owns=(
            "fiber asset identity and connectivity graph",
            "OLT-to-customer topology integrity",
            "ordered validated subscription fiber traces",
            "bounded fiber fault-candidate ranking",
            "cross-customer exact shared-cable fault candidates",
            "customer-trace evidence completeness",
        ),
        depends_on=(
            "network.identity",
            "gis.spatial_sync",
            "network.fiber_source_staging",
        ),
        notes=(
            "Electronic inventory and telemetry remain observations. "
            "Imported geometry is staged evidence until this owner "
            "validates asset identity and connectivity. Trace resolution "
            "fails closed on missing or ambiguous edges; fault candidates "
            "never declare incidents or redefine topology. Numeric cutover "
            "review readiness is owned by network.fiber_cutover_readiness."
        ),
    ),
    SOTService(
        name="network.as_built_plant_projection",
        module="app.services.network.as_built_plant_projection",
        owns=(
            "fiber segment projection of accepted vendor as-built evidence",
            "operator activation of the projected as-built fiber segment",
        ),
        depends_on=(
            "operations.vendor_project_records",
            "network.fiber_plant_integrity",
        ),
        notes=(
            "Accepted as-built geometry previously never reached the "
            "network record. This reconciler owns exactly one derived "
            "thing: the FiberSegment an accepted as-built represents and "
            "the as_built_routes.fiber_segment_id link. The evidence "
            "stays authoritative with operations.vendor_project_records, "
            "so a lost segment rebuilds from the accepted rows alone. It "
            "creates cable inactive, because the projection may not "
            "infer what the cable splices into. Because nothing else "
            "ever activated that row, every accepted as-built stayed "
            "invisible to the is_active-filtered plant and map reads, so "
            "this owner also owns activate_projected_segment: the one "
            "command that binds two operator-named terminations onto its "
            "own projected row and puts it in service. It reaches a "
            "segment only through the fiber_segment_id backlink of an "
            "accepted as-built, never activates anything another owner "
            "created, takes fiber count from the accepted evidence and "
            "refuses to contradict it, and submits the bound segment to "
            "network.fiber_plant_integrity so activation is held to the "
            "same rootedness, endpoint-identity, and exact-core rules as "
            "a reviewed fiber change. It never retires plant, never "
            "deactivates a segment, and never re-binds endpoints on one "
            "that is already active."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=(
                        "fiber segment projection of accepted vendor as-built evidence"
                    ),
                    role=OwnerRole.RECONCILER,
                    input_names=("accepted vendor as-built evidence",),
                    canonical_writer="network.as_built_plant_projection",
                ),
                ConcernContract(
                    name=(
                        "operator activation of the projected as-built fiber segment"
                    ),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "accepted vendor as-built evidence",
                        "active cable operational integrity ruling",
                    ),
                    canonical_writer="network.as_built_plant_projection",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="accepted vendor as-built evidence",
                    owner="operations.vendor_project_records",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the accepted AsBuiltRoute, its route geometry, "
                        "measured length, and line-item cable type and "
                        "fiber count"
                    ),
                ),
                AuthorityInput(
                    name="active cable operational integrity ruling",
                    owner="network.fiber_plant_integrity",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "validate_active_segment and "
                        "ensure_segment_strand_inventory, which decide "
                        "whether the operator-named terminations form an "
                        "exact PON-rooted component; activation fails "
                        "closed on their refusal"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The acceptance decision owns the transaction; the "
                    "projection stages the segment inside it. The repair "
                    "sweep owns its own commit, and so does "
                    "activate_projected_segment, which is one operator "
                    "decision rather than a step inside another."
                ),
                locking=(
                    "The accepted as-built row is already locked by the "
                    "review decision that triggers the projection."
                ),
                idempotency=(
                    "fiber_segment_id makes a replay refresh the same "
                    "segment rather than mint a second cable, and a "
                    "replayed activation returns already_active without "
                    "re-binding the endpoints."
                ),
                retries=(
                    "Safe to re-run: reconcile_accepted_as_builts is a "
                    "no-op for evidence already projected, and a refused "
                    "activation rolls back its own endpoint binding."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.as_built_plant_projection.as_built_not_found",
                    "network.as_built_plant_projection.activation_target_required",
                    "network.as_built_plant_projection.segment_not_projected",
                    "network.as_built_plant_projection.as_built_not_accepted",
                    "network.as_built_plant_projection.termination_point_not_found",
                    "network.as_built_plant_projection.termination_points_not_distinct",
                    "network.as_built_plant_projection.missing_route_geometry",
                    "network.as_built_plant_projection.missing_fiber_count",
                    "network.as_built_plant_projection."
                    "fiber_count_conflicts_with_evidence",
                    "network.as_built_plant_projection.plant_integrity_refused",
                ),
                mapping_owner="app.web.admin.network_fiber_plant",
                fail_closed_on=(
                    "network.as_built_plant_projection.plant_integrity_refused",
                ),
            ),
            events=EventContract(
                event_types=(
                    "vendor_as_built.accepted",
                    "fiber_segment.activated",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Consumes the acceptance event emitted by "
                    "operations.vendor_project_records; the projection "
                    "itself emits nothing, because a derived segment is "
                    "not a new fact about the world. Activation does "
                    "emit fiber_segment.activated: cable entering "
                    "service is a new operational fact, and it is the "
                    "moment the cable becomes visible to every "
                    "is_active-filtered plant and map read."
                ),
                replay=(
                    "Replaying an acceptance refreshes the same segment "
                    "through fiber_segment_id rather than creating one; "
                    "replaying an activation emits nothing further, "
                    "because the already_active path returns before the "
                    "event is staged."
                ),
            ),
            projections=(
                ProjectionContract(
                    name=(
                        "fiber segment projection of accepted vendor as-built evidence"
                    ),
                    input_names=("accepted vendor as-built evidence",),
                    writer="network.as_built_plant_projection",
                    freshness=(
                        "Written in the accepting transaction, so the "
                        "fiber map cannot lag an acceptance already "
                        "committed."
                    ),
                    stale_behavior=(
                        "An unprojected acceptance leaves the segment "
                        "absent rather than wrong; the map shows no "
                        "cable instead of a stale route."
                    ),
                    drift_signal=(
                        "An accepted as-built with NULL fiber_segment_id, "
                        "reported by the dry-run reconcile."
                    ),
                    rebuild_operation=("reconcile_accepted_as_builts(apply=True)"),
                    repair_owner="network.as_built_plant_projection",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.as_built_plant_projection",
                verification=(
                    "Projection, idempotency, variation-refresh, "
                    "unaccepted-evidence, missing-attribute, "
                    "endpoint-abstention, and repair-sweep tests, plus "
                    "activation tests covering map visibility, "
                    "non-projected refusal, endpoint validation, replay, "
                    "and the awaiting-activation queue."
                ),
            ),
            steward="network operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=(
                "tests/test_as_built_plant_projection.py",
                "tests/test_as_built_plant_activation.py",
            ),
        ),
    ),
    SOTService(
        name="network.fiber_plant_integrity",
        module="app.services.network.fiber_plant_integrity",
        owns=(
            "active passive-cable endpoint, geometry, and size validation",
            "serving PON/OLT rootedness and safe cable retirement",
            "exact numbered cable-core materialization and capacity projection",
            "splitter ratio, port-count, and declared-capacity validation",
        ),
        depends_on=("network.fiber_topology",),
        notes=(
            "This is the invariant and exact-capacity owner called by "
            "reviewed asset changes and splitter commands. Cable names are "
            "display metadata only; name or proximity matching never creates "
            "an endpoint, core assignment, or rooted topology edge."
        ),
    ),
    SOTService(
        name="network.splitter_inventory",
        module="app.services.network.splitters",
        owns=(
            "splitter identity and declared ratio/capacity mutations",
            "splitter port identity and bounded port mutations",
            "splitter utilization and spare-output projection",
        ),
        depends_on=("network.fiber_plant_integrity",),
        notes=(
            "API and admin form adapters delegate here. Reviewed attachment "
            "owners remain authoritative for PON inputs, cascades, and ONT "
            "outputs; this inventory owner does not infer those edges."
        ),
    ),
    SOTService(
        name="network.fiber_physical_continuity",
        module="app.services.network.fiber_physical_continuity",
        owns=(
            "fiber rack, ODF/patch-panel, and connector-port inventory invariants",
            "reviewed exact core-splice, strand-termination, and patch-cord decisions",
            "canonical active physical optical links and immutable result evidence",
            "exact ordered cable-core continuity and evidence hash",
        ),
        depends_on=(
            "network.fiber_topology",
            "network.fiber_plant_integrity",
        ),
        notes=(
            "Every connector represents one optical channel; duplex uses "
            "two explicit links sharing an assembly label, while MPO/MTP "
            "fails closed until an exact lane model exists. Cable names, "
            "labels, geometry, proximity, legacy FiberSplice rows, and the "
            "legacy FiberSegment.fiber_strand_id scalar never create exact "
            "continuity. Links require preview, independent review, locked "
            "execution, and exact result evidence."
        ),
    ),
    SOTService(
        name="network.fiber_asset_changes",
        module="app.services.fiber_change_requests",
        owns=(
            "reviewed passive-fiber asset change requests",
            "approved passive-fiber asset mutations",
            "reviewed requests for operational cable size and lifecycle state",
            "review transport for rack, panel, connector, and exact splice decisions",
        ),
        depends_on=(
            "network.fiber_topology",
            "network.fiber_plant_integrity",
            "network.splitter_inventory",
            "network.fiber_support_structures",
            "network.fiber_physical_continuity",
        ),
        notes=(
            "This workflow owns review and application. It delegates cable "
            "and splitter invariants, exact core materialization, physical "
            "inventory, and splice execution to their named owners instead "
            "of maintaining parallel mutation rules."
        ),
    ),
    SOTService(
        name="network.fiber_job_evidence",
        module="app.services.network.fiber_job_evidence",
        owns=("per-job fiber evidence summary projection",),
        depends_on=(
            "network.fiber_asset_changes",
            "network.fiber_splice_plans",
        ),
        notes=(
            "Read-only aggregation of the fiber evidence naming one "
            "native work order: tests with derived-verdict failures and "
            "assertion conflicts, topology source observations, splice "
            "proposals by review status, live cut-sheet progress, "
            "attachments, and pending inventory proposals. Every fact "
            "belongs to its named owner; this projection only counts and "
            "labels, and decides nothing."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="per-job fiber evidence summary projection",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "owner-recorded fiber evidence facts",
                        "reviewed splice change-request state",
                        "live cut-sheet progress",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="owner-recorded fiber evidence facts",
                    owner="network.fiber_job_evidence",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "FieldFiberTestResult, "
                        "FiberTopologyFieldObservation, and "
                        "FieldAttachment rows naming the exact work order"
                    ),
                ),
                AuthorityInput(
                    name="reviewed splice change-request state",
                    owner="network.fiber_asset_changes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "fiber_splice, fiber_segment, and fiber_strand "
                        "change requests with typed work-order provenance"
                    ),
                ),
                AuthorityInput(
                    name="live cut-sheet progress",
                    owner="network.fiber_splice_plans",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "the work order's live plan view and derived execution counts"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=("Pure aggregation over committed state; nothing is written."),
                locking="None; the projection reads committed state only.",
                idempotency=(
                    "Deterministic for identical committed inputs; safe to "
                    "recompute at any time."
                ),
                retries="Safe to re-read; no side effects exist.",
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner=(
                    "field and vendor transports surface the summary as "
                    "data; scoping errors belong to their job resolvers"
                ),
                fail_closed_on=(
                    "unscoped work orders (transports resolve scope "
                    "before this projection runs)",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                old_owner=None,
                new_owner="network.fiber_job_evidence",
                verification=("Focused summary-count and gate-composition tests."),
                cutover_gate=(
                    "Native new projection; the staged-verification "
                    "evidence map remains authoritative for its campaign."
                ),
                fallback_retirement=(
                    "No fallback exists; owners remain the source of "
                    "every underlying fact."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/FIBER_TECH_JOURNEY_GAP_LIST.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=("tests/test_fiber_field_inventory_journey.py",),
        ),
    ),
    SOTService(
        name="network.fiber_test_acceptance",
        module="app.services.network.fiber_test_acceptance",
        owns=(
            "derived fiber test acceptance verdicts",
            "expected downstream link budget derivation",
        ),
        depends_on=("network.fiber_topology",),
        notes=(
            "Observations stay facts: the technician's measurement and "
            "self-assessment are never altered. This policy derives a "
            "typed verdict from declared per-test-type thresholds "
            "(snapshotted beside the assertion with the policy version at "
            "capture time) and an expected downstream link budget from "
            "the canonical trace with every assumption named. Unknown "
            "test types and incomplete inputs yield explicit no_policy / "
            "incomplete outcomes, never a guess."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="derived fiber test acceptance verdicts",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "declared acceptance thresholds",
                        "field fiber test measurement facts",
                    ),
                ),
                ConcernContract(
                    name="expected downstream link budget derivation",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "declared acceptance thresholds",
                        "canonical customer trace evidence",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="declared acceptance thresholds",
                    owner="network.fiber_test_acceptance",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "versioned typed threshold table and planning "
                        "coefficients declared in the policy module"
                    ),
                ),
                AuthorityInput(
                    name="field fiber test measurement facts",
                    owner="network.fiber_test_acceptance",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "FieldFiberTestResult measurements captured by the "
                        "scoped field transport; the capture path stores "
                        "the derived snapshot beside the assertion"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer trace evidence",
                    owner="network.fiber_topology",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "trace_fiber_subscription hops, reviewed splitter "
                        "stage losses, and traced segment lengths"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Pure derivation: verdicts and budgets are computed "
                    "from inputs without mutating state; the field capture "
                    "path persists the verdict snapshot inside its own "
                    "existing transaction."
                ),
                locking="None; derivation reads committed state only.",
                idempotency=(
                    "Deterministic for identical inputs and policy "
                    "version; snapshots carry the version so replays are "
                    "distinguishable from policy changes."
                ),
                retries=(
                    "Safe to recompute at any time; recorded snapshots "
                    "are never rewritten by recomputation."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner=(
                    "field/vendor transports surface derived outcomes as "
                    "data; no transport error mapping is required"
                ),
                fail_closed_on=(
                    "unknown test types (explicit no_policy verdict)",
                    "missing measurements (explicit no_measurement verdict)",
                    "incomplete traces (budget labelled incomplete, "
                    "never presented as the whole path)",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                old_owner=None,
                new_owner="network.fiber_test_acceptance",
                verification=(
                    "Focused verdict-matrix, capture-snapshot, conflict, "
                    "and link-budget tests."
                ),
                cutover_gate=(
                    "Native new authority; the technician assertion "
                    "remains recorded and unaltered beside the verdict."
                ),
                fallback_retirement=(
                    "No fallback exists; tests without policy coverage "
                    "carry an explicit no_policy verdict."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/FIBER_TECH_JOURNEY_GAP_LIST.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=("tests/test_fiber_test_acceptance.py",),
        ),
    ),
    SOTService(
        name="network.fiber_splice_plans",
        module="app.services.network.fiber_splice_plans",
        owns=(
            "planned splice work (cut sheet) lifecycle",
            "planned splice execution linkage",
        ),
        depends_on=(
            "network.fiber_asset_changes",
            "network.fiber_plant_integrity",
            "operations.work_order_commands",
            "events.dispatcher",
        ),
        notes=(
            "The design-first owner for splicing: draft, issued, and "
            "cancelled cut sheets of exact strand-end pairs bound to one "
            "work order, at most one live plan each. Execution stays with "
            "the reviewed splice intake and review with "
            "network.fiber_asset_changes; an item records only the link to "
            "its executing change request, so plan progress is derived "
            "from review state and never drifts on its own. Field "
            "completion of a work order with an issued plan requires every "
            "item executed."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="planned splice work (cut sheet) lifecycle",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "operator cut-sheet command evidence",
                        "native work-order identity",
                        "passive plant closure, tray, and exact strand identity",
                    ),
                    canonical_writer="network.fiber_splice_plans",
                ),
                ConcernContract(
                    name="planned splice execution linkage",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "operator cut-sheet command evidence",
                        "reviewed splice change-request state",
                    ),
                    canonical_writer="network.fiber_splice_plans",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="operator cut-sheet command evidence",
                    owner="network.fiber_splice_plans",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "network:fiber:write (or scoped field/vendor "
                        "execution) plus typed CommandContext actor, scope, "
                        "and reason"
                    ),
                ),
                AuthorityInput(
                    name="native work-order identity",
                    owner="operations.work_order_commands",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=("the active WorkOrder public identity a plan binds to"),
                ),
                AuthorityInput(
                    name=("passive plant closure, tray, and exact strand identity"),
                    owner="network.fiber_plant_integrity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active FiberSpliceClosure and FiberSpliceTray rows "
                        "and exact numbered plannable FiberStrand identities"
                    ),
                ),
                AuthorityInput(
                    name="reviewed splice change-request state",
                    owner="network.fiber_asset_changes",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the fiber_splice FiberChangeRequest linked by "
                        "executed_change_request_id and its review status"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Every plan mutation (create, add/remove item, issue, "
                    "cancel, execution linkage) enters execute_owner_command "
                    "once on a transaction-free session; internals stay "
                    "flush-only and the command boundary commits or rolls "
                    "back atomically with staged events."
                ),
                locking=(
                    "The partial unique live-plan index arbitrates concurrent "
                    "plan creation per work order; item position and "
                    "executed-change-request uniqueness are schema-enforced. "
                    "No advisory locks are required."
                ),
                idempotency=(
                    "cancel_plan replays as a no-op; execution linkage "
                    "carries a plan-item/change-request idempotency key and "
                    "each item holds at most one non-rejected executing "
                    "request."
                ),
                retries=(
                    "Callers may retry failed commands with fresh state; "
                    "live-plan and executed-item conflicts fail closed for "
                    "operator review instead of overwriting."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.fiber_splice_plans.plan_not_found",
                    "network.fiber_splice_plans.work_order_not_found",
                    "network.fiber_splice_plans.name_required",
                    "network.fiber_splice_plans.live_plan_exists",
                    "network.fiber_splice_plans.invalid_identifier",
                    "network.fiber_splice_plans.invalid_strand_end",
                    "network.fiber_splice_plans.self_splice",
                    "network.fiber_splice_plans.splice_type_required",
                    "network.fiber_splice_plans.closure_not_found",
                    "network.fiber_splice_plans.strand_not_found",
                    "network.fiber_splice_plans.strand_not_plannable",
                    "network.fiber_splice_plans.tray_not_found",
                    "network.fiber_splice_plans.tray_closure_mismatch",
                    "network.fiber_splice_plans.duplicate_planned_pair",
                    "network.fiber_splice_plans.plan_not_editable",
                    "network.fiber_splice_plans.plan_full",
                    "network.fiber_splice_plans.item_not_found",
                    "network.fiber_splice_plans.plan_not_issuable",
                    "network.fiber_splice_plans.plan_empty",
                    "network.fiber_splice_plans.plan_not_issued",
                    "network.fiber_splice_plans.plan_work_order_mismatch",
                    "network.fiber_splice_plans.plan_item_mismatch",
                    "network.fiber_splice_plans.item_already_executed",
                    *owner_command_boundary_error_codes("network.fiber_splice_plans"),
                ),
                mapping_owner=("admin fiber API and field/vendor transport adapters"),
                fail_closed_on=(
                    "a proposed splice that does not exactly match its named "
                    "cut-sheet entry",
                    "a second live plan for the same work order",
                    "editing or issuing outside the draft lifecycle",
                ),
            ),
            events=EventContract(
                event_types=(
                    "fiber.splice_plan_issued",
                    "fiber.splice_plan_cancelled",
                    "fiber.splice_plan_item_executed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "New PII-free schemas carrying plan, work-order, item, "
                    "and change-request identifiers only."
                ),
                replay=(
                    "Events stage only on a successful command commit; "
                    "execution linkage is unique per item, so a replayed "
                    "command cannot double-link or double-emit."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                old_owner=None,
                new_owner="network.fiber_splice_plans",
                verification=(
                    "Focused lifecycle, execution-matching, diff, completion-"
                    "gate, and permission tests."
                ),
                cutover_gate=(
                    "Native new authority; capture-first proposals remain "
                    "valid and appear as unplanned work in the diff."
                ),
                fallback_retirement=(
                    "No fallback exists; unplanned proposals are surfaced, not blocked."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/FIBER_TECH_JOURNEY_GAP_LIST.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=("tests/test_fiber_splice_plans.py",),
        ),
    ),
    SOTService(
        name="network.fiber_support_structures",
        module="app.services.network.fiber_support_structures",
        owns=(
            "canonical fiber support identity and operational state",
            "support lifecycle, inspection, ownership, and lease projection",
            "reviewed exact passive-asset-to-support mount decisions",
            "canonical ordered support mount edges and result evidence",
        ),
        depends_on=(
            "network.fiber_topology",
            "observability.audit_log",
        ),
        notes=(
            "Imported poles remain staged observations. Canonical support "
            "creates and state changes are applied here only after reviewed "
            "passive-asset requests. Mounts require exact asset/support IDs, "
            "preview confirmation, independent review, locked revalidation, "
            "and audit evidence; geometry and proximity never create an edge."
        ),
    ),
    SOTService(
        name="network.fiber_identity_decisions",
        module="app.services.network.fiber_topology_identity",
        owns=(
            "reviewed fiber source identity decisions",
            "canonical fiber source identity links",
            "fiber source identity change-request projection",
        ),
        depends_on=(
            "network.fiber_topology",
            "network.fiber_asset_changes",
            "network.fiber_support_structures",
        ),
        notes=(
            "Identity decisions are bound to immutable staged content. "
            "Creates emit reviewed fiber change requests; a canonical "
            "source link is written only after the asset exists."
        ),
    ),
    SOTService(
        name="network.fiber_identity_review",
        module="app.services.network.fiber_topology_review",
        owns=(
            "fiber identity review queue projection",
            "immutable fiber identity proposal batch manifests",
            "fiber identity batch review attestations",
            "bounded fiber identity execution-run evidence",
            "fiber identity change-request finalization sweep",
        ),
        depends_on=("network.fiber_identity_decisions",),
        notes=(
            "Batch review binds an independent attestation to the exact "
            "proposal manifest and delegates every decision transition to "
            "the identity owner. Bounded execution records exact outcomes. "
            "Neither execution nor reconciliation approves a fiber change "
            "request."
        ),
    ),
    SOTService(
        name="network.fiber_field_observations",
        module="app.services.network.fiber_topology_field_observations",
        owns=(
            "immutable staged fiber field observations",
            "exact field-observation and claim evidence digests",
            "field observation agreement, conflict, and drift projection",
        ),
        depends_on=(
            "network.fiber_source_staging",
            "operations.work_orders",
            "network.fiber_field_verification_job_scope",
        ),
        notes=(
            "Every observation binds exact staged content, work order, "
            "technician identity, explicit labels/references, measurement "
            "facts, and active same-job attachment pointers. Contradictory "
            "observations remain evidence. This owner cannot infer identity "
            "or endpoints, generate decisions, approve changes, mutate "
            "canonical topology, or establish cutover thresholds."
        ),
    ),
    SOTService(
        name="network.fiber_field_verification_job_scope",
        module=("app.services.network.fiber_field_verification_job_scope"),
        owns=(
            "fiber field-verification work-order scope metadata contract",
            "exact planned staged-feature observation boundary",
        ),
        notes=(
            "Legacy jobs without an explicit plan retain their existing "
            "observation behavior. Once a plan is present, observations "
            "must name one of its exact staged feature IDs with unchanged "
            "content; names, labels, geometry, and proximity never expand "
            "the scope."
        ),
    ),
    SOTService(
        name="network.fiber_field_verification_worklist",
        module="app.services.network.fiber_topology_field_worklist",
        owns=(
            "exhaustive latest-source fiber field-verification worklist",
            "deterministic field-evidence triage priority projection",
            "exact field-worklist row and report evidence digests",
        ),
        depends_on=(
            "network.fiber_source_staging",
            "network.fiber_field_observations",
        ),
        notes=(
            "This read-only owner keeps every latest staged feature in "
            "the cohort and orders evidence gathering without hiding "
            "current agreement. Existing native work-order references "
            "are context only. It cannot create or assign jobs, record "
            "observations, infer identity/endpoints, generate decisions, "
            "mutate topology, or establish cutover eligibility."
        ),
    ),
    SOTService(
        name="network.fiber_field_verification_jobs",
        module=("app.services.network.fiber_field_verification_job_plans"),
        owns=(
            "exact fiber field-verification job-plan previews",
            "confirmed staged-source-to-native-job plan execution",
            "fiber field-verification job-plan audit evidence",
        ),
        depends_on=(
            "network.fiber_field_verification_worklist",
            "network.fiber_field_verification_job_scope",
            "operations.work_order_commands",
            "observability.audit_log",
        ),
        notes=(
            "The owner binds at most 100 explicitly selected current "
            "worklist rows, exact row/content/geometry hashes, and the "
            "complete worklist report hash. Execute re-previews and "
            "requires the exact plan digest, then delegates create and "
            "optional assignment to operations.work_order_commands in one "
            "transaction. It never writes work-order tables directly and "
            "does not add actions to the read-only worklist or map."
        ),
    ),
    SOTService(
        name="network.fiber_field_verification_map",
        module="app.services.network.fiber_topology_field_map",
        owns=(
            "complete exact-GeoJSON fiber field-verification overlay",
            "field-map presentation geometry classification and bounds",
            "exact field-map feature and overlay evidence digests",
        ),
        depends_on=(
            "network.fiber_source_staging",
            "network.fiber_field_verification_worklist",
        ),
        notes=(
            "This read-only projection attaches exact staged GeoJSON to "
            "the complete owner-produced worklist. Color represents only "
            "worklist priority; blocked source geometry remains visible "
            "without repair. It cannot snap or infer topology, create jobs "
            "or observations, mutate source/canonical state, establish "
            "thresholds, or claim cutover eligibility."
        ),
    ),
    SOTService(
        name="network.fiber_work_order_evidence_map",
        module=("app.services.network.fiber_topology_work_order_evidence_map"),
        owns=(
            "technician-scoped native work-order fiber evidence overlay",
            "exact work-order observation-to-map lineage projection",
            "work-order fiber evidence feature and report digests",
            "work-order evidence and geometry presentation semantics",
        ),
        depends_on=(
            "operations.work_orders",
            "network.fiber_field_observations",
            "network.fiber_field_verification_map",
        ),
        notes=(
            "This read-only projection selects only field-verification "
            "map features represented by immutable observations for one explicitly "
            "scoped native Sub work order. Every observation must map "
            "exactly once; other jobs' evidence is removed. Current and "
            "superseded source context remains distinct. It cannot assign "
            "work, record observations, repair geometry, infer topology, "
            "mutate state, establish thresholds, or decide customer impact."
            " The field mobile client renders this contract and may cache "
            "only one exact public-work-order/report-hash snapshot per "
            "authenticated principal; its offline cache is explicitly "
            "stale and never an authority."
        ),
    ),
    SOTService(
        name="network.fiber_identity_coverage",
        module="app.services.network.fiber_topology_identity_coverage",
        owns=(
            "exhaustive latest staged point-identity coverage reconciliation",
            "fiber point-identity lineage and provenance drift projection",
            "fiber point-identity cutover-review readiness evidence",
        ),
        depends_on=(
            "network.fiber_source_staging",
            "network.fiber_asset_changes",
            "network.fiber_field_observations",
            "network.fiber_identity_decisions",
            "network.fiber_identity_review",
            "network.fiber_support_structures",
        ),
        notes=(
            "One repeatable read-only snapshot keeps canonical-model support, "
            "source coverage, decision lifecycle, change-request state, and "
            "provenance validity separate. Cabinets, FATs, closures, "
            "buildings, and supports use their current canonical models. "
            "The owner cannot infer identity, "
            "create or advance decisions, approve change requests, mutate "
            "assets, or authorize production cutover."
        ),
    ),
    SOTService(
        name="network.fiber_connectivity_decisions",
        module="app.services.network.fiber_topology_connectivity",
        owns=(
            "reviewed staged-path connectivity decisions",
            "typed endpoint termination resolution",
            "canonical fiber segment source provenance",
            "fiber connectivity change-request reconciliation",
        ),
        depends_on=(
            "network.fiber_topology",
            "network.fiber_asset_changes",
            "network.fiber_identity_decisions",
        ),
        notes=(
            "Route geometry remains source evidence. Operational edges "
            "require two explicit typed canonical endpoint references, "
            "independent review, applied termination records, and an "
            "applied segment change request."
        ),
    ),
    SOTService(
        name="network.fiber_connectivity_review",
        module=("app.services.network.fiber_topology_connectivity_review"),
        owns=(
            "immutable fiber connectivity proposal batch manifests",
            "fiber connectivity batch review attestations",
            "bounded fiber connectivity execution evidence",
            "bounded fiber connectivity reconciliation evidence",
        ),
        depends_on=("network.fiber_connectivity_decisions",),
        notes=(
            "Every manifest row binds exact staged content to explicit "
            "canonical endpoint IDs. Batch review and runs delegate every "
            "transition to the connectivity-decision owner; geometry never "
            "selects endpoints and the batch owner never approves canonical "
            "termination or segment change requests."
        ),
    ),
    SOTService(
        name="network.fiber_connectivity_coverage",
        module=("app.services.network.fiber_topology_connectivity_coverage"),
        owns=(
            "exhaustive latest staged-path coverage reconciliation",
            "fiber connectivity lineage and evidence drift projection",
            "fiber connectivity cutover-review readiness evidence",
        ),
        depends_on=(
            "network.fiber_source_staging",
            "network.fiber_asset_changes",
            "network.fiber_field_observations",
            "network.fiber_connectivity_decisions",
            "network.fiber_connectivity_review",
        ),
        notes=(
            "One repeatable read-only snapshot keeps source coverage, "
            "decision lifecycle, canonical mutation state, and provenance "
            "validity separate. It cannot infer endpoints, create or advance "
            "decisions, approve change requests, mutate topology, or authorize "
            "production cutover."
        ),
    ),
    SOTService(
        name="network.fiber_cutover_readiness",
        module=("app.services.network.fiber_topology_cutover_readiness"),
        owns=(
            "versioned numeric fiber cutover-readiness policy",
            "complete global fiber cutover cohort evidence projection",
            "fiber topology cutover-review readiness decision",
        ),
        depends_on=(
            "network.fiber_topology",
            "network.fiber_identity_coverage",
            "network.fiber_connectivity_coverage",
            "network.fiber_field_verification_worklist",
        ),
        notes=(
            "The initial policy accepts only the complete global cohort, "
            "requires exact current identity/connectivity/result/provenance "
            "and customer traces, and applies zero-tolerance blockers. All "
            "latest staged rows remain mandatory until an authoritative "
            "dormant-low-risk classifier exists. Missing POP/OLT, splitter, "
            "and customer-endpoint field contracts fail closed. A passing "
            "report is evidence for independent review and cannot authorize "
            "or execute a production cutover."
        ),
    ),
)
