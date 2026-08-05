"""Canonical SOT declarations for the ai_advisory domain."""

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
    owner_command_boundary_error_codes,
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="ai_advisory",
    services=(
        SOTService(
            name="ai.gateway",
            module="app.services.ai.gateway",
            owns=(
                "LLM provider transport",
                "provider circuit-breaker and endpoint health",
            ),
            notes=(
                "The same species as a payment gateway: an external system "
                "Sub calls. Holds no business rule and owns no domain "
                "state. Credentials resolve through secrets (OpenBao), "
                "never settings rows. See docs/designs/AI_SOT.md."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="LLM provider transport",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "assembled advisory prompt",
                            "resolved provider credential",
                        ),
                    ),
                    ConcernContract(
                        name="provider circuit-breaker and endpoint health",
                        role=OwnerRole.RESOLVER,
                        input_names=("observed provider response",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="assembled advisory prompt",
                        owner="ai.generation",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "System prompt plus either the caller's owned "
                            "advisory projection or ai.intake's bounded "
                            "redacted classification projection."
                        ),
                    ),
                    AuthorityInput(
                        name="resolved provider credential",
                        owner="secrets.reference_store",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="OpenBao-backed provider API key.",
                    ),
                    AuthorityInput(
                        name="observed provider response",
                        owner="external:llm_provider",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="HTTP status, latency and token counts.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "Holds no transaction and writes no row. Circuit "
                        "state is process-local."
                    ),
                    locking="None.",
                    idempotency=(
                        "None: a repeated generation is a new provider "
                        "call and new spend."
                    ),
                    retries=(
                        "Falls back to the secondary endpoint, then fails "
                        "closed. An open circuit refuses before calling."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ai.gateway.disabled",
                        "ai.gateway.circuit_open",
                        "ai.gateway.provider_unavailable",
                    ),
                    mapping_owner="app.services.ai.engine",
                    retryable_codes=("ai.gateway.provider_unavailable",),
                    fail_closed_on=(
                        "ai.gateway.disabled",
                        "ai.gateway.circuit_open",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="ai.gateway",
                    verification=(
                        "tests/test_ai_engine.py exercises the transport "
                        "through the advisory port."
                    ),
                ),
                steward="customer experience platform",
                design_refs=("docs/designs/AI_SOT.md",),
                test_refs=(
                    "tests/test_ai_engine.py",
                    "tests/architecture/test_ai_boundaries.py",
                ),
            ),
        ),
        SOTService(
            name="ai.voice_transcription",
            module="app.services.ai.voice_transcription",
            owns=("zero-retention voice transcription provider transport",),
            depends_on=("auth.permission_gate", "secrets.reference_store"),
            notes=(
                "Transports an authenticated agent's request-scoped audio "
                "to one approved transcription processor. It writes no "
                "audio, transcript, conversation, message, or insight row; "
                "the reviewed transcript becomes Inbox content only through "
                "communications.team_inbox_commands."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="zero-retention voice transcription provider transport",
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "authenticated bounded audio upload",
                            "resolved transcription credential",
                            "observed transcription response",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated bounded audio upload",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Explicit press-and-hold agent recording, "
                            "allowlisted context and media signature, "
                            "25 MiB upload bound, rate limit and one "
                            "in-flight request per agent."
                        ),
                    ),
                    AuthorityInput(
                        name="resolved transcription credential",
                        owner="secrets.reference_store",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="OpenBao-backed provider API-key reference.",
                    ),
                    AuthorityInput(
                        name="observed transcription response",
                        owner="external:voice_transcription_provider",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Provider HTTP status and transcript returned "
                            "without retaining the source audio."
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.NOT_APPLICABLE,
                    boundary=(
                        "Holds no database transaction and writes no row; "
                        "audio exists only for the request lifetime."
                    ),
                    locking="One process-local active slot per agent.",
                    idempotency=(
                        "None: every explicit recording is a new provider call."
                    ),
                    retries=(
                        "At most three configured retries for network "
                        "failure, HTTP 408/409/425/429, or 5xx only."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "ai.voice_transcription.disabled",
                        "ai.voice_transcription.not_configured",
                        "ai.voice_transcription.invalid_context",
                        "ai.voice_transcription.empty_audio",
                        "ai.voice_transcription.audio_too_large",
                        "ai.voice_transcription.unsupported_audio",
                        "ai.voice_transcription.invalid_audio_signature",
                        "ai.voice_transcription.provider_unavailable",
                        "ai.voice_transcription.provider_rejected",
                    ),
                    mapping_owner="app.web.admin.inbox",
                    retryable_codes=("ai.voice_transcription.provider_unavailable",),
                    fail_closed_on=(
                        "ai.voice_transcription.disabled",
                        "ai.voice_transcription.not_configured",
                        "ai.voice_transcription.invalid_context",
                        "ai.voice_transcription.invalid_audio_signature",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="ai.voice_transcription",
                    verification=(
                        "Focused voice transport and AI architecture tests "
                        "prove zero domain writes and safe validation."
                    ),
                ),
                steward="customer experience platform",
                design_refs=(
                    "docs/designs/VOICE_TRANSCRIPTION_DATA_PROTECTION.md",
                    "docs/designs/AI_SOT.md",
                    "docs/runbooks/VOICE_TRANSCRIPTION.md",
                ),
                test_refs=(
                    "tests/test_admin_inbox_implemented_features.py",
                    "tests/architecture/test_ai_boundaries.py",
                ),
            ),
        ),
        SOTService(
            name="ai.intake",
            module="app.services.ai_intake",
            owns=(
                "AI conversational intake configuration lifecycle",
                "customer-message intake eligibility policy",
                "bounded customer-message intent classification",
                "customer contact-data cleaning eligibility policy",
            ),
            depends_on=(
                "ai.gateway",
                "communications.team_inbox_observations",
                "communications.team_inbox_threads",
                "operations.service_team_lifecycle",
                "events.dispatcher",
            ),
            notes=(
                "AiIntakeConfig is the only runtime policy source. The owner "
                "classifies WhatsApp, Facebook Messenger, and Instagram only, "
                "returns validated destination-team metadata and one controlled "
                "follow-up candidate; the Team Inbox outbound owner alone delivers "
                "it. Intake never writes queue position or chooses an agent. "
                "The reserved data-cleaning gate compares exact configured and "
                "conversation team UUIDs and performs no customer-data access. Classification "
                "is a read-only participant in the Inbox coordinator transaction; "
                "configuration writes enter execute_owner_command once."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="AI conversational intake configuration lifecycle",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "reviewed AI intake configuration command",
                            "active fallback and mapped service teams",
                        ),
                        canonical_writer="ai.intake",
                    ),
                    ConcernContract(
                        name="customer-message intake eligibility policy",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "enabled matching AI intake configuration",
                            "normalized inbound conversation state",
                            "channel AI-routing permission",
                        ),
                    ),
                    ConcernContract(
                        name="bounded customer-message intent classification",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "enabled matching AI intake configuration",
                            "bounded redacted inbound message projection",
                            "observed provider classification response",
                        ),
                    ),
                    ConcernContract(
                        name="customer contact-data cleaning eligibility policy",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "enabled matching AI intake configuration",
                            "normalized inbound conversation state",
                            "channel AI-routing permission",
                            "active fallback and mapped service teams",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="reviewed AI intake configuration command",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Authenticated system-settings writer and typed policy.",
                    ),
                    AuthorityInput(
                        name="active fallback and mapped service teams",
                        owner="operations.service_team_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Active ServiceTeam identifiers referenced by policy.",
                    ),
                    AuthorityInput(
                        name="enabled matching AI intake configuration",
                        owner="ai.intake",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Most-specific AiIntakeConfig for provider/account/channel scope.",
                    ),
                    AuthorityInput(
                        name="normalized inbound conversation state",
                        owner="communications.team_inbox_threads",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Conversation lifecycle, ownership, tags, and bounded recent messages.",
                    ),
                    AuthorityInput(
                        name="channel AI-routing permission",
                        owner="communications.team_inbox_routing",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="Effective TeamInboxChannelRoute allow_ai_routing gate.",
                    ),
                    AuthorityInput(
                        name="bounded redacted inbound message projection",
                        owner="communications.team_inbox_observations",
                        kind=AuthorityKind.OBSERVATION,
                        source="Latest normalized customer message plus at most three relevant messages.",
                    ),
                    AuthorityInput(
                        name="observed provider classification response",
                        owner="external:llm_provider",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source="Strict JSON candidate returned through ai.gateway.",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Configuration mutation enters execute_owner_command once. "
                        "Classification is read-only and its typed result is persisted "
                        "by the Inbox coordinator with the inbound message."
                    ),
                    locking=(
                        "Configuration upsert locks the scope row; classification writes no row. "
                        "The Inbox coordinator serializes channel/thread intake with a transaction "
                        "advisory lock and locks an existing conversation row; recovery uses the "
                        "same conversation row lock."
                    ),
                    idempotency=(
                        "Configuration uses command evidence; provider message deduplication "
                        "precedes classification so one inbound fact produces at most one attempt. "
                        "Clarification delivery uses an inbound-message-derived communication-intent "
                        "dedupe key."
                    ),
                    retries="No synchronous retry beyond ai.gateway's configured fallback provider.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        *owner_command_boundary_error_codes("ai.intake"),
                        "ai.intake.invalid_configuration",
                        "ai.intake.invalid_model_output",
                        "ai.intake.gateway_unavailable",
                    ),
                    mapping_owner="Team Inbox processing and AI operations API adapters",
                    fail_closed_on=(
                        "invalid or missing configuration",
                        "invalid provider output",
                        "provider unavailability",
                    ),
                ),
                events=EventContract(
                    event_types=("ai.intake_config_updated",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility="Version 1 carries only bounded configuration-change evidence.",
                    replay="Configuration remains authoritative in AiIntakeConfig.",
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner="parked ai_operations CRUD with no runtime reader",
                    new_owner="ai.intake",
                    verification="Focused classification, routing, deduplication, and architecture tests.",
                    cutover_gate="All conversational channels call the shared owner after normalization.",
                    fallback_retirement="Untyped ai_operations AiIntakeConfig writers are removed.",
                ),
                steward="customer experience platform",
                design_refs=(
                    "docs/designs/AI_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_ai_intake.py",
                    "tests/test_team_inbox_ai_intake_flow.py",
                    "tests/architecture/test_ai_boundaries.py",
                ),
            ),
        ),
        SOTService(
            name="ai.insights",
            module="app.services.ai_operations",
            owns=(
                "AI insight rows",
                "insight lifecycle: create, acknowledge, expire",
            ),
            notes=(
                "The canonical writer of AIInsight. Generated insights "
                "land here and nowhere else. AI is advisory: it never "
                "mutates domain state — acting on a recommendation means "
                "calling the domain's declared owner. Customer-facing "
                "classification is separately owned by ai.intake."
            ),
        ),
        SOTService(
            name="ai.generation",
            module="app.services.ai.engine",
            owns=(
                "the advisory generation path",
                "advisor lookup, token budget, and prompt assembly",
                "input-sensitivity redaction before a prompt leaves",
            ),
            depends_on=("ai.insights", "ai.gateway"),
            notes=(
                "advise() takes the CALLER's owned projection and never "
                "queries a domain model, so the AI boundary holds by "
                "construction rather than by vigilance — this is why "
                "personas were removed from the design. It persists only "
                "through ai.insights. Behind the default-OFF ai.generation "
                "control. Called on demand from the admin report surface."
            ),
        ),
    ),
    entrypoints=(
        "app.api.ai_operations",
        "app.tasks.ai_operations",
        "app.web.admin.reports",
        "app.web.admin.inbox",
        "app.services.team_inbox_channel_receive",
    ),
    rule="Advisory AI advises ON an owned projection and never re-derives one: the "
    "caller hands in what it already computes, so the boundary holds "
    "by construction. AI observes, derives, and recommends; it never "
    "decides domain state. Insight consequences are requested from the "
    "owning domain service, which applies its own guards, events, and "
    "audit. ai.intake is the separate bounded customer-message classifier; "
    "it may select a destination service team but never an agent or queue position.",
)
