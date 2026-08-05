"""provisioning_operations SOT declarations: vendor delivery."""

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
        name="operations.installation_scope",
        module="app.services.installation_projects",
        owns=(
            "idempotent structural InstallationProject root creation",
            "Project-to-InstallationProject subscriber alignment",
            "buildout-rooted installation scope creation",
        ),
        depends_on=("operations.project_lifecycle",),
        notes=(
            "This transaction-neutral owner creates only the installation "
            "root. Vendor lifecycle decisions remain with "
            "operations.vendor_project_lifecycle. Two entry points reach "
            "the same root: a sold installation (subscriber-scoped, "
            "triggered by sales.fulfillment) and a network buildout "
            "(subscriber-less, rooted on a BuildoutProject) so every "
            "downstream vendor decision runs one path. The BuildoutProject "
            "is validated as a referent, not consumed as a decision input; "
            "app.services.qualification is not yet a declared owner."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("idempotent structural InstallationProject root creation"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical native project state",
                        "installation scope creation command",
                    ),
                    canonical_writer="operations.installation_scope",
                ),
                ConcernContract(
                    name="Project-to-InstallationProject subscriber alignment",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical native project state",
                        "installation scope creation command",
                    ),
                ),
                ConcernContract(
                    name="buildout-rooted installation scope creation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("canonical native project state",),
                    canonical_writer="operations.installation_scope",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical native project state",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="the exact native Project and its Subscriber binding",
                ),
                AuthorityInput(
                    name="installation scope creation command",
                    owner="sales.orders",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed Project, Subscriber, optional creator, and actor "
                        "identifiers derived from the exact SalesOrder scope"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The sales fulfillment coordinator owns commit/rollback; "
                    "this owner stages one InstallationProject and event."
                ),
                locking=(
                    "The parent Project is read by exact id and the unique "
                    "project_id constraint arbitrates concurrent creation."
                ),
                idempotency=(
                    "An existing structurally aligned installation scope is "
                    "returned; a conflicting Subscriber binding fails closed."
                ),
                retries=(
                    "The coordinator retries the complete scope command after "
                    "a uniqueness race."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "actor_required",
                    "project_not_found",
                    "subscriber_mismatch",
                    "existing_scope_mismatch",
                ),
                mapping_owner="sales.fulfillment",
                fail_closed_on=(
                    "missing parent Project",
                    "Subscriber mismatch",
                    "conflicting existing scope",
                ),
            ),
            events=EventContract(
                event_types=("installation_scope.created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact installation, Project, Subscriber, "
                    "and actor identifiers."
                ),
                replay=(
                    "The unique Project structural binding and event outbox make "
                    "duplicate creation a no-op."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="unlinked or metadata-only installation creation",
                new_owner="operations.installation_scope",
                verification=(
                    "Structural-link, Subscriber-alignment, idempotency, event, "
                    "and end-to-end lifecycle tests."
                ),
                cutover_gate=(
                    "Sales fulfillment creates installation roots only through "
                    "this participant."
                ),
                fallback_retirement=(
                    "Metadata-only and CRM installation-scope creation paths are "
                    "not present."
                ),
            ),
            steward="service delivery",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_sales_to_service_lifecycle.py",
                "tests/test_sales_orders_services.py",
                "tests/test_sot_relationships.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_material_release",
        module="app.services.vendor_material_release",
        owns=(
            "vendor project material release need and approval",
            "backoffice material issue outcome projection for vendors",
        ),
        depends_on=("operations.vendor_project_lifecycle",),
        notes=(
            "field_material_requests is work-order scoped with a "
            "TechnicianProfile requester, so it models an employee on a "
            "customer job; this owner models a contractor drawing "
            "Dotmac-owned material for a project. Sub decides whether "
            "material is released and records the evidence; the "
            "configured provider owns the stock issue. Sub never posts "
            "stock and never selects a warehouse, and a provider refusal "
            "is recorded as an observation that never reverses a "
            "committed Sub approval."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="vendor project material release need and approval",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("canonical installation-project lifecycle state",),
                    canonical_writer="operations.vendor_material_release",
                ),
                ConcernContract(
                    name=("backoffice material issue outcome projection for vendors"),
                    role=OwnerRole.RECONCILER,
                    input_names=("backoffice material issue outcome",),
                    canonical_writer="operations.vendor_material_release",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical installation-project lifecycle state",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the assigned vendor and the approved or "
                        "in-progress state that makes material releasable"
                    ),
                ),
                AuthorityInput(
                    name="backoffice material issue outcome",
                    owner="integration.dotmac_erp_material_support_adapter",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "the configured provider's issue or refusal, "
                        "carried as a provider-neutral reference plus an "
                        "explicit source-system name"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The vendor workspace or signed staff-review coordinator "
                    "owns commit; this owner stages the request, review, or "
                    "provider outcome."
                ),
                locking=("Review and outcome application lock the release row."),
                idempotency=(
                    "Re-applying the same provider outcome writes the same "
                    "values and never double-issues."
                ),
                retries=(
                    "Provider delivery retries are the adapter's concern; "
                    "the Sub approval stands regardless."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_material_release.release_not_found",
                    "operations.vendor_material_release.project_not_found",
                    "operations.vendor_material_release.project_not_assigned",
                    "operations.vendor_material_release.project_not_releasable",
                    "operations.vendor_material_release.items_required",
                    "operations.vendor_material_release.invalid_quantity",
                    "operations.vendor_material_release.not_reviewable",
                    "operations.vendor_material_release.reason_required",
                    "operations.vendor_material_release.outcome_not_applicable",
                ),
                mapping_owner="app.web.admin.vendor_operations",
            ),
            events=EventContract(
                event_types=(
                    "vendor_material_release.requested",
                    "vendor_material_release.reviewed",
                    "vendor_material_release.issued",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries release, project, vendor, and "
                    "provider-reference identity. It never carries stock "
                    "levels or warehouse identity, which Sub does not own."
                ),
                replay=(
                    "The release row and its items rebuild the request and "
                    "the last observed provider outcome."
                ),
            ),
            projections=(
                ProjectionContract(
                    name=("backoffice material issue outcome projection for vendors"),
                    input_names=("backoffice material issue outcome",),
                    writer="operations.vendor_material_release",
                    freshness=(
                        "As fresh as the last provider observation; the "
                        "row records when it was observed."
                    ),
                    stale_behavior=(
                        "A stale or absent outcome leaves the release "
                        "approved rather than claiming stock moved."
                    ),
                    drift_signal=(
                        "An approved release with no support_status after "
                        "the provider reports an issue."
                    ),
                    rebuild_operation=(
                        "apply_provider_outcome with the provider's current "
                        "state for the release"
                    ),
                    repair_owner=("integration.dotmac_erp_material_support_adapter"),
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="operations.vendor_material_release",
                verification=(
                    "Request, assignment, releasable-state, line "
                    "validation, approval, provider outcome, and "
                    "idempotency tests."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/BACKOFFICE_INTEGRATION_BOUNDARY.md",
            ),
            test_refs=("tests/test_vendor_supply.py",),
        ),
    ),
    SOTService(
        name="operations.vendor_advances",
        module="app.services.vendor_advances",
        owns=(
            "vendor advance eligibility, ceiling, and approval",
            "payables settlement observation for vendor advances",
        ),
        depends_on=(
            "control.settings_spec",
            "operations.vendor_project_lifecycle",
            "operations.vendor_project_records",
        ),
        notes=(
            "Sub decides whether to advance money to a vendor and how "
            "much; the payables provider owns the payment and any netting "
            "against the vendor's later invoice. The amount is entered, "
            "not derived: staff approval is the control. Sub applies two "
            "limits only — a hard bound at the approved quote total, "
            "which is arithmetic rather than policy and counts advances "
            "already committed so it cannot be evaded by splitting a "
            "request, and an optional percentage guard rail from "
            "projects.vendor_advance_max_percent that defaults to no cap "
            "and can only lower that bound, never raise it. A settled "
            "advance is an observation of the provider, never a Sub "
            "decision; Sub never computes settlement and never adjusts an "
            "invoice total."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="vendor advance eligibility, ceiling, and approval",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                        "vendor advance cap policy",
                    ),
                    canonical_writer="operations.vendor_advances",
                ),
                ConcernContract(
                    name="payables settlement observation for vendor advances",
                    role=OwnerRole.RECONCILER,
                    input_names=("vendor payables settlement observation",),
                    canonical_writer="operations.vendor_advances",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical installation-project lifecycle state",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the assigned vendor and the approved or "
                        "in-progress state that makes an advance available"
                    ),
                ),
                AuthorityInput(
                    name="canonical vendor project records",
                    owner="operations.vendor_project_records",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the approved quote total and currency the advance "
                        "draws against"
                    ),
                ),
                AuthorityInput(
                    name="vendor advance cap policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "projects.vendor_advance_max_percent, an optional "
                        "guard rail that defaults to no cap"
                    ),
                ),
                AuthorityInput(
                    name="vendor payables settlement observation",
                    owner="integration.dotmac_erp_payables_adapter",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "the payables provider's settlement state for the "
                        "advance, carried as a provider-neutral reference"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The vendor workspace or signed staff-review coordinator "
                    "owns commit; this owner stages the request, review, or "
                    "provider observation."
                ),
                locking=("Review and observation application lock the advance."),
                idempotency=(
                    "Re-applying the same settlement observation writes the "
                    "same values; the ceiling check counts committed "
                    "advances so a retry cannot double-commit."
                ),
                retries=(
                    "Provider delivery retries are the adapter's concern; "
                    "the Sub approval stands regardless."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_advances.advance_not_found",
                    "operations.vendor_advances.project_not_found",
                    "operations.vendor_advances.project_not_assigned",
                    "operations.vendor_advances.project_not_advanceable",
                    "operations.vendor_advances.approved_quote_required",
                    "operations.vendor_advances.advance_ceiling_exceeded",
                    "operations.vendor_advances.invalid_amount",
                    "operations.vendor_advances.not_reviewable",
                    "operations.vendor_advances.reason_required",
                    "operations.vendor_advances.observation_not_applicable",
                ),
                mapping_owner="app.web.admin.vendor_operations",
            ),
            events=EventContract(
                event_types=(
                    "vendor_advance.requested",
                    "vendor_advance.reviewed",
                    "vendor_advance.settled",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries advance, project, vendor, quote, "
                    "amount, and currency. Settlement identity is a "
                    "provider reference, never a Sub payment record."
                ),
                replay=(
                    "The advance row rebuilds the decision and the last "
                    "observed payables state; Sub never recomputes "
                    "settlement."
                ),
            ),
            projections=(
                ProjectionContract(
                    name=("payables settlement observation for vendor advances"),
                    input_names=("vendor payables settlement observation",),
                    writer="operations.vendor_advances",
                    freshness=(
                        "As fresh as the last payables observation; the row "
                        "records when it was observed."
                    ),
                    stale_behavior=(
                        "A stale or unavailable observation retains the last "
                        "good one and leaves the advance approved rather "
                        "than claiming it was paid."
                    ),
                    drift_signal=(
                        "An approved advance whose payables_status has not "
                        "advanced since approval."
                    ),
                    rebuild_operation=(
                        "apply_payables_observation with the provider's "
                        "current state for the advance"
                    ),
                    repair_owner="integration.dotmac_erp_payables_adapter",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="operations.vendor_advances",
                verification=(
                    "Quote-total bound, stacking, released-ceiling, "
                    "configured-cap, cap-cannot-raise-the-bound, "
                    "assignment, advanceable-state, approval, and "
                    "observed-settlement tests."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/BACKOFFICE_INTEGRATION_BOUNDARY.md",
            ),
            test_refs=("tests/test_vendor_supply.py",),
        ),
    ),
    SOTService(
        name="operations.vendor_project_lifecycle",
        module="app.services.vendor_project_lifecycle",
        owns=(
            "vendor start/complete and staff verify/rework "
            "installation-project transitions",
            "staff bidding publication and direct vendor assignment",
            "durable vendor lifecycle actor/time/reason/event evidence",
            "typed vendor project lifecycle outbox events",
        ),
        depends_on=(
            "auth.permission_gate",
            "events.dispatcher",
            "operations.project_lifecycle",
            "operations.work_order_commands",
        ),
        notes=(
            "This participant is the sole writer for approved -> "
            "in_progress -> completed vendor work transitions. Only the "
            "signed vendor-submission coordinator may call its nested writer. "
            "A committed verification event requests downstream fulfillment; "
            "the vendor owner does not write sales or provisioning roots."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=(
                        "vendor start/complete and staff verify/rework "
                        "installation-project transitions"
                    ),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "authenticated assigned-vendor transition evidence",
                        "vendor lifecycle transition protocol",
                        "work-order as-built evidence policy",
                    ),
                    canonical_writer="operations.vendor_project_lifecycle",
                ),
                ConcernContract(
                    name=("staff bidding publication and direct vendor assignment"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "vendor lifecycle transition protocol",
                    ),
                    canonical_writer="operations.vendor_project_lifecycle",
                ),
                ConcernContract(
                    name=("durable vendor lifecycle actor/time/reason/event evidence"),
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "authenticated assigned-vendor transition evidence",
                        "work-order as-built evidence policy",
                    ),
                    canonical_writer="operations.vendor_project_lifecycle",
                ),
                ConcernContract(
                    name="typed vendor project lifecycle outbox events",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "authenticated assigned-vendor transition evidence",
                    ),
                    canonical_writer="operations.vendor_project_lifecycle",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical installation-project lifecycle state",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active InstallationProject status, assigned "
                        "vendor, native project, and subscriber references"
                    ),
                ),
                AuthorityInput(
                    name="authenticated assigned-vendor transition evidence",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "vendor id, actor id and type carried by the verified "
                        "vendor-submission command"
                    ),
                ),
                AuthorityInput(
                    name="work-order as-built evidence policy",
                    owner="operations.work_order_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "active linked WorkOrder requires_as_built_evidence "
                        "flags and the latest canonical project as-built status"
                    ),
                ),
                AuthorityInput(
                    name="vendor lifecycle transition protocol",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "approved-to-in-progress start, in-progress-to-completed "
                        "completion, completed-to-verified acceptance, and "
                        "completed-to-in-progress rework transitions"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The vendor-submission coordinator owns the root transaction. "
                    "This participant stages project state, immutable lifecycle "
                    "evidence, and the outbox event, then flushes only."
                ),
                locking=(
                    "The active InstallationProject row is selected FOR UPDATE "
                    "before transition eligibility is re-evaluated."
                ),
                idempotency=(
                    "Only the exact expected source status may transition; signed "
                    "submission replay is owned by the calling coordinator."
                ),
                retries=(
                    "The coordinator retries the complete confirmation transaction; "
                    "this participant never retries or commits independently."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_project_lifecycle.not_found",
                    "operations.vendor_project_lifecycle.not_assigned",
                    "operations.vendor_project_lifecycle.unsupported_action",
                    "operations.vendor_project_lifecycle.actor_required",
                    "operations.vendor_project_lifecycle.invalid_transition",
                    "operations.vendor_project_lifecycle.invalid_bidding_window",
                    "operations.vendor_project_lifecycle.already_assigned",
                    "operations.vendor_project_lifecycle.vendor_not_found",
                    "operations.vendor_project_lifecycle.vendor_assignment_required",
                    "operations.vendor_project_lifecycle.reason_required",
                    "operations.vendor_project_lifecycle.reason_too_long",
                    "operations.vendor_project_lifecycle.as_built_evidence_required",
                ),
                mapping_owner="operations.vendor_submission_confirmation",
                fail_closed_on=(
                    "missing or inactive project",
                    "vendor assignment mismatch",
                    "missing actor evidence",
                    "unsupported or invalid status transition",
                ),
            ),
            events=EventContract(
                event_types=(
                    "vendor_project.started",
                    "vendor_project.completed",
                    "vendor_project.verified",
                    "vendor_project.rework_requested",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and carries project, native project, "
                    "vendor, source and target status, and actor evidence."
                ),
                replay=(
                    "InstallationProject and its append-only lifecycle evidence "
                    "rebuild the transition outcome and outbox projection."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.vendor_portal_operations lifecycle helpers "
                    "with optional service-owned commits"
                ),
                new_owner="operations.vendor_project_lifecycle",
                verification=(
                    "Transition, assignment, actor, immutable evidence, event, "
                    "participant, and caller-boundary tests."
                ),
                cutover_gate=(
                    "Signed vendor confirmation is the only application caller "
                    "and commits transition and confirmation evidence together."
                ),
                fallback_retirement=(
                    "Direct start/complete wrappers, optional commit flags, and "
                    "lifecycle writes in the workspace service are removed."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_vendor_lifecycle.py",
                "tests/test_vendor_project_review.py",
                "tests/test_vendor_submission_proposals.py",
                "tests/architecture/test_vendor_project_lifecycle_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_project_workspace",
        module="app.services.vendor_portal_operations",
        owns=(
            "vendor project workspace read and action projections",
            "vendor project workspace mutation coordination",
            "quote creation eligibility",
            "quote submission eligibility and impact snapshot",
            "as-built submission eligibility and impact snapshot",
            "staff project-review eligibility and impact snapshot",
            "staff proposed-route review eligibility and impact snapshot",
            "staff as-built-review eligibility and impact snapshot",
        ),
        depends_on=(
            "auth.permission_gate",
            "control.settings_spec",
            "operations.project_lifecycle",
            "operations.vendor_advances",
            "operations.vendor_material_release",
            "operations.vendor_project_records",
            "operations.vendor_project_lifecycle",
            "operations.work_order_commands",
        ),
        notes=(
            "This service owns vendor workspace queries, impact policy, and "
            "typed public-command coordination. Canonical quote, route, and "
            "as-built writers participate in its transaction or the signed "
            "submission coordinator's transaction."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="vendor project workspace read and action projections",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                    ),
                ),
                ConcernContract(
                    name="vendor project workspace mutation coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated vendor workspace command context",
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                        "canonical vendor material release decisions",
                        "canonical vendor advance decisions",
                        "vendor quote currency and validity policy",
                        "vendor workspace mutation protocol",
                    ),
                ),
                ConcernContract(
                    name="quote creation eligibility",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                    ),
                ),
                ConcernContract(
                    name="quote submission eligibility and impact snapshot",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                        "vendor workspace mutation protocol",
                    ),
                ),
                ConcernContract(
                    name="as-built submission eligibility and impact snapshot",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                        "vendor workspace mutation protocol",
                    ),
                ),
                ConcernContract(
                    name=("staff project-review eligibility and impact snapshot"),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                        "work-order as-built evidence policy",
                        "vendor workspace mutation protocol",
                    ),
                ),
                ConcernContract(
                    name=(
                        "staff proposed-route review eligibility and impact snapshot"
                    ),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                        "vendor workspace mutation protocol",
                    ),
                ),
                ConcernContract(
                    name=("staff as-built-review eligibility and impact snapshot"),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical installation-project lifecycle state",
                        "canonical vendor project records",
                        "vendor workspace mutation protocol",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated vendor workspace command context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "authenticated principal, vendor scope, reason, command, "
                        "and correlation identifiers"
                    ),
                ),
                AuthorityInput(
                    name="canonical installation-project lifecycle state",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active InstallationProject assignment, bidding window, "
                        "status, native project, and approved quote references"
                    ),
                ),
                AuthorityInput(
                    name="canonical vendor project records",
                    owner="operations.vendor_project_records",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "ProjectQuote, ProjectQuoteLineItem, "
                        "ProposedRouteRevision, AsBuiltRoute, and "
                        "AsBuiltLineItem rows"
                    ),
                ),
                AuthorityInput(
                    name="canonical vendor material release decisions",
                    owner="operations.vendor_material_release",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "assigned-project eligibility and staged material "
                        "release request records"
                    ),
                ),
                AuthorityInput(
                    name="canonical vendor advance decisions",
                    owner="operations.vendor_advances",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "approved-quote allowance, committed advances, and "
                        "staged advance request records"
                    ),
                ),
                AuthorityInput(
                    name="work-order as-built evidence policy",
                    owner="operations.work_order_commands",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "active linked WorkOrder requires_as_built_evidence "
                        "flags with default-enabled behavior"
                    ),
                ),
                AuthorityInput(
                    name="vendor quote currency and validity policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "billing.default_currency and "
                        "projects.vendor_quote_validity_days"
                    ),
                ),
                AuthorityInput(
                    name="vendor workspace mutation protocol",
                    owner="operations.vendor_project_workspace",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "editable, submittable, reviewable, assignment, route, "
                        "and as-built evidence invariants"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Each typed public workspace command enters one verified "
                    "coordinator transaction and invokes record-writer staging."
                ),
                locking=(
                    "Commands lock the InstallationProject, ProjectQuote, or "
                    "ProposedRouteRevision aggregate before eligibility and write."
                ),
                idempotency=(
                    "Project quote creation replays to the existing editable "
                    "quote; state transitions require the exact source state and "
                    "command identifiers are retained in event evidence."
                ),
                retries=(
                    "Policy rejections are terminal. Database concurrency failures "
                    "retry the complete typed command with the original context."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_project_workspace.project_not_found",
                    "operations.vendor_project_workspace.quote_not_found",
                    "operations.vendor_project_workspace.quote_line_not_found",
                    "operations.vendor_project_workspace.route_revision_not_found",
                    "operations.vendor_project_workspace.project_not_assigned",
                    "operations.vendor_project_workspace.quote_creation_not_allowed",
                    "operations.vendor_project_workspace.bidding_closed",
                    "operations.vendor_project_workspace.quote_not_editable",
                    "operations.vendor_project_workspace.quote_not_submittable",
                    "operations.vendor_project_workspace.quote_not_reviewable",
                    "operations.vendor_project_workspace.quote_line_required",
                    "operations.vendor_project_workspace.route_revision_not_draft",
                    "operations.vendor_project_workspace.route_revision_not_reviewable",
                    "operations.vendor_project_workspace.as_built_evidence_required",
                    "operations.vendor_project_workspace.as_built_submission_not_allowed",
                    "operations.vendor_project_workspace.as_built_not_reviewable",
                    "operations.vendor_project_workspace.vendor_assignment_required",
                    "operations.vendor_project_workspace.reason_required",
                    "operations.vendor_project_workspace.reason_too_long",
                    "operations.vendor_project_workspace.unsupported_action",
                    "operations.vendor_project_workspace.invalid_as_built_route",
                    "operations.vendor_project_workspace.invalid_write_evidence",
                    "operations.vendor_project_workspace.project_not_releasable",
                    "operations.vendor_project_workspace.items_required",
                    "operations.vendor_project_workspace.invalid_quantity",
                    "operations.vendor_project_workspace.project_not_advanceable",
                    "operations.vendor_project_workspace.approved_quote_required",
                    "operations.vendor_project_workspace.advance_ceiling_exceeded",
                    "operations.vendor_project_workspace.invalid_amount",
                    "operations.vendor_project_workspace.invalid_command_context",
                    "operations.vendor_project_workspace.command_contract_violation",
                    "operations.vendor_project_workspace.nested_owner_command",
                    "operations.vendor_project_workspace.active_caller_transaction",
                    "operations.vendor_project_workspace.nested_transaction_completion",
                ),
                mapping_owner=(
                    "app.api.vendor_portal, app.api.field.manager, "
                    "app.web.vendor_portal, and "
                    "app.web.admin.vendor_operations"
                ),
                fail_closed_on=(
                    "missing or mismatched vendor project records",
                    "assignment or source-state mismatch",
                    "missing quote lines or as-built evidence",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "vendor API and web adapters calling transport-coded "
                    "service methods with helper-owned commits"
                ),
                new_owner="operations.vendor_project_workspace",
                verification=(
                    "Typed command, preview, adapter mapping, transaction, "
                    "participant caller, and architecture boundary tests."
                ),
                cutover_gate=(
                    "Every public mutation adapter passes a typed command on a "
                    "clean session and direct quote/as-built submit APIs return "
                    "signed proposals."
                ),
                fallback_retirement=(
                    "Service HTTP exceptions, direct commits, optional commit "
                    "flags, untyped mutation methods, and direct submission "
                    "adapter paths are removed."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/designs/VENDOR_PROJECT_REVIEW_UI.md",
                "docs/designs/UI_PROJECTION_CONTRACTS.md",
                "docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md",
                "docs/designs/VENDOR_SUPPLY_UI.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_vendor_project_workspace.py",
                "tests/test_vendor_submission_proposals.py",
                "tests/test_vendor_project_review.py",
                "tests/test_vendor_as_built_review.py",
                "tests/test_vendor_route_revision_authoring.py",
                "tests/architecture/test_vendor_project_workspace_boundary.py",
                "tests/test_vendor_action_eligibility.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_project_records",
        module="app.services.vendor_project_records",
        owns=(
            "vendor installation-project quote lifecycle",
            "proposed vendor route-revision lifecycle",
            "staff proposed-route review state and immutable evidence",
            "vendor as-built route and line-item lifecycle",
            "staff as-built review state and immutable evidence",
        ),
        depends_on=(
            "events.dispatcher",
            "operations.project_lifecycle",
        ),
        notes=(
            "Canonical record writers are non-public participants of the "
            "workspace and signed-submission coordinators."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="vendor installation-project quote lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "validated vendor project record transition",
                        "canonical installation-project lifecycle state",
                    ),
                    canonical_writer="operations.vendor_project_records",
                ),
                ConcernContract(
                    name="proposed vendor route-revision lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "validated vendor project record transition",
                        "canonical installation-project lifecycle state",
                    ),
                    canonical_writer="operations.vendor_project_records",
                ),
                ConcernContract(
                    name=("staff proposed-route review state and immutable evidence"),
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "validated vendor project record transition",
                        "canonical installation-project lifecycle state",
                    ),
                    canonical_writer="operations.vendor_project_records",
                ),
                ConcernContract(
                    name="vendor as-built route and line-item lifecycle",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "validated vendor project record transition",
                        "canonical installation-project lifecycle state",
                    ),
                    canonical_writer="operations.vendor_project_records",
                ),
                ConcernContract(
                    name="staff as-built review state and immutable evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "validated vendor project record transition",
                        "canonical installation-project lifecycle state",
                    ),
                    canonical_writer="operations.vendor_project_records",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="validated vendor project record transition",
                    owner="operations.vendor_project_workspace",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed workspace command or signed-confirmation command "
                        "after owner policy and principal validation"
                    ),
                ),
                AuthorityInput(
                    name="canonical installation-project lifecycle state",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active InstallationProject assignment, status, "
                        "and native project references"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The workspace or signed-submission coordinator owns the root "
                    "transaction; record writers stage rows and events and flush."
                ),
                locking=(
                    "All writers lock the parent InstallationProject, ProjectQuote, "
                    "or ProposedRouteRevision before changing child or status rows."
                ),
                idempotency=(
                    "Source-state checks make transitions no-op or terminal on "
                    "replay; signed submissions additionally use the coordinator jti."
                ),
                retries=(
                    "The named coordinator retries the complete command; record "
                    "participants never retry or complete transactions."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_project_workspace.project_not_found",
                    "operations.vendor_project_workspace.quote_not_found",
                    "operations.vendor_project_workspace.quote_line_not_found",
                    "operations.vendor_project_workspace.route_revision_not_found",
                    "operations.vendor_project_workspace.project_not_assigned",
                    "operations.vendor_project_workspace.quote_creation_not_allowed",
                    "operations.vendor_project_workspace.bidding_closed",
                    "operations.vendor_project_workspace.quote_not_editable",
                    "operations.vendor_project_workspace.quote_not_submittable",
                    "operations.vendor_project_workspace.quote_not_reviewable",
                    "operations.vendor_project_workspace.quote_line_required",
                    "operations.vendor_project_workspace.route_revision_not_draft",
                    "operations.vendor_project_workspace.route_revision_not_reviewable",
                    "operations.vendor_project_workspace.as_built_evidence_required",
                    "operations.vendor_project_workspace.as_built_submission_not_allowed",
                    "operations.vendor_project_workspace.as_built_not_reviewable",
                    "operations.vendor_project_workspace.vendor_assignment_required",
                    "operations.vendor_project_workspace.reason_required",
                    "operations.vendor_project_workspace.reason_too_long",
                    "operations.vendor_project_workspace.unsupported_action",
                    "operations.vendor_project_workspace.invalid_as_built_route",
                    "operations.vendor_project_workspace.invalid_write_evidence",
                ),
                mapping_owner=(
                    "operations.vendor_project_workspace and "
                    "operations.vendor_submission_confirmation"
                ),
                fail_closed_on=(
                    "missing or mismatched record",
                    "assignment or state conflict",
                    "invalid route or missing submission evidence",
                ),
            ),
            events=EventContract(
                event_types=(
                    "vendor_quote.changed",
                    "vendor_route_revision.changed",
                    "vendor_route_revision.accepted",
                    "vendor_route_revision.rejected",
                    "vendor_as_built.submitted",
                    "vendor_as_built.accepted",
                    "vendor_as_built.rejected",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and carries action, aggregate, "
                    "project, vendor, command, and correlation identifiers."
                ),
                replay=(
                    "Canonical quote, route-revision, and as-built rows rebuild "
                    "the current record state; events retain transition evidence."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.vendor_portal_operations methods that committed "
                    "or conditionally flushed their own writes"
                ),
                new_owner="operations.vendor_project_records",
                verification=(
                    "Single-writer, lock, event, no-commit, coordinator caller, "
                    "and rollback tests."
                ),
                cutover_gate=(
                    "Only typed workspace commands and signed confirmation call "
                    "record staging paths."
                ),
                fallback_retirement=(
                    "Direct adapter calls to staging methods and all helper-level "
                    "transaction completion are removed."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/designs/VENDOR_PROJECT_REVIEW_UI.md",
                "docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_vendor_project_workspace.py",
                "tests/test_vendor_submission_proposals.py",
                "tests/test_vendor_route_review.py",
                "tests/test_vendor_as_built_review.py",
                "tests/test_vendor_route_revision_authoring.py",
                "tests/architecture/test_vendor_project_workspace_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_purchase_invoices",
        module="app.services.vendor_purchase_invoices",
        owns=(
            "vendor purchase-invoice read and action projections",
            "vendor purchase-invoice mutation coordination",
            "purchase-invoice submission eligibility and financial preview",
            "vendor-facing payables-status observation projection",
        ),
        depends_on=(
            "auth.permission_gate",
            "control.settings_spec",
            "integration.dotmac_erp_payables_adapter",
            "operations.vendor_project_lifecycle",
            "operations.vendor_purchase_invoice_records",
            "ui.projection_contracts",
        ),
        notes=(
            "This service owns purchase-invoice queries, action policy, and "
            "typed public-command coordination. Canonical invoice writers "
            "participate in its transaction or the signed confirmation "
            "coordinator's transaction. Vendor-facing payment state is "
            "rendered only from the timestamped ERP observation and shared "
            "UI projection vocabulary."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="vendor purchase-invoice read and action projections",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical vendor purchase-invoice records",
                        "canonical installation-project lifecycle state",
                    ),
                ),
                ConcernContract(
                    name="vendor purchase-invoice mutation coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated purchase-invoice command context",
                        "canonical vendor purchase-invoice records",
                        "canonical installation-project lifecycle state",
                        "purchase-invoice currency policy",
                        "purchase-invoice mutation protocol",
                    ),
                ),
                ConcernContract(
                    name=(
                        "purchase-invoice submission eligibility and financial preview"
                    ),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical vendor purchase-invoice records",
                        "canonical installation-project lifecycle state",
                        "purchase-invoice mutation protocol",
                    ),
                ),
                ConcernContract(
                    name="vendor-facing payables-status observation projection",
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "canonical vendor purchase-invoice records",
                        "timestamped ERP accounts-payable observation",
                        "UI payment-state projection vocabulary",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated purchase-invoice command context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "authenticated principal, vendor scope, reason, command, "
                        "and correlation identifiers"
                    ),
                ),
                AuthorityInput(
                    name="canonical vendor purchase-invoice records",
                    owner="operations.vendor_purchase_invoice_records",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "VendorPurchaseInvoice, active line-item, attachment link, "
                        "review, and ERP request evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical installation-project lifecycle state",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active InstallationProject assignment, eligible vendor "
                        "quote, and ERP purchase-order reference"
                    ),
                ),
                AuthorityInput(
                    name="purchase-invoice currency policy",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="billing.default_currency unless the command supplies one",
                ),
                AuthorityInput(
                    name="purchase-invoice mutation protocol",
                    owner="operations.vendor_purchase_invoices",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "editable, submittable, reviewable, attachment, invoice "
                        "number, and amount invariants"
                    ),
                ),
                AuthorityInput(
                    name="timestamped ERP accounts-payable observation",
                    owner=("integration.dotmac_erp_payables_adapter"),
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "validated ERP status, total, paid, balance, source "
                        "timestamp, observation timestamp, and refresh error"
                    ),
                ),
                AuthorityInput(
                    name="UI payment-state projection vocabulary",
                    owner="ui.projection_contracts",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "StateValue availability and freshness plus canonical "
                        "ERP supplier-invoice status presentation"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Each typed public purchase-invoice command enters one "
                    "verified coordinator transaction and invokes record staging."
                ),
                locking=(
                    "Commands lock the vendor, InstallationProject, and invoice "
                    "aggregate needed by their uniqueness or state decision."
                ),
                idempotency=(
                    "Creation replays to the active project/vendor invoice; signed "
                    "submission uses confirmation jti evidence and review requires "
                    "the exact source status."
                ),
                retries=(
                    "Policy rejections are terminal. Database or object-transport "
                    "failures retry the complete typed command with its context."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_purchase_invoices.invoice_not_found",
                    "operations.vendor_purchase_invoices.invoice_line_not_found",
                    "operations.vendor_purchase_invoices.attachment_not_found",
                    "operations.vendor_purchase_invoices.project_not_found",
                    "operations.vendor_purchase_invoices.invoice_not_editable",
                    "operations.vendor_purchase_invoices.invoice_not_reviewable",
                    "operations.vendor_purchase_invoices.invoice_number_required",
                    "operations.vendor_purchase_invoices.invoice_number_conflict",
                    "operations.vendor_purchase_invoices.submitted_quote_required",
                    "operations.vendor_purchase_invoices.invoice_line_required",
                    "operations.vendor_purchase_invoices.empty_attachment",
                    "operations.vendor_purchase_invoices.invalid_attachment",
                    "operations.vendor_purchase_invoices.invalid_write_evidence",
                    "operations.vendor_purchase_invoices.invalid_command_context",
                    "operations.vendor_purchase_invoices.command_contract_violation",
                    "operations.vendor_purchase_invoices.nested_owner_command",
                    "operations.vendor_purchase_invoices.active_caller_transaction",
                    "operations.vendor_purchase_invoices.nested_transaction_completion",
                ),
                mapping_owner=(
                    "app.api.vendor_portal, app.api.field.manager, "
                    "app.web.vendor_portal, and app.web.admin.vendor_operations"
                ),
                fail_closed_on=(
                    "missing or mismatched invoice, project, vendor, or line",
                    "duplicate invoice number or invalid source status",
                    "missing eligible quote, line, attachment, or currency evidence",
                    "active caller transaction or manifest mismatch",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "transport-coded purchase-invoice service methods with direct "
                    "commits, rollback recovery, and generic attribute mutation"
                ),
                new_owner="operations.vendor_purchase_invoices",
                verification=(
                    "Typed-command, currency-setting, signed-submit, transaction, "
                    "adapter, participant, event, rollback, and vendor payment "
                    "visibility tests."
                ),
                cutover_gate=(
                    "Every public mutation adapter passes a typed command on a "
                    "clean session and direct submit returns a signed proposal."
                ),
                fallback_retirement=(
                    "Service HTTP exceptions, direct commit/rollback, generic "
                    "setattr, direct submit, and split approval/enqueue paths are "
                    "removed; ERP creation responses no longer imply settlement "
                    "state."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_phase5_vendor_purchase_invoices.py",
                "tests/test_vendor_payment_visibility.py",
                "tests/test_vendor_submission_proposals.py",
                "tests/architecture/test_vendor_purchase_invoice_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_purchase_invoice_records",
        module="app.services.vendor_purchase_invoice_records",
        owns=(
            "vendor purchase-invoice lifecycle",
            "vendor purchase-invoice line-item lifecycle",
            "purchase-invoice attachment and ERP request evidence",
        ),
        depends_on=(
            "events.dispatcher",
            "operations.vendor_project_lifecycle",
        ),
        notes=(
            "Canonical invoice record writers are non-public participants of "
            "the purchase-invoice and signed-confirmation coordinators."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="vendor purchase-invoice lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "validated purchase-invoice transition",
                        "canonical installation-project lifecycle state",
                    ),
                    canonical_writer=("operations.vendor_purchase_invoice_records"),
                ),
                ConcernContract(
                    name="vendor purchase-invoice line-item lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "validated purchase-invoice transition",
                        "canonical installation-project lifecycle state",
                    ),
                    canonical_writer=("operations.vendor_purchase_invoice_records"),
                ),
                ConcernContract(
                    name="purchase-invoice attachment and ERP request evidence",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "validated purchase-invoice transition",
                        "canonical installation-project lifecycle state",
                    ),
                    canonical_writer=("operations.vendor_purchase_invoice_records"),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="validated purchase-invoice transition",
                    owner="operations.vendor_purchase_invoices",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed invoice command or signed-confirmation command "
                        "after owner policy and principal validation"
                    ),
                ),
                AuthorityInput(
                    name="canonical installation-project lifecycle state",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked InstallationProject assignment, quote eligibility, "
                        "and ERP purchase-order reference"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The invoice or signed-submission coordinator owns the root "
                    "transaction; record writers stage rows, outbox evidence, and "
                    "events and flush."
                ),
                locking=(
                    "Writers lock vendor, project, and invoice aggregates before "
                    "uniqueness checks, eligibility decisions, or mutations."
                ),
                idempotency=(
                    "Project/vendor creation returns the existing active invoice; "
                    "content-addressed object keys and outbox idempotency keys make "
                    "whole-command retry safe."
                ),
                retries=(
                    "The named coordinator retries the complete command; record "
                    "participants never retry or complete transactions."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_purchase_invoices.invoice_not_found",
                    "operations.vendor_purchase_invoices.invoice_line_not_found",
                    "operations.vendor_purchase_invoices.project_not_found",
                    "operations.vendor_purchase_invoices.invoice_not_editable",
                    "operations.vendor_purchase_invoices.invoice_not_reviewable",
                    "operations.vendor_purchase_invoices.invoice_number_required",
                    "operations.vendor_purchase_invoices.invoice_number_conflict",
                    "operations.vendor_purchase_invoices.submitted_quote_required",
                    "operations.vendor_purchase_invoices.invoice_line_required",
                    "operations.vendor_purchase_invoices.empty_attachment",
                    "operations.vendor_purchase_invoices.invalid_attachment",
                    "operations.vendor_purchase_invoices.invalid_write_evidence",
                ),
                mapping_owner=(
                    "operations.vendor_purchase_invoices and "
                    "operations.vendor_submission_confirmation"
                ),
                fail_closed_on=(
                    "missing or mismatched invoice, project, vendor, or line",
                    "duplicate invoice number or invalid source status",
                    "missing quote, line, currency, or attachment evidence",
                ),
            ),
            events=EventContract(
                event_types=(
                    "vendor_purchase_invoice.changed",
                    "vendor_purchase_invoice.approved",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and carries action, invoice, project, "
                    "vendor, status, command, and correlation identifiers."
                ),
                replay=(
                    "Canonical invoice, line, attachment-link, review, and ERP "
                    "outbox rows rebuild current state; events retain transition "
                    "evidence."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.vendor_purchase_invoices methods that wrote rows, "
                    "files, reviews, and ERP requests while completing transactions"
                ),
                new_owner="operations.vendor_purchase_invoice_records",
                verification=(
                    "Single-writer, lock, event, staged-file, atomic ERP enqueue, "
                    "participant caller, and rollback tests."
                ),
                cutover_gate=(
                    "Only typed invoice commands and signed confirmation call "
                    "record staging paths."
                ),
                fallback_retirement=(
                    "Direct adapter staging calls, helper commits/rollbacks, and "
                    "pre-commit physical attachment deletion are removed."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_phase5_vendor_purchase_invoices.py",
                "tests/test_vendor_submission_proposals.py",
                "tests/architecture/test_vendor_purchase_invoice_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_submission_confirmation",
        module="app.services.vendor_submission_proposals",
        owns=(
            "short-lived signed vendor submission proposal",
            "vendor submission stale-preview verification",
            "vendor submission idempotency and replay result",
        ),
        depends_on=(
            "auth.permission_gate",
            "auth.token_signing",
            "events.dispatcher",
            "operations.vendor_project_lifecycle",
            "operations.vendor_project_records",
            "operations.vendor_project_workspace",
            "operations.vendor_purchase_invoices",
        ),
        notes=(
            "Web adapters only request a preview or confirm its signed "
            "proposal. Domain owners recheck under lock and commit the "
            "mutation with its idempotency result."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="short-lived signed vendor submission proposal",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated vendor principal context",
                        "vendor project workspace submission preview",
                        "vendor project lifecycle submission preview",
                        "vendor purchase-invoice submission preview",
                        "capability signing envelope",
                        "vendor confirmation protocol invariants",
                    ),
                ),
                ConcernContract(
                    name="vendor submission stale-preview verification",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated vendor principal context",
                        "vendor project workspace submission preview",
                        "vendor project lifecycle submission preview",
                        "vendor purchase-invoice submission preview",
                        "capability signing envelope",
                    ),
                ),
                ConcernContract(
                    name="vendor submission idempotency and replay result",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated vendor principal context",
                        "vendor project workspace submission preview",
                        "vendor project lifecycle submission preview",
                        "vendor purchase-invoice submission preview",
                        "capability signing envelope",
                        "canonical vendor submission replay record",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated vendor principal context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "authenticated vendor, vendor-user, scope, reason, "
                        "command, and correlation identifiers"
                    ),
                ),
                AuthorityInput(
                    name="vendor project workspace submission preview",
                    owner="operations.vendor_project_workspace",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=("locked quote or as-built impact and state fingerprint"),
                ),
                AuthorityInput(
                    name="vendor project lifecycle submission preview",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "locked installation-project lifecycle impact and state "
                        "fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="vendor purchase-invoice submission preview",
                    owner="operations.vendor_purchase_invoices",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "locked purchase-invoice financial impact and state fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="capability signing envelope",
                    owner="auth.token_signing",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="configured context-signing key and algorithm",
                ),
                AuthorityInput(
                    name="vendor confirmation protocol invariants",
                    owner="operations.vendor_submission_confirmation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "versioned purpose, issuer, claim allowlist, maximum "
                        "token size, ten-minute lifetime, and submission scopes"
                    ),
                ),
                AuthorityInput(
                    name="canonical vendor submission replay record",
                    owner="operations.vendor_submission_confirmation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "IdempotencyKey row keyed by signed proposal jti and "
                        "submission scope"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "A typed confirmation command enters one verified owner "
                    "transaction. Locked stale verification, replay reservation, "
                    "domain mutation, result evidence, and event commit together."
                ),
                locking=(
                    "The delegated domain preview locks the exact project, "
                    "quote, or invoice aggregate before the coordinator reserves "
                    "the signed proposal jti."
                ),
                idempotency=(
                    "The signed proposal jti and submission scope identify one "
                    "stable result. Exact replay returns that result without "
                    "rerunning the mutation."
                ),
                retries=(
                    "Expired, malformed, context-mismatched, or stale proposals "
                    "are terminal. Database concurrency failures retry the whole "
                    "owner command; delivery retries use the stable result."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_submission_confirmation.unsupported_submission_type",
                    "operations.vendor_submission_confirmation.invalid_proposal",
                    "operations.vendor_submission_confirmation.expired_proposal",
                    "operations.vendor_submission_confirmation.proposal_context_mismatch",
                    "operations.vendor_submission_confirmation.confirmation_in_progress",
                    "operations.vendor_submission_confirmation.invalid_payload",
                    "operations.vendor_submission_confirmation.stale_proposal",
                    "operations.vendor_submission_confirmation.missing_result_evidence",
                    "operations.vendor_submission_confirmation.lifecycle_not_found",
                    "operations.vendor_submission_confirmation.lifecycle_not_assigned",
                    "operations.vendor_submission_confirmation.lifecycle_unsupported_action",
                    "operations.vendor_submission_confirmation.lifecycle_actor_required",
                    "operations.vendor_submission_confirmation.lifecycle_invalid_transition",
                    "operations.vendor_submission_confirmation.invalid_command_context",
                    "operations.vendor_submission_confirmation.command_contract_violation",
                    "operations.vendor_submission_confirmation.nested_owner_command",
                    "operations.vendor_submission_confirmation.active_caller_transaction",
                    "operations.vendor_submission_confirmation.nested_transaction_completion",
                ),
                mapping_owner="app.web.vendor_portal",
                fail_closed_on=(
                    "invalid or expired signed proposal",
                    "vendor, user, project, or target mismatch",
                    "state fingerprint drift",
                    "ambiguous concurrent confirmation",
                    "missing stable result evidence",
                ),
            ),
            events=EventContract(
                event_types=("vendor_submission.confirmed",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and contains submission, project, "
                    "stable result, command, and correlation identifiers only."
                ),
                replay=(
                    "The idempotency row is authoritative for command replay; "
                    "domain records and lifecycle events rebuild the outcome."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "app.services.vendor_submission_proposals direct HTTP errors, "
                    "helper rollback, and service-owned commit"
                ),
                new_owner="operations.vendor_submission_confirmation",
                verification=(
                    "Proposal scope, expiry, stale-state, replay, rollback, event, "
                    "web mapping, and architecture boundary tests."
                ),
                cutover_gate=(
                    "Vendor confirmation routes pass a typed command on a clean "
                    "session and all mutation branches return stable result evidence."
                ),
                fallback_retirement=(
                    "Transport-coded errors, direct commit/rollback, and mutation "
                    "before locked stale verification are removed."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/designs/UI_PROJECTION_CONTRACTS.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_vendor_submission_proposals.py",
                "tests/architecture/test_vendor_submission_confirmation_boundary.py",
                "tests/test_vendor_lifecycle.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_supply_review_confirmation",
        module="app.services.vendor_supply_review_proposals",
        owns=(
            "short-lived signed vendor supply review proposal",
            "vendor supply review stale-preview verification",
            "vendor supply review idempotency and replay result",
        ),
        depends_on=(
            "auth.permission_gate",
            "auth.token_signing",
            "operations.vendor_advances",
            "operations.vendor_material_release",
            "ui.vendor_supply_projection",
        ),
        notes=(
            "This coordinator cannot decide stock issue or payment. It binds "
            "an authenticated staff actor to an exact material-release or "
            "advance preview, revalidates it under lock, and invokes the "
            "declaring participant owner once."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="short-lived signed vendor supply review proposal",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated vendor supply review context",
                        "canonical vendor supply review preview",
                        "capability signing envelope",
                    ),
                ),
                ConcernContract(
                    name="vendor supply review stale-preview verification",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "canonical vendor supply review preview",
                        "capability signing envelope",
                    ),
                ),
                ConcernContract(
                    name="vendor supply review idempotency and replay result",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated vendor supply review context",
                        "canonical vendor supply review preview",
                        "vendor supply review replay record",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated vendor supply review context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "authenticated staff actor, inventory or accounts-payable "
                        "permission, action, command, and correlation identifiers"
                    ),
                ),
                AuthorityInput(
                    name="canonical vendor supply review preview",
                    owner="ui.vendor_supply_projection",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "exact release lines or advance amount, project/vendor "
                        "identity, allowance facts, target action, and fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="capability signing envelope",
                    owner="auth.token_signing",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="configured context-signing key and algorithm",
                ),
                AuthorityInput(
                    name="vendor supply review replay record",
                    owner="operations.vendor_supply_review_confirmation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "IdempotencyKey row keyed by proposal jti, supply type, "
                        "and review action"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "A typed confirmation command owns locked stale verification, "
                    "replay reservation, participant mutation, and one root commit."
                ),
                locking=(
                    "The material release or advance is locked before fingerprint "
                    "comparison and participant review."
                ),
                idempotency=(
                    "Signed jti plus supply type and action identifies one stable "
                    "review result."
                ),
                retries=(
                    "Invalid or stale proposals are terminal; concurrency failures "
                    "retry the complete typed confirmation."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_supply_review_confirmation.actor_required",
                    "operations.vendor_supply_review_confirmation.unsupported_supply_type",
                    "operations.vendor_supply_review_confirmation.unsupported_action",
                    "operations.vendor_supply_review_confirmation.invalid_proposal",
                    "operations.vendor_supply_review_confirmation.expired_proposal",
                    "operations.vendor_supply_review_confirmation.proposal_context_mismatch",
                    "operations.vendor_supply_review_confirmation.confirmation_in_progress",
                    "operations.vendor_supply_review_confirmation.stale_proposal",
                    "operations.vendor_supply_review_confirmation.material_release_not_found",
                    "operations.vendor_supply_review_confirmation.advance_not_found",
                    "operations.vendor_supply_review_confirmation.material_not_reviewable",
                    "operations.vendor_supply_review_confirmation.advance_not_reviewable",
                    "operations.vendor_supply_review_confirmation.reason_required",
                    "operations.vendor_supply_review_confirmation.reason_too_long",
                    "operations.vendor_supply_review_confirmation.not_reviewable",
                    "operations.vendor_supply_review_confirmation.invalid_command_context",
                    "operations.vendor_supply_review_confirmation.command_contract_violation",
                    "operations.vendor_supply_review_confirmation.nested_owner_command",
                    "operations.vendor_supply_review_confirmation.active_caller_transaction",
                    "operations.vendor_supply_review_confirmation.nested_transaction_completion",
                ),
                mapping_owner="app.web.admin.vendor_operations",
                fail_closed_on=(
                    "invalid, expired, or context-mismatched proposal",
                    "request, quote allowance, or lifecycle drift",
                    "ambiguous concurrent confirmation",
                ),
            ),
            events=EventContract(
                event_types=(
                    "vendor_material_release.reviewed",
                    "vendor_advance.reviewed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries record, project, vendor, decision, actor, "
                    "and amount where applicable."
                ),
                replay=(
                    "The supply record, outbox event, and idempotency row rebuild "
                    "the decision and replay result."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "admin supply routes calling participant committed wrappers"
                ),
                new_owner="operations.vendor_supply_review_confirmation",
                verification=(
                    "Preview, expiry, stale-state, replay, rollback, permission, "
                    "and adapter boundary tests."
                ),
                cutover_gate=(
                    "Every staff supply decision uses signed preview and typed "
                    "confirmation on a clean session."
                ),
                fallback_retirement=(
                    "Direct admin calls to material or advance committed wrappers "
                    "are removed."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/designs/VENDOR_SUPPLY_UI.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_vendor_supply_ui.py",
                "tests/architecture/test_vendor_supply_ui_boundary.py",
                "tests/test_vendor_delivery_portfolio.py",
                "tests/architecture/test_vendor_delivery_portfolio_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_project_review_confirmation",
        module="app.services.vendor_project_review_proposals",
        owns=(
            "short-lived signed staff project-review proposal",
            "staff project-review stale-preview verification",
            "staff project-review idempotency and replay result",
        ),
        depends_on=(
            "auth.permission_gate",
            "auth.token_signing",
            "operations.vendor_project_lifecycle",
            "operations.vendor_project_workspace",
        ),
        notes=(
            "This supporting service cannot decide project state. It binds "
            "an authenticated staff actor to the lifecycle owner's preview "
            "and invokes that owner once after lock-time revalidation."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="short-lived signed staff project-review proposal",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated staff review context",
                        "canonical staff project-review preview",
                        "capability signing envelope",
                        "staff project-review confirmation protocol",
                    ),
                ),
                ConcernContract(
                    name="staff project-review stale-preview verification",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated staff review context",
                        "canonical staff project-review preview",
                        "capability signing envelope",
                    ),
                ),
                ConcernContract(
                    name="staff project-review idempotency and replay result",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated staff review context",
                        "canonical staff project-review preview",
                        "capability signing envelope",
                        "canonical staff project-review replay record",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated staff review context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "authenticated staff actor, action, reason, command, "
                        "and correlation identifiers"
                    ),
                ),
                AuthorityInput(
                    name="canonical staff project-review preview",
                    owner="operations.vendor_project_workspace",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "locked project verification or rework impact, work-order "
                        "evidence policy, and state fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="capability signing envelope",
                    owner="auth.token_signing",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="configured context-signing key and algorithm",
                ),
                AuthorityInput(
                    name="staff project-review confirmation protocol",
                    owner="operations.vendor_project_review_confirmation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "versioned purpose, issuer, claim allowlist, ten-minute "
                        "lifetime, and verify/rework scopes"
                    ),
                ),
                AuthorityInput(
                    name="canonical staff project-review replay record",
                    owner="operations.vendor_project_review_confirmation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "IdempotencyKey row keyed by signed proposal jti and "
                        "staff project-review scope"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "A typed confirmation command owns locked stale verification, "
                    "replay reservation, lifecycle participant mutation, result "
                    "evidence, and one root commit."
                ),
                locking=(
                    "The InstallationProject aggregate is locked before replay "
                    "reservation and fingerprint comparison."
                ),
                idempotency=(
                    "Signed jti plus verify/rework scope identifies one immutable "
                    "lifecycle-event result."
                ),
                retries=(
                    "Invalid or stale proposals are terminal; concurrency failures "
                    "retry the complete typed command."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_project_review_confirmation.invalid_proposal",
                    "operations.vendor_project_review_confirmation.expired_proposal",
                    "operations.vendor_project_review_confirmation.proposal_context_mismatch",
                    "operations.vendor_project_review_confirmation.confirmation_in_progress",
                    "operations.vendor_project_review_confirmation.stale_proposal",
                    "operations.vendor_project_review_confirmation.missing_result_evidence",
                    "operations.vendor_project_review_confirmation.invalid_command_context",
                    "operations.vendor_project_review_confirmation.command_contract_violation",
                    "operations.vendor_project_review_confirmation.nested_owner_command",
                    "operations.vendor_project_review_confirmation.active_caller_transaction",
                    "operations.vendor_project_review_confirmation.nested_transaction_completion",
                ),
                mapping_owner="app.web.admin.vendor_operations",
                fail_closed_on=(
                    "invalid, expired, or context-mismatched proposal",
                    "project state, policy, or evidence drift",
                    "ambiguous concurrent confirmation",
                ),
            ),
            events=EventContract(
                event_types=(
                    "vendor_project.verified",
                    "vendor_project.rework_requested",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries project, vendor, transition, actor, reason, "
                    "and verification-evidence fields additively."
                ),
                replay=(
                    "InstallationProjectLifecycleEvent and the idempotency row "
                    "rebuild the decision and stable replay result."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "staff review proposal helper with transport-coded errors, "
                    "helper rollback, and direct commit"
                ),
                new_owner="operations.vendor_project_review_confirmation",
                verification=(
                    "Proposal, stale-state, evidence-policy, replay, rollback, "
                    "event, and adapter-mapping tests."
                ),
                cutover_gate=(
                    "Staff confirmation routes pass a typed command on a clean "
                    "session and lifecycle writes remain participant-only."
                ),
                fallback_retirement=(
                    "Untyped confirmation arguments and helper-owned rollback or "
                    "manual commit paths are removed."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_vendor_project_review.py",
                "tests/architecture/test_vendor_project_lifecycle_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_route_review_confirmation",
        module="app.services.vendor_route_review_proposals",
        owns=(
            "short-lived signed staff proposed-route review proposal",
            "staff proposed-route review stale-preview verification",
            "staff proposed-route review idempotency and replay result",
        ),
        depends_on=(
            "auth.permission_gate",
            "auth.token_signing",
            "operations.vendor_project_records",
            "operations.vendor_project_workspace",
        ),
        notes=(
            "This supporting service carries no quote or project decision "
            "policy. It binds staff to the vendor operations owner's "
            "proposed-route preview and invokes that owner after revalidation."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("short-lived signed staff proposed-route review proposal"),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated staff proposed-route review context",
                        "canonical staff proposed-route review preview",
                        "capability signing envelope",
                        "staff proposed-route review confirmation protocol",
                    ),
                ),
                ConcernContract(
                    name=("staff proposed-route review stale-preview verification"),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated staff proposed-route review context",
                        "canonical staff proposed-route review preview",
                        "capability signing envelope",
                    ),
                ),
                ConcernContract(
                    name=("staff proposed-route review idempotency and replay result"),
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated staff proposed-route review context",
                        "canonical staff proposed-route review preview",
                        "capability signing envelope",
                        "canonical staff proposed-route review replay record",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated staff proposed-route review context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "authenticated staff actor, action, reason, command, "
                        "and correlation identifiers"
                    ),
                ),
                AuthorityInput(
                    name="canonical staff proposed-route review preview",
                    owner="operations.vendor_project_workspace",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "locked proposed-route state, immutable evidence impact, "
                        "geometry identity, and state fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="capability signing envelope",
                    owner="auth.token_signing",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="configured context-signing key and algorithm",
                ),
                AuthorityInput(
                    name="staff proposed-route review confirmation protocol",
                    owner="operations.vendor_route_review_confirmation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "versioned purpose, issuer, claim allowlist, ten-minute "
                        "lifetime, and accept/reject scopes"
                    ),
                ),
                AuthorityInput(
                    name="canonical staff proposed-route review replay record",
                    owner="operations.vendor_route_review_confirmation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "IdempotencyKey row keyed by signed proposal jti and "
                        "staff proposed-route review scope"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "A typed confirmation command owns locked stale "
                    "verification, replay reservation, record participant "
                    "mutation, result evidence, and one root commit."
                ),
                locking=(
                    "The ProposedRouteRevision aggregate is locked before "
                    "replay reservation and fingerprint comparison."
                ),
                idempotency=(
                    "Signed jti plus accept/reject scope identifies one "
                    "immutable review-event result."
                ),
                retries=(
                    "Invalid or stale proposals are terminal; concurrency "
                    "failures retry the complete typed command."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_route_review_confirmation.invalid_proposal",
                    "operations.vendor_route_review_confirmation.expired_proposal",
                    "operations.vendor_route_review_confirmation.proposal_context_mismatch",
                    "operations.vendor_route_review_confirmation.confirmation_in_progress",
                    "operations.vendor_route_review_confirmation.stale_proposal",
                    "operations.vendor_route_review_confirmation.missing_result_evidence",
                    "operations.vendor_route_review_confirmation.invalid_command_context",
                    "operations.vendor_route_review_confirmation.command_contract_violation",
                    "operations.vendor_route_review_confirmation.nested_owner_command",
                    "operations.vendor_route_review_confirmation.active_caller_transaction",
                    "operations.vendor_route_review_confirmation.nested_transaction_completion",
                ),
                mapping_owner="app.web.admin.vendor_operations",
                fail_closed_on=(
                    "invalid, expired, or context-mismatched proposal",
                    "proposed-route state, geometry, or evidence drift",
                    "ambiguous concurrent confirmation",
                ),
            ),
            events=EventContract(
                event_types=(
                    "vendor_route_revision.accepted",
                    "vendor_route_revision.rejected",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries revision, quote, project, vendor, "
                    "transition, actor, and reason fields additively."
                ),
                replay=(
                    "ProposedRouteRevisionReviewEvent and the idempotency "
                    "row rebuild the decision and stable replay result."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "read-only staff route page with no proposed-route decision owner"
                ),
                new_owner="operations.vendor_route_review_confirmation",
                verification=(
                    "Proposal, stale-state, replay, rollback, "
                    "immutable-evidence, event, and adapter-mapping tests."
                ),
                cutover_gate=(
                    "Staff confirmation routes pass a typed command on a "
                    "clean session and route writes remain participant-only."
                ),
                fallback_retirement=(
                    "Direct route-page status mutation and unsigned "
                    "accept/reject paths are absent."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/designs/VENDOR_PROJECT_REVIEW_UI.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_vendor_route_review.py",
                "tests/architecture/test_vendor_project_workspace_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="operations.vendor_as_built_review_confirmation",
        module="app.services.vendor_as_built_review_proposals",
        owns=(
            "short-lived signed staff as-built review proposal",
            "staff as-built review stale-preview verification",
            "staff as-built review idempotency and replay result",
        ),
        depends_on=(
            "auth.permission_gate",
            "auth.token_signing",
            "operations.vendor_project_records",
            "operations.vendor_project_workspace",
        ),
        notes=(
            "This supporting service carries no evidence or project "
            "decision policy. It binds staff to the vendor operations "
            "owner's preview and invokes that owner after revalidation."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="short-lived signed staff as-built review proposal",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated staff as-built review context",
                        "canonical staff as-built review preview",
                        "capability signing envelope",
                        "staff as-built review confirmation protocol",
                    ),
                ),
                ConcernContract(
                    name="staff as-built review stale-preview verification",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "authenticated staff as-built review context",
                        "canonical staff as-built review preview",
                        "capability signing envelope",
                    ),
                ),
                ConcernContract(
                    name="staff as-built review idempotency and replay result",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated staff as-built review context",
                        "canonical staff as-built review preview",
                        "capability signing envelope",
                        "canonical staff as-built review replay record",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated staff as-built review context",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "authenticated staff actor, action, reason, command, "
                        "and correlation identifiers"
                    ),
                ),
                AuthorityInput(
                    name="canonical staff as-built review preview",
                    owner="operations.vendor_project_workspace",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "locked as-built record state, immutable evidence impact, "
                        "and state fingerprint"
                    ),
                ),
                AuthorityInput(
                    name="capability signing envelope",
                    owner="auth.token_signing",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="configured context-signing key and algorithm",
                ),
                AuthorityInput(
                    name="staff as-built review confirmation protocol",
                    owner="operations.vendor_as_built_review_confirmation",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "versioned purpose, issuer, claim allowlist, ten-minute "
                        "lifetime, and accept/reject scopes"
                    ),
                ),
                AuthorityInput(
                    name="canonical staff as-built review replay record",
                    owner="operations.vendor_as_built_review_confirmation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "IdempotencyKey row keyed by signed proposal jti and "
                        "staff as-built review scope"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "A typed confirmation command owns locked stale verification, "
                    "replay reservation, record participant mutation, result "
                    "evidence, and one root commit."
                ),
                locking=(
                    "The AsBuiltRoute aggregate is locked before replay reservation "
                    "and fingerprint comparison."
                ),
                idempotency=(
                    "Signed jti plus accept/reject scope identifies one immutable "
                    "review-event result."
                ),
                retries=(
                    "Invalid or stale proposals are terminal; concurrency failures "
                    "retry the complete typed command."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "operations.vendor_as_built_review_confirmation.invalid_proposal",
                    "operations.vendor_as_built_review_confirmation.expired_proposal",
                    "operations.vendor_as_built_review_confirmation.proposal_context_mismatch",
                    "operations.vendor_as_built_review_confirmation.confirmation_in_progress",
                    "operations.vendor_as_built_review_confirmation.stale_proposal",
                    "operations.vendor_as_built_review_confirmation.missing_result_evidence",
                    "operations.vendor_as_built_review_confirmation.invalid_command_context",
                    "operations.vendor_as_built_review_confirmation.command_contract_violation",
                    "operations.vendor_as_built_review_confirmation.nested_owner_command",
                    "operations.vendor_as_built_review_confirmation.active_caller_transaction",
                    "operations.vendor_as_built_review_confirmation.nested_transaction_completion",
                ),
                mapping_owner="app.web.admin.vendor_operations",
                fail_closed_on=(
                    "invalid, expired, or context-mismatched proposal",
                    "as-built state or evidence drift",
                    "ambiguous concurrent confirmation",
                ),
            ),
            events=EventContract(
                event_types=(
                    "vendor_as_built.accepted",
                    "vendor_as_built.rejected",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries as-built, project, vendor, transition, "
                    "actor, and reason fields additively."
                ),
                replay=(
                    "AsBuiltRouteReviewEvent and the idempotency row rebuild the "
                    "decision and stable replay result."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "as-built review proposal helper with transport-coded errors, "
                    "helper rollback, and direct commit"
                ),
                new_owner="operations.vendor_as_built_review_confirmation",
                verification=(
                    "Proposal, stale-state, replay, rollback, immutable-evidence, "
                    "event, and adapter-mapping tests."
                ),
                cutover_gate=(
                    "Staff confirmation routes pass a typed command on a clean "
                    "session and as-built writes remain participant-only."
                ),
                fallback_retirement=(
                    "Untyped confirmation arguments and helper-owned rollback or "
                    "manual commit paths are removed."
                ),
            ),
            steward="vendor operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/adr/0002-owner-command-transaction-boundary.md",
            ),
            test_refs=(
                "tests/test_vendor_as_built_review.py",
                "tests/architecture/test_vendor_project_workspace_boundary.py",
            ),
        ),
    ),
)
