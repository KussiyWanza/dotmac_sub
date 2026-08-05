"""Canonical SOT declarations for the notifications_communications domain."""

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


def _team_inbox_contract(
    *,
    service_name: str,
    concerns: tuple[tuple[str, OwnerRole], ...],
    inputs: tuple[AuthorityInput, ...],
    transaction_mode: TransactionMode,
    event_types: tuple[str, ...] = (),
    projections: tuple[str, ...] = (),
    mapping_owner: str = "Team Inbox transport and web adapters",
    design_refs: tuple[str, ...] | None = None,
    test_refs: tuple[str, ...] | None = None,
) -> ServiceContract:
    """Build the uniformly complete contract shared by the Inbox owner family."""

    input_names = tuple(item.name for item in inputs)
    writer_roles = {
        OwnerRole.AUTHORITATIVE_RECORD,
        OwnerRole.OBSERVATION_COLLECTOR,
        OwnerRole.COMMAND_WRITER,
        OwnerRole.RECONCILER,
        OwnerRole.PROJECTION_WRITER,
    }
    has_writer = any(role in writer_roles for _name, role in concerns)
    boundary_codes = (
        owner_command_boundary_error_codes(service_name)
        if transaction_mode
        in {TransactionMode.OWNER_MANAGED, TransactionMode.COORDINATOR_MANAGED}
        else ()
    )
    return ServiceContract(
        concerns=tuple(
            ConcernContract(
                name=name,
                role=role,
                input_names=input_names,
                canonical_writer=service_name if role in writer_roles else None,
            )
            for name, role in concerns
        ),
        authoritative_inputs=inputs,
        transaction=TransactionContract(
            mode=transaction_mode,
            boundary=(
                "Public commands enter execute_owner_command once on a transaction-free "
                "session; participants only flush; read and transport owners never "
                "complete a business transaction."
            ),
            locking=(
                "Conversation, observation, assignment, contact-link, message, and read "
                "cursor identities are locked in stable aggregate order; database unique "
                "constraints arbitrate concurrent provider and operator retries."
            ),
            idempotency=(
                "Provider/account/event identity, external message identity, communication "
                "intent identity, active assignment/contact uniqueness, and operator read "
                "cursors replay the same stable outcome or reject changed evidence."
            ),
            retries=(
                "Adapters retry only after complete rollback; deterministic duplicates and "
                "reordered receipts return stable non-regressing outcomes."
            ),
        ),
        errors=ErrorContract(
            domain_codes=(
                f"{service_name}.invalid_command",
                f"{service_name}.invalid_observation",
                f"{service_name}.invalid_read_time",
                f"{service_name}.not_found",
                f"{service_name}.conversation_not_found",
                f"{service_name}.message_not_found",
                f"{service_name}.message_scope_mismatch",
                f"{service_name}.observation_not_found",
                f"{service_name}.identity_collision",
                f"{service_name}.provider_event_identity_collision",
                f"{service_name}.command_rejected",
                *boundary_codes,
            ),
            mapping_owner=mapping_owner,
            retryable_codes=(),
            fail_closed_on=(
                "unverified provider provenance",
                "ambiguous contact identity",
                "provider identity reuse with changed evidence",
                "stale or cross-conversation operator input",
            ),
        ),
        events=(
            EventContract(
                event_types=event_types or (f"{service_name}.changed.v1",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries stable message, conversation, participant, channel, "
                    "command/correlation, provenance, and outcome identifiers without raw "
                    "provider payloads."
                ),
                replay=(
                    "Authoritative Inbox rows plus observation, intent, assignment, contact, "
                    "receipt, and read-cursor evidence deterministically rebuild projections."
                ),
            )
            if has_writer
            else None
        ),
        projections=tuple(
            ProjectionContract(
                name=name,
                input_names=input_names,
                writer=service_name,
                freshness="Transaction-current for database reads; realtime is best effort.",
                stale_behavior=(
                    "Database truth remains usable with explicit freshness; realtime clients "
                    "refetch when an event is missed or unavailable."
                ),
                drift_signal=(
                    "Projection parity queries compare cohort counts, read cursors, receipt "
                    "order, contact links, and current conversation/message state."
                ),
                rebuild_operation=(
                    "The owner recomputes the projection from authoritative Inbox rows and "
                    "committed provider observations without replaying transports."
                ),
                repair_owner=service_name,
            )
            for name in projections
        ),
        migration=MigrationContract(
            state=AuthorityMigrationState.COMPLETE,
            old_owner=(
                "communications.team_inbox catch-all plus route-owned list contracts and "
                "helper-level transaction completion"
            ),
            new_owner=service_name,
            verification=(
                "Focused Inbox behavior, idempotency, projection, UI contract, and architecture tests."
            ),
            cutover_gate=(
                "Every adapter delegates to the typed owner and all provider facts are durable before consequences."
            ),
            fallback_retirement=(
                "The catch-all legacy manifest rows, route list definition, raw provider decision paths, and helper commits are removed."
            ),
        ),
        steward="customer experience platform",
        design_refs=design_refs
        or (
            "docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md",
            "docs/SOT_RELATIONSHIP_MAP.md",
            "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
        ),
        test_refs=test_refs
        or (
            "tests/test_team_inbox_sot_completion.py",
            "tests/architecture/test_team_inbox_boundaries.py",
            "tests/architecture/test_team_inbox_sot_contracts.py",
        ),
    )


DOMAIN = DomainSOT(
    domain="notifications_communications",
    services=(
        SOTService(
            name="communications.surveys",
            module="app.services.surveys",
            owns=(
                "survey lifecycle and content",
                "survey invitation records",
                "survey response records",
            ),
            depends_on=(
                "party.registry",
                "customer.accounts",
                "support.ticket_lifecycle",
                "operations.field_completion",
                "communications.intents",
                "events.store",
            ),
            notes=(
                "Survey creation always records a draft. Public response and "
                "automatic trigger eligibility require both active lifecycle "
                "status and is_active=true. Ticket and field adapters consume "
                "committed owner events; they never poll or infer completion."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="survey lifecycle and content",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "typed Survey command",
                            "authenticated administrator Person binding",
                            "persisted Survey aggregate",
                        ),
                        canonical_writer="communications.surveys",
                    ),
                    ConcernContract(
                        name="survey invitation records",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "persisted Survey aggregate",
                            "committed ticket closure outcome",
                            "committed work-order completion outcome",
                            "canonical subscriber identity",
                            "durable communication intent outcome",
                        ),
                        canonical_writer="communications.surveys",
                    ),
                    ConcernContract(
                        name="survey response records",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "persisted Survey aggregate",
                            "typed public Survey response",
                        ),
                        canonical_writer="communications.surveys",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="typed Survey command",
                        owner="communications.surveys",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "SurveyCreate, SurveyUpdate, lifecycle and send command "
                            "objects validated before explicit field assignment"
                        ),
                    ),
                    AuthorityInput(
                        name="authenticated administrator Person binding",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active SystemUser.person_party_id reviewed Person Party "
                            "binding resolved inside the create command"
                        ),
                    ),
                    AuthorityInput(
                        name="persisted Survey aggregate",
                        owner="communications.surveys",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Survey lifecycle, content, trigger, public access, "
                            "metrics and invitation rows"
                        ),
                    ),
                    AuthorityInput(
                        name="committed ticket closure outcome",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "ticket.resolution_confirmed durable event with "
                            "canonical subscriber identity"
                        ),
                    ),
                    AuthorityInput(
                        name="committed work-order completion outcome",
                        owner="operations.field_completion",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "work_order.field_outcome_recorded durable event whose "
                            "outcome is complete"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscriber identity",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active Subscriber row addressed by the owner event",
                    ),
                    AuthorityInput(
                        name="durable communication intent outcome",
                        owner="communications.intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "deduplicated Survey invitation communication intent and "
                            "notification outbox rows"
                        ),
                    ),
                    AuthorityInput(
                        name="typed public Survey response",
                        owner="communications.surveys",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "SubmitSurveyResponseCommand answers validated against "
                            "the stored typed question contract"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Every create, edit, lifecycle, invitation and response "
                        "mutation enters execute_owner_command once on a "
                        "transaction-free adapter session; nested communication "
                        "helpers only flush."
                    ),
                    locking=(
                        "Lifecycle, send and response commands lock the Survey; "
                        "tracked responses also lock the invitation. Public slug, "
                        "creation key and event-recipient unique constraints "
                        "arbitrate concurrent winners."
                    ),
                    idempotency=(
                        "Creation keys bind to a content fingerprint; automatic "
                        "invitations are unique per Survey, recipient and source "
                        "event; tracked invitations admit one response."
                    ),
                    retries=(
                        "Exact creation and trigger retries replay their persisted "
                        "outcome. Constraint conflicts map to stable domain errors; "
                        "other transaction failures roll back for caller retry."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        *owner_command_boundary_error_codes("communications.surveys"),
                        "communications.surveys.answer_required",
                        "communications.surveys.choice_invalid",
                        "communications.surveys.closed_survey",
                        "communications.surveys.creator_not_authorized",
                        "communications.surveys.creator_person_unresolved",
                        "communications.surveys.duplicate_answer",
                        "communications.surveys.duplicate_question_key",
                        "communications.surveys.free_text_too_long",
                        "communications.surveys.idempotency_conflict",
                        "communications.surveys.idempotency_key_invalid",
                        "communications.surveys.idempotency_key_required",
                        "communications.surveys.invalid_questions",
                        "communications.surveys.invitation_completed",
                        "communications.surveys.invitation_unavailable",
                        "communications.surveys.nps_invalid",
                        "communications.surveys.pause_requires_active",
                        "communications.surveys.public_slug_duplicate",
                        "communications.surveys.questions_required",
                        "communications.surveys.rating_invalid",
                        "communications.surveys.recipient_not_found",
                        "communications.surveys.response_not_found",
                        "communications.surveys.survey_expired",
                        "communications.surveys.survey_inactive",
                        "communications.surveys.survey_not_found",
                        "communications.surveys.survey_reference_required",
                        "communications.surveys.survey_unavailable",
                        "communications.surveys.unknown_answer_key",
                    ),
                    mapping_owner="Survey web, API, public and event adapters",
                    fail_closed_on=(
                        "unresolved creator Person identity",
                        "draft paused closed inactive or expired public access",
                        "invalid or empty questions during activation or send",
                        "malformed response answers or duplicate invitation use",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "survey.created",
                        "survey.updated",
                        "survey.activated",
                        "survey.paused",
                        "survey.closed",
                        "survey.archived",
                        "survey.sent",
                        "survey.trigger_invitations_created",
                        "survey.response_recorded",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 custom-event payloads are identifier-only and "
                        "additive; customer answers never enter event payloads."
                    ),
                    replay=(
                        "Survey and invitation rows plus audit evidence rebuild "
                        "current state; source-event uniqueness makes durable event "
                        "redelivery a no-op."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.comms Survey CRUD and route-local raw JSON "
                        "validation"
                    ),
                    new_owner="communications.surveys",
                    verification=(
                        "Focused owner, form, public safety and trigger tests plus "
                        "the Survey architecture boundary guard."
                    ),
                    cutover_gate=(
                        "All Survey adapters use typed commands, no old Survey "
                        "writer remains, and draft/public/trigger guards fail closed."
                    ),
                    fallback_retirement=(
                        "The Surveys and SurveyResponses writers are removed from "
                        "app.services.comms; rebuild_survey_projections recomputes "
                        "metrics from canonical invitation and response rows, while "
                        "invitation repair replays canonical owner events."
                    ),
                ),
                steward="customer experience platform",
                design_refs=(
                    "docs/designs/SURVEY_LIFECYCLE_AND_CREATION.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_surveys.py",
                    "tests/architecture/test_survey_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="communications.channel_policy",
            module="app.services.notification_channel_policy",
            owns=(
                "channel eligibility",
                "channel preference resolution",
                "stored channel policy (default/category/event overrides)",
            ),
            notes=(
                "Channel selection for customer notifications happens here "
                "and nowhere else. Feature areas must not carry their own "
                "channel setting; they state intent (template code, event "
                "type, category) and the policy resolves the channels. "
                "Operator surface: /admin/notifications/channels."
            ),
        ),
        SOTService(
            name="communications.customer_policy",
            module="app.services.customer_notification_policy",
            owns=(
                "customer notification eligibility",
                "cohort-batched customer notification eligibility",
            ),
            depends_on=(
                "communications.channel_policy",
                "communications.eligibility",
                "customer.accounts",
                "customer.identity_scope",
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="customer notification eligibility",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "customer notification identity and preferences",
                            "account notification status",
                            "channel configuration",
                            "recipient suppression ledger",
                            "recent notification history",
                            "evaluation time",
                        ),
                    ),
                    ConcernContract(
                        name="cohort-batched customer notification eligibility",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "customer notification identity and preferences",
                            "account notification status",
                            "channel configuration",
                            "recipient suppression ledger",
                            "recent notification history",
                            "evaluation time",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="customer notification identity and preferences",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Subscriber and SubscriberContact identity, recipient, "
                            "and notification-preference fields"
                        ),
                    ),
                    AuthorityInput(
                        name="account notification status",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Subscriber lifecycle status",
                    ),
                    AuthorityInput(
                        name="channel configuration",
                        owner="communications.channel_policy",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="canonical channel enablement configuration",
                    ),
                    AuthorityInput(
                        name="recipient suppression ledger",
                        owner="communications.eligibility",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="normalized active communication suppression entries",
                    ),
                    AuthorityInput(
                        name="recent notification history",
                        owner="communications.notification_service",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "persisted recipient, event, category, status, and "
                            "creation time used by the dedupe window"
                        ),
                    ),
                    AuthorityInput(
                        name="evaluation time",
                        owner="external:system_clock",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="explicit UTC cohort evaluation time",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The caller owns the session. Individual and cohort policy "
                        "queries read canonical inputs and never write or complete "
                        "a transaction."
                    ),
                    locking=(
                        "No row locks are acquired. The notification intent owner "
                        "rechecks policy when materializing a delivery."
                    ),
                    idempotency=(
                        "The same typed candidates, evaluation time, and visible "
                        "canonical evidence produce the same ordered decisions."
                    ),
                    retries=(
                        "Transient reads may be retried; malformed typed inputs fail "
                        "before policy evaluation."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="calling notification adapter or intent owner",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="communications.customer_policy",
                ),
                steward="customer communications",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/CODING_STANDARD.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_customer_bulk_actions.py",
                    "tests/test_communication_eligibility.py",
                    "tests/architecture/test_customer_notification_policy_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="communications.eligibility",
            module="app.services.communication_eligibility",
            owns=(
                "recipient suppression ledger",
                "transactional versus marketing send eligibility",
            ),
        ),
        SOTService(
            name="communications.intents",
            module="app.services.communication_intents",
            owns=(
                "communication intent lifecycle",
                "recipient and channel delivery expansion",
                "intent delivery outcome projection",
                "durable delivery attachment reference contract",
            ),
            depends_on=(
                "communications.channel_policy",
                "communications.customer_policy",
                "communications.eligibility",
                "communications.notification_service",
                "financial.invoices",
            ),
            notes=(
                "Invoice email attachments persist only a typed invoice-PDF "
                "reference. The delivery worker revalidates account scope and "
                "materializes bytes through the canonical billing invoice PDF "
                "service immediately before SMTP transport. Required attachment "
                "failure retries the complete delivery; body-only fallback is "
                "forbidden."
            ),
        ),
        SOTService(
            name="communications.customer_experience_intents",
            module="app.services.customer_experience_communications",
            owns=(
                "customer-work lifecycle communication intent names",
                "customer-work communication content and native lineage metadata",
                "customer-work communication dedupe identities",
            ),
            depends_on=(
                "communications.intents",
                "customer.experience_lifecycle",
                "operations.field_completion",
                "support.ticket_lifecycle",
            ),
            notes=(
                "This service requests delivery outcomes only. The intent "
                "control plane expands primary and authorized-contact "
                "recipients, resolves email/direct WhatsApp/push channels, "
                "applies suppressions and preferences, and owns delivery state."
            ),
        ),
        SOTService(
            name="communications.ephemeral_actions",
            module="app.services.ephemeral_communication_actions",
            owns=(
                "typed non-secret ephemeral communication action envelope",
                "just-in-time sensitive message materialization orchestration",
                "secret-free transport outcome persistence contract",
            ),
            depends_on=(
                "communications.intents",
                "communications.eligibility",
                "communications.notification_service",
            ),
            notes=(
                "Calling domains own capability purpose, claims, lifetime, "
                "and consequences. The communications worker materializes an "
                "allowlisted action immediately before transport and never "
                "persists or logs its rendered bearer content."
            ),
        ),
        SOTService(
            name="communications.notification_service",
            module="app.services.notification",
            owns=("notification row lifecycle", "delivery state"),
            depends_on=(
                "communications.channel_policy",
                "communications.customer_policy",
            ),
        ),
        SOTService(
            name="communications.customer_read_state",
            module="app.services.customer_portal_notifications",
            owns=(
                "customer notification read/unread state",
                "customer notification unread counts",
                "legacy device read-state migration boundary",
            ),
            depends_on=(
                "customer.identity_scope",
                "communications.customer_policy",
                "communications.notification_service",
            ),
        ),
        SOTService(
            name="operations.sla_escalation",
            module="app.services.operational_escalation",
            owns=(
                "operational SLA event policy lifecycle",
                "event-scoped escalation timing and channel policy",
                "operational escalation event and delivery planning",
                "operational escalation acknowledgement and cancellation",
            ),
            depends_on=("auth.permission_gate", "events.dispatcher"),
            notes=(
                "Operational domains emit named facts. Operators configure "
                "entity type, event key, levels, delays and delivery channels "
                "in the admin UI; domain services do not embed SLA timings."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="operational SLA event policy lifecycle",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "validated SLA policy command",
                            "current operational SLA records",
                        ),
                        canonical_writer="operations.sla_escalation",
                    ),
                    ConcernContract(
                        name="event-scoped escalation timing and channel policy",
                        role=OwnerRole.EVENT_POLICY,
                        input_names=(
                            "current operational SLA records",
                            "validated operational event observation",
                        ),
                    ),
                    ConcernContract(
                        name="operational escalation event and delivery planning",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "current operational SLA records",
                            "validated operational event observation",
                            "operational participant records",
                        ),
                        canonical_writer="operations.sla_escalation",
                    ),
                    ConcernContract(
                        name=(
                            "operational escalation acknowledgement and cancellation"
                        ),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authenticated escalation command evidence",
                            "current operational SLA records",
                        ),
                        canonical_writer="operations.sla_escalation",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="validated SLA policy command",
                        owner="operations.sla_escalation_commands",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed create, update, or deactivate command admitted by "
                            "the operational SLA policy coordinator"
                        ),
                    ),
                    AuthorityInput(
                        name="current operational SLA records",
                        owner="operations.sla_escalation",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "OperationalEscalationPolicy, OperationalEscalationEvent, "
                            "and OperationalEscalationDelivery rows"
                        ),
                    ),
                    AuthorityInput(
                        name="validated operational event observation",
                        owner="operations.sla_escalation",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "normalized entity type, dotted event key, severity, "
                            "affected-customer count, and owner-supplied metadata"
                        ),
                    ),
                    AuthorityInput(
                        name="operational participant records",
                        owner="operations.sla_escalation",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active OperationalOwner, OperationalWatcher, and "
                            "OperationalRoomLink records"
                        ),
                    ),
                    AuthorityInput(
                        name="authenticated escalation command evidence",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "authorized actor and target carried by the calling domain "
                            "or admin command boundary"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "The calling domain or operations.sla_escalation_commands "
                        "owns the root transaction; this service stages rows and "
                        "flushes but never commits or rolls back."
                    ),
                    locking=(
                        "Database uniqueness protects active event-policy levels and "
                        "delivery deduplication; callers serialize command targets."
                    ),
                    idempotency=(
                        "Open event identity and delivery deduplication keys return "
                        "existing records for repeated observations."
                    ),
                    retries=(
                        "Only the root transaction owner retries the complete fact, "
                        "event, and delivery-plan operation."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "operations.sla_escalation.invalid_event",
                        "operations.sla_escalation.invalid_participant",
                    ),
                    mapping_owner=(
                        "operations.sla_escalation_commands and calling domain adapters"
                    ),
                    fail_closed_on=(
                        "unsupported operational entity",
                        "invalid event key",
                        "ambiguous participant target",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "operational.sla_escalation.recorded",
                        "operational.sla_escalation.acknowledged",
                        "operational.sla_escalation.canceled",
                        "operational.sla_delivery.planned",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 identifies the operational entity, event key, "
                        "policy level, lifecycle state, and delivery-plan identity."
                    ),
                    replay=(
                        "Operational policy, event, participant, and delivery rows "
                        "reconstruct escalation state and pending delivery plans."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "domain-specific hard-coded SLA durations and direct "
                        "notification branches"
                    ),
                    new_owner="operations.sla_escalation",
                    verification=(
                        "Operational SLA ownership, policy UI, ticket SLA, payment "
                        "proof, project, and outage behavior tests."
                    ),
                    cutover_gate=(
                        "Operational emitters provide named facts and read configured "
                        "policy instead of embedding escalation timings."
                    ),
                    fallback_retirement=(
                        "Hard-coded domain SLA timing and direct delivery branches are "
                        "absent from migrated emitters."
                    ),
                ),
                steward="operations platform",
                design_refs=(
                    "docs/ARCHITECTURE.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_operational_escalation.py",
                    "tests/test_operational_sla_policy_ui.py",
                    "tests/architecture/test_operational_sla_policy_ownership.py",
                ),
            ),
        ),
        SOTService(
            name="operations.sla_escalation_commands",
            module="app.services.web_notifications_sla_policies",
            owns=("operational SLA policy command confirmation",),
            depends_on=(
                "auth.permission_gate",
                "operations.sla_escalation",
            ),
            notes=(
                "This coordinator admits typed admin policy commands and owns the "
                "root transaction. The operational escalation participant validates "
                "and stages the canonical policy record."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="operational SLA policy command confirmation",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authenticated SLA policy command evidence",
                            "current operational SLA records",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated SLA policy command evidence",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "CommandContext plus normalized admin policy form values"
                        ),
                    ),
                    AuthorityInput(
                        name="current operational SLA records",
                        owner="operations.sla_escalation",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "current policy identity and active entity/event/level "
                            "uniqueness evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "execute_owner_command admits a transaction-free session, "
                        "runs one create, update, or deactivate operation, and commits "
                        "or rolls back the complete command."
                    ),
                    locking=(
                        "Policy identity is resolved inside the owned transaction and "
                        "database uniqueness closes concurrent active-level races."
                    ),
                    idempotency=(
                        "Create rejects an existing active entity/event/level owner; "
                        "update and deactivate target one immutable policy id."
                    ),
                    retries=(
                        "Adapters may retry only with a fresh transaction after a "
                        "complete rollback and preserved command evidence."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "operations.sla_escalation_commands.invalid_policy",
                        "operations.sla_escalation_commands.not_found",
                        "operations.sla_escalation_commands.duplicate_active_policy",
                        "operations.sla_escalation_commands.active_caller_transaction",
                        "operations.sla_escalation_commands.command_contract_violation",
                        "operations.sla_escalation_commands.invalid_command_context",
                        "operations.sla_escalation_commands.nested_owner_command",
                        "operations.sla_escalation_commands.nested_transaction_completion",
                    ),
                    mapping_owner="admin notification routes",
                    fail_closed_on=(
                        "missing authenticated actor",
                        "invalid event or channel policy",
                        "duplicate active policy level",
                        "missing policy identity",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "admin notification handlers and service helpers with manual "
                        "commit and rollback ownership"
                    ),
                    new_owner="operations.sla_escalation_commands",
                    verification=(
                        "Owner-command transaction tests and operational SLA policy UI "
                        "behavior tests."
                    ),
                    cutover_gate=(
                        "Admin routes carry CommandContext and perform no transaction "
                        "completion around policy commands."
                    ),
                    fallback_retirement=(
                        "Operational escalation participant commit helpers and route "
                        "rollback branches are removed."
                    ),
                ),
                steward="operations platform",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_operational_sla_policy_ui.py",
                    "tests/architecture/test_operational_sla_policy_ownership.py",
                    "tests/architecture/test_owner_command_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="communications.staff_notifications",
            module="app.services.staff_notifications",
            owns=(
                "admin/staff notification creation",
                "permission-targeted staff notification audience resolution",
                "staff review inbox materialization",
            ),
            depends_on=(
                "communications.notification_service",
                "operations.sla_escalation",
            ),
        ),
        SOTService(
            name="communications.campaigns",
            module="app.services.comms_campaigns",
            owns=(
                "native communication campaign lifecycle",
                "periodic campaign admission decision",
                "campaign sender and sequence lifecycle",
                "campaign audience and recipient delivery state",
            ),
            depends_on=(
                "communications.eligibility",
                "communications.intents",
                "communications.team_inbox_campaigns",
            ),
            notes=(
                "Owns Sub outbound communication campaigns, not external "
                "advertising campaigns. External provider campaign IDs are "
                "lead-origin provenance owned by sales.lead_lifecycle. The "
                "campaign-processing setting gates new periodic campaign "
                "admission only; admitted campaign and sequence work drains "
                "without a scheduler enablement control."
            ),
        ),
        SOTService(
            name="communications.team_inbox_participants",
            module="app.services.team_inbox_participants",
            owns=("conversation participant endpoint projection",),
            depends_on=("communications.team_inbox_routing",),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_participants",
                concerns=(
                    (
                        "conversation participant endpoint projection",
                        OwnerRole.PROJECTION_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="stored conversation message headers",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Persisted InboxMessage from, to and cc endpoints for one conversation.",
                    ),
                    AuthorityInput(
                        name="owned mailbox register",
                        owner="communications.team_inbox_routing",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Configured team inbox email routes and intake recipients, so our own mailboxes are never admitted as participants.",
                    ),
                ),
                transaction_mode=TransactionMode.PARTICIPANT,
                projections=("inbox_conversation_participants",),
                design_refs=("docs/designs/INBOX_CONVERSATION_PARTICIPANTS.md",),
                test_refs=(
                    "tests/test_team_inbox_participants.py",
                    "tests/architecture/test_team_inbox_boundaries.py",
                    "tests/architecture/test_team_inbox_sot_contracts.py",
                ),
            ),
        ),
        SOTService(
            name="communications.team_inbox_observations",
            module="app.services.team_inbox_observations",
            owns=("normalized inbound provider observation ledger",),
            depends_on=("integration.inbox",),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_observations",
                concerns=(
                    (
                        "normalized inbound provider observation ledger",
                        OwnerRole.OBSERVATION_COLLECTOR,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="verified normalized provider fact",
                        owner="external:communications_provider",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Authenticated email, WhatsApp, social, or widget adapter output with bounded message or receipt fields.",
                    ),
                    AuthorityInput(
                        name="verified webhook admission",
                        owner="integration.inbox",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Committed integration receipt trust binding, provider-event identity, digest, and retry lifecycle.",
                    ),
                ),
                transaction_mode=TransactionMode.OWNER_MANAGED,
                event_types=("team_inbox.provider_observation_recorded.v1",),
            ),
        ),
        SOTService(
            name="communications.team_inbox_field_job",
            module="app.services.team_inbox_field_job",
            owns=(
                "field job chat conversation lifecycle",
                "work order to inbox conversation link",
            ),
            depends_on=("communications.team_inbox_routing",),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_field_job",
                concerns=(
                    (
                        "field job chat conversation lifecycle",
                        OwnerRole.POLICY,
                    ),
                    (
                        "work order to inbox conversation link",
                        OwnerRole.AUTHORITATIVE_RECORD,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="committed field job departure",
                        owner="operations.field_completion",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Technician en_route/complete transition, row-locked "
                            "and idempotent on the client event id."
                        ),
                    ),
                ),
                transaction_mode=TransactionMode.PARTICIPANT,
            ),
        ),
        SOTService(
            name="communications.team_inbox_processing",
            module="app.services.team_inbox_processing",
            owns=("provider observation consequence coordination",),
            depends_on=(
                "ai.intake",
                "communications.team_inbox_observations",
                "communications.team_inbox_threads",
                "communications.team_inbox_contact_resolution",
                "communications.team_inbox_routing",
                "communications.team_inbox_delivery_receipts",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_processing",
                concerns=(
                    (
                        "provider observation consequence coordination",
                        OwnerRole.APPLICATION_COORDINATOR,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="committed normalized observation",
                        owner="communications.team_inbox_observations",
                        kind=AuthorityKind.OBSERVATION,
                        source="Locked InboxProviderObservation recorded in an earlier owner transaction.",
                    ),
                    AuthorityInput(
                        name="conversation identity",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Canonical provider, channel, thread, conversation, and message identity.",
                    ),
                    AuthorityInput(
                        name="contact decision",
                        owner="communications.team_inbox_contact_resolution",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Explicit matched, ambiguous, suppressed, or unmatched contact outcome.",
                    ),
                    AuthorityInput(
                        name="routing decision",
                        owner="communications.team_inbox_routing",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Effective team, assignment, and escalation outcome.",
                    ),
                    AuthorityInput(
                        name="delivery receipt state",
                        owner="communications.team_inbox_delivery_receipts",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Timestamp-monotonic provider delivery state.",
                    ),
                    AuthorityInput(
                        name="validated AI intake result",
                        owner="ai.intake",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Bounded destination-team classification or explicit fallback status.",
                    ),
                ),
                transaction_mode=TransactionMode.COORDINATOR_MANAGED,
            ),
        ),
        SOTService(
            name="communications.team_inbox_threads",
            module="app.services.team_inbox_receive",
            owns=(
                "conversation identity and threading",
                "authoritative conversation and message records",
            ),
            depends_on=("communications.team_inbox_observations",),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_threads",
                concerns=(
                    ("conversation identity and threading", OwnerRole.RESOLVER),
                    (
                        "authoritative conversation and message records",
                        OwnerRole.AUTHORITATIVE_RECORD,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="normalized inbound message fact",
                        owner="communications.team_inbox_observations",
                        kind=AuthorityKind.OBSERVATION,
                        source="Provider/account/message identity, channel, observed time, references, subject, participant address, and bounded content.",
                    ),
                ),
                transaction_mode=TransactionMode.PARTICIPANT,
                event_types=(
                    "team_inbox.message_recorded.v1",
                    "team_inbox.conversation_opened.v1",
                ),
            ),
        ),
        SOTService(
            name="communications.team_inbox_contact_resolution",
            module="app.services.team_inbox_contact_links",
            owns=(
                "contact subscriber reseller and ticket association resolution",
                "reviewed contact association and projection repair",
            ),
            depends_on=(
                "party.registry",
                "customer.identity_scope",
                "communications.team_inbox_threads",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_contact_resolution",
                concerns=(
                    (
                        "contact subscriber reseller and ticket association resolution",
                        OwnerRole.RESOLVER,
                    ),
                    (
                        "reviewed contact association and projection repair",
                        OwnerRole.PROJECTION_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="canonical party contact facts",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Reviewed Party, contact point, provider scope, and relationship evidence.",
                    ),
                    AuthorityInput(
                        name="customer identity scope",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Active Subscriber and reseller ownership identifiers; never fuzzy name or shared-address inference.",
                    ),
                    AuthorityInput(
                        name="conversation contact route",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Conversation channel and normalized contact address.",
                    ),
                ),
                transaction_mode=TransactionMode.OWNER_MANAGED,
                event_types=("team_inbox.contact_link_changed.v1",),
                projections=("InboxContactLink canonical contact-point projection",),
            ),
        ),
        SOTService(
            name="communications.team_inbox_routing",
            module="app.services.team_inbox_assignment",
            owns=(
                "routing assignment and escalation policy",
                "routing assignment and escalation transitions",
            ),
            depends_on=(
                "ai.intake",
                "communications.team_inbox_threads",
                "operations.sla_escalation",
                "auth.permission_gate",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_routing",
                concerns=(
                    ("routing assignment and escalation policy", OwnerRole.POLICY),
                    (
                        "routing assignment and escalation transitions",
                        OwnerRole.COMMAND_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="conversation routing facts",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Recipients, current owner team, assignment, priority, and lifecycle.",
                    ),
                    AuthorityInput(
                        name="operational escalation policy",
                        owner="operations.sla_escalation",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Configured Inbox event, severity, delay, participant, and level.",
                    ),
                    AuthorityInput(
                        name="operator authorization",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Granular assignment and escalation permission evidence.",
                    ),
                    AuthorityInput(
                        name="validated AI intake destination metadata",
                        owner="ai.intake",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Approved intent, category, confidence, department, and fallback policy.",
                    ),
                ),
                transaction_mode=TransactionMode.OWNER_MANAGED,
                event_types=(
                    "team_inbox.assignment_changed.v1",
                    "team_inbox.escalated.v1",
                ),
            ),
        ),
        SOTService(
            name="communications.team_inbox_operator_state",
            module="app.services.team_inbox_read_state",
            owns=("operator read cursor", "operator unread projection repair"),
            depends_on=(
                "communications.team_inbox_threads",
                "auth.permission_gate",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_operator_state",
                concerns=(
                    ("operator read cursor", OwnerRole.COMMAND_WRITER),
                    ("operator unread projection repair", OwnerRole.RECONCILER),
                ),
                inputs=(
                    AuthorityInput(
                        name="message chronology",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Inbound occurrence time and stable message/conversation identity.",
                    ),
                    AuthorityInput(
                        name="operator principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Authenticated person UUID and granular Inbox update scope.",
                    ),
                ),
                transaction_mode=TransactionMode.OWNER_MANAGED,
                event_types=("team_inbox.operator_read_state_changed.v1",),
                projections=("per-operator unread conversation projection",),
            ),
        ),
        SOTService(
            name="communications.team_inbox_outbound_intents",
            module="app.services.team_inbox_outbound",
            owns=(
                "transactional outbound communication intent",
                "outbound Inbox message attempt projection",
            ),
            depends_on=(
                "communications.team_inbox_threads",
                "communications.intents",
                "communications.channel_policy",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_outbound_intents",
                concerns=(
                    (
                        "transactional outbound communication intent",
                        OwnerRole.COMMAND_WRITER,
                    ),
                    (
                        "outbound Inbox message attempt projection",
                        OwnerRole.PROJECTION_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="conversation reply target",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Active unresolved conversation, channel, participant, subject, and team sender context.",
                    ),
                    AuthorityInput(
                        name="communication intent lifecycle",
                        owner="communications.intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Transactional intent, Notification outbox identity, eligibility, and queue outcome.",
                    ),
                    AuthorityInput(
                        name="effective channel policy",
                        owner="communications.channel_policy",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Provider-neutral channel and sender eligibility.",
                    ),
                ),
                transaction_mode=TransactionMode.OWNER_MANAGED,
                event_types=("team_inbox.outbound_intent_recorded.v1",),
                projections=("outbound attempt and failed-worklist projection",),
            ),
        ),
        SOTService(
            name="communications.team_inbox_delivery_receipts",
            module="app.services.team_inbox_delivery_receipts",
            owns=("provider delivery receipt reconciliation",),
            depends_on=(
                "communications.team_inbox_observations",
                "communications.team_inbox_outbound_intents",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_delivery_receipts",
                concerns=(
                    (
                        "provider delivery receipt reconciliation",
                        OwnerRole.PROJECTION_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="normalized receipt",
                        owner="communications.team_inbox_observations",
                        kind=AuthorityKind.OBSERVATION,
                        source="Provider message identity, bounded status, observed time, recipient reference, and error codes.",
                    ),
                    AuthorityInput(
                        name="outbound attempt identity",
                        owner="communications.team_inbox_outbound_intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Inbox message and communication intent provider-message linkage.",
                    ),
                ),
                transaction_mode=TransactionMode.PARTICIPANT,
                event_types=("team_inbox.delivery_receipt_reconciled.v1",),
                projections=("timestamp-monotonic Inbox delivery state",),
            ),
        ),
        SOTService(
            name="communications.team_inbox_commands",
            module="app.services.team_inbox_commands",
            owns=("operator conversation and collaboration commands",),
            depends_on=(
                "auth.permission_gate",
                "communications.team_inbox_threads",
                "communications.team_inbox_contact_resolution",
                "communications.team_inbox_routing",
                "communications.team_inbox_outbound_intents",
                "communications.team_inbox_operator_state",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_commands",
                concerns=(
                    (
                        "operator conversation and collaboration commands",
                        OwnerRole.APPLICATION_COORDINATOR,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="authenticated operator command",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="CommandContext, granular permission, typed targets, expected state, and reason.",
                    ),
                    AuthorityInput(
                        name="current conversation state",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Locked conversation, collaboration records, labels, comments, and messages.",
                    ),
                    AuthorityInput(
                        name="contact association decision",
                        owner="communications.team_inbox_contact_resolution",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Reviewed subscriber/reseller/contact-point outcome.",
                    ),
                    AuthorityInput(
                        name="routing transition decision",
                        owner="communications.team_inbox_routing",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Assignment, escalation, and lifecycle eligibility.",
                    ),
                    AuthorityInput(
                        name="outbound intent outcome",
                        owner="communications.team_inbox_outbound_intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Stable queued or suppressed intent and message identifiers.",
                    ),
                    AuthorityInput(
                        name="operator read state",
                        owner="communications.team_inbox_operator_state",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Per-person read cursor and unread cohort.",
                    ),
                ),
                transaction_mode=TransactionMode.COORDINATOR_MANAGED,
            ),
        ),
        SOTService(
            name="communications.team_inbox_widget",
            module="app.services.team_inbox_widget",
            owns=("authenticated visitor message and read-state commands",),
            depends_on=(
                "customer.identity_scope",
                "communications.team_inbox_threads",
                "control.settings_spec",
            ),
            notes=(
                "ADR 0006 temporarily assigns portal live-chat authority to CRM "
                "when comms.chat_session_authority=crm. This native command owner "
                "then fails closed for both new and previously issued widget tokens; "
                "it never mirrors or falls back to a local write."
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_widget",
                concerns=(
                    (
                        "authenticated visitor message and read-state commands",
                        OwnerRole.COMMAND_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="authenticated visitor principal",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Exact Subscriber or reseller principal and bounded signed widget-session identity.",
                    ),
                    AuthorityInput(
                        name="widget conversation identity",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Native chat-widget conversation and message chronology.",
                    ),
                    AuthorityInput(
                        name="live-chat authority selection",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Database-authoritative "
                            "comms.chat_session_authority control; native commands "
                            "are accepted only when the value resolves to selfcare."
                        ),
                    ),
                ),
                transaction_mode=TransactionMode.OWNER_MANAGED,
                event_types=(
                    "team_inbox.widget_message_recorded.v1",
                    "team_inbox.widget_read_state_changed.v1",
                ),
                design_refs=(
                    "docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md",
                    "docs/adr/0006-temporary-crm-chat-authority.md",
                    "docs/runbooks/TEMPORARY_CRM_CHAT_AUTHORITY.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_chat_session.py",
                    "tests/test_team_inbox_widget_native.py",
                    "tests/architecture/test_team_inbox_boundaries.py",
                    "tests/architecture/test_team_inbox_sot_contracts.py",
                ),
            ),
        ),
        SOTService(
            name="communications.team_inbox_projection",
            module="app.services.team_inbox_projection",
            owns=(
                "Inbox list detail metrics response cohort unread and action projection",
            ),
            depends_on=(
                "communications.team_inbox_threads",
                "communications.team_inbox_contact_resolution",
                "communications.team_inbox_routing",
                "communications.team_inbox_delivery_receipts",
                "communications.team_inbox_operator_state",
                "communications.conversation_ticket_handoff",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_projection",
                concerns=(
                    (
                        "Inbox list detail metrics response cohort unread and action projection",
                        OwnerRole.RESOLVER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="conversation records",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Current conversation/message chronology and lifecycle.",
                    ),
                    AuthorityInput(
                        name="contact projection",
                        owner="communications.team_inbox_contact_resolution",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Reviewed link and explicit resolution state.",
                    ),
                    AuthorityInput(
                        name="routing state",
                        owner="communications.team_inbox_routing",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Team, assignee, escalation, priority, mute, and snooze state.",
                    ),
                    AuthorityInput(
                        name="delivery projection",
                        owner="communications.team_inbox_delivery_receipts",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Current monotonic delivery state and bounded failure codes.",
                    ),
                    AuthorityInput(
                        name="unread projection",
                        owner="communications.team_inbox_operator_state",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Per-person read cursor and unread decision.",
                    ),
                    AuthorityInput(
                        name="ticket handoff provenance",
                        owner="communications.conversation_ticket_handoff",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("Ticket origin links issued from Inbox conversations."),
                    ),
                ),
                transaction_mode=TransactionMode.READ_ONLY,
                projections=(
                    "Inbox queue detail metrics response cohorts actions and unread cohorts",
                ),
                test_refs=(
                    "tests/test_team_inbox_sot_completion.py",
                    "tests/test_team_inbox_needs_attention.py",
                    "tests/architecture/test_team_inbox_boundaries.py",
                    "tests/architecture/test_team_inbox_sot_contracts.py",
                ),
            ),
        ),
        SOTService(
            name="communications.team_inbox_maintenance",
            module="app.services.team_inbox_maintenance",
            owns=("scheduled Inbox projection maintenance and repair",),
            depends_on=(
                "ai.intake",
                "communications.team_inbox_threads",
                "communications.team_inbox_outbound_intents",
                "communications.team_inbox_projection",
                "communications.team_inbox_routing",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_maintenance",
                concerns=(
                    (
                        "scheduled Inbox projection maintenance and repair",
                        OwnerRole.RECONCILER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="current Inbox projection",
                        owner="communications.team_inbox_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Failed outbound, stale conversation, and unmaterialized media worklists.",
                    ),
                    AuthorityInput(
                        name="canonical conversation identity",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Current conversation, message, lifecycle, and attachment metadata.",
                    ),
                    AuthorityInput(
                        name="outbound intent state",
                        owner="communications.team_inbox_outbound_intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Retryable failed communication intent and message evidence.",
                    ),
                    AuthorityInput(
                        name="AI intake recovery state",
                        owner="ai.intake",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Bounded classifying or awaiting-follow-up state and configured fallback deadline.",
                    ),
                ),
                transaction_mode=TransactionMode.OWNER_MANAGED,
                event_types=("team_inbox.projection_repaired.v1",),
                projections=("repairable Inbox worklists and media projection",),
            ),
        ),
        SOTService(
            name="communications.team_inbox_realtime",
            module="app.services.team_inbox_realtime",
            owns=("best-effort realtime Inbox projection and rebuild",),
            depends_on=("communications.team_inbox_projection",),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_realtime",
                concerns=(
                    (
                        "best-effort realtime Inbox projection and rebuild",
                        OwnerRole.TRANSPORT,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="current Inbox projection",
                        owner="communications.team_inbox_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="Committed conversation state serialized into the realtime v1 envelope.",
                    ),
                ),
                transaction_mode=TransactionMode.NOT_APPLICABLE,
                projections=("best-effort conversation topic projection",),
            ),
        ),
        SOTService(
            name="communications.team_inbox_smtp_transport",
            module="app.services.team_inbox_smtp_inbound",
            owns=("dedicated SMTP intake process and envelope transport",),
            depends_on=("communications.team_inbox_observations",),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_smtp_transport",
                concerns=(
                    (
                        "dedicated SMTP intake process and envelope transport",
                        OwnerRole.TRANSPORT,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="SMTP envelope and RFC822 bytes",
                        owner="external:customer_mail_server",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Allowed recipient envelope and RFC822 bytes normalized before observation admission.",
                    ),
                ),
                transaction_mode=TransactionMode.NOT_APPLICABLE,
            ),
        ),
        SOTService(
            name="communications.team_inbox_health",
            module="app.services.team_inbox_health",
            owns=("verified SMTP probe delivery projection",),
            depends_on=("communications.team_inbox_threads",),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_health",
                concerns=(
                    (
                        "verified SMTP probe delivery projection",
                        OwnerRole.PROJECTION_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="exact synthetic SMTP message",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Exact runtime-generated Message-ID and bounded probe marker on the committed Inbox message.",
                    ),
                ),
                transaction_mode=TransactionMode.OWNER_MANAGED,
                projections=("verified SMTP delivery health evidence",),
            ),
        ),
        SOTService(
            name="communications.team_inbox_campaigns",
            module="app.services.team_inbox_campaigns",
            owns=("campaign-sourced conversation and message materialization",),
            depends_on=(
                "communications.team_inbox_threads",
                "communications.team_inbox_outbound_intents",
            ),
            contract=_team_inbox_contract(
                service_name="communications.team_inbox_campaigns",
                concerns=(
                    (
                        "campaign-sourced conversation and message materialization",
                        OwnerRole.PROJECTION_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="canonical conversation identity",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Existing channel/subscriber thread or deterministic campaign thread identity.",
                    ),
                    AuthorityInput(
                        name="outbound intent",
                        owner="communications.team_inbox_outbound_intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Stable intent, Notification, channel, content, and delivery state.",
                    ),
                ),
                transaction_mode=TransactionMode.PARTICIPANT,
                event_types=("team_inbox.campaign_message_materialized.v1",),
                projections=("campaign conversation and outbound message projection",),
            ),
        ),
        SOTService(
            name="communications.conversation_ticket_handoff",
            module="app.services.conversation_ticket_handoff",
            owns=(
                "conversation-to-ticket issuance eligibility",
                "native conversation-to-ticket provenance",
            ),
            depends_on=(
                "communications.team_inbox_threads",
                "support.ticket_lifecycle",
                "observability.audit_log",
            ),
            notes=(
                "An agent holding support:ticket:update explicitly issues a "
                "ticket from an active conversation. Ticket identity, state "
                "and official timeline stay owned by "
                "support.ticket_lifecycle; this owner writes only "
                "Ticket.origin_conversation_id, through the keyword-only "
                "provenance argument on the Ticket create command. One "
                "conversation may issue many tickets. Issuance never "
                "transitions the conversation — opening a ticket and "
                "resolving a thread are separate decisions and conversation "
                "status belongs to communications.team_inbox. Replay is "
                "keyed on conversation, actor and title rather than the "
                "transport request id, so a double-submitted form replays "
                "instead of opening a second ticket."
            ),
            contract=_team_inbox_contract(
                service_name="communications.conversation_ticket_handoff",
                concerns=(
                    (
                        "conversation-to-ticket issuance eligibility",
                        OwnerRole.APPLICATION_COORDINATOR,
                    ),
                    (
                        "native conversation-to-ticket provenance",
                        OwnerRole.COMMAND_WRITER,
                    ),
                ),
                inputs=(
                    AuthorityInput(
                        name="canonical conversation state",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Active InboxConversation identity, status, "
                            "channel, resolved subscriber and primary "
                            "service team."
                        ),
                    ),
                    AuthorityInput(
                        name="typed issuance request",
                        owner="communications.conversation_ticket_handoff",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "ConversationTicketIssueCommand with explicit "
                            "actor, permission keys, title, reason and "
                            "derived idempotency key."
                        ),
                    ),
                    AuthorityInput(
                        name="ticket command result",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Ticket created by the canonical Ticket create "
                            "command, including number and status defaults."
                        ),
                    ),
                ),
                transaction_mode=TransactionMode.COORDINATOR_MANAGED,
                projections=("conversation-to-ticket provenance link",),
                design_refs=(
                    "docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_conversation_ticket_handoff.py",
                    "tests/architecture/test_conversation_ticket_handoff_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.services.events.handlers.notification",
        "app.tasks.notifications",
        "app.api.me",
        "app.web.customer.routes",
        "app.web.admin.notifications",
        "app.web.admin.inbox",
        "app.services.team_inbox_*",
        "app.services.conversation_ticket_handoff",
        "app.web.admin.surveys",
        "app.web.public.surveys",
        "app.api.comms",
        "app.services.events.handlers.surveys",
    ),
    rule="Domain services request communication outcomes; channel choice, "
    "notification rows, and recipient read state stay inside "
    "communication services. Survey adapters delegate lifecycle, invitation "
    "and response writes to communications.surveys. Admin inbox mutation "
    "routes delegate to the committed team-inbox command boundary.",
)
