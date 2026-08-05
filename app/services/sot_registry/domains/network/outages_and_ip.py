"""network SOT declarations: outages and ip."""

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
        name="network.outage_impact",
        module="app.services.network.outage_impact",
        owns=(
            "affected-customer impact",
            "outage scope impact",
            "tokenized cabinet audience membership",
        ),
        depends_on=(
            "network.access_path",
            "network.forwarding_topology",
        ),
        notes=(
            "resolve_fdh_audience projects the exact subscription "
            "membership behind one FDH plus an order-independent "
            "membership token; consumers (network.cabinet_notice) "
            "compare that token between preview and execution instead "
            "of trusting a snapshot."
        ),
    ),
    SOTService(
        name="network.cabinet_notice",
        module="app.services.network.cabinet_notice",
        owns=(
            "operator-initiated cabinet service notices",
            "cabinet notice recipient preview and drift protection",
        ),
        depends_on=(
            "network.outage_impact",
            "communications.customer_policy",
            "communications.intents",
        ),
        notes=(
            "Service (transactional) communication, deliberately NOT a "
            "campaign: campaigns hard-filter on marketing consent and would "
            "silently drop most of a cabinet from an outage notice. One "
            "deduplicated email per distinct customer via a durable "
            "communication-intent dedupe key; preview binds membership "
            "(scope token) and content + per-recipient dispositions "
            "(impact token), and execution refuses on drift."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="operator-initiated cabinet service notices",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "tokenized cabinet audience",
                        "customer notification policy decisions",
                        "operator notice command",
                    ),
                    canonical_writer="network.cabinet_notice",
                ),
                ConcernContract(
                    name=("cabinet notice recipient preview and drift protection"),
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "tokenized cabinet audience",
                        "customer notification policy decisions",
                        "operator notice command",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="tokenized cabinet audience",
                    owner="network.outage_impact",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "resolve_fdh_audience: exact active "
                        "subscriptions behind the FDH plus the "
                        "order-independent membership scope token"
                    ),
                ),
                AuthorityInput(
                    name="customer notification policy decisions",
                    owner="communications.customer_policy",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "cohort policy evaluation for channel=email, "
                        "category=service: channel configuration, "
                        "account status, preferences, durable "
                        "suppression ledger, dedupe window"
                    ),
                ),
                AuthorityInput(
                    name="operator notice command",
                    owner="network.cabinet_notice",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed draft (subject/body, CRLF-normalized) "
                        "plus explicit confirmation restating the "
                        "previewed eligible count, scope token, and "
                        "impact token"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Preview is read-only. Send owns its commit: the "
                    "intents, the dispatch event, and the audit trail "
                    "land atomically in the command or not at all; "
                    "the admin web adapter never commits."
                ),
                locking=(
                    "No mutation locks of its own; per-customer "
                    "dedupe keys make concurrent confirms converge on "
                    "one intent per customer per content."
                ),
                idempotency=(
                    "Identical content to the same cabinet and "
                    "customer is a durable no-op via the "
                    "communication-intent dedupe key; edited content "
                    "is a new key and requires a fresh preview."
                ),
                retries=(
                    "A retried or double-submitted confirm re-runs "
                    "the drift check and dedupe-key short-circuit; it "
                    "never queues a second email for the same "
                    "cabinet, customer, and content."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "cabinet_notice.validation_error",
                    "cabinet_notice.membership_drift",
                    *owner_command_boundary_error_codes("network.cabinet_notice"),
                ),
                mapping_owner=(
                    "admin web adapter (validation -> 400, drift -> "
                    "409 with a refreshed preview)"
                ),
            ),
            events=EventContract(
                event_types=("cabinet_notice.dispatched",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Operational breadcrumb (fdh, scope token, "
                    "queued/deduplicated/suppressed counts, actor); "
                    "customer emails are queued directly through "
                    "communication intents, never via this event."
                ),
                replay=(
                    "Re-dispatching an already-sent notice queues "
                    "nothing (dedupe keys) and emits a new event "
                    "whose counts show zero queued."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "none — manual cabinet outages had no customer "
                    "communication path (the outage-impact page was "
                    "preview-only and the classifier dispatcher is "
                    "incident-bound)"
                ),
                new_owner="network.cabinet_notice",
                verification=(
                    "Membership/content drift 409 tests, marketing-"
                    "unsubscribed-still-eligible regression pin, "
                    "per-customer dedupe and idempotent re-send "
                    "tests, adapter permission and commit tests."
                ),
                cutover_gate=(
                    "The cabinet detail and outage-impact pages link "
                    "only to this console for messaging cabinet "
                    "audiences."
                ),
                fallback_retirement=(
                    "No campaign segment or ad hoc email path targets "
                    "cabinet audiences."
                ),
            ),
            steward="network operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=("tests/test_cabinet_notice.py",),
        ),
    ),
    SOTService(
        name="network.device_groups",
        module="app.services.network.device_groups",
        owns=(
            "network device group mutations",
            "device group membership",
            "device group bulk action queueing",
        ),
        depends_on=("network.identity",),
    ),
    SOTService(
        name="network.ip_assignment_lifecycle",
        module="app.services.ip_assignment_lifecycle",
        owns=(
            "exact service ownership of active IPv4 assignments",
            "reviewed exact-service IPv4 assignment lifecycle repair",
            "reviewed exact-service IPv4 served projection repair",
        ),
        depends_on=(
            "access.radius_projection",
            "access.session_enforcement",
            "access.subscription_lifecycle",
            "events.dispatcher",
            "network.identity",
            "observability.audit_log",
            "sessions.radius_reconciliation",
        ),
        notes=(
            "IPAssignment remains the desired-address authority. This "
            "shadowing owner retains the safe ownership-only backfill and "
            "fingerprinted exact-service create/link/deactivate repair. The "
            "reviewed projection command may converge only the exact "
            "Subscription.ipv4_address copy; its durable event delegates "
            "RADIUS and old-IP session consequences to their owners. Normal "
            "provisioning writers remain declared migration debt until the "
            "later runtime cutover. The admin subscription replacement "
            "adapter is cut over to the two reviewed owner commands and is "
            "isolated from recurring add-on and billing writes. The admin "
            "subscription detail projects the same fingerprinted served-IP "
            "preview through a confirmed ActionForm and delegates repair "
            "directly to this owner."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="exact service ownership of active IPv4 assignments",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "canonical active IPv4 assignment",
                        "canonical active subscription identity",
                        "served IPv4 compatibility projection",
                        "reviewed ownership repair command",
                    ),
                    canonical_writer=("network.ip_assignment_lifecycle"),
                ),
                ConcernContract(
                    name=("reviewed exact-service IPv4 assignment lifecycle repair"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical active IPv4 assignment",
                        "canonical active subscription identity",
                        "serviceable IPv4 address inventory",
                        "reviewed lifecycle repair command",
                    ),
                    canonical_writer="network.ip_assignment_lifecycle",
                ),
                ConcernContract(
                    name=("reviewed exact-service IPv4 served projection repair"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical active IPv4 assignment",
                        "canonical active subscription identity",
                        "served IPv4 compatibility projection",
                        "observed RADIUS IPv4 projection",
                        "active RADIUS session observation",
                        "reviewed served projection repair command",
                    ),
                    canonical_writer="network.ip_assignment_lifecycle",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical active IPv4 assignment",
                    owner="network.ip_assignment_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "active IPAssignment identity, address, subscriber, "
                        "and exact subscription bridge"
                    ),
                ),
                AuthorityInput(
                    name="canonical active subscription identity",
                    owner="access.subscription_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=("active Subscription identity and Subscriber ownership"),
                ),
                AuthorityInput(
                    name="served IPv4 compatibility projection",
                    owner="network.ip_assignment_lifecycle",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "Subscription.ipv4_address used only to verify an "
                        "existing assignment-to-service link; never to "
                        "manufacture or move an allocation"
                    ),
                ),
                AuthorityInput(
                    name="reviewed ownership repair command",
                    owner="network.ip_assignment_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "exact assignment cohort, preview SHA-256, actor, "
                        "reason, and idempotency key"
                    ),
                ),
                AuthorityInput(
                    name="serviceable IPv4 address inventory",
                    owner="network.ip_assignment_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "IPv4Address and IpPool identity, active state, "
                        "reservation, management allocation, ONT binding, "
                        "network-device address identity, and active "
                        "routed-block exclusions"
                    ),
                ),
                AuthorityInput(
                    name="reviewed lifecycle repair command",
                    owner="network.ip_assignment_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "exact subscription, desired IPv4 address, exact "
                        "deactivation cohort, preview SHA-256, actor, reason, "
                        "and idempotency key"
                    ),
                ),
                AuthorityInput(
                    name="observed RADIUS IPv4 projection",
                    owner="access.radius_projection",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "DB-configured external radcheck and radreply state "
                        "for the exact selected service login"
                    ),
                ),
                AuthorityInput(
                    name="active RADIUS session observation",
                    owner="sessions.radius_reconciliation",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "active exact-subscription session identities and "
                        "their currently framed IPv4 addresses"
                    ),
                ),
                AuthorityInput(
                    name="reviewed served projection repair command",
                    owner="network.ip_assignment_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "exact subscription and assignment identifiers, "
                        "preview SHA-256, actor, reason, and idempotency key"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each public ownership, lifecycle, or served-projection "
                    "command enters "
                    "execute_owner_command once on a transaction-free "
                    "session; the operator adapter owns session lifecycle."
                ),
                locking=(
                    "The exact Subscription, Subscriber, selected "
                    "IPAssignment, desired IPv4Address, desired IpPool, and "
                    "all relevant assignment rows are locked. PostgreSQL "
                    "also holds routed-block and device-IP inventories in "
                    "SHARE mode for ledger repair; every command recomputes "
                    "its complete evidence before mutation."
                ),
                idempotency=(
                    "A durable audit row binds the idempotency key to the "
                    "exact preview fingerprint; changed evidence conflicts."
                ),
                retries=(
                    "Retry only after complete rollback using the same "
                    "fingerprint and idempotency key; stale evidence requires "
                    "a new preview."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.ip_assignment_lifecycle.active_caller_transaction",
                    "network.ip_assignment_lifecycle.assignment_not_found",
                    "network.ip_assignment_lifecycle.command_contract_violation",
                    "network.ip_assignment_lifecycle.duplicate_assignment",
                    "network.ip_assignment_lifecycle.empty_cohort",
                    "network.ip_assignment_lifecycle.idempotency_conflict",
                    "network.ip_assignment_lifecycle.invalid_command_context",
                    "network.ip_assignment_lifecycle.missing_idempotency_key",
                    "network.ip_assignment_lifecycle.nested_owner_command",
                    "network.ip_assignment_lifecycle.nested_transaction_completion",
                    "network.ip_assignment_lifecycle.stale_preview",
                    "network.ip_assignment_lifecycle.subscriber_not_found",
                    "network.ip_assignment_lifecycle.subscription_not_found",
                    "network.ip_assignment_lifecycle.unsafe_projection_repair",
                    "network.ip_assignment_lifecycle.unsafe_repair",
                    "network.ip_assignment_lifecycle.unsafe_cohort",
                ),
                mapping_owner="operator CLI and administrative web adapters",
                fail_closed_on=(
                    "ambiguous service ownership",
                    "multiple active services or assignments",
                    "subscriber or served-address disagreement",
                    "cross-service deactivation or address ownership",
                    "reserved, management, routed, or inactive-pool address",
                    "changed preview evidence",
                    "RADIUS or session observation disagreement",
                    "shared-login selection disagreement",
                ),
            ),
            events=EventContract(
                event_types=(
                    "ip_assignment.service_ownership_reconciled",
                    "ip_assignment.lifecycle_repaired",
                    "ip_assignment.served_projection_repaired",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 events carry exact assignment identifiers, "
                    "subscription identity, preview fingerprint, bounded "
                    "mutation counts, and old/new address consequence "
                    "evidence without customer identity data."
                ),
                replay=(
                    "The durable batch audit row and item audit rows "
                    "reconstruct each ownership or lifecycle outcome."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="IPAssignment exact-service ownership bridge",
                    input_names=(
                        "canonical active IPv4 assignment",
                        "canonical active subscription identity",
                        "served IPv4 compatibility projection",
                        "reviewed ownership repair command",
                    ),
                    writer="network.ip_assignment_lifecycle",
                    freshness=(
                        "A linked active assignment is current only while "
                        "its active service, subscriber, and address "
                        "compatibility evidence still agree."
                    ),
                    stale_behavior=(
                        "Ambiguous, missing, or conflicting links remain "
                        "visible blockers and are never inferred at runtime."
                    ),
                    drift_signal=(
                        "The exhaustive preview classifies every active IPv4 "
                        "assignment and fingerprints the exact cohort."
                    ),
                    rebuild_operation=(
                        "Re-run the dry-run preview and confirm only the "
                        "reviewed repairable cohort with its exact SHA-256."
                    ),
                    repair_owner=("network.ip_assignment_lifecycle"),
                ),
                ProjectionContract(
                    name="exact-service served IPv4 compatibility projection",
                    input_names=(
                        "canonical active IPv4 assignment",
                        "canonical active subscription identity",
                        "served IPv4 compatibility projection",
                        "observed RADIUS IPv4 projection",
                        "active RADIUS session observation",
                        "reviewed served projection repair command",
                    ),
                    writer="network.ip_assignment_lifecycle",
                    freshness=(
                        "Subscription.ipv4_address is current only while it "
                        "equals the single active exact-service assignment; "
                        "RADIUS and session observations are checked at each "
                        "preview and again under the command lock."
                    ),
                    stale_behavior=(
                        "Missing, multiple, shared-login, RADIUS-disagreed, "
                        "or session-conflicted evidence fails closed."
                    ),
                    drift_signal=(
                        "The exact-service IP consistency audit compares one "
                        "unambiguous assignment to served and policy-aware "
                        "RADIUS projections."
                    ),
                    rebuild_operation=(
                        "Run the dry-run exact-service projection adapter, "
                        "apply its exact fingerprint, then let the durable "
                        "event reconcile RADIUS and old-IP sessions."
                    ),
                    repair_owner="network.ip_assignment_lifecycle",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                old_owner=(
                    "generic IPAssignments CRUD, provisioning_helpers, "
                    "web_network_ip, subscriber_wan_ipam, and ip_lifecycle "
                    "direct writers"
                ),
                new_owner="network.ip_assignment_lifecycle",
                verification=(
                    "Full-fleet classification, exact-service ledger and "
                    "served-projection previews, and focused contract, "
                    "stale-preview, consequence, and idempotency tests."
                ),
                cutover_gate=(
                    "Every active assignment has an exact service link or "
                    "reviewed quarantine reason; reviewed repair converges "
                    "the IPAM ledger and served/RADIUS/session projections; "
                    "remaining projection drift is near zero before "
                    "unconditional exact-service runtime cutover."
                ),
                fallback_retirement=(
                    "Normal provisioning, admin assignment, terminal release, "
                    "WAN claim, and generic CRUD writers delegate to this "
                    "owner or are removed before migration completion."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/FINANCIAL_ACCESS_ENFORCEMENT.md",
                "docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_ip_assignment_repair.py",
                "tests/test_ip_assignment_lifecycle.py",
                "tests/test_web_ipv4_projection_reconciliation.py",
                "tests/architecture/test_ip_assignment_service_ownership.py",
            ),
        ),
    ),
    SOTService(
        name="network.ip_pool_utilization",
        module="app.services.ip_pool_utilization_snapshot",
        owns=(
            "IP pool utilization snapshot capture and retention",
            "IP pool utilization history reads",
            "live IP pool used/total report counts",
        ),
        notes=(
            "Snapshot rows are point-in-time capacity observations; "
            "the live report counts are counted address rows. Both "
            "definitions live here so readers do not maintain "
            "parallel counting paths."
        ),
    ),
    SOTService(
        name="network.outage_lifecycle",
        module="app.services.topology.outage",
        owns=(
            "persisted outage incident status vocabulary",
            "outage incident lifecycle",
            "immutable incident scope and audience revision history",
            "incident ticket link composition",
            "typed outage lifecycle output emission",
            "committed outage output consumption",
        ),
        depends_on=(
            "network.outage_impact",
            "events.dispatcher",
            "events.owner_outputs",
            "operations.sla_escalation",
            "support.ticket_lifecycle",
        ),
        notes=(
            "Every incident transition stages its typed outage output "
            "(plus the legacy network.alert webhook fan-out) atomically "
            "with the status write; the registered projection handler "
            "delivers those outputs back to the receipted consume_* "
            "commands, which attach operational owners/watchers and "
            "plan or cancel SLA escalations through the escalation "
            "participants. Outage resolution emits recovery evidence "
            "only and never closes support Tickets or WorkOrders. "
            "Declare, suspect, reroot, and audience-drift transitions "
            "append immutable scope revisions with order-independent "
            "membership tokens and exact entered/retained/left member "
            "deltas (OUTAGE_SLA_SPINE §3); the incident root stays the "
            "mutable latest projection while revisions preserve the "
            "history the downtime ledger consumes."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="persisted outage incident status vocabulary",
                    role=OwnerRole.EVENT_POLICY,
                    input_names=("recorded outage incidents",),
                ),
                ConcernContract(
                    name="outage incident lifecycle",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "recorded outage incidents",
                        "resolved outage impact",
                    ),
                    canonical_writer="network.outage_lifecycle",
                ),
                ConcernContract(
                    name=("immutable incident scope and audience revision history"),
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "recorded outage incidents",
                        "resolved outage impact",
                    ),
                    canonical_writer="network.outage_lifecycle",
                ),
                ConcernContract(
                    name="incident ticket link composition",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "recorded outage incidents",
                        "support ticket identities",
                    ),
                    canonical_writer="network.outage_lifecycle",
                ),
                ConcernContract(
                    name="typed outage lifecycle output emission",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("recorded outage incidents",),
                    canonical_writer="network.outage_lifecycle",
                ),
                ConcernContract(
                    name="committed outage output consumption",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "recorded outage incidents",
                        "operational escalation surface",
                        "receipted owner-output deliveries",
                    ),
                    canonical_writer="network.outage_lifecycle",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="recorded outage incidents",
                    owner="network.outage_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "outage_incidents rows with operator/classifier "
                        "provenance and lifecycle timestamps"
                    ),
                ),
                AuthorityInput(
                    name="resolved outage impact",
                    owner="network.outage_impact",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "affected-customer impact resolved from the "
                        "authoritative forwarding topology"
                    ),
                ),
                AuthorityInput(
                    name="operational escalation surface",
                    owner="operations.sla_escalation",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "operational owners, watchers, room links, "
                        "escalation events, and deliveries"
                    ),
                ),
                AuthorityInput(
                    name="support ticket identities",
                    owner="support.ticket_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "support_tickets row identities for the one "
                        "canonical infrastructure link and the "
                        "deduplicated complaint links; ticket "
                        "transitions stay with the Support owner"
                    ),
                ),
                AuthorityInput(
                    name="receipted owner-output deliveries",
                    owner="events.owner_outputs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique (consumer, event_id) receipts committing "
                        "atomically with each consumed outage effect"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Incident transitions flush into their calling "
                    "adapter's transaction and stage outputs atomically; "
                    "consume_outage_activation and "
                    "consume_outage_termination each enter "
                    "execute_owner_command once on a transaction-free "
                    "session."
                ),
                locking=(
                    "Transitions operate on the loaded incident row inside "
                    "the reconcile scan's advisory-locked pass or the "
                    "operator adapter's transaction; consumers reload the "
                    "incident before applying consequences."
                ),
                idempotency=(
                    "Escalation participants are idempotent per trigger; "
                    "consumer receipts make redelivery an exact no-op, and "
                    "a terminal incident skips fresh planning."
                ),
                retries=(
                    "A failed consequence leaves no receipt; the outbox "
                    "redelivers until the consumer commits or the failure "
                    "is reviewed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.outage_lifecycle.active_caller_transaction",
                    "network.outage_lifecycle.command_contract_violation",
                    "network.outage_lifecycle.invalid_command_context",
                    "network.outage_lifecycle.nested_owner_command",
                    "network.outage_lifecycle.nested_transaction_completion",
                ),
                mapping_owner="network monitoring and event adapters",
                fail_closed_on=(
                    "an unknown outage status value",
                    "operator termination of a classifier incident",
                    "consequence application outside an owner command",
                ),
            ),
            events=EventContract(
                event_types=(
                    "outage.created",
                    "outage.suspected",
                    "outage.confirmed",
                    "outage.clearing",
                    "outage.reopened",
                    "outage.rerooted",
                    "outage.discarded",
                    "outage.resolved",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries incident identity, status, "
                    "provenance, scope, severity, affected count, and "
                    "lifecycle timestamps; the legacy network.alert "
                    "fan-out keeps the identical payload for external "
                    "webhook subscribers."
                ),
                replay=(
                    "Incident rows and EventStore evidence reconstruct "
                    "each transition; consumer receipts make redelivered "
                    "consequences exact no-ops."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "inline cross-owner escalation calls and a pre-commit "
                    "swallowed network.alert-only fan-out"
                ),
                new_owner="network.outage_lifecycle",
                verification=(
                    "Chain behavior tests (atomic staging, receipts, "
                    "replay, failed-delivery visibility, ticket/work-order "
                    "non-closure) and the outage boundary architecture "
                    "test."
                ),
                cutover_gate=(
                    "Typed outputs and receipted consumers are the only "
                    "consequence path; no inline cross-owner call remains "
                    "in the transitions."
                ),
                fallback_retirement=(
                    "The pre-commit swallowed emission and inline "
                    "ensure/plan/cancel calls are removed from the "
                    "lifecycle transitions."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md",
                "docs/designs/OUTAGE_SLA_SPINE.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=(
                "tests/services/topology/test_outage_lifecycle_chain.py",
                "tests/architecture/test_outage_lifecycle_chain_boundary.py",
                "tests/services/topology/test_outage_reconcile.py",
                "tests/services/topology/test_outage_scope_revisions.py",
            ),
        ),
    ),
    SOTService(
        name="network.service_impact",
        module="app.services.network.service_impact",
        owns=("per-subscription service impact evidence resolution",),
        depends_on=(
            "network.outage_lifecycle",
            "network.outage_impact",
            "network.radius_sessions",
        ),
        notes=(
            "Read-only six-state impact resolver "
            "(OUTAGE_SLA_SPINE §1): audience membership from the "
            "immutable scope revisions proves exposure; the incident "
            "lifecycle word supplies provider-fault evidence; live "
            "RADIUS sessions prove continued service and prevent "
            "accrual. Exposure is never downtime, a lone dark "
            "endpoint or stale telemetry resolves unknown rather "
            "than confirmed, and excluded stays reserved for the "
            "maintenance owner. It persists nothing and sends "
            "nothing; the downtime ledger consumes its words."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("per-subscription service impact evidence resolution"),
                    role=OwnerRole.RESOLVER,
                    input_names=(
                        "incident lifecycle and scope revisions",
                        "live session observations",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="incident lifecycle and scope revisions",
                    owner="network.outage_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "live OutageIncident status words plus the "
                        "immutable scope revisions carrying exact "
                        "audience membership and tokens"
                    ),
                ),
                AuthorityInput(
                    name="live session observations",
                    owner="network.radius_sessions",
                    kind=AuthorityKind.OBSERVATION,
                    source=(
                        "RadiusActiveSession rows as "
                        "continued-service proof per subscription"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.READ_ONLY,
                boundary=(
                    "Resolves impact words from committed incident, "
                    "revision, and session state without a business "
                    "write and without device I/O."
                ),
                locking="Read resolution acquires no mutation locks.",
                idempotency=(
                    "The same incident status, scope revision, and "
                    "session set produce the same impact words and "
                    "evidence."
                ),
                retries="Read resolution calls are safe to retry.",
            ),
            errors=ErrorContract(
                domain_codes=(),
                mapping_owner="app.web.admin.network_monitoring",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.service_impact",
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/OUTAGE_SLA_SPINE.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=("tests/services/topology/test_service_impact.py",),
        ),
    ),
    SOTService(
        name="network.maintenance_lifecycle",
        module="app.services.network.maintenance_lifecycle",
        owns=(
            "planned maintenance window lifecycle",
            "typed maintenance lifecycle output emission",
            "planned-maintenance SLA exclusion eligibility",
        ),
        depends_on=(
            "network.outage_impact",
            "network.outage_lifecycle",
            "events.dispatcher",
        ),
        notes=(
            "Sole writer of network_maintenance_windows "
            "(OUTAGE_SLA_SPINE §5): draft, approved, announced, "
            "in_progress, completed, canceled, overrun. Every "
            "transition stages its typed maintenance.* output "
            "atomically with the status write. Seven calendar days "
            "of notice gate SLA exclusion; the audience token is "
            "resolved at announce and re-resolved at begin, and "
            "material drift refuses a silent start. Only the "
            "properly announced planned window is excludable — "
            "unannounced or emergency work and overrun time are "
            "unplanned downtime, and an overrun escalates to a "
            "declared outage through the lifecycle owner so accrual "
            "and consequences flow through the normal incident "
            "chain."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="planned maintenance window lifecycle",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "resolved maintenance audience",
                        "declared outage escalation surface",
                    ),
                    canonical_writer="network.maintenance_lifecycle",
                ),
                ConcernContract(
                    name=("typed maintenance lifecycle output emission"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("resolved maintenance audience",),
                    canonical_writer="network.maintenance_lifecycle",
                ),
                ConcernContract(
                    name=("planned-maintenance SLA exclusion eligibility"),
                    role=OwnerRole.POLICY,
                    input_names=("resolved maintenance audience",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="resolved maintenance audience",
                    owner="network.outage_impact",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "exact subscription cohorts per node, "
                        "basestation, or cabinet with "
                        "order-independent membership tokens"
                    ),
                ),
                AuthorityInput(
                    name="declared outage escalation surface",
                    owner="network.outage_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "declare_outage command for the "
                        "overrun-to-outage handoff with the linked "
                        "incident identity"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each transition validates the state machine, "
                    "writes the window, and stages its typed output "
                    "atomically in the caller's transaction."
                ),
                locking=(
                    "Transitions are guarded by explicit "
                    "current-state checks; drift refusal requires an "
                    "explicit approval flag."
                ),
                idempotency=(
                    "Overrun escalation returns the already-linked "
                    "incident; repeated transition calls against the "
                    "wrong state raise instead of double-writing."
                ),
                retries=(
                    "Failed transitions raise before any partial "
                    "write; event staging shares the transaction."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.maintenance_lifecycle.active_caller_transaction",
                    "network.maintenance_lifecycle.command_contract_violation",
                    "network.maintenance_lifecycle.invalid_command_context",
                    "network.maintenance_lifecycle.nested_owner_command",
                    "network.maintenance_lifecycle.nested_transaction_completion",
                ),
                mapping_owner="app.web.admin.network_monitoring",
            ),
            events=EventContract(
                event_types=(
                    "maintenance.announced",
                    "maintenance.started",
                    "maintenance.completed",
                    "maintenance.canceled",
                    "maintenance.overrun",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries window identity, status, "
                    "scope, planned bounds, announcement time, "
                    "audience count, and any linked outage; fields "
                    "are additive."
                ),
                replay=(
                    "No projection handler consumes these outputs "
                    "yet; replays are safe because window state is "
                    "the authority and transitions are "
                    "state-guarded."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.maintenance_lifecycle",
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/OUTAGE_SLA_SPINE.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=("tests/services/topology/test_maintenance_lifecycle.py",),
        ),
    ),
    SOTService(
        name="network.customer_outage_accrual",
        module="app.services.network.customer_outage_accrual",
        owns=(
            "immutable customer outage interval ledger",
            "committed outage output accrual consumption",
        ),
        depends_on=(
            "network.outage_lifecycle",
            "network.service_impact",
            "network.maintenance_lifecycle",
            "events.owner_outputs",
        ),
        notes=(
            "Sole writer of customer_outage_intervals "
            "(OUTAGE_SLA_SPINE §2/§7). Reconciles the impact "
            "resolver's words into per-subscription intervals under "
            "the approved clocks: earliest qualifying observation "
            "start (audience entry for joiners), provisional "
            "first-healthy-observation end, one continuous interval "
            "across clearing/reopened, finalization at the proven "
            "recovery timestamp on resolve, and reviewed "
            "incident_discarded exclusion on discard — resolved_at "
            "never determines downtime and unknown never accrues. "
            "Delivery is the lifecycle projection handler invoking "
            "the receipted consume command per committed output; "
            "reruns and redeliveries converge with no duplicate or "
            "overlapping rows (partial unique open-interval index)."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="immutable customer outage interval ledger",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=(
                        "per-subscription impact words",
                        "incident lifecycle and scope history",
                        "planned-maintenance exclusion eligibility",
                    ),
                    canonical_writer="network.customer_outage_accrual",
                ),
                ConcernContract(
                    name=("committed outage output accrual consumption"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("receipted lifecycle output deliveries",),
                    canonical_writer="network.customer_outage_accrual",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="per-subscription impact words",
                    owner="network.service_impact",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "six-state impact resolution with typed "
                        "evidence per audience member"
                    ),
                ),
                AuthorityInput(
                    name="incident lifecycle and scope history",
                    owner="network.outage_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "incident status words, lifecycle stamps, and "
                        "immutable scope revisions with member entry "
                        "times"
                    ),
                ),
                AuthorityInput(
                    name="planned-maintenance exclusion eligibility",
                    owner="network.maintenance_lifecycle",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "the reviewed planned_maintenance word when a "
                        "properly announced window covers the "
                        "interval start inside its planned bounds"
                    ),
                ),
                AuthorityInput(
                    name="receipted lifecycle output deliveries",
                    owner="events.owner_outputs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique (consumer, event_id) receipts making "
                        "each redelivery an exact no-op"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Each consumed output reconciles the ledger and "
                    "writes its receipt atomically inside one owner "
                    "command on a fresh owner session."
                ),
                locking=(
                    "The partial unique open-interval index per "
                    "(incident, subscription) makes concurrent "
                    "openers conflict at the database instead of "
                    "double-accruing."
                ),
                idempotency=(
                    "Reconciliation converges: reruns open nothing "
                    "new, provisional ends clear on re-darkening, and "
                    "(consumer, event_id) receipts short-circuit "
                    "redeliveries."
                ),
                retries=(
                    "A failed consequence leaves the delivery failed "
                    "and retryable; the receipt only exists when the "
                    "effect committed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.customer_outage_accrual.active_caller_transaction",
                    "network.customer_outage_accrual.command_contract_violation",
                    "network.customer_outage_accrual.invalid_command_context",
                    "network.customer_outage_accrual.nested_owner_command",
                    "network.customer_outage_accrual.nested_transaction_completion",
                ),
                mapping_owner=(
                    "app.services.events.handlers.outage_lifecycle_projection"
                ),
            ),
            events=EventContract(
                event_types=(
                    "outage.created",
                    "outage.suspected",
                    "outage.confirmed",
                    "outage.clearing",
                    "outage.reopened",
                    "outage.discarded",
                    "outage.resolved",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Consumes the version-1 outage lifecycle envelope "
                    "(incident identity, status, scope, timestamps) "
                    "additively; the ledger emits no events of its "
                    "own."
                ),
                replay=(
                    "Redeliveries short-circuit on the "
                    "(consumer, event_id) receipt; replaying the full "
                    "stream rebuilds identical intervals because "
                    "reconciliation is content-idempotent."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.customer_outage_accrual",
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/OUTAGE_SLA_SPINE.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=("tests/services/topology/test_customer_outage_accrual.py",),
        ),
    ),
    SOTService(
        name="network.outage_communications",
        module="app.services.topology.outage_communications",
        owns=(
            "customer outage communication decisions",
            "customer outage notice record",
            "committed outage output communication consumption",
        ),
        depends_on=(
            "network.outage_lifecycle",
            "network.service_impact",
            "network.customer_outage_accrual",
        ),
        notes=(
            "OUTAGE_SLA_SPINE §3. Decides WHETHER a customer is owed "
            "a message, which stage, and when — never the audience, "
            "the impact word, the measured downtime, or the delivery. "
            "The restoration cohort is derived from queued notice "
            "rows with communication-intent lineage, never from the "
            "current audience: a mid-incident joiner was promised "
            "nothing and a customer who left is still owed the "
            "all-clear. Supersedes the classifier-bound "
            "network.outage_notifications and "
            "network.outage_auto_notify send paths; arming "
            "outage_customer_comms_enabled stands both of them down "
            "so two customer outage senders are never live at once."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="customer outage communication decisions",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "per-subscription impact words",
                        "incident lifecycle and scope history",
                        "measured customer downtime",
                        "communication gate configuration",
                    ),
                ),
                ConcernContract(
                    name="customer outage notice record",
                    role=OwnerRole.AUTHORITATIVE_RECORD,
                    input_names=("per-subscription impact words",),
                    canonical_writer="network.outage_communications",
                ),
                ConcernContract(
                    name=("committed outage output communication consumption"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("incident lifecycle and scope history",),
                    canonical_writer="network.outage_communications",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="per-subscription impact words",
                    owner="network.service_impact",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "six-state impact resolution per audience "
                        "member with typed evidence; only "
                        "confirmed_unavailable opens a conversation "
                        "and only restored closes one"
                    ),
                ),
                AuthorityInput(
                    name="incident lifecycle and scope history",
                    owner="network.outage_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "incident status, lifecycle stamps, and the "
                        "immutable scope revision the message was "
                        "composed under"
                    ),
                ),
                AuthorityInput(
                    name="measured customer downtime",
                    owner="network.customer_outage_accrual",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "exact-quality customer outage intervals; a "
                        "restoration message quotes the ledger and "
                        "never recomputes a duration"
                    ),
                ),
                AuthorityInput(
                    name="communication gate configuration",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "outage_customer_comms_enabled, dry-run, "
                        "settling window, minimum affected count, "
                        "update interval, per-run recipient cap and "
                        "per-customer cooldown"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Planning is read-only. A send stages notice "
                    "rows, communication intents and the breadcrumb "
                    "event in one transaction owned by the receipted "
                    "consumer or the operator command; a partial "
                    "write would suppress a message nobody received."
                ),
                locking=(
                    "The unique dedupe key is the concurrency guard: "
                    "two workers deciding the same message converge "
                    "on one row instead of two emails."
                ),
                idempotency=(
                    "Conversation history makes a replay produce no "
                    "candidates at all; the dedupe key holds when "
                    "history has not yet committed. Dry-run plans "
                    "and blocked recipients use separate key "
                    "namespaces so neither can mute a later genuine "
                    "message."
                ),
                retries=(
                    "A rolled-back pass leaves no notice row, so no "
                    "customer is silently marked as already told."
                ),
            ),
            errors=ErrorContract(
                domain_codes=owner_command_boundary_error_codes(
                    "network.outage_communications"
                ),
                mapping_owner="app.web.admin.network_monitoring",
                fail_closed_on=(
                    "communications disarmed",
                    "incident suspected or exposure-only",
                    "incident still inside the settling window",
                    "incident below the minimum affected count",
                    "preview token no longer matches the plan",
                ),
            ),
            events=EventContract(
                event_types=("outage_customer_notice.dispatched",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries incident identity and status, "
                    "per-stage counts, queued and planned totals and "
                    "the dry-run flag; fields are additive. The "
                    "customer messages themselves are communication "
                    "intents, never this event."
                ),
                replay=(
                    "Operational breadcrumb only; no projection "
                    "handler consumes it, and replaying it sends "
                    "nothing."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.SHADOWING,
                new_owner="network.outage_communications",
                old_owner="network.outage_notifications",
                verification=(
                    "Dry run is the default and records a notice row "
                    "per decided message, so the plan is countable "
                    "against what the NOC saw — ADR 0004's dry run "
                    "only logged, which is why nobody could evaluate "
                    "it."
                ),
                cutover_gate=(
                    "Dry-run notice rows show no opening message an "
                    "operator would not have sent, restoration "
                    "cohorts match the customers actually told, and "
                    "per-run recipient counts are within "
                    "expectation."
                ),
                fallback_retirement=(
                    "Arming outage_customer_comms_enabled makes both "
                    "legacy send paths refuse with "
                    "superseded_by_outage_communications. They are "
                    "removed once the new owner has run armed "
                    "through a full incident cycle."
                ),
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/OUTAGE_SLA_SPINE.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
            ),
            test_refs=("tests/services/topology/test_outage_communications.py",),
        ),
    ),
    SOTService(
        name="network.outage_auto_notify",
        module="app.services.topology.outage_auto_notify",
        owns=(
            "automation eligibility for customer outage notification",
            "automated dispatch trigger and its transaction",
        ),
        depends_on=(
            "network.outage_lifecycle",
            "network.outage_impact",
        ),
        notes=(
            "ADR 0004. Owns WHICH incidents automation may notify "
            "about and the trigger, never the send itself: "
            "outage_notifications.dispatch_outage_notifications stays "
            "the only writer of a customer outage notification and "
            "keeps its confidence gate, debounce, opt-out and caps. "
            "Channel selection belongs to "
            "communications.channel_policy. Automated sends are "
            "stamped with AUTO_ACTOR_ID so the audit separates them "
            "from operator dispatches."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name=("automation eligibility for customer outage notification"),
                    role=OwnerRole.POLICY,
                    input_names=(
                        "classifier outage incident",
                        "automation gate configuration",
                    ),
                ),
                ConcernContract(
                    name="automated dispatch trigger and its transaction",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "classifier outage incident",
                        "affected subscription set",
                        "automation gate configuration",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="classifier outage incident",
                    owner="network.outage_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "OutageIncident rows with detection_source, "
                        "status, classification, affected_count and "
                        "confirmed_at"
                    ),
                ),
                AuthorityInput(
                    name="affected subscription set",
                    owner="network.outage_impact",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source=(
                        "subscriptions downstream of the incident "
                        "boundary node, base station or cabinet"
                    ),
                ),
                AuthorityInput(
                    name="automation gate configuration",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "outage_auto_notify_enabled, dry-run, settling "
                        "window, minimum affected count and per-run "
                        "incident cap"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "One automated pass commits its dispatch audit rows "
                    "together. The Celery task is an adapter and never "
                    "commits; a failure rolls the whole pass back."
                ),
                locking=(
                    "A Postgres advisory lock makes the pass "
                    "single-flight. Concurrent passes would each read "
                    "the debounce table before the other wrote it."
                ),
                idempotency=(
                    "The persisted debounce window in "
                    "outage_notification_dispatches suppresses a "
                    "boundary already notified, so a repeated pass "
                    "re-notifies nobody."
                ),
                retries=(
                    "A rolled-back pass is safe to retry: no audit row "
                    "survives, so no boundary is muted by a send that "
                    "never happened. Ineligible incidents fail closed."
                ),
            ),
            errors=ErrorContract(
                domain_codes=owner_command_boundary_error_codes(
                    "network.outage_auto_notify"
                ),
                mapping_owner="scheduled task adapter",
                fail_closed_on=(
                    "automation disabled",
                    "incident not a customer-visible classifier node_outage",
                    "incident still inside the settling window",
                    "incident below the minimum affected count",
                    "no affected subscriptions resolved",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.outage_auto_notify",
                old_owner=None,
                verification=(
                    "Dry-run mode plans and logs recipients without "
                    "sending, so automated selection can be compared "
                    "against what the NOC would have dispatched."
                ),
                cutover_gate=(
                    "Dry-run output shows no incident an operator would "
                    "not have notified about, and per-run recipient "
                    "counts are within expectation."
                ),
                fallback_retirement=(
                    "None. The operator dispatch path is retained for "
                    "incidents automation deliberately excludes: "
                    "radio_cluster, operator-declared, and "
                    "below-threshold incidents."
                ),
            ),
            steward="Network operations",
            design_refs=(
                "docs/adr/0004-automated-outage-notification-dispatch.md",
                "docs/designs/OUTAGE_CLASSIFIER.md",
            ),
            test_refs=("tests/test_outage_auto_notify.py",),
        ),
    ),
    SOTService(
        name="network.connection_health",
        module="app.services.topology.connection_status",
        owns=(
            "customer-safe connection health vocabulary",
            "customer-safe last-mile and area-outage verdict",
            "customer connection headline, message, and advice",
        ),
        depends_on=(
            "network.access_path",
            "network.radius_sessions",
            "network.outage_impact",
            "network.outage_lifecycle",
        ),
        notes=(
            "This customer diagnostic vocabulary is separate from "
            "network.device_state and raw RADIUS session observations."
        ),
    ),
)
