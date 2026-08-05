"""sales_referrals SOT declarations: acquisition."""

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

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="sales.capture",
        module="app.services.sales.capture",
        owns=(
            "provider-neutral Party-first Lead capture command",
            "source-interaction idempotency and collision decision",
            "verified integration receipt to Lead consequence",
        ),
        depends_on=(
            "integration.inbox",
            "party.registry",
            "sales.lead_lifecycle",
            "events.dispatcher",
        ),
        notes=(
            "Provider adapters submit the canonical contract and never "
            "write Party, Lead, or attribution state directly."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="provider-neutral Party-first Lead capture command",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "validated lead-capture contract",
                        "canonical Party identity state",
                        "canonical Lead lifecycle state",
                    ),
                ),
                ConcernContract(
                    name="source-interaction idempotency and collision decision",
                    role=OwnerRole.POLICY,
                    input_names=(
                        "validated lead-capture contract",
                        "immutable captured origin evidence",
                    ),
                ),
                ConcernContract(
                    name="verified integration receipt to Lead consequence",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "verified integration receipt",
                        "validated lead-capture contract",
                        "canonical Party identity state",
                        "canonical Lead lifecycle state",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="validated lead-capture contract",
                    owner="sales.capture",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed capture method, source platform, exact interaction "
                        "identity, attribution, Party input, and policy version"
                    ),
                ),
                AuthorityInput(
                    name="verified integration receipt",
                    owner="integration.inbox",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked verified IntegrationInbox receipt, capability "
                        "binding, provider event id, and payload digest"
                    ),
                ),
                AuthorityInput(
                    name="canonical Party identity state",
                    owner="party.registry",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "exact supplied Party or newly created Party, prospect "
                        "role, and unverified contact observations"
                    ),
                ),
                AuthorityInput(
                    name="canonical Lead lifecycle state",
                    owner="sales.lead_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Party-bound Lead and immutable LeadOriginCapture",
                ),
                AuthorityInput(
                    name="immutable captured origin evidence",
                    owner="sales.lead_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique source-platform and source-interaction identity, "
                        "canonical fingerprint, and append-only origin row"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Direct capture owns one optional root transaction; verified "
                    "receipt capture locks receipt, stages Party, Lead, origin, "
                    "receipt consequence and events, then commits once."
                ),
                locking=(
                    "Verified receipts are selected FOR UPDATE; unique interaction "
                    "identity and origin constraints arbitrate concurrent capture."
                ),
                idempotency=(
                    "The same interaction and fingerprint return the original "
                    "Party/Lead/origin; different content under the identity fails."
                ),
                retries=(
                    "A uniqueness loser reloads the canonical origin and applies "
                    "the same fingerprint replay decision."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "sales.capture.active_caller_transaction",
                    "sales.capture.command_contract_violation",
                    "sales.capture.invalid_command_context",
                    "sales.capture.nested_owner_command",
                    "sales.capture.nested_transaction_completion",
                    "actor_required",
                    "source_interaction_collision",
                    "captured_lead_party_missing",
                    "invalid_contact_observation",
                    "receipt_not_found",
                    "wrong_capability",
                    "receipt_identity_mismatch",
                    "capture_conflict",
                    "capture_rejected",
                ),
                mapping_owner="lead capture HTTP and installed connector adapters",
                fail_closed_on=(
                    "unverified or wrong-capability receipt",
                    "interaction identity mismatch",
                    "fingerprint collision",
                    "ambiguous Party context",
                ),
            ),
            events=EventContract(
                event_types=("lead.created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact Lead, Party, origin, capture method, "
                    "platform, and source-interaction identifiers without contact PII."
                ),
                replay=(
                    "The immutable origin fingerprint and IntegrationInbox "
                    "consequence reproduce the original capture outcome."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "manual Subscriber-first entry and aspirational CRM bridge "
                    "capture without an authoritative interaction receipt"
                ),
                new_owner="sales.capture",
                verification=(
                    "Valid signature, replay, collision, invalid receipt, Party, "
                    "origin immutability, and connector registry tests."
                ),
                cutover_gate=(
                    "Installed provider adapters create verified inbox receipts and "
                    "invoke this owner; agents use the same typed capture contract."
                ),
                fallback_retirement=(
                    "CRM and dotmac_mkt have no capture writer or attribution path."
                ),
            ),
            steward="sales operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_lead_capture_webhook.py",
                "tests/test_sales_capture_account_conversion.py",
                "tests/architecture/test_service_http_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="sales.lead_intake",
        module="app.services.sales.lead_intake",
        owns=(
            "versioned lead-intake template lifecycle",
            "sales lead eligibility and invitation lifecycle",
            "atomic Inbox form to Party and Lead conversion",
        ),
        depends_on=(
            "ai.intake",
            "auth.staff_provisioning",
            "communications.team_inbox_contact_resolution",
            "communications.team_inbox_outbound_intents",
            "communications.team_inbox_participants",
            "communications.team_inbox_processing",
            "communications.team_inbox_routing",
            "control.settings_spec",
            "events.dispatcher",
            "gis.geocoding",
            "observability.audit_log",
            "party.registry",
            "sales.lead_lifecycle",
            "sales.service",
        ),
        notes=(
            "The general ai.intake owner classifies and routes every eligible "
            "customer message. Only its final high-confidence sales result is "
            "handed to this owner for a single form invitation and Party-first Lead."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="versioned lead-intake template lifecycle",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "authenticated Lead intake template command",
                        "canonical Sales routing configuration",
                    ),
                ),
                ConcernContract(
                    name="sales lead eligibility and invitation lifecycle",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical unknown Inbox conversation state",
                        "shared customer intake sales handoff",
                        "explicit Lead intake rollout configuration",
                        "published Lead intake template versions",
                    ),
                ),
                ConcernContract(
                    name="atomic Inbox form to Party and Lead conversion",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "validated public Lead intake submission",
                        "canonical Lead intake invitation",
                        "server-resolved Nigerian service address",
                        "canonical Party identity state",
                        "canonical Lead lifecycle state",
                        "canonical unknown Inbox conversation state",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="authenticated Lead intake template command",
                    owner="sales.lead_intake",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="typed template values, routing references, actor and CommandContext",
                ),
                AuthorityInput(
                    name="canonical Sales routing configuration",
                    owner="sales.service",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active ServiceTeam, SystemUser, Pipeline and Stage references",
                ),
                AuthorityInput(
                    name="canonical unknown Inbox conversation state",
                    owner="communications.team_inbox_processing",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked unmatched Meta conversation, message and provider-scoped endpoint",
                ),
                AuthorityInput(
                    name="shared customer intake sales handoff",
                    owner="ai.intake",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="classified new-connection or coverage intent, customer type and message identity",
                ),
                AuthorityInput(
                    name="explicit Lead intake rollout configuration",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="automatic-send switch, bounded TTL and AiIntakeConfig confidence threshold",
                ),
                AuthorityInput(
                    name="published Lead intake template versions",
                    owner="sales.lead_intake",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="one immutable published individual and organization template",
                ),
                AuthorityInput(
                    name="validated public Lead intake submission",
                    owner="sales.lead_intake",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="typed customer fields, address confirmation, privacy acknowledgement and token",
                ),
                AuthorityInput(
                    name="canonical Lead intake invitation",
                    owner="sales.lead_intake",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="hashed token, template, endpoint, expiry, delivery and completion state",
                ),
                AuthorityInput(
                    name="server-resolved Nigerian service address",
                    owner="gis.geocoding",
                    kind=AuthorityKind.OBSERVATION,
                    source="reverse-geocoded coordinates, state or FCT and country code",
                ),
                AuthorityInput(
                    name="canonical Party identity state",
                    owner="party.registry",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Party, representative, prospect role and exact contact point",
                ),
                AuthorityInput(
                    name="canonical Lead lifecycle state",
                    owner="sales.lead_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="Party-first Lead and immutable Inbox-form origin",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary="Each mutation enters execute_owner_command once and commits or rolls back atomically.",
                locking="Templates, conversation, message, invitation, participant and actor are locked before mutation.",
                idempotency="One assessment per message, one automatic invite per conversation and one completion per token.",
                retries="Adapters retry only the complete owner command after rollback.",
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes("sales.lead_intake"),
                    "sales.lead_intake.actor_not_eligible",
                    "sales.lead_intake.channel_not_supported",
                    "sales.lead_intake.conversation_not_found",
                    "sales.lead_intake.conversation_not_unknown",
                    "sales.lead_intake.invitation_unavailable",
                    "sales.lead_intake.message_not_eligible",
                    "sales.lead_intake.provider_scope_missing",
                    "sales.lead_intake.template_not_found",
                    "sales.lead_intake.template_not_published",
                    "sales.lead_intake.published_template_immutable",
                    "sales.lead_intake.address_not_confirmed",
                    "sales.lead_intake.address_outside_nigeria",
                    "sales.lead_intake.state_unresolved",
                    "sales.lead_intake.privacy_acknowledgement_required",
                ),
                mapping_owner="Inbox, Sales admin and public Lead intake adapters",
                fail_closed_on=(
                    "known or ambiguous customer identity",
                    "unsupported channel or missing account scope",
                    "disabled rollout, missing templates or low confidence",
                    "invalid token or service address",
                ),
            ),
            events=EventContract(
                event_types=("lead.created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility="No form values, endpoints or tokens are emitted.",
                replay="Invitation completion and immutable origin reproduce the outcome.",
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="none; additive Inbox-to-Lead capability",
                new_owner="sales.lead_intake",
                verification="Focused template, invite, form, handoff and boundary tests.",
                cutover_gate="Both templates are published and automatic sends are explicitly enabled.",
                fallback_retirement="No adapter directly creates Lead, Party, invitation or routing state.",
            ),
            steward="sales operations",
            design_refs=(
                "docs/designs/INBOX_LEAD_INTAKE.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_CUSTOMER_LIFECYCLE.md",
            ),
            test_refs=(
                "tests/test_lead_intake.py",
                "tests/test_web_lead_intake.py",
                "tests/architecture/test_lead_intake_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="sales.lead_authoring",
        module="app.services.sales.lead_authoring",
        owns=("atomic admin Person and Lead authoring",),
        depends_on=(
            "auth.staff_provisioning",
            "events.dispatcher",
            "observability.audit_log",
            "party.registry",
            "sales.lead_lifecycle",
            "sales.service",
        ),
        notes=(
            "The admin adapter submits one typed command. This owner validates "
            "the staff actor, eligible owner, Pipeline/Stage, configured Region, "
            "Organization, Person profile and contact points, then commits the "
            "Person Party, immutable Lead origin, Lead, audit, and event once."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="atomic admin Person and Lead authoring",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "Lead authoring command evidence",
                        "canonical staff actor state",
                        "canonical Party identity state",
                        "canonical sales pipeline state",
                        "configured Region and Organization state",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="Lead authoring command evidence",
                    owner="sales.lead_authoring",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed submission identity, Person profile, contact rows, "
                        "owner, Pipeline/Stage, value, Region, and notes"
                    ),
                ),
                AuthorityInput(
                    name="canonical staff actor state",
                    owner="auth.staff_provisioning",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active authenticated SystemUser and eligible sales owner",
                ),
                AuthorityInput(
                    name="canonical Party identity state",
                    owner="party.registry",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Person Party, prospect role, normalized PartyContactPoints, "
                        "and optional Organization relationship"
                    ),
                ),
                AuthorityInput(
                    name="canonical sales pipeline state",
                    owner="sales.service",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active Pipeline and Stage membership plus Lead status vocabulary",
                ),
                AuthorityInput(
                    name="configured Region and Organization state",
                    owner="sales.lead_authoring",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "active RegionZone and active Organization profile resolved "
                        "by authoritative identifiers"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "execute_owner_command commits Person Party, contact points, "
                    "relationship, Lead origin, Lead, audit, and event once"
                ),
                locking=(
                    "The actor and selected owner are locked; the deterministic Lead "
                    "and Person identifiers plus database constraints arbitrate retries."
                ),
                idempotency=(
                    "The server-issued submission UUID deterministically identifies "
                    "the Lead and Person; an exact fingerprint replays and drift conflicts."
                ),
                retries=(
                    "Safe exact retries replay the saved outcome; validation and "
                    "constraint failures roll back the complete command."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "sales.lead_authoring.active_caller_transaction",
                    "sales.lead_authoring.command_contract_violation",
                    "sales.lead_authoring.invalid_command_context",
                    "sales.lead_authoring.nested_owner_command",
                    "sales.lead_authoring.nested_transaction_completion",
                    "sales.lead_authoring.actor_not_eligible",
                    "sales.lead_authoring.display_name_too_long",
                    "sales.lead_authoring.email_invalid",
                    "sales.lead_authoring.primary_email_in_use",
                    "sales.lead_authoring.phone_invalid",
                    "sales.lead_authoring.owner_not_eligible",
                    "sales.lead_authoring.pipeline_stage_incomplete",
                    "sales.lead_authoring.pipeline_not_active",
                    "sales.lead_authoring.stage_pipeline_mismatch",
                    "sales.lead_authoring.region_not_active",
                    "sales.lead_authoring.organization_not_active",
                    "sales.lead_authoring.organization_party_ineligible",
                    "sales.lead_authoring.status_not_allowed",
                    "sales.lead_authoring.submission_conflict",
                ),
                mapping_owner="admin sales Lead web adapter",
                fail_closed_on=(
                    "inactive or forged actor/owner",
                    "invalid Pipeline/Stage or configured Region",
                    "invalid Organization identity",
                    "contact or private identity validation failure",
                    "submission fingerprint collision",
                ),
            ),
            events=EventContract(
                event_types=("lead.created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries Lead, Party, status, source and Pipeline "
                    "identifiers without contact values or NIN."
                ),
                replay=(
                    "The stored authoring key and fingerprint reproduce the exact "
                    "Lead/Party outcome without duplicate contact points."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="admin web form plus per-row sales.service commits",
                new_owner="sales.lead_authoring",
                verification=(
                    "Focused authoring tests cover identity derivation, contacts, "
                    "ownership, Region, Pipeline/Stage, rollback, and replay."
                ),
                cutover_gate=(
                    "The New Lead POST invokes only the typed owner command and "
                    "ordinary validation failures map back to the HTML form."
                ),
                fallback_retirement=(
                    "The New Lead adapter no longer accepts a Party/Person identifier "
                    "or calls the legacy Leads.create path."
                ),
            ),
            steward="sales operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_web_sales_lead_authoring.py",
                "tests/test_admin_sales_web.py",
                "tests/architecture/test_sales_lifecycle_chain_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="sales.quote_authoring",
        module="app.services.sales.quote_authoring",
        owns=("atomic Lead-backed Draft/Sent Quote authoring",),
        depends_on=(
            "auth.staff_provisioning",
            "events.dispatcher",
            "financial.tax_configuration",
            "observability.audit_log",
            "party.registry",
            "sales.lead_lifecycle",
            "sales.service",
            "service_intent.catalog_policy",
        ),
        notes=(
            "Staff author one Lead-backed Draft or Sent Quote and all of its "
            "lines under one transaction. Initial Accepted authoring and every "
            "Subscriber, order, Project, Task, or WorkOrder consequence are "
            "forbidden; acceptance is a separate sales.quote_acceptance command."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="atomic Lead-backed Draft/Sent Quote authoring",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "Quote authoring command evidence",
                        "canonical staff actor state",
                        "canonical Lead and Party state",
                        "canonical commercial reference state",
                        "canonical Quote lifecycle state",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="Quote authoring command evidence",
                    owner="sales.quote_authoring",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed submission id, Lead, Draft/Sent status, currency, "
                        "tax choice, install location, required Project Type, line values, "
                        "actor, and CommandContext provenance"
                    ),
                ),
                AuthorityInput(
                    name="canonical staff actor state",
                    owner="auth.staff_provisioning",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked active SystemUser addressed by the session actor",
                ),
                AuthorityInput(
                    name="canonical Lead and Party state",
                    owner="sales.lead_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked active open Party-bound Lead",
                ),
                AuthorityInput(
                    name="canonical commercial reference state",
                    owner="sales.quote_authoring",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "validated active offer, field-item, tax-rate, currency, "
                        "quantity, price, discount, and install-pin references"
                    ),
                ),
                AuthorityInput(
                    name="canonical Quote lifecycle state",
                    owner="sales.service",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "Quote with first-class Project Type and QuoteLineItem "
                        "records keyed by submission UUID"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "author_quote enters execute_owner_command once on a clean "
                    "adapter session; Quote, lines, quote.created event, and audit "
                    "evidence commit or roll back together"
                ),
                locking=(
                    "The actor and Lead lock FOR UPDATE; the supplied Quote UUID "
                    "and database key arbitrate concurrent submissions."
                ),
                idempotency=(
                    "Submission UUID plus a canonical command fingerprint returns "
                    "the original Quote; changed content under that UUID fails closed."
                ),
                retries=(
                    "Equivalent retries use the same submission UUID; transient "
                    "failures retry the complete command after rollback."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes("sales.quote_authoring"),
                    "sales.quote_authoring.actor_not_eligible",
                    "sales.quote_authoring.currency_invalid",
                    "sales.quote_authoring.initial_status_invalid",
                    "sales.quote_authoring.install_pin_incomplete",
                    "sales.quote_authoring.inventory_description_mismatch",
                    "sales.quote_authoring.inventory_item_not_active",
                    "sales.quote_authoring.latitude_invalid",
                    "sales.quote_authoring.lead_not_eligible",
                    "sales.quote_authoring.lead_not_found",
                    "sales.quote_authoring.lead_person_ineligible",
                    "sales.quote_authoring.lead_person_required",
                    "sales.quote_authoring.line_description_invalid",
                    "sales.quote_authoring.line_discount_invalid",
                    "sales.quote_authoring.line_items_required",
                    "sales.quote_authoring.line_price_invalid",
                    "sales.quote_authoring.line_quantity_invalid",
                    "sales.quote_authoring.line_source_ambiguous",
                    "sales.quote_authoring.longitude_invalid",
                    "sales.quote_authoring.manual_tax_invalid",
                    "sales.quote_authoring.offer_description_mismatch",
                    "sales.quote_authoring.offer_not_active",
                    "sales.quote_authoring.submission_conflict",
                    "sales.quote_authoring.tax_rate_not_active",
                ),
                mapping_owner="admin sales Quote form adapter",
                fail_closed_on=(
                    "inactive or closed Lead/Party state",
                    "inactive actor or commercial reference",
                    "initial Accepted/Rejected/Expired status",
                    "ambiguous or stale line references",
                ),
            ),
            events=EventContract(
                event_types=("quote.created",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 identifies the Quote, Lead, Party, status, "
                    "currency, and total without contact PII."
                ),
                replay=(
                    "The submission UUID and authoring fingerprint reproduce the "
                    "original Quote and suppress duplicate event staging."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner="admin web form plus per-row sales.service commits",
                new_owner="sales.quote_authoring",
                verification=(
                    "Lead and Project Type requirements, Draft/Sent restriction, "
                    "atomic lines, install metadata, exact replay, manifest, and "
                    "boundary tests."
                ),
                cutover_gate=(
                    "The admin form submits one typed owner command on a clean "
                    "session and exposes only Draft/Sent initial states."
                ),
                fallback_retirement=(
                    "The form cannot create an Accepted Quote or Subscriber and no "
                    "adapter creates initial Quote lines through separate commits."
                ),
            ),
            steward="sales operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_web_sales_quote_authoring.py",
                "tests/test_quote_acceptance_workflow.py",
                "tests/architecture/test_sales_lifecycle_chain_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="sales.quote_documents",
        module="app.services.sales.quote_documents",
        owns=("immutable branded Quote PDF generation",),
        depends_on=(
            "customer.branding",
            "events.dispatcher",
            "observability.audit_log",
            "sales.service",
        ),
        notes=(
            "This owner snapshots the authoritative Quote, lines, recipient "
            "display identity, and resolved company brand into one immutable, "
            "content-addressed PDF artifact. Repeated exports of the same "
            "snapshot reuse the canonical artifact."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="immutable branded Quote PDF generation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "Quote document command evidence",
                        "canonical Quote commercial state",
                        "canonical company branding state",
                    ),
                    canonical_writer="sales.quote_documents",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="Quote document command evidence",
                    owner="sales.quote_documents",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="typed Quote id and CommandContext provenance",
                ),
                AuthorityInput(
                    name="canonical Quote commercial state",
                    owner="sales.service",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active Quote, QuoteLineItems, Lead Party identity, "
                        "currency, totals, expiry, and installation metadata"
                    ),
                ),
                AuthorityInput(
                    name="canonical company branding state",
                    owner="customer.branding",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "resolved subscriber, organization, reseller, or platform "
                        "brand profile and immutable logo digest"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "generate_quote_pdf enters execute_owner_command once; the "
                    "stored-file record, immutable export row, audit evidence, and "
                    "quote.pdf_exported event commit or roll back together"
                ),
                locking="The active Quote is selected FOR UPDATE before snapshotting.",
                idempotency=(
                    "A SHA-256 fingerprint of canonical Quote and brand inputs maps "
                    "to one deterministic export UUID and unique Quote snapshot."
                ),
                retries=(
                    "Equivalent retries reuse the existing export; transient failures "
                    "retry the complete owner command after rollback."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes("sales.quote_documents"),
                    "sales.quote_documents.artifact_missing",
                    "sales.quote_documents.export_not_found",
                    "sales.quote_documents.invalid_pdf",
                    "sales.quote_documents.owner_command_required",
                    "sales.quote_documents.quote_not_found",
                    "sales.quote_documents.renderer_unavailable",
                ),
                mapping_owner="admin Quote detail adapter",
                fail_closed_on=(
                    "missing or inactive Quote",
                    "missing stored artifact",
                    "unavailable or invalid PDF renderer output",
                ),
            ),
            events=EventContract(
                event_types=("quote.pdf_exported",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 identifies the Quote, export, and snapshot fingerprint "
                    "without customer contact data."
                ),
                replay=(
                    "Fingerprint replay returns the existing artifact and suppresses "
                    "duplicate audit and event staging."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="sales.quote_documents",
            ),
            steward="sales operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
            ),
            test_refs=(
                "tests/test_quote_documents_and_delivery.py",
                "tests/architecture/test_quote_document_delivery_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="sales.quote_delivery",
        module="app.services.sales.quote_delivery",
        owns=("idempotent branded Quote email request",),
        depends_on=(
            "communications.intents",
            "customer.branding",
            "events.dispatcher",
            "observability.audit_log",
            "party.registry",
            "sales.quote_documents",
            "sales.service",
        ),
        notes=(
            "This owner resolves the Quote recipient from Party contact points, "
            "attaches the exact immutable branded PDF, and submits one durable "
            "communication intent. The notification dispatcher remains transport; "
            "SMTP acceptance is not treated as mailbox proof."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="idempotent branded Quote email request",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "Quote delivery command evidence",
                        "canonical Quote commercial state",
                        "canonical Party recipient state",
                        "canonical Quote PDF artifact",
                    ),
                    canonical_writer="sales.quote_delivery",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="Quote delivery command evidence",
                    owner="sales.quote_delivery",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed Quote id plus actor, command, correlation, reason, "
                        "scope, and required idempotency evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical Quote commercial state",
                    owner="sales.service",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked active, unexpired, sendable Quote and lines",
                ),
                AuthorityInput(
                    name="canonical Party recipient state",
                    owner="party.registry",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "primary-first active email contact point reached through "
                        "Quote to Lead to Party"
                    ),
                ),
                AuthorityInput(
                    name="canonical Quote PDF artifact",
                    owner="sales.quote_documents",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="immutable branded Quote snapshot and stored PDF",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "send_quote_email enters execute_owner_command once; the PDF "
                    "artifact, delivery request, communication intent, Quote Sent "
                    "transition, audit evidence, and event commit or roll back together"
                ),
                locking="The active Quote is selected FOR UPDATE before eligibility.",
                idempotency=(
                    "The required request key uniquely identifies one Quote delivery; "
                    "exact replay returns the original intent and notification ids."
                ),
                retries=(
                    "Equivalent retries replay the durable request; transient failures "
                    "retry the complete command after rollback."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    *owner_command_boundary_error_codes("sales.quote_delivery"),
                    "sales.quote_delivery.idempotency_conflict",
                    "sales.quote_delivery.idempotency_key_required",
                    "sales.quote_delivery.line_items_required",
                    "sales.quote_delivery.quote_expired",
                    "sales.quote_delivery.quote_not_found",
                    "sales.quote_delivery.recipient_email_required",
                    "sales.quote_delivery.status_not_sendable",
                ),
                mapping_owner="admin Quote detail adapter",
                fail_closed_on=(
                    "missing, inactive, rejected, or expired Quote",
                    "Quote without line items",
                    "missing authoritative active recipient email",
                    "idempotency key reuse for another Quote",
                ),
            ),
            events=EventContract(
                event_types=("quote.delivery_requested",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 identifies the Quote, request, intent, export, and "
                    "queue decision without recipient contact data."
                ),
                replay=(
                    "The delivery request key returns the original result and "
                    "suppresses duplicate communication, audit, and event staging."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="sales.quote_delivery",
            ),
            steward="sales operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
            ),
            test_refs=(
                "tests/test_quote_documents_and_delivery.py",
                "tests/architecture/test_quote_document_delivery_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="sales.account_conversion",
        module="app.services.sales.account_conversion",
        owns=(
            "exact Lead and Party account conversion",
            "customer and pending-subscriber role establishment",
        ),
        depends_on=(
            "customer.accounts",
            "party.registry",
            "sales.lead_lifecycle",
            "events.dispatcher",
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="exact Lead and Party account conversion",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical attributed Lead state",
                        "canonical Party identity state",
                        "reviewed account conversion command",
                        "canonical customer account state",
                    ),
                    canonical_writer="sales.account_conversion",
                ),
                ConcernContract(
                    name=("customer and pending-subscriber role establishment"),
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical Party identity state",
                        "canonical customer account state",
                        "reviewed account conversion command",
                    ),
                    canonical_writer="sales.account_conversion",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical attributed Lead state",
                    owner="sales.lead_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="locked Party-bound Lead and exact Subscriber link",
                ),
                AuthorityInput(
                    name="canonical Party identity state",
                    owner="party.registry",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="exact Lead Party, roles, and Subscriber binding",
                ),
                AuthorityInput(
                    name="canonical customer account state",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "exact existing Subscriber or newly prepared native "
                        "Subscriber account"
                    ),
                ),
                AuthorityInput(
                    name="reviewed account conversion command",
                    owner="sales.account_conversion",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed Lead, Party, actor, and exactly one existing or new "
                        "account target"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "This required Quote-acceptance participant locks the Lead and "
                    "stages account, Party roles/binding, Lead attachment, and "
                    "events without transaction completion. The outer "
                    "sales.quote_acceptance coordinator commits or rolls back once."
                ),
                locking=(
                    "The exact Lead and any existing Subscriber target are selected "
                    "FOR UPDATE before binding."
                ),
                idempotency=(
                    "An already attached Lead returns its canonical exact account; "
                    "different Party/account context fails closed."
                ),
                retries=(
                    "The complete reviewed conversion command is retried with the "
                    "same exact identifiers after transient transaction failure."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "actor_required",
                    "account_target_required",
                    "lead_not_found",
                    "party_mismatch",
                    "existing_account_mismatch",
                    "subscriber_not_found",
                    "existing_target_not_allowed",
                    "conversion_rejected",
                ),
                mapping_owner="sales Quote-acceptance coordinator",
                fail_closed_on=(
                    "Lead/Party mismatch",
                    "ambiguous account target",
                    "existing binding conflict",
                ),
            ),
            events=EventContract(
                event_types=(
                    "subscriber.created",
                    "lead.account_converted",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact Lead, Party, Subscriber, outcome, and "
                    "actor identifiers without contact observations."
                ),
                replay=(
                    "Lead.subscriber_id plus Party/Subscriber bindings reproduce the "
                    "outcome; identical conversion is a no-op."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "referral-private conversion and Subscriber-first sales paths"
                ),
                new_owner="sales.account_conversion",
                verification=(
                    "Create, attach, replay, Party mismatch, role, event, rollback, "
                    "and transport-boundary tests."
                ),
                cutover_gate=(
                    "Quote acceptance is the only sales workflow allowed to "
                    "invoke this Lead/Party conversion participant."
                ),
                fallback_retirement=(
                    "The public Lead account-conversion API and service command, "
                    "contact-based matching, and CRM conversion authority are absent."
                ),
            ),
            steward="sales operations",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_sales_capture_account_conversion.py",
                "tests/test_sales_to_service_lifecycle.py",
                "tests/architecture/test_service_http_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="sales.quote_acceptance",
        module="app.services.sales.quote_acceptance",
        owns=(
            "atomic accepted-Quote sales conversion",
            "accepted-Quote commercial snapshot immutability",
        ),
        depends_on=(
            "customer.accounts",
            "events.dispatcher",
            "observability.audit_log",
            "operations.project_lifecycle",
            "operations.work_order_commands",
            "party.registry",
            "sales.account_conversion",
            "sales.fulfillment",
            "sales.lead_lifecycle",
            "sales.orders",
            "sales.service",
        ),
        notes=(
            "Quote acceptance is the sole sales conversion event. It locks the "
            "Quote and Lead, creates or replays the exact account, copies the "
            "order and lines, copies the Quote-selected Project Type, assigns its "
            "configured active template and Tasks, creates only policy-enabled "
            "WorkOrders, and stages event and audit evidence under one owner "
            "transaction. ProjectTasks capture that automation policy; replay "
            "repairs only missing captured-policy WorkOrders while preserving "
            "manual work and ignoring later template edits, while generic task "
            "metadata updates preserve the captured policy. Initial acceptance "
            "fails closed when the locked Quote has expired. Deposit-backed "
            "acceptance fingerprints the normalized "
            "reference, amount, and provider; only an exact replay is accepted. The "
            "accepted Quote and its copied line terms then remain immutable; revised "
            "commercial terms require a new Quote."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="atomic accepted-Quote sales conversion",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "accepted-Quote command evidence",
                        "canonical Lead and Party state",
                        "canonical Quote and line state",
                        "canonical customer account state",
                        "configured implementation automation",
                    ),
                ),
                ConcernContract(
                    name="accepted-Quote commercial snapshot immutability",
                    role=OwnerRole.POLICY,
                    input_names=("canonical Quote and line state",),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="accepted-Quote command evidence",
                    owner="sales.quote_acceptance",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "typed Quote id and CommandContext actor, command, "
                        "correlation, reason, scope, and idempotency evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical Lead and Party state",
                    owner="sales.lead_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active Party-bound Lead, immutable Party binding, "
                        "and any exact accepted account link"
                    ),
                ),
                AuthorityInput(
                    name="canonical Quote and line state",
                    owner="sales.service",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active Lead-backed Draft, Sent, or Accepted Quote, "
                        "its required first-class Project Type, and priced line items"
                    ),
                ),
                AuthorityInput(
                    name="canonical customer account state",
                    owner="customer.accounts",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "exact Lead-attached Subscriber or typed account prepared "
                        "from the reviewed Party profile"
                    ),
                ),
                AuthorityInput(
                    name="configured implementation automation",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "active ProjectTemplate mapped by Quote Project Type, ordered "
                        "template tasks, and explicit WorkOrder automation flags"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "The public accept_quote command enters execute_owner_command "
                    "once on a transaction-free adapter session. Every participant "
                    "uses the supplied session, flushes only, and the coordinator "
                    "commits or rolls back Quote, Lead, account, order, lines, "
                    "Project, Tasks, WorkOrders, events, and audit together."
                ),
                locking=(
                    "The exact Quote then Lead and Party are selected FOR UPDATE; "
                    "every Quote and line mutation locks the same parent Quote first; "
                    "SalesOrder and Project unique structural keys arbitrate concurrent "
                    "replays."
                ),
                idempotency=(
                    "Quote identity is the idempotency scope. Unique Quote-to-order, "
                    "order-to-Project, template-task identity, and deterministic "
                    "WorkOrder public ids return the original complete outcome. A "
                    "replay re-runs the captured ProjectTask automation and creates "
                    "only a missing deterministic WorkOrder; unrelated WorkOrders "
                    "are preserved. A "
                    "deposit-backed retry must match the normalized reference, amount, "
                    "and provider stored at initial acceptance."
                ),
                retries=(
                    "Equivalent retries re-lock the Quote and return canonical "
                    "identifiers. Conflicting state fails closed; transient database "
                    "failures retry the entire command."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "sales.quote_acceptance.account_profile_incomplete",
                    "sales.quote_acceptance.account_profile_invalid",
                    "sales.quote_acceptance.accepted_quote_immutable",
                    "sales.quote_acceptance.active_caller_transaction",
                    "sales.quote_acceptance.command_contract_violation",
                    "sales.quote_acceptance.deposit_evidence_conflict",
                    "sales.quote_acceptance.deposit_evidence_invalid",
                    "sales.quote_acceptance.invalid_command_context",
                    "sales.quote_acceptance.invalid_transition",
                    "sales.quote_acceptance.lead_party_required",
                    "sales.quote_acceptance.lead_required",
                    "sales.quote_acceptance.line_items_required",
                    "sales.quote_acceptance.nested_owner_command",
                    "sales.quote_acceptance.nested_transaction_completion",
                    "sales.quote_acceptance.party_not_found",
                    "sales.quote_acceptance.participant_rejected",
                    "sales.quote_acceptance.project_template_required",
                    "sales.quote_acceptance.quote_account_conflict",
                    "sales.quote_acceptance.quote_expired",
                    "sales.quote_acceptance.quote_not_found",
                ),
                mapping_owner="sales Quote API and admin web adapters",
                fail_closed_on=(
                    "missing or ambiguous Lead/Party/account evidence",
                    "non-Draft/Sent transition",
                    "expired Quote at initial acceptance",
                    "deposit evidence reuse with changed reference, amount, or provider",
                    "commercial mutation after Quote acceptance",
                    "empty commercial lines or missing Quote Project Type/template",
                    "any account, order, project, task, work-order, event, or audit failure",
                ),
            ),
            events=EventContract(
                event_types=(
                    "subscriber.created",
                    "lead.account_converted",
                    "quote.accepted",
                    "project.created",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact Quote, Lead, Subscriber, SalesOrder, "
                    "Project, ProjectTemplate, actor, and currency/value identifiers."
                ),
                replay=(
                    "Structural unique keys and deterministic WorkOrder ids rebuild "
                    "the same outcome, repair missing captured-policy WorkOrders, and "
                    "preserve manual WorkOrders without duplicate consequences."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "Subscriber-first Quote authoring plus sales.service helper "
                    "commits before Lead, order, and Project consequences"
                ),
                new_owner="sales.quote_acceptance",
                verification=(
                    "Success, failure rollback, exact replay, Project Type template "
                    "assignment, template Tasks, configured WorkOrders, expiry "
                    "rejection, missing configured-WorkOrder replay repair, manual "
                    "WorkOrder preservation, exact deposit replay and conflict "
                    "rejection, accepted commercial immutability, API delegation, "
                    "manifest, and architecture-boundary tests."
                ),
                cutover_gate=(
                    "Every Accepted transition delegates to this coordinator and "
                    "Lead/Quote generic updates cannot create accounts or mark Won."
                ),
                fallback_retirement=(
                    "Lead creation and Quote authoring do not require or create a "
                    "Subscriber; helper commits and swallowed acceptance events are absent."
                ),
            ),
            steward="sales and service delivery",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_quote_acceptance_workflow.py",
                "tests/architecture/test_sales_lifecycle_chain_boundary.py",
            ),
        ),
    ),
    SOTService(
        name="sales.orders",
        module="app.services.sales_orders",
        owns=("sales order lifecycle",),
        depends_on=(
            "sales.service",
            "sales.lead_lifecycle",
            "sales.fulfillment",
        ),
    ),
    SOTService(
        name="sales.fulfillment",
        module="app.services.sales_fulfillment",
        owns=(
            "SalesOrder implementation-scope coordination",
            "verified implementation release coordination",
            "committed lifecycle output consumption",
        ),
        depends_on=(
            "control.settings_spec",
            "operations.project_lifecycle",
            "operations.installation_scope",
            "operations.vendor_project_lifecycle",
            "operations.service_order_lifecycle",
            "events.dispatcher",
            "events.owner_outputs",
        ),
        notes=(
            "Coordinates exact structural identifiers while each domain "
            "owner remains the only writer of its own root. The "
            "funding, verified-implementation, service-order-release, "
            "and CX-acceptance outputs are consumed through receipted "
            "owner commands so each effect commits atomically with its "
            "unique (consumer, event_id) receipt. Funding completion "
            "also stages the structural Phase 1 shadow-contract input; "
            "it does not write billing records itself."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="SalesOrder implementation-scope coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical SalesOrder implementation contract",
                        "configured project defaults",
                        "canonical native project state",
                        "canonical installation scope",
                    ),
                ),
                ConcernContract(
                    name="verified implementation release coordination",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical vendor verification evidence",
                        "canonical native project state",
                        "canonical sales ServiceOrder state",
                    ),
                ),
                ConcernContract(
                    name="committed lifecycle output consumption",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=(
                        "canonical vendor verification evidence",
                        "canonical sales ServiceOrder state",
                        "canonical SalesOrder implementation contract",
                        "receipted owner-output deliveries",
                    ),
                    canonical_writer="sales.fulfillment",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical SalesOrder implementation contract",
                    owner="sales.orders",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "locked active SalesOrder, first-class Quote Project Type, "
                        "exact Lead, Subscriber, line, and funding state"
                    ),
                ),
                AuthorityInput(
                    name="configured project defaults",
                    owner="control.settings_spec",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "projects-domain status, priority, numbering, duration, "
                        "and non-Quote sales type defaults"
                    ),
                ),
                AuthorityInput(
                    name="canonical native project state",
                    owner="operations.project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique structurally linked Project and verified completion "
                        "evidence"
                    ),
                ),
                AuthorityInput(
                    name="canonical installation scope",
                    owner="operations.installation_scope",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="unique Project-bound InstallationProject root",
                ),
                AuthorityInput(
                    name="canonical vendor verification evidence",
                    owner="operations.vendor_project_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "verified InstallationProject plus its exact append-only "
                        "verification event id"
                    ),
                ),
                AuthorityInput(
                    name="canonical sales ServiceOrder state",
                    owner="operations.service_order_lifecycle",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "all structurally Project-bound ServiceOrders ordered by "
                        "creation and identity"
                    ),
                ),
                AuthorityInput(
                    name="receipted owner-output deliveries",
                    owner="events.owner_outputs",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "unique (consumer, event_id) receipts committing "
                        "atomically with each consumed lifecycle effect"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Scope and release commands may own the root transaction or "
                    "flush into the invoking order/event coordinator; the "
                    "consume_* commands each enter execute_owner_command once "
                    "on a transaction-free session; each called domain owner "
                    "remains transaction-neutral in nested use."
                ),
                locking=(
                    "The exact SalesOrder or InstallationProject is selected FOR "
                    "UPDATE; downstream owners lock their own Project and "
                    "ServiceOrder roots."
                ),
                idempotency=(
                    "Unique SalesOrder-to-Project, Project-to-installation, and "
                    "verification-event constraints make replay deterministic."
                ),
                retries=(
                    "Committed vendor events replay the same installation and event "
                    "ids; the reconciler invokes the identical coordinator."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "sales.fulfillment.active_caller_transaction",
                    "sales.fulfillment.command_contract_violation",
                    "sales.fulfillment.invalid_command_context",
                    "sales.fulfillment.nested_owner_command",
                    "sales.fulfillment.nested_transaction_completion",
                    "actor_required",
                    "sales_order_not_found",
                    "sales_order_canceled",
                    "subscriber_not_found",
                    "quote_project_type_required",
                    "project_type_unconfigured",
                    "fulfillment_rejected",
                    "installation_not_found",
                    "implementation_not_verified",
                ),
                mapping_owner="sales order and lifecycle event adapters",
                fail_closed_on=(
                    "missing Quote Project Type or configured Project Template",
                    "structural root mismatch",
                    "unverified implementation",
                    "conflicting verification evidence",
                ),
            ),
            events=EventContract(
                event_types=(
                    "project.created",
                    "installation_scope.created",
                    "implementation.released",
                    "service_order.released",
                    "sales.fulfillment.funding_applied",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 carries exact SalesOrder, Project, installation, "
                    "ServiceOrder, Subscriber, and verification identifiers."
                ),
                replay=(
                    "Structural unique keys and append-only verification evidence "
                    "reproduce scope and release without inferred state."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.COMPLETE,
                old_owner=(
                    "sales project stubs and structurally separate vendor and "
                    "provisioning paths"
                ),
                new_owner="sales.fulfillment",
                verification=(
                    "Scope, funding receipt and shadow output, replay, funding "
                    "gate, vendor verification, release, PostgreSQL constraints, "
                    "and end-to-end lifecycle tests."
                ),
                cutover_gate=(
                    "Every non-cancelled SalesOrder has one structural Project and "
                    "installation scope before sales ServiceOrder creation."
                ),
                fallback_retirement=(
                    "Project integration stubs and metadata-only lifecycle joins "
                    "are not used by new writes."
                ),
            ),
            steward="sales and service delivery",
            design_refs=(
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/PARTY_CUSTOMER_LIFECYCLE.md",
                "docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md",
            ),
            test_refs=(
                "tests/test_sales_to_service_lifecycle.py",
                "tests/test_sales_orders_services.py",
                "tests/test_sales_lifecycle_migration.py",
                "tests/test_billing_shadow_pipeline.py",
                "tests/architecture/test_service_http_boundary.py",
            ),
        ),
    ),
)
