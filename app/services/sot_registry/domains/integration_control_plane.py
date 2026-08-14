"""Canonical SOT declarations for the integration_control_plane domain."""

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
    domain="integration_control_plane",
    setting_domains=("imports",),
    services=(
        SOTService(
            name="integration.registry",
            module="app.services.integrations.registry",
            owns=(
                "deployed integration connector catalogue",
                "current connector capability metadata",
            ),
            notes=(
                "The live manifest, installation, capability, and isolation "
                "contract is docs/designs/INTEGRATION_PLATFORM_SOT.md. "
                "Definitions are deployed code artifacts and the manifest "
                "registry is the executable connector contract."
            ),
        ),
        SOTService(
            name="integration.oauth_tokens",
            module="app.services.meta_oauth",
            owns=(
                "Meta OAuth refresh candidate selection",
                "Meta OAuth access-token refresh persistence",
                "OAuth token expiry health projection",
            ),
            depends_on=(
                "control.settings_spec",
                "secrets.reference_store",
                "events.dispatcher",
            ),
            notes=(
                "The scheduler is a thin adapter. This owner permits only the "
                "Meta long-lived user-token exchange, resolves the client secret "
                "from an approved reference, writes encrypted OAuthToken state, "
                "and records only redacted failure and event evidence."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="Meta OAuth refresh candidate selection",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical OAuth token state",
                            "Meta refresh protocol",
                        ),
                    ),
                    ConcernContract(
                        name="Meta OAuth access-token refresh persistence",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "canonical OAuth token state",
                            "Meta OAuth client configuration",
                            "approved Meta client secret reference",
                            "Meta token exchange observation",
                            "Meta refresh protocol",
                        ),
                        canonical_writer="integration.oauth_tokens",
                    ),
                    ConcernContract(
                        name="OAuth token expiry health projection",
                        role=OwnerRole.RESOLVER,
                        input_names=("canonical OAuth token state",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical OAuth token state",
                        owner="integration.oauth_tokens",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Encrypted OAuthToken access token, token class, active "
                            "state, expiry, refresh time, and sanitized refresh error"
                        ),
                    ),
                    AuthorityInput(
                        name="Meta OAuth client configuration",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "DB-authoritative comms.meta_app_id and "
                            "comms.meta_graph_api_version settings"
                        ),
                    ),
                    AuthorityInput(
                        name="approved Meta client secret reference",
                        owner="secrets.reference_store",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "DB-authoritative comms.meta_app_secret OpenBao or "
                            "approved local secret reference"
                        ),
                    ),
                    AuthorityInput(
                        name="Meta token exchange observation",
                        owner="external:meta_graph_api",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "Meta OAuth access-token exchange status, new token, "
                            "token type, and expiry interval"
                        ),
                    ),
                    AuthorityInput(
                        name="Meta refresh protocol",
                        owner="integration.oauth_tokens",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Closed fb_exchange_token grant and user-token class "
                            "allowlist plus compare-before-write retry semantics"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "The Celery adapter opens a transaction-free session; one "
                        "typed refresh command locks one OAuthToken and the owner "
                        "commits token, expiry, sanitized failure, and event evidence."
                    ),
                    locking=(
                        "Each refresh locks one OAuthToken row and compares its current "
                        "expiry with the immutable candidate expiry before exchange."
                    ),
                    idempotency=(
                        "Task request, token id, and observed expiry identify an "
                        "attempt; changed expiry skips stale retries without another "
                        "provider call."
                    ),
                    retries=(
                        "Provider unavailability is safe to retry as a new bounded "
                        "attempt; invalid configuration, token class, storage class, "
                        "or provider rejection remains durable sanitized evidence."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "integration.oauth_tokens.token_not_found",
                        "integration.oauth_tokens.configuration_missing",
                        "integration.oauth_tokens.secret_reference_required",
                        "integration.oauth_tokens.secret_resolution_failed",
                        "integration.oauth_tokens.invalid_graph_version",
                        "integration.oauth_tokens.invalid_grant_type",
                        "integration.oauth_tokens.token_inactive",
                        "integration.oauth_tokens.token_class_not_permitted",
                        "integration.oauth_tokens.token_missing",
                        "integration.oauth_tokens.token_storage_not_permitted",
                        "integration.oauth_tokens.provider_unavailable",
                        "integration.oauth_tokens.provider_rejected",
                        "integration.oauth_tokens.provider_response_invalid",
                        *owner_command_boundary_error_codes("integration.oauth_tokens"),
                    ),
                    mapping_owner="app.tasks.oauth Celery transport adapter",
                    retryable_codes=("integration.oauth_tokens.provider_unavailable",),
                    fail_closed_on=(
                        "plaintext or unresolved Meta client secret",
                        "unsupported grant or token class",
                        "secret-referenced token without a writable secret owner",
                        "stale token expiry or ambiguous provider response",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "oauth_token.refreshed",
                        "oauth_token.refresh_failed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 identifies the OAuthToken, connector, token "
                        "class, grant, expiry or failure code, and command evidence; "
                        "it never carries access tokens or client secrets."
                    ),
                    replay=(
                        "Encrypted OAuthToken state plus redacted EventStore evidence "
                        "reconstructs current expiry, last success, and last failure."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.tasks.oauth direct ORM writes and unresolved "
                        "app.services.meta_oauth import"
                    ),
                    new_owner="integration.oauth_tokens",
                    verification=(
                        "Focused owner, secret-redaction, provider transport, "
                        "candidate-class, task-adapter, and manifest tests."
                    ),
                    cutover_gate=(
                        "The task imports this owner, contains no ORM or transaction "
                        "writes, and targeted refresh tests pass."
                    ),
                    fallback_retirement=(
                        "Task-owned token selection, provider exchange, commit, "
                        "rollback, and raw exception persistence are removed."
                    ),
                ),
                steward="platform integrations",
                design_refs=(
                    "docs/CODING_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_meta_oauth.py",
                    "tests/test_oauth_tasks.py",
                ),
            ),
        ),
        SOTService(
            name="integration.installations",
            module="app.services.integrations.installations",
            owns=(
                "version-pinned integration installation lifecycle",
                "explicit integration manifest adoption",
                "immutable integration configuration revisions",
                "integration capability grants and bindings",
                "Meta social installation configuration",
                "pre-activation integration webhook verification",
            ),
            depends_on=("integration.registry", "secrets.reference_store"),
            notes=(
                "This is the sole owner of integration_installations, "
                "integration_config_revisions, and integration_capability_"
                "bindings. CRM, ERP, WhatsApp, payment, and webhook callers "
                "resolve configuration only through versioned bindings."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="version-pinned integration installation lifecycle",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "deployed connector manifest",
                            "integration installation protocol",
                            "canonical integration installation aggregate",
                        ),
                        canonical_writer="integration.installations",
                    ),
                    ConcernContract(
                        name="explicit integration manifest adoption",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "deployed connector manifest",
                            "integration installation protocol",
                            "canonical integration installation aggregate",
                        ),
                        canonical_writer="integration.installations",
                    ),
                    ConcernContract(
                        name="immutable integration configuration revisions",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "deployed connector manifest",
                            "approved integration secret references",
                            "integration installation protocol",
                            "canonical integration installation aggregate",
                        ),
                        canonical_writer="integration.installations",
                    ),
                    ConcernContract(
                        name="integration capability grants and bindings",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "deployed connector manifest",
                            "integration installation protocol",
                            "canonical integration installation aggregate",
                        ),
                        canonical_writer="integration.installations",
                    ),
                    ConcernContract(
                        name="Meta social installation configuration",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "deployed connector manifest",
                            "approved integration secret references",
                            "integration installation protocol",
                            "canonical integration installation aggregate",
                        ),
                        canonical_writer="integration.installations",
                    ),
                    ConcernContract(
                        name="pre-activation integration webhook verification",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "approved integration secret references",
                            "integration installation protocol",
                            "canonical integration installation aggregate",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="deployed connector manifest",
                        owner="integration.registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "registered connector version, digest, runtime, config "
                            "schema, secret declarations, and capabilities"
                        ),
                    ),
                    AuthorityInput(
                        name="approved integration secret references",
                        owner="secrets.reference_store",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "validated OpenBao references; secret material is never "
                            "persisted in configuration revisions"
                        ),
                    ),
                    AuthorityInput(
                        name="integration installation protocol",
                        owner="integration.installations",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "draft, validated, enabled, quarantined, and retired "
                            "transition rules plus immutable revision semantics"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical integration installation aggregate",
                        owner="integration.installations",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "IntegrationInstallation, IntegrationConfigRevision, and "
                            "IntegrationCapabilityBinding rows"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Installation mutations stage one aggregate and the named "
                        "installation commit boundary completes the unit of work."
                    ),
                    locking=(
                        "Lifecycle transitions resolve one installation; manifest "
                        "adoption locks that installation row and immutable "
                        "configuration revisions serialize by installation revision."
                    ),
                    idempotency=(
                        "Configuration digests replay to an existing immutable revision; "
                        "capability synchronization converges by capability id; manifest "
                        "adoption converges by exact target version and digest."
                    ),
                    retries=(
                        "Manifest, secret-reference, or lifecycle violations are "
                        "terminal; database conflicts retry the whole mutation."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "integration.installations.not_found",
                        "integration.installations.invalid_manifest",
                        "integration.installations.invalid_configuration",
                        "integration.installations.invalid_secret_reference",
                        "integration.installations.invalid_transition",
                        "integration.installations.manifest_adoption_incompatible",
                        "integration.installations.manifest_adoption_scope_invalid",
                        "integration.installations.capability_provisioning_scope_invalid",
                        "integration.installations.invalid_capability",
                        "integration.installations.stale_capability_binding",
                        "integration.installations.connection_validation_failed",
                        "integration.installations.stale_manifest_pin",
                        "integration.installations.target_manifest_not_deployed",
                        "integration.installations.invalid_command_context",
                        "integration.installations.command_contract_violation",
                        "integration.installations.nested_owner_command",
                        "integration.installations.active_caller_transaction",
                        "integration.installations.nested_transaction_completion",
                        "integration.installations.meta_configuration_ambiguous",
                        "integration.installations.meta_configuration_invalid",
                        "integration.installations.meta_configuration_scope_invalid",
                        "integration.installations.whatsapp_webhook_not_configured",
                        "integration.installations.whatsapp_webhook_configuration_ambiguous",
                        "integration.installations.whatsapp_webhook_installation_not_ready",
                        "integration.installations.whatsapp_webhook_configuration_invalid",
                        "integration.installations.whatsapp_webhook_secret_reference_missing",
                        "integration.installations.whatsapp_webhook_secret_unavailable",
                    ),
                    mapping_owner=(
                        "app.api.integrations and integration admin web adapters"
                    ),
                    fail_closed_on=(
                        "missing deployed connector version or digest",
                        "unreviewed, stale, or incompatible manifest adoption",
                        "undeclared or materialized secret value",
                        "ambiguous enabled default capability",
                        "retired or quarantined lifecycle mismatch",
                        "ambiguous, invalid, or unavailable pre-activation webhook verification input",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "integration.installation.lifecycle.v1",
                        "integration.installation.manifest_adopted",
                        "integration.installation.capability_provisioned",
                        "integration.installation.meta_social_configured",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 installation evidence is additive and identifies "
                        "the installation, manifest pin, revision, capability, state, "
                        "and actor without secret material."
                    ),
                    replay=(
                        "Canonical installation, immutable revision, validation, and "
                        "capability-binding rows rebuild current installation state."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "connector configuration, provider, hook, and integration-job "
                        "records with independent runtime selection"
                    ),
                    new_owner="integration.installations",
                    verification=(
                        "Manifest pin, immutable revision, secret reference, lifecycle, "
                        "explicit adoption, deployment readiness, graceful gateway "
                        "availability, capability, API, and migration cutover tests."
                    ),
                    cutover_gate=(
                        "Every enabled installation pin resolves to a current or bounded "
                        "historical deployed definition before service replacement; "
                        "operators explicitly adopt a reviewed current version/digest."
                    ),
                    fallback_retirement=(
                        "Legacy connector configs, hooks, provider secret columns, and "
                        "arbitrary registration paths are removed by revision 380; a "
                        "historical definition is removed only after the deployment "
                        "readiness report proves no enabled installation pins it."
                    ),
                ),
                steward="platform integrations",
                design_refs=(
                    "docs/designs/INTEGRATION_PLATFORM_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_integration_installations.py",
                    "tests/test_integration_installation_api.py",
                    "tests/test_integration_meta_social.py",
                    "tests/test_team_inbox_whatsapp_webhook.py",
                    "tests/test_integration_manifest_deployment_gate.py",
                    "tests/architecture/test_integration_platform_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="integration.runtime",
            module="app.services.integrations.runtime_execution",
            owns=(
                "version-pinned connector runner selection",
                "connector operation envelope construction",
                "bounded secret materialization for connector execution",
            ),
            depends_on=(
                "integration.registry",
                "integration.installations",
                "secrets.reference_store",
            ),
            notes=(
                "Runtime code selects an explicitly registered runner and "
                "passes it a pinned envelope. Runners receive no Sub "
                "database session and return observations or receipts; "
                "domain owners decide every consequence."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=concern,
                        role=role,
                        input_names=(
                            "deployed connector runtime definition",
                            "enabled version-pinned capability binding",
                            "bounded integration secret materialization",
                        ),
                    )
                    for concern, role in (
                        (
                            "version-pinned connector runner selection",
                            OwnerRole.RESOLVER,
                        ),
                        (
                            "connector operation envelope construction",
                            OwnerRole.POLICY,
                        ),
                        (
                            "bounded secret materialization for connector execution",
                            OwnerRole.POLICY,
                        ),
                    )
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="deployed connector runtime definition",
                        owner="integration.registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "registered connector version, digest, runtime type, and "
                            "operation-capability declaration"
                        ),
                    ),
                    AuthorityInput(
                        name="enabled version-pinned capability binding",
                        owner="integration.installations",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "enabled installation, current immutable config revision, "
                            "and enabled capability binding"
                        ),
                    ),
                    AuthorityInput(
                        name="bounded integration secret materialization",
                        owner="secrets.reference_store",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "declared secret references resolved only into the runtime "
                            "execution context and never returned or persisted"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Runtime construction reads pinned installation state and "
                        "returns an isolated executor; it never writes Sub domain state."
                    ),
                    locking=(
                        "No write lock is taken; manifest and installation pins fail "
                        "closed if deployment state differs during construction."
                    ),
                    idempotency=(
                        "The same binding, manifest pin, config revision, operation, "
                        "and input produce the same execution envelope."
                    ),
                    retries=(
                        "Pin and capability mismatches are terminal; connector retry "
                        "receipts are interpreted only by the calling domain owner."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "integration.runtime.binding_not_found",
                        "integration.runtime.installation_disabled",
                        "integration.runtime.configuration_missing",
                        "integration.runtime.version_pin_mismatch",
                        "integration.runtime.manifest_digest_mismatch",
                        "integration.runtime.capability_not_declared",
                        "integration.runtime.secret_resolution_failed",
                    ),
                    mapping_owner="calling integration or domain command owner",
                    fail_closed_on=(
                        "missing or disabled binding",
                        "version or manifest digest drift",
                        "undeclared capability or unresolved secret reference",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "provider-specific clients selecting configuration and "
                        "materializing credentials independently"
                    ),
                    new_owner="integration.runtime",
                    verification=(
                        "Manifest pin, capability, secret isolation, runner selection, "
                        "and connector persistence-boundary tests."
                    ),
                    cutover_gate=(
                        "CRM, ERP, WhatsApp, payment, and webhook execution enters only "
                        "through a registered version-pinned runtime binding."
                    ),
                    fallback_retirement=(
                        "Provider-specific runtime selection and persisted secret-value "
                        "fallbacks are removed."
                    ),
                ),
                steward="platform integrations",
                design_refs=(
                    "docs/designs/INTEGRATION_PLATFORM_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_integration_manifest_registry.py",
                    "tests/test_integration_installations.py",
                    "tests/architecture/test_integration_platform_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="integration.delivery",
            module="app.services.integrations.delivery",
            owns=(
                "integration event subscription projection",
                "deduplicated integration delivery lifecycle",
                "outbound capability delivery evidence",
            ),
            depends_on=(
                "events.store",
                "integration.installations",
                "integration.runtime",
            ),
            notes=(
                "Every outbound endpoint is an installation-bound typed "
                "capability. Delivery identity, retry, replay, and terminal "
                "failure state have one canonical writer."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="integration event subscription projection",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "canonical domain event envelope",
                            "enabled outbound capability binding",
                            "integration delivery protocol",
                        ),
                        canonical_writer="integration.delivery",
                    ),
                    ConcernContract(
                        name="deduplicated integration delivery lifecycle",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "canonical domain event envelope",
                            "enabled outbound capability binding",
                            "connector delivery receipt",
                            "integration delivery protocol",
                        ),
                        canonical_writer="integration.delivery",
                    ),
                    ConcernContract(
                        name="outbound capability delivery evidence",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "canonical domain event envelope",
                            "enabled outbound capability binding",
                            "connector delivery receipt",
                        ),
                        canonical_writer="integration.delivery",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical domain event envelope",
                        owner="events.store",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "versioned EventStore identity, type, actor, subject, and "
                            "payload selected by an enabled event subscription"
                        ),
                    ),
                    AuthorityInput(
                        name="enabled outbound capability binding",
                        owner="integration.installations",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "enabled events.deliver.v1 capability binding, installation, "
                            "subscription filter, and payload policy"
                        ),
                    ),
                    AuthorityInput(
                        name="connector delivery receipt",
                        owner="integration.runtime",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "typed connector operation status, external receipt, error "
                            "code, response status, and retry-after observation"
                        ),
                    ),
                    AuthorityInput(
                        name="integration delivery protocol",
                        owner="integration.delivery",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "source-event, destination, and payload digest identity plus "
                            "pending, leased, delivered, retry, reconciliation, and "
                            "dead-letter transition rules"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Subscription changes, delivery creation, execution evidence, "
                        "and authorized replay each complete as one delivery-owned unit."
                    ),
                    locking=(
                        "Unique source-event/destination identity and worker leasing "
                        "serialize delivery creation and terminal transitions."
                    ),
                    idempotency=(
                        "Source event, capability destination, and payload digest map to "
                        "one IntegrationDelivery; terminal replay returns that evidence."
                    ),
                    retries=(
                        "Typed runtime receipts drive bounded backoff, dead-letter, or "
                        "reconciliation-required state; operators replay only eligible rows."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "integration.delivery.binding_not_found",
                        "integration.delivery.capability_mismatch",
                        "integration.delivery.binding_disabled",
                        "integration.delivery.event_type_required",
                        "integration.delivery.subscription_not_found",
                        "integration.delivery.delivery_not_found",
                        "integration.delivery.not_replayable",
                        "integration.delivery.invalid_command_context",
                        "integration.delivery.command_contract_violation",
                        "integration.delivery.nested_owner_command",
                        "integration.delivery.active_caller_transaction",
                        "integration.delivery.nested_transaction_completion",
                    ),
                    mapping_owner=(
                        "integration delivery task and integration admin adapters"
                    ),
                    retryable_codes=("integration.delivery.binding_disabled",),
                    fail_closed_on=(
                        "missing or disabled capability binding",
                        "empty event type or ambiguous delivery identity",
                        "unauthorized replay or unsupported connector receipt",
                    ),
                ),
                events=EventContract(
                    event_types=("domain_event.v1",),
                    schema_version=1,
                    delivery_owner="integration.delivery",
                    compatibility=(
                        "Delivery consumes the additive domain-event envelope and applies "
                        "the subscription payload policy without changing domain meaning."
                    ),
                    replay=(
                        "EventStore plus subscription and IntegrationDelivery evidence "
                        "rebuilds missing pending deliveries idempotently."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="integration event subscription projection",
                        input_names=(
                            "canonical domain event envelope",
                            "enabled outbound capability binding",
                            "integration delivery protocol",
                        ),
                        writer="integration.delivery",
                        freshness=(
                            "Subscriptions change synchronously with the selected event set."
                        ),
                        stale_behavior=(
                            "Disabled or absent subscriptions create no delivery; existing "
                            "delivery evidence remains immutable."
                        ),
                        drift_signal=(
                            "An enabled binding's selected event set differs from its "
                            "persisted enabled subscriptions."
                        ),
                        rebuild_operation=(
                            "Synchronize the selected event set for the capability binding."
                        ),
                        repair_owner="integration.delivery",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "integration hooks, webhook delivery rows, provider-specific "
                        "webhook jobs, and independent retry paths"
                    ),
                    new_owner="integration.delivery",
                    verification=(
                        "Subscription, deduplication, typed runtime, retry, replay, task, "
                        "and legacy-cutover tests."
                    ),
                    cutover_gate=(
                        "Every outbound webhook is an enabled events.deliver.v1 binding "
                        "and legacy hook/delivery paths are absent."
                    ),
                    fallback_retirement=(
                        "Legacy webhook, integration-hook, and CRM webhook-delivery models, "
                        "services, tasks, and routes are removed."
                    ),
                ),
                steward="platform integrations",
                design_refs=(
                    "docs/designs/INTEGRATION_PLATFORM_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_integration_delivery.py",
                    "tests/architecture/test_integration_platform_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="integration.inbox",
            module="app.services.integrations.inbox",
            owns=(
                "verified provider event receipt identity",
                "integration inbox deduplication lifecycle",
                "inbound consequence processing evidence",
            ),
            depends_on=("integration.installations", "integration.runtime"),
            notes=(
                "Provider-specific routes verify signatures before writing "
                "a receipt. The inbox records facts and processing state; "
                "the Team Inbox, financial, and other domain owners alone "
                "decide and persist consequences."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="verified provider event receipt identity",
                        role=OwnerRole.OBSERVATION_COLLECTOR,
                        input_names=(
                            "verified external provider event",
                            "enabled inbound capability binding",
                            "integration inbox protocol",
                        ),
                        canonical_writer="integration.inbox",
                    ),
                    ConcernContract(
                        name="integration inbox deduplication lifecycle",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "verified external provider event",
                            "enabled inbound capability binding",
                            "integration inbox protocol",
                        ),
                        canonical_writer="integration.inbox",
                    ),
                    ConcernContract(
                        name="inbound consequence processing evidence",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "canonical domain consequence result",
                            "integration inbox protocol",
                        ),
                        canonical_writer="integration.inbox",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="verified external provider event",
                        owner="external:integration_provider",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "provider event id, verified signature context, event type, "
                            "normalized headers, payload, and payload digest"
                        ),
                    ),
                    AuthorityInput(
                        name="enabled inbound capability binding",
                        owner="integration.installations",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "enabled installation capability binding selected by the "
                            "provider-specific signature-verifying adapter"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical domain consequence result",
                        owner="integration.runtime",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "typed result returned after the named Sub domain owner "
                            "accepts, rejects, or reconciles the verified fact"
                        ),
                    ),
                    AuthorityInput(
                        name="integration inbox protocol",
                        owner="integration.inbox",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "provider-event identity, payload-collision quarantine, claim, "
                            "processed, retryable, dead-letter, and replay rules"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "The inbox owner commits the verified receipt before domain work, "
                        "then atomically commits successful consequence evidence or rolls "
                        "back partial domain writes before recording retry evidence."
                    ),
                    locking=(
                        "Capability/provider-event uniqueness deduplicates receipt; claim "
                        "and replay operate on the canonical receipt aggregate."
                    ),
                    idempotency=(
                        "The same binding and provider event id with the same payload digest "
                        "returns the existing receipt without a second consequence."
                    ),
                    retries=(
                        "Identity collisions quarantine the installation; processing "
                        "failures become bounded retryable or dead-letter evidence."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "integration.inbox.receipt_not_found",
                        "integration.inbox.invalid_state",
                        "integration.inbox.binding_not_found",
                        "integration.inbox.provider_event_id_required",
                        "integration.inbox.provider_event_identity_collision",
                        "integration.inbox.dead_letter_requires_replay",
                        "integration.inbox.not_replayable",
                        "integration.inbox.invalid_command_context",
                        "integration.inbox.command_contract_violation",
                        "integration.inbox.nested_owner_command",
                        "integration.inbox.active_caller_transaction",
                        "integration.inbox.nested_transaction_completion",
                    ),
                    mapping_owner=(
                        "provider webhook, integration task, and integration admin adapters"
                    ),
                    retryable_codes=("integration.inbox.invalid_state",),
                    fail_closed_on=(
                        "unverified provider request",
                        "missing capability binding or provider identity",
                        "same provider identity with a different payload digest",
                        "unauthorized dead-letter replay",
                    ),
                ),
                events=EventContract(
                    event_types=("provider_event.v1",),
                    schema_version=1,
                    delivery_owner="integration.inbox",
                    compatibility=(
                        "Version 1 receipt identity is additive and retains provider id, "
                        "type, digest, normalized headers, payload, and processing state."
                    ),
                    replay=(
                        "The canonical IntegrationInbox row re-enters processing only via "
                        "authorized replay; domain consequences remain owned by their service."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "provider webhooks writing domain state directly or maintaining "
                        "provider-specific deduplication records"
                    ),
                    new_owner="integration.inbox",
                    verification=(
                        "Verified receipt identity, collision quarantine, consequence, "
                        "retry, replay, WhatsApp, and legacy-cutover tests."
                    ),
                    cutover_gate=(
                        "Provider-specific routes verify signatures, write one inbox receipt, "
                        "and delegate every consequence to a named Sub owner."
                    ),
                    fallback_retirement=(
                        "Legacy webhook receipt models and direct provider-to-domain write "
                        "paths are removed."
                    ),
                ),
                steward="platform integrations",
                design_refs=(
                    "docs/designs/INTEGRATION_PLATFORM_SOT.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_integration_installation_api.py",
                    "tests/test_integration_whatsapp_capability.py",
                    "tests/architecture/test_integration_platform_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="integration.jobs",
            module="app.services.integration",
            owns=("integration targets", "integration jobs", "integration runs"),
            depends_on=(
                "integration.registry",
                "integration.installations",
                "scheduler.registry",
            ),
            notes=(
                "Jobs bind directly to versioned connector capabilities; "
                "adapter/action transport selection is not a runtime input. "
                "The CRM ticket cutover activates its historical job only "
                "through the exact-state owner command."
            ),
            contract=ServiceContract(
                concerns=tuple(
                    ConcernContract(
                        name=concern,
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "deployed capability contract",
                            "enabled integration capability binding",
                            "integration job lifecycle protocol",
                            "scheduler-owned cadence",
                        ),
                        canonical_writer="integration.jobs",
                    )
                    for concern in (
                        "integration targets",
                        "integration jobs",
                        "integration runs",
                    )
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="deployed capability contract",
                        owner="integration.registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "registered capability identity, supported modes, "
                            "and connector contract version"
                        ),
                    ),
                    AuthorityInput(
                        name="enabled integration capability binding",
                        owner="integration.installations",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "enabled version-pinned installation binding selected "
                            "for one exact integration job"
                        ),
                    ),
                    AuthorityInput(
                        name="integration job lifecycle protocol",
                        owner="integration.jobs",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "target, inactive or active job, exact capability "
                            "binding, run identity, and terminal run evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="scheduler-owned cadence",
                        owner="scheduler.registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "canonical feature enablement and cadence; a manual "
                            "capability job does not create a second schedule"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each migrated public job command completes one exact "
                        "target/job/run aggregate transaction."
                    ),
                    locking=(
                        "Capability activation locks the selected job, target, and "
                        "binding in stable order before changing executable state."
                    ),
                    idempotency=(
                        "An already active job on the reviewed binding replays; "
                        "changed reviewed job state fails closed."
                    ),
                    retries=(
                        "Stale state and lifecycle conflicts require a new preview; "
                        "database conflicts retry the complete command."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "integration.jobs.job_activation_scope_invalid",
                        "integration.jobs.invalid_capability",
                        "integration.jobs.job_not_found",
                        "integration.jobs.target_not_found",
                        "integration.jobs.target_type_mismatch",
                        "integration.jobs.target_disabled",
                        "integration.jobs.job_type_mismatch",
                        "integration.jobs.binding_not_found",
                        "integration.jobs.binding_capability_mismatch",
                        "integration.jobs.binding_disabled",
                        "integration.jobs.stale_job_state",
                        "integration.jobs.binding_conflict",
                        "integration.jobs.invalid_command_context",
                        "integration.jobs.command_contract_violation",
                        "integration.jobs.nested_owner_command",
                        "integration.jobs.active_caller_transaction",
                        "integration.jobs.nested_transaction_completion",
                    ),
                    mapping_owner=(
                        "integration admin API and reviewed integration cutover CLI"
                    ),
                    fail_closed_on=(
                        "missing or disabled capability binding",
                        "inactive or wrong-type target",
                        "stale reviewed job state",
                        "a second scheduler cadence path",
                    ),
                ),
                events=EventContract(
                    event_types=("integration.job.capability_activated",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 identifies the job, target, connector, "
                        "capability binding, actor, and command without secrets."
                    ),
                    replay=(
                        "The authoritative job and binding rows rebuild executable "
                        "state; replay does not emit another activation event."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUTOVER_READY,
                    old_owner=(
                        "legacy adapter/action jobs disabled without explicit "
                        "capability reactivation"
                    ),
                    new_owner="integration.jobs",
                    verification=(
                        "Exact-state activation, replay, stale-state, scheduler "
                        "readiness, deployment-gate, and CRM sync tests."
                    ),
                    cutover_gate=(
                        "Enabled crm.ticket_pull requires exactly one enabled "
                        "ticket-observation binding and one active bound manual job."
                    ),
                    fallback_retirement=(
                        "Unbound active jobs and independent interval scheduling "
                        "remain prohibited."
                    ),
                ),
                steward="platform integrations",
                design_refs=(
                    "docs/designs/INTEGRATION_PLATFORM_SOT.md",
                    "docs/runbooks/CRM_TICKET_CAPABILITY_CUTOVER.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_integration_capability_sync.py",
                    "tests/test_crm_ticket_capability_cutover.py",
                    "tests/architecture/test_integration_platform_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="integration.sync",
            module="app.services.integration_sync",
            owns=("integration sync orchestration", "sync run lifecycle"),
            depends_on=("integration.jobs", "integration.runtime"),
            notes=(
                "CRM observation jobs resolve their version-pinned bindings "
                "and execute only through the registered CRM runner."
            ),
        ),
        SOTService(
            name="integration.backoffice_adapter",
            module="app.services.backoffice",
            owns=(
                "Sub-local backoffice integration port",
                "provider-neutral capability requests",
                "provider-neutral delivery requests from Sub owners",
            ),
            depends_on=("integration.installations", "integration.runtime"),
            notes=(
                "This is an anti-corruption boundary inside Sub, not an "
                "enterprise-wide capability or identifier registry. The default "
                "enabled binding selects a replaceable connector, which has no "
                "authority over Sub customer, subscriber, service, workflow, or "
                "operational state."
            ),
        ),
        SOTService(
            name="integration.workforce_attendance_adapter",
            module="app.services.workforce_attendance",
            owns=(
                "provider-neutral workforce attendance query translation",
                "provider-neutral workforce attendance punch transport",
                "ERP attendance response normalization",
            ),
            depends_on=(
                "auth.permission_gate",
                "integration.backoffice_adapter",
            ),
            notes=(
                "Selfcare authenticates the staff subject and transports fresh, "
                "untrusted browser location evidence through the enabled attendance "
                "capability. Dotmac ERP alone owns employee resolution, shift and "
                "timezone policy, geofence decisions, attendance state, and persistence."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "provider-neutral workforce attendance query translation"
                        ),
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "authenticated Selfcare staff subject",
                            "enabled workforce attendance capability binding",
                            "ERP attendance observation",
                        ),
                    ),
                    ConcernContract(
                        name=("provider-neutral workforce attendance punch transport"),
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "authenticated Selfcare staff subject",
                            "fresh browser location observation",
                            "enabled workforce attendance capability binding",
                            "ERP attendance observation",
                        ),
                    ),
                    ConcernContract(
                        name="ERP attendance response normalization",
                        role=OwnerRole.RESOLVER,
                        input_names=("ERP attendance observation",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated Selfcare staff subject",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active SystemUser principal identity whose authenticated "
                            "principal id exactly matches the request user"
                        ),
                    ),
                    AuthorityInput(
                        name="fresh browser location observation",
                        owner="external:staff_browser",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "schema-validated latitude, longitude, accuracy, and browser "
                            "observation time captured for the individual punch"
                        ),
                    ),
                    AuthorityInput(
                        name="enabled workforce attendance capability binding",
                        owner="integration.installations",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "enabled version-pinned workforce.attendance.read.v1 and "
                            "workforce.attendance.punch.v1 capability bindings"
                        ),
                    ),
                    AuthorityInput(
                        name="ERP attendance observation",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "ERP-owned daily attendance state, policy reason, permitted "
                            "actions, timestamps, status, timezone, and working hours"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Selfcare reads the capability binding and performs no local "
                        "attendance mutation; ERP owns and completes the punch transaction."
                    ),
                    locking=(
                        "No Sub attendance row exists to lock; ERP locks its employee and "
                        "daily attendance state before deciding the punch."
                    ),
                    idempotency=(
                        "The browser idempotency key passes unchanged through the "
                        "capability runtime to ERP; duplicate or ambiguous results are "
                        "resolved by reading ERP's authoritative state."
                    ),
                    retries=(
                        "Interactive transport uses bounded connector retries; transient "
                        "or ambiguous failure returns unavailable and requests a fresh ERP "
                        "read rather than inferring success."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "attendance_unavailable",
                        "invalid_provider_response",
                        "employee_not_linked",
                        "employee_mapping_ambiguous",
                        "employee_inactive",
                        "attendance_disabled",
                        "outside_geofence",
                        "invalid_location",
                        "location_required",
                        "already_checked_in",
                        "already_checked_out",
                        "check_in_required",
                        "overnight_shift_not_supported",
                        "authorization_failed",
                    ),
                    mapping_owner="app.services.web_admin_attendance",
                    retryable_codes=("attendance_unavailable",),
                    fail_closed_on=(
                        "missing or mismatched authenticated staff subject",
                        "missing or disabled capability binding",
                        "invalid ERP response",
                        "missing or rejected browser location",
                        "ambiguous employee identity",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="integration.workforce_attendance_adapter",
                ),
                steward="workforce integrations",
                design_refs=(
                    "docs/designs/WORKFORCE_ATTENDANCE_INTEGRATION.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_workforce_attendance_capability.py",
                    "tests/test_admin_dashboard_attendance.py",
                    "tests/architecture/test_integration_platform_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="integration.dotmac_erp_operational_context_adapter",
            module="app.services.dotmac_erp.domain_sync",
            owns=(
                "typed ERP operational-context projection mapping",
                "version-2 ERP operational-context transport and response validation",
                "per-domain ERP operational-context delivery watermarks",
            ),
            depends_on=(
                "events.dispatcher",
                "integration.backoffice_adapter",
                "operations.project_lifecycle",
                "operations.work_order_commands",
                "support.ticket_lifecycle",
            ),
            notes=(
                "Sub retains project, project-task, ticket, and work-order authority. "
                "ERP receives a rebuildable projection for its own finance context."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="typed ERP operational-context projection mapping",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical Sub projects and project tasks",
                            "canonical Sub support tickets",
                            "canonical Sub service work orders",
                            "enabled ERP operational-sync capability",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "version-2 ERP operational-context transport and response "
                            "validation"
                        ),
                        role=OwnerRole.TRANSPORT,
                        input_names=(
                            "enabled ERP operational-sync capability",
                            "ERP version-2 operational-sync response",
                        ),
                    ),
                    ConcernContract(
                        name="per-domain ERP operational-context delivery watermarks",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "canonical Sub projects and project tasks",
                            "canonical Sub support tickets",
                            "canonical Sub service work orders",
                            "ERP version-2 operational-sync response",
                        ),
                        canonical_writer=(
                            "integration.dotmac_erp_operational_context_adapter"
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical Sub projects and project tasks",
                        owner="operations.project_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "canonical project aggregates, hierarchy, status, schedule, "
                            "and task-to-ticket source relationship"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Sub support tickets",
                        owner="support.ticket_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "canonical ticket identity, number, status, priority, and "
                            "customer relationship"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Sub service work orders",
                        owner="operations.work_order_commands",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "canonical service work-order identity, schedule, status, "
                            "and project/ticket relationships"
                        ),
                    ),
                    AuthorityInput(
                        name="enabled ERP operational-sync capability",
                        owner="integration.backoffice_adapter",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "enabled version-pinned erp.operational_context.sync.v1 "
                            "binding, domain allowlist, and bounded batch policy"
                        ),
                    ),
                    AuthorityInput(
                        name="ERP version-2 operational-sync response",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "contract version, accepted entity counts, and item errors "
                            "returned by /api/v1/sync/sub/bulk"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "One public sync command validates the ERP response and commits "
                        "all selected domain watermarks together only after zero errors."
                    ),
                    locking=(
                        "Per-domain cursor rows serialize watermark advancement; the "
                        "keyset cursor orders source updates by updated_at and UUID."
                    ),
                    idempotency=(
                        "ERP upserts by organization, entity type, and Sub source UUID; "
                        "a failed batch replays from unchanged watermarks."
                    ),
                    retries=(
                        "The scheduled capability retries transient transport failures; "
                        "item errors leave every selected watermark unchanged."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        *owner_command_boundary_error_codes(
                            "integration.dotmac_erp_operational_context_adapter"
                        ),
                        "integration.dotmac_erp_operational_context_adapter.invalid_domain",
                        "integration.dotmac_erp_operational_context_adapter.invalid_response",
                        "integration.dotmac_erp_operational_context_adapter.transport_unavailable",
                    ),
                    mapping_owner="app.tasks.dotmac_erp_outbox",
                    retryable_codes=(
                        "integration.dotmac_erp_operational_context_adapter.transport_unavailable",
                    ),
                    fail_closed_on=(
                        "missing or disabled ERP capability binding",
                        "missing version-2 response",
                        "any ERP item error",
                        "unsupported domain selection",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUTOVER_READY,
                    old_owner="untyped operational sync helper and ERP version-1 endpoint",
                    new_owner=("integration.dotmac_erp_operational_context_adapter"),
                    verification=(
                        "Typed Sub tests plus ERP API/PostgreSQL end-to-end acceptance "
                        "for project, ticket, task, forms, and idempotent replay."
                    ),
                    cutover_gate=(
                        "Deploy ERP schema and version-2 endpoint before enabling the "
                        "Self-Care operational-sync capability."
                    ),
                    fallback_retirement=(
                        "Self-Care never falls back to /sync/crm/bulk; the legacy CRM "
                        "endpoint retires independently after remaining CRM callers."
                    ),
                ),
                steward="service delivery integrations",
                design_refs=(
                    "docs/designs/CONFIGURABLE_ERP_OPERATIONAL_SYNC.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=("tests/test_dotmac_erp_domain_sync.py",),
                events=EventContract(
                    event_types=("erp.operational_context.watermark_advanced",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Additive payload evolution; counts and selected domains remain "
                        "available for version 1 consumers."
                    ),
                    replay=(
                        "The durable dispatcher may replay by event id; the event is "
                        "informational and does not advance a second cursor."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="ERP operational-context delivery watermarks",
                        input_names=(
                            "canonical Sub projects and project tasks",
                            "canonical Sub support tickets",
                            "canonical Sub service work orders",
                            "ERP version-2 operational-sync response",
                        ),
                        writer=("integration.dotmac_erp_operational_context_adapter"),
                        freshness="scheduled five-minute keyset batches",
                        stale_behavior=(
                            "Expense context in ERP may lag, while Sub remains authoritative."
                        ),
                        drift_signal=(
                            "ERP item errors, invalid contract response, or unchanged cursor "
                            "with eligible source rows"
                        ),
                        rebuild_operation=(
                            "reset selected erp_domain_sync_cursors and replay source UUIDs"
                        ),
                        repair_owner=(
                            "integration.dotmac_erp_operational_context_adapter"
                        ),
                    ),
                ),
            ),
        ),
        SOTService(
            name="integration.dotmac_erp_payables_adapter",
            module="app.services.dotmac_erp.purchase_invoice_sync",
            owns=(
                "Dotmac ERP purchase-invoice payload mapping",
                "Dotmac ERP attachment delivery",
                "timestamped Dotmac ERP payables-status observation",
            ),
            depends_on=("integration.backoffice_adapter",),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="Dotmac ERP purchase-invoice payload mapping",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "canonical vendor purchase-invoice records",
                            "ERP purchase-invoice origination response",
                            "ERP purchase-invoice flow controls",
                        ),
                        canonical_writer=("integration.dotmac_erp_payables_adapter"),
                    ),
                    ConcernContract(
                        name="Dotmac ERP attachment delivery",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "canonical vendor purchase-invoice records",
                            "ERP purchase-invoice attachment response",
                            "ERP purchase-invoice flow controls",
                        ),
                        canonical_writer=("integration.dotmac_erp_payables_adapter"),
                    ),
                    ConcernContract(
                        name=("timestamped Dotmac ERP payables-status observation"),
                        role=OwnerRole.RECONCILER,
                        input_names=(
                            "canonical vendor purchase-invoice records",
                            "ERP accounts-payable status observation",
                            "ERP purchase-invoice flow controls",
                        ),
                        canonical_writer=("integration.dotmac_erp_payables_adapter"),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical vendor purchase-invoice records",
                        owner="operations.vendor_purchase_invoice_records",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active VendorPurchaseInvoice, ERP purchase-order link, "
                            "approved lines, attachment, currency, and ERP invoice link"
                        ),
                    ),
                    AuthorityInput(
                        name="ERP purchase-invoice origination response",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "ERP purchase-invoice identity and creation status returned "
                            "for one source invoice"
                        ),
                    ),
                    AuthorityInput(
                        name="ERP purchase-invoice attachment response",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "accepted attachment upload for one immutable ERP invoice "
                            "identity and content-addressed source attachment"
                        ),
                    ),
                    AuthorityInput(
                        name="ERP accounts-payable status observation",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "source and ERP invoice identities, currency, status, total, "
                            "paid, balance, and source update timestamp"
                        ),
                    ),
                    AuthorityInput(
                        name="ERP purchase-invoice flow controls",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "ERP sync enablement, purchase_invoice ownership, bounded "
                            "batch size, and scheduler cadence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Outbox origination writes participate in the approved invoice "
                        "transaction; repair and status reconciliation commit one "
                        "revalidated invoice projection per ERP result."
                    ),
                    locking=(
                        "Status reconciliation snapshots identifiers before transport, "
                        "then locks the active invoice and rechecks ERP identity and "
                        "currency before writing."
                    ),
                    idempotency=(
                        "Stable invoice and attachment keys deduplicate origination; "
                        "equivalent status observations converge while refreshing only "
                        "their observation timestamp."
                    ),
                    retries=(
                        "Each failed invoice rolls back independently, records a bounded "
                        "refresh error, and is repairable by a later scheduled pass."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        ("integration.dotmac_erp_payables_adapter.invalid_observation"),
                        ("integration.dotmac_erp_payables_adapter.identity_mismatch"),
                        ("integration.dotmac_erp_payables_adapter.amount_mismatch"),
                        (
                            "integration.dotmac_erp_payables_adapter."
                            "transport_unavailable"
                        ),
                        (
                            "integration.dotmac_erp_payables_adapter."
                            "invalid_command_context"
                        ),
                        (
                            "integration.dotmac_erp_payables_adapter."
                            "command_contract_violation"
                        ),
                        (
                            "integration.dotmac_erp_payables_adapter."
                            "nested_owner_command"
                        ),
                        (
                            "integration.dotmac_erp_payables_adapter."
                            "active_caller_transaction"
                        ),
                        (
                            "integration.dotmac_erp_payables_adapter."
                            "nested_transaction_completion"
                        ),
                    ),
                    mapping_owner=(
                        "app.tasks.dotmac_erp_outbox and vendor payment read adapters"
                    ),
                    retryable_codes=(
                        (
                            "integration.dotmac_erp_payables_adapter."
                            "transport_unavailable"
                        ),
                    ),
                    fail_closed_on=(
                        "source or ERP invoice identity mismatch",
                        "currency or amount reconciliation mismatch",
                        "missing or stale canonical invoice link",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "vendor_purchase_invoice.erp_projection_refreshed",
                        "vendor_purchase_invoice.payment_observed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and identifies the source invoice, ERP "
                        "invoice, projection kind, observation time, and changed fields."
                    ),
                    replay=(
                        "Canonical invoice and attachment rows plus FieldErpSyncEvent "
                        "origination evidence rebuild links; scheduled ERP observation "
                        "repairs settlement projection drift."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="vendor purchase-invoice ERP projection",
                        input_names=(
                            "canonical vendor purchase-invoice records",
                            "ERP purchase-invoice origination response",
                            "ERP purchase-invoice attachment response",
                            "ERP accounts-payable status observation",
                        ),
                        writer=("integration.dotmac_erp_payables_adapter"),
                        freshness=(
                            "Origination and attachment links are durable; AP status is "
                            "fresh for fifteen minutes from observed_at."
                        ),
                        stale_behavior=(
                            "Retain the last valid observation with explicit stale or "
                            "unavailable presentation; never infer paid from creation."
                        ),
                        drift_signal=(
                            "A linked invoice lacks a recent valid observation, reports "
                            "a refresh error, or has an unsynchronized attachment."
                        ),
                        rebuild_operation=(
                            "Run purchase-invoice repair and status reconciliation from "
                            "the canonical invoice and its stable ERP identities."
                        ),
                        repair_owner=("integration.dotmac_erp_payables_adapter"),
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner=(
                        "ERP creation-response status and vendor UI paths that treated "
                        "origination evidence as accounts-payable settlement state"
                    ),
                    new_owner=("integration.dotmac_erp_payables_adapter"),
                    verification=(
                        "Identity, currency, amount, rollback, stale, last-good, creation "
                        "separation, scheduler, and vendor visibility tests."
                    ),
                    cutover_gate=(
                        "The additive payment projection migration is applied and the "
                        "ERP status endpoint returns the validated source contract."
                    ),
                    fallback_retirement=(
                        "ERP creation status remains origination evidence only; vendor "
                        "payment reads no longer use it as a settlement fallback."
                    ),
                ),
                steward="vendor finance integrations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_vendor_payment_visibility.py",
                    "tests/test_dotmac_erp_outbox.py",
                ),
            ),
            notes=(
                "Provider-specific transport and observation only. The configured "
                "payables system owns settlement; this adapter has no Sub domain "
                "authority and can be retired when another provider replaces it."
            ),
        ),
        SOTService(
            name="integration.dotmac_erp_material_support_adapter",
            module="app.services.dotmac_erp.material_sync",
            owns=(
                "Sub-to-Dotmac-ERP material-support payload mapping",
                "provider-specific stable idempotency key",
                "Dotmac ERP material-outcome observation and reconciliation",
            ),
            depends_on=(
                "integration.backoffice_adapter",
                "operations.material_dependencies",
            ),
            notes=(
                "Provider-specific transport and observation only. Dotmac ERP "
                "decides its backoffice outcome; operations.material_dependencies "
                "alone projects the observation into Sub service-workflow state."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="Sub-to-Dotmac-ERP material-support payload mapping",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "approved canonical material dependency",
                            "ERP material-support transport contract",
                        ),
                    ),
                    ConcernContract(
                        name="provider-specific stable idempotency key",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "approved canonical material dependency",
                            "ERP material-support transport contract",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "Dotmac ERP material-outcome observation and reconciliation"
                        ),
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "canonical material dependency projection target",
                            "ERP material-support outcome response",
                            "ERP material-support transport contract",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="approved canonical material dependency",
                        owner="operations.material_dependencies",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "approved FieldMaterialRequest, request items, technician, "
                            "warehouse, and serialized-unit evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical material dependency projection target",
                        owner="operations.material_dependencies",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active FieldMaterialRequest and work-order allocation "
                            "aggregate revalidated before outcome application"
                        ),
                    ),
                    AuthorityInput(
                        name="ERP material-support outcome response",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "ERP material request identity and normalized stock issue, "
                            "fulfilment, cancellation, or refusal status"
                        ),
                    ),
                    AuthorityInput(
                        name="ERP material-support transport contract",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "material_request flow ownership, validated ERP capability "
                            "bindings, ISSUE payload schema, bounded batch size, and "
                            "retry cadence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "Approval enqueues through the material owner transaction; each "
                        "accepted delivery or scheduled poll asks that owner to project "
                        "one ERP outcome before the surrounding row commit."
                    ),
                    locking=(
                        "The material owner resolves the active request and rejects a "
                        "changed ERP identity before applying workflow consequences."
                    ),
                    idempotency=(
                        "mr-{request_id}-approve-v1 deduplicates transport and repeated "
                        "normalized ERP outcomes are no-op owner requests."
                    ),
                    retries=(
                        "The outbox retries transport with the stable key; scheduled "
                        "reconciliation isolates failures per request and repairs later."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "integration.dotmac_erp_material_support_adapter.invalid_payload",
                        "integration.dotmac_erp_material_support_adapter.ineligible_request",
                        "integration.dotmac_erp_material_support_adapter.transport_unavailable",
                        "integration.dotmac_erp_material_support_adapter.invalid_outcome",
                        "integration.dotmac_erp_material_support_adapter.invalid_command_context",
                        "integration.dotmac_erp_material_support_adapter.command_contract_violation",
                        "integration.dotmac_erp_material_support_adapter.nested_owner_command",
                        "integration.dotmac_erp_material_support_adapter.active_caller_transaction",
                        (
                            "integration.dotmac_erp_material_support_adapter."
                            "nested_transaction_completion"
                        ),
                    ),
                    mapping_owner=(
                        "app.tasks.dotmac_erp_outbox and "
                        "operations.material_dependencies"
                    ),
                    retryable_codes=(
                        "integration.dotmac_erp_material_support_adapter.transport_unavailable",
                    ),
                    fail_closed_on=(
                        "flow not owned by Sub",
                        "missing ERP delivery capability after cutover",
                        "missing warehouse, technician, item, or serial evidence",
                        "changed ERP material-request identity",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUTOVER_READY,
                    old_owner=(
                        "CRM material-request ERP mapper, delivery path, and local "
                        "material fulfilment workflow"
                    ),
                    new_owner="integration.dotmac_erp_material_support_adapter",
                    verification=(
                        "Payload parity, stable-key, flow gate, approval atomicity, "
                        "outbox response, reconciliation, and failure-isolation tests."
                    ),
                    cutover_gate=(
                        "Assign material_request flow ownership to Sub only after ERP "
                        "payload acceptance and shadow outcome comparison are verified."
                    ),
                    fallback_retirement=(
                        "Retire CRM delivery and the local issue/fulfil compatibility "
                        "path after the Sub-owned reconciler is operationally verified."
                    ),
                ),
                steward="field operations integrations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_dotmac_erp_material_sync.py",
                    "tests/test_field_material_requests.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.web.admin.integrations",
        "app.api.*_webhooks",
        "app.tasks.integrations",
        "app.tasks.dotmac_erp_outbox",
        "app.tasks.integration_delivery",
    ),
    rule="Integration routes and webhooks validate and enqueue through typed "
    "capabilities; connectors never become business-state writers. Sub "
    "domain owners depend only on the Sub-local backoffice port and typed "
    "capability contracts, never on a provider database or foreign key.",
)
