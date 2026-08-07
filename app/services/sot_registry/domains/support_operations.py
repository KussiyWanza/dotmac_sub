"""Canonical SOT declarations for the support_operations domain."""

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
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="support_operations",
    services=(
        SOTService(
            name="support.ticket_assignment_rule_configuration",
            module="app.services.ticket_assignment.admin",
            owns=("ticket assignment-rule configuration",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ticket assignment-rule configuration",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed assignment-rule command",
                            "assignment rules",
                        ),
                        canonical_writer=(
                            "support.ticket_assignment_rule_configuration"
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed assignment-rule command",
                        owner="support.ticket_assignment_rule_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed create/delete command with explicit region, team, "
                            "assignee, strategy, priority, actor, and idempotency evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="assignment rules",
                        owner="support.ticket_assignment_rule_configuration",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="TicketAssignmentRule rows and their active evaluation order",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "create/delete commands enter execute_owner_command once; validation "
                        "and persistence are flush-only inside the root transaction"
                    ),
                    locking="rule identity and target references are rechecked before write",
                    idempotency=(
                        "stable rule identifiers and command idempotency evidence converge "
                        "replay"
                    ),
                    retries="retry the complete configuration command after rollback",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "assignment_rule_not_found",
                        "assignment_rule_invalid",
                        *owner_command_boundary_error_codes(
                            "support.ticket_assignment_rule_configuration"
                        ),
                    ),
                    mapping_owner="support settings web adapters",
                    fail_closed_on=(
                        "invalid team/person target",
                        "ambiguous rule identity",
                    ),
                ),
                events=EventContract(
                    event_types=("ticket.assignment_rule_changed",),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility="Version 1 records rule identity and change type only.",
                    replay="TicketAssignmentRule rows plus audit evidence reconstruct changes.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="support ticket settings route and direct-commit helpers",
                    new_owner="support.ticket_assignment_rule_configuration",
                    verification="assignment configuration and architecture tests",
                    cutover_gate="all assignment rule writes use typed owner commands",
                    fallback_retirement="direct settings-route rule writes are absent",
                ),
                steward="support operations",
                design_refs=(
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_ticket_assignment_engine.py",
                    "tests/architecture/test_support_ticket_sot_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_assignment_evaluation",
            module="app.services.ticket_assignment.engine",
            owns=(
                "ticket assignment-rule evaluation",
                "ticket assignment round-robin cursor",
            ),
            depends_on=("support.ticket_assignment_rule_configuration",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ticket assignment-rule evaluation",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical assignment rules",
                            "ticket assignment facts",
                        ),
                    ),
                    ConcernContract(
                        name="ticket assignment round-robin cursor",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "canonical assignment rules",
                            "assignment cursor state",
                        ),
                        canonical_writer="support.ticket_assignment_evaluation",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical assignment rules",
                        owner="support.ticket_assignment_rule_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="active TicketAssignmentRule rows in priority order",
                    ),
                    AuthorityInput(
                        name="ticket assignment facts",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Ticket region, customer, existing team/person assignment, and "
                            "eligible team membership facts"
                        ),
                    ),
                    AuthorityInput(
                        name="assignment cursor state",
                        owner="support.ticket_assignment_evaluation",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="locked TicketAssignmentCounter row for a rule/team scope",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "The Ticket lifecycle owner holds the root transaction; evaluation "
                        "returns a typed AssignmentResult and may only flush its locked cursor"
                    ),
                    locking="round-robin counter is selected FOR UPDATE",
                    idempotency=(
                        "one cursor advance occurs within the atomic Ticket command; rollback "
                        "restores both proposal cursor and Ticket mutation"
                    ),
                    retries="the Ticket owner reruns evaluation after full rollback",
                ),
                errors=ErrorContract(
                    domain_codes=("assignment_evaluation_invalid_scope",),
                    mapping_owner="support.ticket_lifecycle",
                    fail_closed_on=("inactive team", "ineligible assignee"),
                ),
                events=EventContract(
                    event_types=("ticket.assignment_evaluated",),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility=(
                        "Version 1 records matched rule/scope and selected team/person "
                        "identifiers without customer payloads."
                    ),
                    replay=(
                        "TicketAssignmentRule, TicketAssignmentCounter, and Ticket audit "
                        "evidence explain the selected proposal."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="assignment engine direct Ticket writer",
                    new_owner="support.ticket_assignment_evaluation",
                    verification="assignment proposal-only and lifecycle application tests",
                    cutover_gate="evaluation returns typed proposals without Ticket writes",
                    fallback_retirement="policy-side Ticket and TicketAssignee writes are absent",
                ),
                steward="support operations",
                design_refs=(
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_ticket_assignment_engine.py",
                    "tests/architecture/test_support_ticket_sot_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_automation_rule_configuration",
            module="app.services.support_automation_rules",
            owns=("ticket automation-rule configuration",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ticket automation-rule configuration",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "typed automation-rule command",
                            "automation rules",
                        ),
                        canonical_writer=(
                            "support.ticket_automation_rule_configuration"
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed automation-rule command",
                        owner="support.ticket_automation_rule_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed create/update/delete/activation command with validated "
                            "trigger, conditions, action, actor, and idempotency evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="automation rules",
                        owner="support.ticket_automation_rule_configuration",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="TicketAutomationRule rows and active priority order",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary="each rule mutation enters execute_owner_command once",
                    locking="rule row is locked for update/delete/activation",
                    idempotency="command key and stable rule identity bound replay",
                    retries="retry the complete rule command after rollback",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "automation_rule_not_found",
                        "automation_rule_invalid",
                        *owner_command_boundary_error_codes(
                            "support.ticket_automation_rule_configuration"
                        ),
                    ),
                    mapping_owner="support automation web adapters",
                    fail_closed_on=("invalid trigger", "invalid action target"),
                ),
                events=EventContract(
                    event_types=("ticket.automation_rule_changed",),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility="Version 1 records rule identity and change type only.",
                    replay="TicketAutomationRule rows and audit evidence reconstruct changes.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="automation route and direct-commit service helpers",
                    new_owner="support.ticket_automation_rule_configuration",
                    verification="automation configuration and architecture tests",
                    cutover_gate="all rule writes use typed owner commands",
                    fallback_retirement="HTTP and direct transaction behavior are absent",
                ),
                steward="support operations",
                design_refs=(
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_support_automation.py",
                    "tests/architecture/test_support_ticket_sot_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_automation_evaluation",
            module="app.services.support_automation",
            owns=("ticket automation-rule evaluation",),
            depends_on=("support.ticket_automation_rule_configuration",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ticket automation-rule evaluation",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical automation rules",
                            "ticket automation facts",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical automation rules",
                        owner="support.ticket_automation_rule_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="active TicketAutomationRule rows for the trigger",
                    ),
                    AuthorityInput(
                        name="ticket automation facts",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="current typed Ticket fields and identity-review evidence",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary="evaluation is side-effect-free and returns typed proposals",
                    locking="not applicable; lifecycle owner rechecks state before application",
                    idempotency="same rules and Ticket facts produce the same ordered proposals",
                    retries="re-evaluate from canonical inputs",
                ),
                errors=ErrorContract(
                    domain_codes=("automation_evaluation_invalid_rule",),
                    mapping_owner="support.ticket_lifecycle",
                    fail_closed_on=("identity manual review", "unknown action"),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="automation engine direct Ticket writer",
                    new_owner="support.ticket_automation_evaluation",
                    verification="proposal-only automation and lifecycle application tests",
                    cutover_gate="policy returns TicketAutomationProposal values only",
                    fallback_retirement="automation policy cannot mutate or flush Ticket rows",
                ),
                steward="support operations",
                design_refs=(
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_support_automation.py",
                    "tests/architecture/test_support_ticket_sot_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_vocabulary",
            module="app.models.support",
            owns=("ticket status vocabulary",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ticket status vocabulary",
                        role=OwnerRole.RESOLVER,
                        input_names=("typed ticket status values",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed ticket status values",
                        owner="support.ticket_vocabulary",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "closed TicketStatus enum values and terminal-state semantics"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "Pure typed vocabulary resolution performs no database work."
                    ),
                    locking="Immutable enum values require no locking.",
                    idempotency=(
                        "The same application version returns the same closed value set."
                    ),
                    retries="Pure resolution requires no retry.",
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="support command and configuration adapters",
                    fail_closed_on=("unknown ticket status value",),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="support.ticket_lifecycle bundled vocabulary concern",
                    new_owner="support.ticket_vocabulary",
                    verification=(
                        "ticket status transition and SOT relationship tests"
                    ),
                    cutover_gate=(
                        "lifecycle and configuration name the shared vocabulary owner"
                    ),
                    fallback_retirement=(
                        "no service claims the vocabulary as lifecycle state"
                    ),
                ),
                steward="support operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                ),
                test_refs=(
                    "tests/test_ticket_status_transition.py",
                    "tests/test_sot_relationships.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_lifecycle",
            module="app.services.support",
            owns=(
                "ticket lifecycle mutations",
                "ticket creation and identity",
                "admin-created ticket customer email acknowledgement",
                "support ticket human-readable number allocation",
                "guarded ticket status transitions",
                "ticket lifecycle timestamps and consequences",
                "ticket team and person assignment",
                "ticket comments mentions and attachments",
                "ticket links duplicates and merges",
                "signed-link and authenticated resolution confirmation/dispute",
                "ticket CSAT and satisfaction",
                "ticket audit official timeline and transactional events",
            ),
            depends_on=(
                "support.ticket_configuration",
                "support.ticket_vocabulary",
                "support.ticket_assignment_evaluation",
                "support.ticket_automation_evaluation",
                "customer.identity_scope",
                "auth.staff_provisioning",
                "customer.branding",
                "communications.intents",
                "communications.notification_service",
                "communications.nextcloud_talk_staff",
            ),
            contract=ServiceContract(
                concerns=(
                    *tuple(
                        ConcernContract(
                            name=name,
                            role=(
                                OwnerRole.COMMAND_WRITER
                                if name
                                in {
                                    "ticket lifecycle mutations",
                                    "support ticket human-readable number allocation",
                                }
                                else OwnerRole.AUTHORITATIVE_RECORD
                            ),
                            input_names=(
                                "typed ticket command",
                                "canonical ticket state",
                                "ticket configuration",
                                "portal team-routing resolution",
                                "customer identity evidence",
                                "assignment policy proposal",
                                "automation policy proposal",
                                *(
                                    (
                                        "active assigned staff contact identity",
                                        "customer-scoped helpdesk contact",
                                        "staff notification delivery queue",
                                    )
                                    if name
                                    == "ticket lifecycle timestamps and consequences"
                                    else ()
                                ),
                            ),
                            canonical_writer="support.ticket_lifecycle",
                        )
                        for name in (
                            "ticket lifecycle mutations",
                            "ticket creation and identity",
                            "support ticket human-readable number allocation",
                            "guarded ticket status transitions",
                            "ticket lifecycle timestamps and consequences",
                            "ticket team and person assignment",
                            "ticket comments mentions and attachments",
                            "ticket links duplicates and merges",
                            "signed-link and authenticated resolution confirmation/dispute",
                            "ticket CSAT and satisfaction",
                            "ticket audit official timeline and transactional events",
                        )
                    ),
                    ConcernContract(
                        name="admin-created ticket customer email acknowledgement",
                        role=OwnerRole.EVENT_POLICY,
                        input_names=(
                            "typed ticket command",
                            "canonical ticket state",
                            "customer identity evidence",
                            "customer communication delivery intent",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed ticket command",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed TicketCreate, TicketUpdate, comment, merge, link, "
                            "resolution, satisfaction, attachment, and bulk command inputs "
                            "with CommandContext plus the closed silent-internal creation "
                            "consequence mode"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical ticket state",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Ticket, TicketAssignee, TicketComment, TicketLink, "
                            "TicketMerge, TicketAccessToken, and transactional audit/event "
                            "rows"
                        ),
                    ),
                    AuthorityInput(
                        name="ticket configuration",
                        owner="support.ticket_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "validated status subset, priorities, types, defaults, routing, "
                            "and SLA target settings"
                        ),
                    ),
                    AuthorityInput(
                        name="portal team-routing resolution",
                        owner="support.ticket_configuration",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "typed active-team fallback resolution plus the explicit "
                            "TicketCreationRoutingMode supplied by the customer portal"
                        ),
                    ),
                    AuthorityInput(
                        name="customer identity evidence",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "reviewed subscriber, account, inbound sender, and Lead binding "
                            "evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="assignment policy proposal",
                        owner="support.ticket_assignment_evaluation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "typed proposal containing team/person/scope and rule evidence; "
                            "never a Ticket mutation"
                        ),
                    ),
                    AuthorityInput(
                        name="automation policy proposal",
                        owner="support.ticket_automation_evaluation",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "typed action proposal derived from a matching active rule; "
                            "never a Ticket mutation"
                        ),
                    ),
                    AuthorityInput(
                        name="active assigned staff contact identity",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active SystemUser identity and email resolved from current "
                            "Ticket SystemUser or canonical Person assignment identifiers"
                        ),
                    ),
                    AuthorityInput(
                        name="customer-scoped helpdesk contact",
                        owner="customer.branding",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "ResolvedBrand.support_email under organization, reseller, "
                            "platform, and legacy brand precedence"
                        ),
                    ),
                    AuthorityInput(
                        name="staff notification delivery queue",
                        owner="communications.notification_service",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "durable queued Notification rows and post-commit delivery state"
                        ),
                    ),
                    AuthorityInput(
                        name="customer communication delivery intent",
                        owner="communications.intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "deduplicated customer email intent and durable Notification "
                            "delivery state"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public Ticket mutation enters execute_owner_command once on a "
                        "transaction-free session. Lifecycle helpers, SLA clocks, policy "
                        "evaluators, notifications, audit, and events are flush-only "
                        "participants. The unmatched-radio coordinator may request only the "
                        "restricted silent-internal creation/observation participants; those "
                        "participants still allocate identity and stage audit/event evidence."
                    ),
                    locking=(
                        "Existing Ticket mutations lock or operate under the root command; "
                        "status guards and merge/confirmation state are rechecked before "
                        "writing."
                    ),
                    idempotency=(
                        "Adapter Idempotency-Key is carried in CommandContext where supplied; "
                        "link identity, merge source state, access-token state, notification "
                        "keys, and event/audit evidence make replay observable and bounded."
                    ),
                    retries=(
                        "Retry the complete command after rollback with the same typed input "
                        "and idempotency key; nested helpers never retry independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ticket_not_found",
                        "comment_not_found",
                        "sla_event_not_found",
                        "lead_not_found",
                        "subscriber_not_found",
                        "lead_subscriber_mismatch",
                        "ticket_merged_source",
                        "invalid_ticket_filter",
                        "invalid_ticket_status",
                        "ticket_merge_self",
                        "satisfaction_not_eligible",
                        "resolution_confirmation_not_eligible",
                        "resolution_confirmation_terminal",
                        "resolution_confirmation_not_pending",
                        "resolution_confirmation_inactive",
                        "resolution_dispute_terminal",
                        "invalid_ticket_creation_mode",
                        "internal_ticket_source_mismatch",
                        "internal_ticket_participant_requires_transaction",
                        *owner_command_boundary_error_codes("support.ticket_lifecycle"),
                    ),
                    mapping_owner=(
                        "support API, admin web, customer portal, public confirmation, task, "
                        "and CRM transport adapters"
                    ),
                    fail_closed_on=(
                        "invalid or stale lifecycle status",
                        "ambiguous customer identity",
                        "merged source",
                        "inactive confirmation capability",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "ticket.created",
                        "ticket.assigned",
                        "ticket.resolution_requested",
                        "ticket.resolution_confirmed",
                        "ticket.resolution_disputed",
                        "support.resolution_confirmation_due",
                        "support.ticket_sla_breach_due",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 carries stable Ticket/account identifiers and bounded "
                        "change evidence, including the explicit creation consequence mode; "
                        "private comment bodies and attachments are not placed in transport "
                        "events."
                    ),
                    replay=(
                        "Canonical Ticket rows, official comments, links/merges, access "
                        "tokens, and AuditEvent rows reconstruct the lifecycle."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "support route/API/task mutations, direct service transaction "
                        "completion, assignment/automation policy Ticket writers, and direct "
                        "unmatched-radio Ticket construction"
                    ),
                    new_owner="support.ticket_lifecycle",
                    verification=(
                        "Support lifecycle, assignment, automation, comment, attachment, "
                        "link, duplicate, merge, CSAT, portal, unmatched-radio silent creation "
                        "and legacy-number repair, and architecture tests"
                    ),
                    cutover_gate=(
                        "all Ticket mutations enter the registered owner; policies return "
                        "proposals and all participants are transaction-neutral"
                    ),
                    fallback_retirement=(
                        "the retired lifecycle-owner alias, HTTPException service coupling, direct "
                        "service commits/rollbacks, policy-side Ticket writes, and direct "
                        "unmatched-radio Ticket construction are absent"
                    ),
                ),
                steward="support operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                    "docs/designs/SUPPORT_UX_POLISH_AUDIT.md",
                ),
                test_refs=(
                    "tests/test_support_services.py",
                    "tests/test_ticket_status_transition.py",
                    "tests/test_support_automation.py",
                    "tests/test_ticket_assignment_engine.py",
                    "tests/architecture/test_support_ticket_sot_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_configuration",
            module="app.services.support_ticket_settings",
            owns=(
                "ticket configuration mutations",
                "operator-visible ticket status subset",
                "ticket priority and type options",
                "ticket routing and priority/type SLA target policy",
                "customer-portal ticket fallback team routing",
            ),
            depends_on=(
                "support.ticket_vocabulary",
                "operations.service_team_lifecycle",
            ),
            notes=(
                "Configured status choices are constrained to the lifecycle "
                "vocabulary and do not own semantic colors or tones. Every "
                "ticket SLA target is operator-managed in the ticket settings UI. "
                "Customer-portal routing resolves current native Service Teams by "
                "exact case-insensitive name without owning their identity."
            ),
            contract=ServiceContract(
                concerns=(
                    *tuple(
                        ConcernContract(
                            name=name,
                            role=OwnerRole.AUTHORITATIVE_RECORD,
                            input_names=(
                                "typed ticket configuration command",
                                "ticket lifecycle vocabulary",
                                "current ticket configuration",
                            ),
                            canonical_writer="support.ticket_configuration",
                        )
                        for name in (
                            "ticket configuration mutations",
                            "operator-visible ticket status subset",
                            "ticket priority and type options",
                            "ticket routing and priority/type SLA target policy",
                        )
                    ),
                    ConcernContract(
                        name="customer-portal ticket fallback team routing",
                        role=OwnerRole.RESOLVER,
                        input_names=("active service-team identity",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed ticket configuration command",
                        owner="support.ticket_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "validated option, routing, team membership, auto-assignment, "
                            "and SLA target collections with CommandContext"
                        ),
                    ),
                    AuthorityInput(
                        name="ticket lifecycle vocabulary",
                        owner="support.ticket_vocabulary",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="TicketStatus enum and guarded terminal-state semantics",
                    ),
                    AuthorityInput(
                        name="current ticket configuration",
                        owner="support.ticket_configuration",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "workflow DomainSetting rows plus synchronized ServiceTeam and "
                            "ServiceTeamMember records"
                        ),
                    ),
                    AuthorityInput(
                        name="active service-team identity",
                        owner="operations.service_team_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "current active ServiceTeam rows matched exactly after "
                            "case normalization"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "update_options enters execute_owner_command once; all settings, "
                        "team, routing, and SLA writes flush in the same root transaction"
                    ),
                    locking=(
                        "setting keys and referenced team/member identities are re-read and "
                        "synchronized in the owner command"
                    ),
                    idempotency=(
                        "normalized option values and stable team/member identifiers make "
                        "repeated replacement commands converge"
                    ),
                    retries="retry the complete configuration replacement after rollback",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ticket_configuration_invalid",
                        *owner_command_boundary_error_codes(
                            "support.ticket_configuration"
                        ),
                    ),
                    mapping_owner=(
                        "admin system ticket-settings and customer-portal adapters"
                    ),
                    fail_closed_on=(
                        "status outside lifecycle vocabulary",
                        "invalid team/person identity",
                        "invalid SLA target",
                    ),
                ),
                events=EventContract(
                    event_types=("ticket.configuration_changed",),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility=(
                        "Version 1 identifies the configuration owner and operation without "
                        "publishing staff or customer details."
                    ),
                    replay=(
                        "workflow DomainSetting, ServiceTeam, ServiceTeamMember, and audit "
                        "rows reconstruct configuration."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="ticket settings form and multi-commit settings helpers",
                    new_owner="support.ticket_configuration",
                    verification=(
                        "ticket settings, portal routing, SLA, assignment, and "
                        "architecture tests"
                    ),
                    cutover_gate=(
                        "one typed owner command replaces the complete settings set; "
                        "portal routing uses the typed exact-name resolver"
                    ),
                    fallback_retirement=(
                        "mid-command commit, settings-route writes, portal hard-coded "
                        "team identifiers, and parallel fallback routing are absent"
                    ),
                ),
                steward="support operations",
                design_refs=(
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_support_ticket_settings.py",
                    "tests/test_sla_assignment.py",
                    "tests/architecture/test_support_ticket_sot_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_region_projection",
            module="app.services.support_ticket_region_projection",
            owns=("canonical support-ticket region projection",),
            depends_on=(
                "support.ticket_configuration",
                "support.ticket_lifecycle",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="canonical support-ticket region projection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "current ticket configuration",
                            "canonical ticket regions",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="current ticket configuration",
                        owner="support.ticket_configuration",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="configured workflow region option values",
                    ),
                    AuthorityInput(
                        name="canonical ticket regions",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "distinct normalized non-empty Region values on current "
                            "active Ticket rows"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "list_canonical_region_options reads configuration and Ticket "
                        "rows without writes."
                    ),
                    locking="A transaction-current read requires no row lock.",
                    idempotency=(
                        "Re-reading the same committed inputs returns the same normalized "
                        "values ordered by region value ascending."
                    ),
                    retries=(
                        "Adapters may retry the complete read after transient database "
                        "failure."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="support form and API adapters",
                    fail_closed_on=("unavailable current region inputs",),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "support.ticket_configuration bundled region projection concern"
                    ),
                    new_owner="support.ticket_region_projection",
                    verification="support settings and SOT relationship tests",
                    cutover_gate=(
                        "region reads name both configuration and Ticket provenance"
                    ),
                    fallback_retirement=(
                        "configuration no longer claims lifecycle-derived region authority"
                    ),
                ),
                steward="support operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                ),
                test_refs=(
                    "tests/test_support_ticket_settings.py",
                    "tests/test_sot_relationships.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_sla_clock",
            module="app.services.sla_assignment",
            owns=(
                "ticket SLA policy assignment",
                "ticket SLA clock lifecycle",
                "ticket SLA breach records",
                "ticket SLA breach event emission",
            ),
            depends_on=(
                "support.ticket_lifecycle",
                "support.ticket_configuration",
                "operations.sla_escalation",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ticket SLA policy assignment",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical ticket lifecycle state",
                            "configured ticket SLA targets",
                        ),
                    ),
                    ConcernContract(
                        name="ticket SLA clock lifecycle",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "canonical ticket lifecycle state",
                            "configured ticket SLA targets",
                            "current ticket SLA records",
                        ),
                        canonical_writer="support.ticket_sla_clock",
                    ),
                    ConcernContract(
                        name="ticket SLA breach records",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "canonical ticket lifecycle state",
                            "current ticket SLA records",
                        ),
                        canonical_writer="support.ticket_sla_clock",
                    ),
                    ConcernContract(
                        name="ticket SLA breach event emission",
                        role=OwnerRole.EVENT_POLICY,
                        input_names=(
                            "canonical ticket lifecycle state",
                            "current ticket SLA records",
                            "configured operational escalation policy",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical ticket lifecycle state",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Ticket identity, status, priority, type, assignments, "
                            "created_at, and due_at"
                        ),
                    ),
                    AuthorityInput(
                        name="configured ticket SLA targets",
                        owner="support.ticket_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "operator-managed priority and ticket-type resolution "
                            "targets plus the active ticket SlaPolicy"
                        ),
                    ),
                    AuthorityInput(
                        name="current ticket SLA records",
                        owner="support.ticket_sla_clock",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="SlaClock and SlaBreach rows for the ticket",
                    ),
                    AuthorityInput(
                        name="configured operational escalation policy",
                        owner="operations.sla_escalation",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "active ticket.sla_breached escalation policies and "
                            "participant delivery rules"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "The support ticket lifecycle or scheduled breach evaluator "
                        "owns the root transaction; this service stages clock, breach, "
                        "watcher, event, and delivery-plan rows without committing."
                    ),
                    locking=(
                        "Ticket and active-clock visibility is re-evaluated inside the "
                        "caller transaction; clock identity prevents duplicate policy "
                        "assignment and breached_at guards repeated breach writes."
                    ),
                    idempotency=(
                        "An existing ticket/policy clock is reused and only running "
                        "clocks without breached_at can create a breach."
                    ),
                    retries=(
                        "The root transaction owner retries the complete ticket, clock, "
                        "breach, and escalation operation after rollback."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=("support.ticket_sla_clock.invalid_ticket_id",),
                    mapping_owner=(
                        "support ticket lifecycle and scheduled task adapters"
                    ),
                    fail_closed_on=("invalid ticket identifier",),
                ),
                events=EventContract(
                    event_types=("ticket.sla_breached",),
                    schema_version=1,
                    delivery_owner="operations.sla_escalation",
                    compatibility=(
                        "Version 1 carries ticket identity, configured due time, "
                        "priority, title, target URL, and support category metadata."
                    ),
                    replay=(
                        "Ticket, SlaClock, SlaBreach, and configured operational "
                        "escalation rows reconstruct the breach consequence."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "support ticket lifecycle methods and hard-coded ticket-type "
                        "breach notification branches"
                    ),
                    new_owner="support.ticket_sla_clock",
                    verification=(
                        "Ticket SLA assignment, configured target, breach, and generic "
                        "operational escalation tests."
                    ),
                    cutover_gate=(
                        "Ticket lifecycle delegates clock writes and breach evaluation "
                        "to this owner and reads operator-managed targets."
                    ),
                    fallback_retirement=(
                        "Hard-coded ticket SLA duration sets and direct breach delivery "
                        "branches are absent."
                    ),
                ),
                steward="support operations",
                design_refs=(
                    "docs/ARCHITECTURE.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_sla_assignment.py",
                    "tests/test_operational_sla_policy_ui.py",
                    "tests/architecture/test_operational_sla_policy_ownership.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_work_order_handoff",
            module="app.services.ticket_work_order_handoff",
            owns=(
                "ticket-to-work-order issuance eligibility",
                "native ticket-to-work-order provenance",
                "field-outcome projection onto the ticket timeline",
                "committed field outcome consumption",
            ),
            depends_on=(
                "support.ticket_lifecycle",
                "operations.work_order_commands",
                "operations.field_completion",
                "observability.audit_log",
                "events.dispatcher",
                "events.owner_outputs",
            ),
            notes=(
                "An active member of the ticket's assigned team explicitly "
                "issues each field scope. A ticket may issue many work orders "
                "through WorkOrder.origin_ticket_id and may scope one issuance "
                "to a ticket-linked ProjectTask. Tags and ticket metadata "
                "carry no issuance authority. Field completion records an "
                "internal timeline fact and never closes the ticket."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="ticket-to-work-order issuance eligibility",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "canonical ticket lifecycle state",
                            "assigned support-team membership",
                            "typed issuance request",
                            "work-order command result",
                        ),
                    ),
                    ConcernContract(
                        name="native ticket-to-work-order provenance",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "canonical ticket lifecycle state",
                            "typed issuance request",
                            "work-order command result",
                        ),
                        canonical_writer="support.ticket_work_order_handoff",
                    ),
                    ConcernContract(
                        name="field-outcome projection onto the ticket timeline",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "native ticket-to-work-order provenance",
                            "authoritative field outcome",
                            "canonical ticket lifecycle state",
                        ),
                        canonical_writer="support.ticket_work_order_handoff",
                    ),
                    ConcernContract(
                        name="committed field outcome consumption",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authoritative field outcome",
                            "native ticket-to-work-order provenance",
                            "receipted owner-output deliveries",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical ticket lifecycle state",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked Ticket identity, active state, status, subscriber, "
                            "priority, and assigned service_team_id"
                        ),
                    ),
                    AuthorityInput(
                        name="assigned support-team membership",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active ServiceTeam and ServiceTeamMember rows selected by "
                            "the Ticket assignment"
                        ),
                    ),
                    AuthorityInput(
                        name="typed issuance request",
                        owner="support.ticket_work_order_handoff",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "TicketWorkOrderIssueCommand with explicit actor, both "
                            "permissions, scope, reason, and idempotency key"
                        ),
                    ),
                    AuthorityInput(
                        name="work-order command result",
                        owner="operations.work_order_commands",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "participant-created WorkOrder with origin_ticket_id and "
                            "stable native-create fingerprint"
                        ),
                    ),
                    AuthorityInput(
                        name="native ticket-to-work-order provenance",
                        owner="support.ticket_work_order_handoff",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "WorkOrder.origin_ticket_id plus ticket.work_order_issued "
                            "audit evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="authoritative field outcome",
                        owner="operations.field_completion",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "committed work_order.field_outcome_recorded output "
                            "from the field transition owner"
                        ),
                    ),
                    AuthorityInput(
                        name="receipted owner-output deliveries",
                        owner="events.owner_outputs",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "unique (consumer, event_id) receipts committing "
                            "atomically with each consumed field outcome"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "issue_work_order enters execute_owner_command once on a "
                        "transaction-free adapter session; work-order creation and audit "
                        "are flush-only participants."
                    ),
                    locking=(
                        "The Ticket is selected FOR UPDATE before eligibility; the "
                        "deterministic WorkOrder public id serializes concurrent replays."
                    ),
                    idempotency=(
                        "Scope-local Idempotency-Key is mandatory and derives the stable "
                        "work-order id and ticket issuance audit request id."
                    ),
                    retries=(
                        "Integrity conflicts roll back the complete root command; the "
                        "adapter retries with the same idempotency key."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ticket_not_found",
                        "ticket_inactive",
                        "ticket_terminal",
                        "ticket_subscriber_required",
                        "ticket_team_required",
                        "ticket_team_inactive",
                        "assigned_team_membership_required",
                        "project_task_not_found",
                        "project_task_ticket_mismatch",
                        "project_task_project_mismatch",
                        "permission_required",
                        "idempotency_key_required",
                        "invalid_command_scope",
                        *owner_command_boundary_error_codes(
                            "support.ticket_work_order_handoff"
                        ),
                    ),
                    mapping_owner="support API and admin web adapters",
                    fail_closed_on=(
                        "missing permission evidence",
                        "missing idempotency evidence",
                        "stale or terminal ticket state",
                        "ambiguous project-task scope",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "ticket.work_order_issued",
                        "work_order.field_outcome_recorded",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 records native ticket/work-order/project scope and "
                        "the assigned team without customer payloads."
                    ),
                    replay=(
                        "WorkOrder.origin_ticket_id and request-scoped AuditEvent rows "
                        "reconstruct issuance."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="ticket field-outcome timeline evidence",
                        input_names=(
                            "native ticket-to-work-order provenance",
                            "authoritative field outcome",
                            "canonical ticket lifecycle state",
                        ),
                        writer="support.ticket_work_order_handoff",
                        freshness="transactional with the field outcome command",
                        stale_behavior=(
                            "Ticket remains open for Support verification; absence never "
                            "implies resolution."
                        ),
                        drift_signal=(
                            "origin-linked terminal WorkOrder without matching ticket "
                            "timeline evidence"
                        ),
                        rebuild_operation=(
                            "replay terminal WorkOrder field events by field_event_id"
                        ),
                        repair_owner="support.ticket_work_order_handoff",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "support automation field_visit tag, Ticket.metadata work_order_id, "
                        "and overloaded WorkOrder.crm_ticket_id"
                    ),
                    new_owner="support.ticket_work_order_handoff",
                    verification=(
                        "migration provenance validator and handoff behavior/architecture "
                        "tests"
                    ),
                    cutover_gate=(
                        "native origin_ticket_id is populated only from corroborated links; "
                        "external CRM provenance remains preserved"
                    ),
                    fallback_retirement=(
                        "tags and metadata cannot issue or link field work and generic work-"
                        "order schemas cannot accept origin_ticket_id"
                    ),
                ),
                steward="support and field operations",
                design_refs=(
                    "docs/designs/TICKET_WORK_ORDER_HANDOFF_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/runbooks/TICKET_WORK_ORDER_PROVENANCE_CUTOVER.md",
                ),
                test_refs=(
                    "tests/test_ticket_work_order_handoff.py",
                    "tests/test_ticket_work_order_handoff_migration.py",
                    "tests/architecture/test_ticket_work_order_handoff_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="support.ticket_bulk_commands",
            module="app.services.web_support_ticket_bulk",
            owns=(
                "selected support-ticket bulk membership resolution",
                "support-ticket bulk change normalization",
                "support-ticket bulk update eligibility preview",
                "support-ticket bulk confirmation drift detection",
                "structured support-ticket bulk update outcomes",
            ),
            depends_on=(
                "support.ticket_lifecycle",
                "support.ticket_configuration",
                "ui.bulk_action_contracts",
            ),
            notes=(
                "Execution delegates each eligible mutation to "
                "app.services.support.Tickets.update through Tickets.bulk_update "
                "so SLA, automation, assignment, notification, "
                "event, audit, and workqueue consequences have one owner."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=name,
                        role=(
                            OwnerRole.POLICY
                            if name
                            in {
                                "support-ticket bulk update eligibility preview",
                                "support-ticket bulk confirmation drift detection",
                            }
                            else OwnerRole.RESOLVER
                        ),
                        input_names=(
                            "typed bulk selection and changes",
                            "canonical ticket lifecycle state",
                            "ticket configuration",
                        ),
                    )
                    for name in (
                        "selected support-ticket bulk membership resolution",
                        "support-ticket bulk change normalization",
                        "support-ticket bulk update eligibility preview",
                        "support-ticket bulk confirmation drift detection",
                        "structured support-ticket bulk update outcomes",
                    )
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed bulk selection and changes",
                        owner="support.ticket_bulk_commands",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "page-only explicit Ticket identifiers, normalized change set, "
                            "expected count, scope token, and confirmation flag"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical ticket lifecycle state",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active Ticket identity, merge state, and current field values",
                    ),
                    AuthorityInput(
                        name="ticket configuration",
                        owner="support.ticket_configuration",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="configured status, priority, and assignment choices",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "membership, normalization, preview, and drift validation are read-"
                        "only; confirmed writes enter support.ticket_lifecycle separately"
                    ),
                    locking=(
                        "confirmation recomputes exact membership and eligibility immediately "
                        "before the lifecycle command"
                    ),
                    idempotency=(
                        "deterministic scope token covers normalized changes and every "
                        "selected eligibility outcome"
                    ),
                    retries="re-preview from canonical Ticket/configuration inputs",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "support_ticket_bulk_invalid",
                        "support_ticket_bulk_preview_required",
                        "support_ticket_bulk_scope_drift",
                        "support_ticket_bulk_confirmation_required",
                    ),
                    mapping_owner="admin support bulk HTTP adapter",
                    fail_closed_on=(
                        "missing confirmation",
                        "membership drift",
                        "eligibility drift",
                        "unconfigured value",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="admin bulk route and client-selected membership",
                    new_owner="support.ticket_bulk_commands",
                    verification="bulk preview, drift, execution, and architecture tests",
                    cutover_gate="route delegates exact preview and confirmed execution scope",
                    fallback_retirement="route-side eligibility and unverified membership are absent",
                ),
                steward="support operations",
                design_refs=(
                    "docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_support_ticket_bulk_actions.py",
                    "tests/architecture/test_support_ticket_sot_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.api.support",
        "app.api.me.support",
        "app.web.admin.support",
        "app.web.customer.support",
        "mobile",
    ),
    rule="Support adapters request ticket mutations through the ticket service. "
    "The lifecycle owner validates raw statuses; settings may expose a "
    "subset but cannot add states or define their semantic presentation.",
)
