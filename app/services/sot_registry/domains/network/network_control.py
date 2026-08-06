"""network SOT declarations: network control."""

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

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="network.routeros_sot",
        module="app.services.router_management.sot_policy",
        owns=(
            "typed RouterOS desired-state contract",
            "managed RouterOS resource and field policy",
            "Dotmac RouterOS resource ownership identity",
        ),
        depends_on=(
            "network.identity",
            "runtime.db_sessions",
            "observability.recording",
        ),
        notes=(
            "Vendor-specific RouterOS desired state projects through the "
            "shared network.control_plane_intent lifecycle."
        ),
    ),
    SOTService(
        name="network.forwarding_topology",
        module="app.services.network.forwarding_topology",
        owns=(
            "reviewed downstream-to-upstream forwarding declarations",
            "normalized BGP-peer and routing-table observations",
            "forwarding declaration agreement and drift projection",
            "authoritative core, border, NAS, site, interface, and VRF graph",
            "official customer upstream path and outage ancestry",
        ),
        depends_on=(
            "network.identity",
            "network.monitoring_inventory",
            "network.radius_sessions",
            "network.control_plane_intent",
            "network.routeros_sot",
        ),
        notes=(
            "Declarations bind exact devices, interfaces, sites, roles, "
            "VRFs, configuration intent, and where applicable peer, "
            "route, and NAS identity. Preview is write-free; proposal "
            "and review are separated; execution locks and revalidates "
            "exact evidence. LLDP, BGP, routing-table, and RADIUS data "
            "remain observations and cannot create or retire official "
            "path. Configuration remains owned by control-plane intent "
            "and RouterOS SOT. Customer paths, reachability, and outage "
            "blast radius consume only agreeing declarations. The "
            "RouterOS collector is a GET-only, declaration-scoped "
            "observation adapter behind the fail-closed "
            "network.forwarding_observation_collection control; enabling "
            "it starts evidence shadowing, not authority cutover."
        ),
    ),
    SOTService(
        name="network.nas_inventory",
        module="app.services.nas.devices",
        owns=("NAS administrative lifecycle state", "NAS inventory reads"),
        depends_on=("network.identity",),
    ),
    SOTService(
        name="network.ppp_delivery_authorization",
        module="app.services.network.ppp_delivery_authorization",
        owns=(
            "delivery-time PPP termination authorization",
            "PPP delivery action-bundle membership",
        ),
        depends_on=(
            "network.ont_assignment_identity",
            "network.nas_inventory",
        ),
        notes=(
            "Second, independent half of the CPE dialer containment. The "
            "producer decides whether to STAGE a credential; this decides "
            "whether a staged plan may REACH a device, and does not trust "
            "the producer. delivery.pending_apply, stored desired values "
            "and credential fingerprints are evidence that something once "
            "wrote desired state, never authorization to deliver it: "
            "production carries 1,318 ONTs with pending_apply set and PPP "
            "credentials staged onto 1,373 services whose termination is "
            "not the ONT. Intent is read from OntWanServiceInstance, which "
            "already expresses connection_type=pppoe, rather than "
            "introducing another parallel field. OntAssignment.wan_mode, "
            "ip_mode and pppoe_username are deliberately NOT read: "
            "migration 084 copied them into desired config and then set "
            "them NULL, so surviving values are unexplained residue. The "
            "whole PPP bundle is gated -- ACS credential writes, object "
            "create/delete, NAT, OMCI provisioning and OLT service-port "
            "work -- because each can establish or disturb a termination "
            "on its own. Unrelated ONT reconciliation is untouched. An "
            "absent ruling is a refusal, so the gate is not skippable by a "
            "caller that forgot to resolve it."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="delivery-time PPP termination authorization",
                    role=OwnerRole.POLICY,
                    input_names=("active ONT WAN service instances",),
                ),
                ConcernContract(
                    name="PPP delivery action-bundle membership",
                    role=OwnerRole.POLICY,
                    input_names=("planned reconcile actions",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="active ONT WAN service instances",
                    owner="network.ont_assignment_identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="ont_wan_service_instances",
                ),
                AuthorityInput(
                    name="planned reconcile actions",
                    owner="network.ont_assignment_commands",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="reconcile planner Plan.actions",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Pure ruling. Reads service intent inside the "
                    "caller's session and writes nothing; the caller "
                    "carries the ruling into apply."
                ),
                locking="none",
                idempotency=(
                    "A ruling is a function of stored intent, so the "
                    "same inputs always yield the same decision."
                ),
                retries=(
                    "Not retryable and not retried: a refusal is an "
                    "answer, not a transient failure."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "no_active_service_intent",
                    "bridged_service_intent",
                    "no_active_assignment",
                    "ambiguous_assignment",
                    "unresolvable_ont",
                    "scope_mismatch",
                ),
                mapping_owner="app.services.network.reconcile.applier",
                fail_closed_on=(
                    "no ACTIVE owner-managed intent for the exact "
                    "ont+subscription pair, which includes every "
                    "pre-owner row quarantined as unverified",
                    "an active bridged service instance",
                    "no active subscriber assignment on the ONT",
                    "more than one active assignment, so no exact "
                    "service can be resolved",
                    "an ONT identity that cannot be resolved",
                    "a ruling presented for a different ONT, service "
                    "or credential scope than the one being delivered",
                    "an absent ruling at apply time",
                    "an action whose PPP purpose is indeterminate",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                new_owner="network.ppp_delivery_authorization",
                old_owner="implicit: staged desired state",
                verification=(
                    "tests/test_ppp_delivery_authorization.py pins that "
                    "PPP-bearing actions are gated while management work "
                    "is not, that an absent or wrong-scope ruling refuses "
                    "rather than passes, and that an unverified legacy row "
                    "does not authorise even when legacy is_active is true."
                ),
                cutover_gate=(
                    "Authority is network.ont_wan_service_intent."
                    "active_primary_internet_intent at exact "
                    "ont+subscription grain. Legacy is_active is NOT read: "
                    "migration 456 leaves it untouched, so reading it "
                    "would authorise exactly the unverified rows the owner "
                    "slice quarantined. Remaining gate: the read-only "
                    "legacy worklist, adjudication through owner commands, "
                    "then the partial unique indexes."
                ),
                fallback_retirement=(
                    "The 1,318-row staged backlog is removed by a "
                    "separately reviewed operation that recomputes "
                    "pending_apply rather than mass-clearing it."
                ),
            ),
            steward="network",
            design_refs=(
                "docs/designs/ONT_WAN_SERVICE_INTENT_SOT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_ppp_delivery_authorization.py",
                "tests/test_cpe_dialer_credential_intent_gate.py",
            ),
        ),
    ),
    SOTService(
        name="network.ont_reconcile_eligibility",
        module="app.services.network.ont_reconcile_eligibility",
        owns=(
            "per-ONT automatic reconciliation eligibility",
            "reviewed automatic reconciliation cohort admission",
            "overdue reconcile-hold alert consequence policy",
        ),
        depends_on=(),
        notes=(
            "Replaces the fleet-wide network.ont_reconcile control as the "
            "way to stop the sweeper touching a device. That control is "
            "far too blunt: it halts convergence for every ONT, and "
            "because _close_expired_remote_access and "
            "_reconcile_dialer_credentials run inside "
            "run_ont_reconcile_sweep AFTER the gate, disabling it also "
            "silently pauses expired remote-access cleanup and the dialer "
            "reconcile. Enabling that fleet control is not positive "
            "authority: the executor catalogs only ONTs with reviewed, "
            "named, unexpired admissions and rechecks that permission "
            "under the ONT lock before contact. Active holds past "
            "review_due_at remain in force; "
            "this owner also projects one critical, idempotent alert "
            "consequence per overdue hold for the shared admin-alert sink."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="per-ONT automatic reconciliation eligibility",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "reviewed cohort admission",
                        "reviewed hold decision",
                    ),
                    canonical_writer="network.ont_reconcile_eligibility",
                ),
                ConcernContract(
                    name="reviewed automatic reconciliation cohort admission",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("reviewed cohort admission",),
                    canonical_writer="network.ont_reconcile_eligibility",
                ),
                ConcernContract(
                    name="overdue reconcile-hold alert consequence policy",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=("reviewed hold decision",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="reviewed cohort admission",
                    owner="network.ont_reconcile_eligibility",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "ont_reconcile_admissions; named, expiring "
                        "operator decision with a distinct reviewer"
                    ),
                ),
                AuthorityInput(
                    name="reviewed hold decision",
                    owner="network.ont_reconcile_eligibility",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "ont_reconcile_holds; operator decision with a "
                        "distinct reviewer"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Typed admit/revoke and hold place/release commands "
                    "through execute_owner_command; the eligibility "
                    "query is a pure read."
                ),
                locking=(
                    "Canonical OntUnit -> admission/hold SELECT FOR "
                    "UPDATE order serialises commands with the sweeper's "
                    "point-of-use decision."
                ),
                idempotency=(
                    "Retried admit/place commands with the same key and "
                    "whole typed input return the existing record; key "
                    "reuse with changed evidence is refused."
                ),
                retries=(
                    "Refusals are answers, not transient failures, and are not retried."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "reconcile_hold_missing_ont",
                    "reconcile_hold_missing_reason",
                    "reconcile_hold_missing_explanation",
                    "reconcile_hold_missing_reviewer",
                    "reconcile_hold_reviewer_is_actor",
                    "reconcile_hold_missing_review_due",
                    "reconcile_hold_review_due_in_past",
                    "reconcile_hold_already_active",
                    "reconcile_hold_not_found",
                    "reconcile_hold_already_released",
                    "reconcile_hold_missing_idempotency_key",
                    "reconcile_hold_idempotency_conflict",
                    "reconcile_hold_ont_not_found",
                    "reconcile_hold_concurrent_release",
                    "reconcile_admission_missing_ont",
                    "reconcile_admission_missing_cohort",
                    "reconcile_admission_missing_reason",
                    "reconcile_admission_missing_explanation",
                    "reconcile_admission_missing_reviewer",
                    "reconcile_admission_reviewer_is_actor",
                    "reconcile_admission_missing_expiry",
                    "reconcile_admission_expiry_missing_timezone",
                    "reconcile_admission_expiry_in_past",
                    "reconcile_admission_already_active",
                    "reconcile_admission_not_found",
                    "reconcile_admission_already_ended",
                    "reconcile_admission_missing_idempotency_key",
                    "reconcile_admission_idempotency_conflict",
                    "reconcile_admission_ont_not_found",
                    "reconcile_admission_concurrent_end",
                    # Owner-command boundary codes, raised by
                    # execute_owner_command before this owner's own
                    # validation runs.
                    ("network.ont_reconcile_eligibility.active_caller_transaction"),
                    ("network.ont_reconcile_eligibility.command_contract_violation"),
                    ("network.ont_reconcile_eligibility.invalid_command_context"),
                    ("network.ont_reconcile_eligibility.nested_owner_command"),
                    ("network.ont_reconcile_eligibility.nested_transaction_completion"),
                ),
                mapping_owner=("app.services.network.ont_reconcile_eligibility"),
                fail_closed_on=(
                    "an absent ONT identity or missing, expired or "
                    "revoked admission, which yields ineligible",
                    "a reviewer equal to the actor, because suppressing "
                    "convergence on a customer device is a two-person "
                    "decision",
                    "missing reason code, explanation, reviewer or review date",
                    "a review date already in the past, which would hide "
                    "the decision it records",
                    "a second active hold for the same ONT and scope",
                    "a second effective admission for the same ONT and scope",
                ),
            ),
            events=EventContract(
                event_types=(
                    "ont_reconcile_admission.admitted",
                    "ont_reconcile_admission.expired",
                    "ont_reconcile_admission.revoked",
                    "ont_reconcile_hold.placed",
                    "ont_reconcile_hold.released",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Additive only; consumers must tolerate unknown fields."
                ),
                replay=(
                    "Admission and hold rows are durable evidence of who "
                    "granted or suppressed what and why; history is never "
                    "deleted."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                new_owner="network.ont_reconcile_eligibility",
                old_owner="network.ont_reconcile (fleet-wide control)",
                verification=(
                    "tests/test_ont_reconcile_eligibility.py pins that an "
                    "unadmitted or expired ONT cannot be cataloged or "
                    "contacted, admission is rechecked under lock, and "
                    "OVERDUE hold still suppresses, that the sweeper skips "
                    "held ONTs before any ping/read/write, and that held "
                    "is reported separately from skipped_unreachable. "
                    "tests/test_ont_reconcile_hold_alerts.py pins durable, "
                    "idempotent critical alert delivery and resolution."
                ),
                cutover_gate=(
                    "The fleet-wide stop stays active until a small named "
                    "cohort has reviewed, expiring admissions and its "
                    "lock-time refusals are verified. Re-enabling the "
                    "global control then reaches only that cohort."
                ),
                fallback_retirement=(
                    "network.ont_reconcile remains as an emergency "
                    "fleet-wide stop; it is no longer the mechanism for "
                    "excluding individual devices."
                ),
            ),
            steward="network",
            design_refs=(
                "docs/designs/ONT_RECONCILE_ELIGIBILITY_SOT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_ont_reconcile_eligibility.py",
                "tests/test_ont_reconcile_hold_alerts.py",
                "tests/test_ont_reconcile_admission_cli.py",
                "tests/test_ont_reconcile_admission_migration.py",
                "tests/architecture/test_ont_reconcile_admission_boundary.py",
                "tests/integration/test_ont_reconcile_hold_concurrency.py",
            ),
        ),
    ),
    SOTService(
        name="network.ont_wan_service_intent",
        module="app.services.network.ont_wan_service_intent",
        owns=(
            "declared ONT WAN service intent lifecycle",
            "active primary Internet termination selection",
        ),
        depends_on=(
            "network.ont_assignment_identity",
            "access.subscription_lifecycle",
            "runtime.db_sessions",
        ),
        notes=(
            "OntWanServiceInstance modelled service intent but had no "
            "application writer: no constructor outside tests, and 8 "
            "production rows against 1,523 ONTs. Rows written by nothing "
            "cannot authorise anything, so this owner is what makes them "
            "mean something. Intent is declared at EXACT service grain "
            "(ont_id AND subscription_id): an ONT-grain row claims the "
            "device may terminate PPP, which is not the claim that a "
            "given SERVICE terminates there, and a delivery ruling built "
            "on the weaker claim can hand one service's credential to "
            "another. lifecycle_state is the single authority -- "
            "planned/unverified do not authorise, and is_active is "
            "derived and maintained only here. is_primary selects; "
            "priority orders and never selects authority. One active "
            "primary Internet instance per subscription and per ONT, "
            "enforced by the commands because the partial unique indexes "
            "land only after inventory, backfill and verification. Every "
            "pre-existing row starts unverified and non-authorising. "
            "Retirement preserves history: assignment release, service "
            "movement, cancellation and return-to-inventory retire "
            "through this owner instead of deleting service-instance "
            "rows, because that record is the evidence a later "
            "adjudication depends on."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="declared ONT WAN service intent lifecycle",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "exact ONT and subscription identity",
                        "declared service and connection type",
                    ),
                    canonical_writer="network.ont_wan_service_intent",
                ),
                ConcernContract(
                    name="active primary Internet termination selection",
                    role=OwnerRole.RESOLVER,
                    input_names=("declared WAN service intent records",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="exact ONT and subscription identity",
                    owner="network.ont_assignment_identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="ont_units, subscriptions",
                ),
                AuthorityInput(
                    name="declared service and connection type",
                    owner="network.ont_wan_service_intent",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="operator declaration with work-order evidence",
                ),
                AuthorityInput(
                    name="declared WAN service intent records",
                    owner="network.ont_wan_service_intent",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="ont_wan_service_instances",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each command enters execute_owner_command once on a "
                    "transaction-free session. Replace retires and "
                    "activates inside one owned transaction, so a failure "
                    "leaves neither half applied."
                ),
                locking=(
                    "Primary-invariant checks read active rows within the "
                    "owned transaction; the partial unique indexes that "
                    "will make this structural land after backfill."
                ),
                idempotency=(
                    "Retiring an already-retired intent is a no-op that "
                    "returns the existing revision. Activation accepts an "
                    "expected revision and refuses on conflict."
                ),
                retries=(
                    "No automatic retry. A duplicate-primary refusal is an "
                    "ownership answer, not a transient failure."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.ont_wan_service_intent.active_caller_transaction",
                    "network.ont_wan_service_intent.command_contract_violation",
                    "network.ont_wan_service_intent.invalid_command_context",
                    "network.ont_wan_service_intent.nested_owner_command",
                    ("network.ont_wan_service_intent.nested_transaction_completion"),
                    "wan_intent_missing_subscription",
                    "wan_intent_missing_ont",
                    "wan_intent_missing_evidence",
                    "wan_intent_instance_not_found",
                    "wan_intent_already_retired",
                    "wan_intent_duplicate_primary_subscription",
                    "wan_intent_duplicate_primary_ont",
                    "wan_intent_revision_conflict",
                ),
                mapping_owner="app.services.network.ont_wan_service_intent",
                fail_closed_on=(
                    "an intent with no subscription",
                    "a service that already has an active primary",
                    "an ONT already carrying another service's primary",
                    "a revision that moved since the caller read it",
                    "a transition with no actor or reason",
                ),
            ),
            events=EventContract(
                event_types=(
                    "ont_wan_service_intent.declared.v1",
                    "ont_wan_service_intent.activated.v1",
                    "ont_wan_service_intent.retired.v1",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries ONT, subscription, service and "
                    "connection type, primary flag, lifecycle state, "
                    "revision, actor, reason and evidence reference. It "
                    "never carries PPPoE credentials."
                ),
                replay=(
                    "Transitions are replayable as history, never as "
                    "commands: replaying an activation would re-assert an "
                    "ownership decision whose preconditions have moved."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                new_owner="network.ont_wan_service_intent",
                old_owner="unwritten OntWanServiceInstance rows",
                verification=(
                    "tests/test_ont_wan_service_intent.py pins that "
                    "declaring does not authorise, that intent is scoped "
                    "to a service rather than a device, that priority "
                    "never selects authority, and that retirement "
                    "preserves the row."
                ),
                cutover_gate=(
                    "Partial unique indexes on active primary per "
                    "subscription and per ONT, added after inventory, "
                    "backfill and verification of the pre-owner rows."
                ),
                fallback_retirement=(
                    "is_active remains readable but derived; it is "
                    "retired once every reader consults lifecycle_state."
                ),
            ),
            steward="network",
            design_refs=(
                "docs/designs/ONT_WAN_SERVICE_INTENT_SOT.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/test_ont_wan_service_intent.py",
                "tests/test_return_to_inventory.py",
            ),
        ),
    ),
    SOTService(
        name="network.nas_local_secret_boundary",
        module="app.services.nas.local_secret_policy",
        owns=(
            "NAS-local PPPoE secret prohibition rulings",
            "local-secret command-text admissibility",
            "typed local-secret retirement planning and verification",
        ),
        depends_on=(
            "network.nas_inventory",
            "access.radius_projection",
            "access.radius_state",
        ),
        notes=(
            "RouterOS consults RADIUS only when the username is absent "
            "from /ppp secret, so a NAS-local per-customer record bypasses "
            "the RADIUS projection rather than overriding an attribute. "
            "Create, suspend, unsuspend and change_ip are prohibited for "
            "MikroTik PPPoE on BOTH execution surfaces — the activation "
            "command builder and the operator-editable provisioning "
            "template runner — so the prohibition cannot be edited around "
            "in the database. Deletion is corrective, not a second "
            "authority: it runs only through the reviewed cleanup "
            "operation, which refuses a login shared by more than one "
            "projected subscription, refuses a login RADIUS does not "
            "serve, and fails rather than reporting success when the "
            "device still reports the secret after removal. DHCP, IPoE, "
            "static and hotspot provisioning are untouched."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="NAS-local PPPoE secret prohibition rulings",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "NAS vendor and connection type",
                        "requested per-subscriber NAS action",
                    ),
                ),
                ConcernContract(
                    name="local-secret command-text admissibility",
                    role=OwnerRole.POLICY,
                    input_names=("rendered or stored command text",),
                ),
                ConcernContract(
                    name=("typed local-secret retirement planning and verification"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "projected subscription cohort for a login",
                        "device local-secret readback",
                    ),
                    canonical_writer="network.nas_local_secret_boundary",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="NAS vendor and connection type",
                    owner="network.nas_inventory",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="nas_devices",
                ),
                AuthorityInput(
                    name="requested per-subscriber NAS action",
                    owner="service_intent.subscription_lifecycle",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="activation and provisioning callers",
                ),
                AuthorityInput(
                    name="projected subscription cohort for a login",
                    owner="access.radius_projection",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="plan_login_radius_projections",
                ),
                AuthorityInput(
                    name="device local-secret readback",
                    owner="network.nas_local_secret_boundary",
                    kind=AuthorityKind.OBSERVATION,
                    source="RouterOS local-secret existence count",
                ),
                AuthorityInput(
                    name="rendered or stored command text",
                    owner="network.nas_inventory",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="provisioning_templates",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "Rulings and command-text checks are pure. Retirement "
                    "reads inside the caller's session and writes the "
                    "NetworkOperation ledger plus the device; it never "
                    "writes customer or access state."
                ),
                locking=(
                    "One active operation per (NAS, login) correlation "
                    "key, so a duplicate event delivery or a concurrent "
                    "operator run is rejected rather than repeated."
                ),
                idempotency=(
                    "Retirement on an already-absent secret is a verified "
                    "no-op that opens no operation. An apply must echo the "
                    "plan fingerprint, so a cohort that shifted between "
                    "preview and apply is refused."
                ),
                retries=(
                    "Failures are retryable through the operation ledger, "
                    "never by silent re-execution. An unverified removal "
                    "records a durable failure and raises, so a still-live "
                    "parallel authority is never reported as cleaned."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "nas_local_secret_cleanup_invalid_request",
                    "nas_local_secret_cleanup_shared_login",
                    "nas_local_secret_cleanup_radius_not_serving",
                    "nas_local_secret_cleanup_radius_still_serving",
                    "nas_local_secret_cleanup_dependent_subscription",
                    "nas_local_secret_cleanup_fingerprint_mismatch",
                    "nas_local_secret_cleanup_unverified",
                    "nas_local_secret_command_text_rejected",
                ),
                mapping_owner="app.services.nas.provisioner",
                fail_closed_on=(
                    "login shared by more than one nonterminal subscription",
                    "migrate intent with no active RADIUS projection",
                    "terminal intent while RADIUS still projects the login",
                    "terminal intent with a nonterminal dependant",
                    "plan fingerprint changed between preview and apply",
                    "unreadable device count",
                    "secret still present on device after removal",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.CUT_OVER,
                new_owner="network.nas_local_secret_boundary",
                old_owner="NAS-local per-customer PPPoE secret",
                verification=(
                    "Per-customer PPPoE access state moved to "
                    "access.radius_projection and suspension to "
                    "access.session_enforcement; this owner retains only "
                    "the prohibition ruling and the corrective removal. "
                    "tests/architecture/test_nas_local_secret_prohibition.py "
                    "pins that /ppp secret add and local enable/disable "
                    "cannot return from either execution surface."
                ),
                cutover_gate=(
                    "Code-level prohibition is unconditional; no runtime "
                    "flag can re-enable it."
                ),
                fallback_retirement=(
                    "Pre-existing device secrets are retired two ways: "
                    "terminal_retirement staged from the durable "
                    "subscription.canceled handler after the terminal "
                    "RADIUS projection succeeds, and migrate_to_radius "
                    "run per NAS in bounded operator cohorts. Both verify "
                    "by device count; neither rolls back a lifecycle "
                    "transition on device failure."
                ),
            ),
            events=EventContract(
                event_types=("nas_local_secret_cleanup_applied.v1",),
                schema_version=1,
                delivery_owner="observability.recording",
                compatibility=(
                    "Version 1 carries login, NAS device, reviewer and "
                    "reason. It never carries a customer password or any "
                    "other device credential."
                ),
                replay=(
                    "Not replayable as a command. The device readback is "
                    "the authoritative record of whether a secret is "
                    "still present; re-running cleanup re-reads the "
                    "device rather than trusting a prior emission."
                ),
            ),
            steward="network",
            design_refs=("docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md",),
            test_refs=(
                "tests/architecture/test_nas_local_secret_prohibition.py",
                "tests/test_nas_local_secret_policy.py",
                "tests/test_nas_local_secret_retirement.py",
            ),
        ),
    ),
    SOTService(
        name="network.nas_lifecycle",
        module="app.services.nas_lifecycle",
        owns=(
            "NAS lifecycle reconciliation plans",
            "subscription NAS relink decisions",
            "NAS lifecycle RADIUS projection commands",
        ),
        depends_on=(
            "network.identity",
            "network.access_path",
            "network.radius_sessions",
            "network.nas_inventory",
            "service_intent.subscription_nas_assignment",
            "access.radius_state",
            "runtime.db_sessions",
            "observability.recording",
        ),
    ),
    SOTService(
        name="network.nas_access_path_evidence",
        module="app.services.nas_access_path_evidence",
        owns=(
            "manual NAS lifecycle evidence reports",
            "historical access-path review recommendations",
        ),
        depends_on=(
            "network.radius_sessions",
            "network.nas_lifecycle",
            "runtime.db_sessions",
        ),
    ),
)
