"""Canonical SOT declarations for the network_access_control_plane domain."""

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
    domain="network_access_control_plane",
    setting_domains=("radius",),
    authentication_mechanisms=("radius",),
    services=(
        SOTService(
            name="access.subscription_lifecycle",
            module="app.services.account_lifecycle",
            owns=(
                "enforcement lock lifecycle",
                "persisted access restriction intent",
                "subscription access-status transitions",
                "subscription billing-anchor projection",
                "active subscription billing-anchor invariant",
                "subscriber access-status projection",
                "subscriber portal/account-active projection",
                "atomic account and child-service access projection",
                "sole persisted Subscription.access_state writes",
            ),
            depends_on=(
                "events.dispatcher",
                "financial.prepaid_enforcement_state",
            ),
            notes=(
                "Locks, subscriber status, subscriber account-active state, "
                "subscription status and every child access-state projection are "
                "derived under the lifecycle transaction. "
                "Adapters request a lifecycle command or reconciliation; they do not "
                "write the access-state projection. Subscription creation enters "
                "active state through this owner, and every service-period, grant, "
                "settlement, and reviewed-repair decision submits a typed, locked "
                "compare-and-set billing-anchor projection to its one writer."
            ),
        ),
        SOTService(
            name="access.subscription_lifecycle_evidence",
            module="app.services.subscription_lifecycle_evidence",
            owns=(
                "immutable subscription lifecycle transition evidence",
                "period-scoped subscription lifecycle evidence history",
            ),
            depends_on=(
                "access.subscription_lifecycle",
                "events.dispatcher",
            ),
            notes=(
                "The subscription lifecycle owner applies status and invokes this "
                "flush-only participant in the same transaction. Effective time, "
                "recorded time, admitted source, source identity and fingerprint "
                "are all required before a row can support a contractual period. "
                "A RESTRICT parent link retains subscription identity, so catalog "
                "deletion cannot erase evidence or its scored-period lineage. "
                "Legacy rows and asynchronous event observations remain diagnostic; "
                "prospective baselines repair coverage without inventing history."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="immutable subscription lifecycle transition evidence",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "canonical subscription lifecycle state",
                            "typed lifecycle command evidence",
                        ),
                        canonical_writer="access.subscription_lifecycle_evidence",
                    ),
                    ConcernContract(
                        name="period-scoped subscription lifecycle evidence history",
                        role=OwnerRole.RESOLVER,
                        input_names=("immutable subscription lifecycle evidence rows",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical subscription lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "The locked Subscription row after the lifecycle owner "
                            "has applied its reviewed status transition"
                        ),
                    ),
                    AuthorityInput(
                        name="typed lifecycle command evidence",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "CommandContext actor, scope, reason, command identity, "
                            "correlation and idempotency evidence plus an aware "
                            "effective instant"
                        ),
                    ),
                    AuthorityInput(
                        name="immutable subscription lifecycle evidence rows",
                        owner="access.subscription_lifecycle_evidence",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Append-only subscription_lifecycle_events admitted by "
                            "source, grade, identity, effective and recorded times, "
                            "and fingerprint"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "The lifecycle, catalog-creation, or reviewed recovery owner "
                        "controls completion; this participant validates, appends, "
                        "and flushes only."
                    ),
                    locking=(
                        "The parent owner locks or exclusively creates the "
                        "Subscription before applying status and appending evidence; "
                        "the source-identity unique constraint arbitrates retries."
                    ),
                    idempotency=(
                        "One (evidence_source, source_id) stores one exact fingerprint; "
                        "an identical replay returns the row and changed material "
                        "fails closed."
                    ),
                    retries=(
                        "A database collision is retried only by rerunning the complete "
                        "parent owner transaction, which re-reads the winner and proves "
                        "replay or conflict."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "access.subscription_lifecycle_evidence.naive_effective_at",
                        "access.subscription_lifecycle_evidence.untrusted_source",
                        "access.subscription_lifecycle_evidence.untrusted_grade",
                        "access.subscription_lifecycle_evidence.no_state_change",
                        "access.subscription_lifecycle_evidence.baseline_has_from_status",
                        (
                            "access.subscription_lifecycle_evidence."
                            "incomplete_replay_state"
                        ),
                        "access.subscription_lifecycle_evidence.idempotency_conflict",
                        (
                            "access.subscription_lifecycle_evidence."
                            "subscription_not_found"
                        ),
                        "access.subscription_lifecycle_evidence.status_not_applied",
                        (
                            "access.subscription_lifecycle_evidence."
                            "invalid_baseline_source"
                        ),
                    ),
                    mapping_owner=(
                        "subscription lifecycle, catalog creation, and reviewed "
                        "recovery owners"
                    ),
                    fail_closed_on=(
                        "untrusted source or grade",
                        "missing or naive effective time",
                        "status not yet applied",
                        "source identity reused for different evidence",
                        "subscription deletion with retained lifecycle evidence",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "subscription.created",
                        "subscription.activated",
                        "subscription.suspended",
                        "subscription.resumed",
                        "subscription.disabled",
                        "subscription.expired",
                        "subscription.canceled",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Lifecycle events remain transport consequences of the status "
                        "owner; they are never an evidence ingestion path."
                    ),
                    replay=(
                        "Evidence replay is resolved from its database source identity "
                        "and fingerprint; event redelivery creates no lifecycle row."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    new_owner="access.subscription_lifecycle_evidence",
                    old_owner=(
                        "generic lifecycle CRUD plus asynchronous lifecycle-event "
                        "handler reconstruction using handler time"
                    ),
                    verification=(
                        "Behavior, architecture, append-only PostgreSQL, and fresh plus "
                        "incremental migration tests prove admission and period coverage."
                    ),
                    cutover_gate=(
                        "All lifecycle status paths append admitted evidence or an "
                        "explicit prospective baseline in their owning transaction."
                    ),
                    fallback_retirement=(
                        "Generic creation and event-handler writes are removed; legacy "
                        "rows remain immutable and explicitly unsupported."
                    ),
                ),
                steward="Access lifecycle and customer service-level owners",
                design_refs=("docs/designs/OUTAGE_SLA_SPINE.md",),
                test_refs=(
                    "tests/test_subscription_lifecycle_evidence.py",
                    "tests/test_subscription_lifecycle_history.py",
                    "tests/integration/test_lifecycle_events_append_only_postgres.py",
                    "tests/integration/test_lifecycle_evidence_authority_migration.py",
                ),
            ),
        ),
        SOTService(
            name="access.credential_binding",
            module="app.services.access_credential_binding",
            owns=("access credential subscription and RADIUS-profile binding",),
            depends_on=(
                "access.subscription_lifecycle",
                "access.radius_projection",
                "service_intent.catalog_policy",
                "events.dispatcher",
            ),
            notes=(
                "This flush-only participant changes only the service/profile "
                "binding of one existing active credential. Username and secret "
                "authority remain unchanged; RADIUS tables remain projections."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=(
                            "access credential subscription and RADIUS-profile binding"
                        ),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "canonical subscriber access credential",
                            "canonical subscription lifecycle state",
                            "catalog-linked target RADIUS profile",
                            "typed credential binding command evidence",
                        ),
                        canonical_writer="access.credential_binding",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical subscriber access credential",
                        owner="access.radius_projection",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Active AccessCredential identity, subscriber, username, "
                            "and secret state"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscription lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Locked Subscription identity, subscriber, and status",
                    ),
                    AuthorityInput(
                        name="catalog-linked target RADIUS profile",
                        owner="service_intent.catalog_policy",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Exact active subscription override or unambiguous active "
                            "OfferRadiusProfile link"
                        ),
                    ),
                    AuthorityInput(
                        name="typed credential binding command evidence",
                        owner="access.credential_binding",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Exact credential, subscriber, target subscription, and "
                            "profile identifiers admitted only inside the active "
                            "subscription-correction owner command"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "The subscription-correction coordinator owns completion; "
                        "this participant locks, stages an event, and flushes only."
                    ),
                    locking=(
                        "Lock the selected AccessCredential after the coordinator "
                        "locks both subscription rows in stable UUID order."
                    ),
                    idempotency=(
                        "Assigning the same target subscription and profile is a "
                        "deterministic no-drift write and emits the reviewed evidence "
                        "only within the parent correction."
                    ),
                    retries=(
                        "Retry only through the parent correction idempotency key; "
                        "missing, inactive, or changed binding evidence fails closed."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "access.credential_binding.coordinator_required",
                        "access.credential_binding.credential_missing",
                        "access.credential_binding.account_mismatch",
                        "access.credential_binding.radius_profile_inactive",
                    ),
                    mapping_owner="admin catalog subscription correction adapter",
                    fail_closed_on=(
                        "missing or inactive credential",
                        "subscriber mismatch",
                        "missing or inactive target profile",
                        "call outside the named coordinator transaction",
                    ),
                ),
                events=EventContract(
                    event_types=("access_credential.binding_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 contains credential and binding identifiers only; "
                        "it never contains the credential secret."
                    ),
                    replay=(
                        "The parent correction idempotency reservation reproduces the "
                        "same binding outcome without a second transition."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    new_owner="access.credential_binding",
                    old_owner=(
                        "generic AccessCredentials.update followed by an uncommitted "
                        "best-effort RADIUS synchronization"
                    ),
                    verification=(
                        "Correction behavior and architecture tests prove exact binding, "
                        "flush-only transaction participation, and secret-safe events."
                    ),
                    cutover_gate=(
                        "Mistaken-subscription repair uses only this typed participant."
                    ),
                    fallback_retirement=(
                        "The correction UI and coordinator never call generic credential "
                        "CRUD or write AccessCredential binding fields directly."
                    ),
                ),
                steward="network access",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_subscription_correction.py",
                    "tests/architecture/test_subscription_correction_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="access.subscription_correction",
            module="app.services.subscription_correction",
            owns=("atomic mistaken-subscription correction coordination",),
            depends_on=(
                "access.subscription_lifecycle",
                "access.credential_binding",
                "access.fup_runtime_state",
                "access.radius_projection",
                "financial.invoices",
                "service_intent.catalog_policy",
                "events.dispatcher",
            ),
            notes=(
                "The reviewed coordinator never guesses the correct plan and never "
                "hard-deletes history. It fails closed on billing history or ambiguous "
                "credential/profile evidence, active target locks, or malformed legacy "
                "served-IP projection evidence, then commits lifecycle, binding, FUP, "
                "and durable event evidence once. It validates but never writes IP "
                "projection fields."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="atomic mistaken-subscription correction coordination",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "canonical subscription lifecycle state",
                            "canonical access credential binding",
                            "canonical FUP runtime state",
                            "canonical invoice-line history",
                            "catalog-linked target RADIUS profile",
                            "reviewed correction preview",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical subscription lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Locked mistaken and target Subscription rows and locks; "
                            "legacy served-IP scalars are checked only as non-authoritative "
                            "resume-provisioning evidence pending explicit IPv6 projection "
                            "ownership"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical access credential binding",
                        owner="access.credential_binding",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Exactly one active subscriber credential and its current "
                            "subscription/profile binding"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical FUP runtime state",
                        owner="access.fup_runtime_state",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Per-subscription FupState rows and active lock evidence",
                    ),
                    AuthorityInput(
                        name="canonical invoice-line history",
                        owner="financial.invoices",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Any InvoiceLine record linked to the mistaken subscription"
                        ),
                    ),
                    AuthorityInput(
                        name="catalog-linked target RADIUS profile",
                        owner="service_intent.catalog_policy",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Exact active subscription override or one active offer profile"
                        ),
                    ),
                    AuthorityInput(
                        name="reviewed correction preview",
                        owner="access.subscription_correction",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Server-calculated fingerprint over both subscriptions, "
                            "credential, profile, FUP, locks, and invoice evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "The public command enters execute_owner_command once on a "
                        "transaction-free session; lifecycle, credential-binding, and "
                        "FUP participants flush and the coordinator commits once."
                    ),
                    locking=(
                        "Lock both Subscription rows in stable UUID order, then the one "
                        "active credential and FUP states; revalidate the preview before writes."
                    ),
                    idempotency=(
                        "A scoped idempotency key stores the reviewed preview fingerprint; "
                        "same-input retry returns the original binding while key reuse with "
                        "different evidence fails closed."
                    ),
                    retries=(
                        "Preview drift and business ambiguity require operator re-review; "
                        "transient transaction failures may retry with the same key."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "access.subscription_correction.invalid_active_subscription_id",
                        "access.subscription_correction.invalid_target_subscription_id",
                        "access.subscription_correction.invalid_idempotency_key",
                        "access.subscription_correction.same_subscription",
                        "access.subscription_correction.subscription_not_found",
                        "access.subscription_correction.account_mismatch",
                        "access.subscription_correction.active_subscription_required",
                        "access.subscription_correction.target_not_restorable",
                        "access.subscription_correction.target_not_prior",
                        "access.subscription_correction.billing_approval_required",
                        "access.subscription_correction.account_lifecycle_override",
                        "access.subscription_correction.financial_history_present",
                        "access.subscription_correction.credential_missing",
                        "access.subscription_correction.credential_ambiguous",
                        "access.subscription_correction.credential_binding_conflict",
                        "access.subscription_correction.credential_username_missing",
                        "access.subscription_correction.target_login_missing",
                        "access.subscription_correction.credential_target_login_mismatch",
                        "access.subscription_correction.credential_active_login_mismatch",
                        "access.subscription_correction.radius_profile_missing",
                        "access.subscription_correction.radius_profile_ambiguous",
                        "access.subscription_correction.radius_profile_inactive",
                        "access.subscription_correction.radius_profile_speed_invalid",
                        (
                            "access.subscription_correction."
                            "radius_profile_speed_unconfigured"
                        ),
                        "access.subscription_correction.active_ipv4_invalid",
                        "access.subscription_correction.active_ipv6_invalid",
                        "access.subscription_correction.target_ipv4_invalid",
                        "access.subscription_correction.target_ipv6_invalid",
                        (
                            "access.subscription_correction."
                            "target_enforcement_lock_present"
                        ),
                        "access.subscription_correction.preview_changed",
                        "access.subscription_correction.correction_ineligible",
                        "access.subscription_correction.correction_not_applied",
                        "access.subscription_correction.idempotency_conflict",
                        "access.subscription_correction.replay_state_missing",
                    )
                    + owner_command_boundary_error_codes(
                        "access.subscription_correction"
                    ),
                    mapping_owner="admin catalog subscription correction adapter",
                    fail_closed_on=(
                        "changed preview evidence",
                        "any existing invoice line",
                        "ambiguous credential or target profile",
                        "missing or mismatched PPPoE identity",
                        "unconfigured target speed or malformed served-IP projection",
                        "active enforcement lock on the target subscription",
                        "account/subscription mismatch or lifecycle override",
                    ),
                ),
                events=EventContract(
                    event_types=("subscription.correction_applied",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 names the replaced and restored subscriptions, "
                        "credential/profile identifiers, cleared FUP scopes, and preview "
                        "fingerprint without secrets or customer identity data."
                    ),
                    replay=(
                        "The scoped idempotency record prevents duplicate correction; "
                        "lifecycle and RADIUS reconcilers remain independently idempotent."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    new_owner="access.subscription_correction",
                    old_owner=(
                        "separate admin cancel/restore clicks plus manual credential, "
                        "profile, FUP, and RADIUS repair"
                    ),
                    verification=(
                        "Focused preview, transaction rollback, idempotency, UI, event, "
                        "and architecture tests plus staging acceptance."
                    ),
                    cutover_gate=(
                        "The correction action appears only for an active subscription "
                        "with an explicit restorable sibling and executes this owner; "
                        "generic restore previews reject a same-login active sibling."
                    ),
                    fallback_retirement=(
                        "No correction route performs direct ORM writes or generic CRUD; "
                        "financially linked subscriptions remain blocked for finance review."
                    ),
                ),
                steward="network access and billing operations",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                ),
                test_refs=(
                    "tests/test_subscription_correction.py",
                    "tests/test_subscription_lifecycle_ui.py",
                    "tests/playwright/e2e/test_subscription_correction.py",
                    "tests/architecture/test_subscription_correction_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="access.event_policy",
            module="app.services.enforcement_event_policy",
            owns=(
                "event-driven enforcement feature policy",
                "FUP enforcement action settings",
            ),
            depends_on=(
                "access.fup_enforcement_sweep",
                "control.settings_spec",
            ),
            notes=(
                "Resolves canonical settings and typed usage-exhausted action "
                "evidence. Invoice-overdue events are observations only; the "
                "financial dunning owner decides their consequences."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="event-driven enforcement feature policy",
                        role=OwnerRole.EVENT_POLICY,
                        input_names=("canonical RADIUS event settings",),
                    ),
                    ConcernContract(
                        name="FUP enforcement action settings",
                        role=OwnerRole.EVENT_POLICY,
                        input_names=(
                            "canonical FUP event settings",
                            "usage-exhausted action evidence",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical RADIUS event settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed radius.group_routing_enabled and "
                            "radius.refresh_sessions_on_profile_change values"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical FUP event settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed usage.fup_action and "
                            "usage.fup_throttle_radius_profile_id values"
                        ),
                    ),
                    AuthorityInput(
                        name="usage-exhausted action evidence",
                        owner="access.fup_enforcement_sweep",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "versioned usage.exhausted event action emitted from the "
                            "canonical FUP sweep decision"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Caller creates and closes the session; typed policy "
                        "resolvers read canonical settings and perform no writes or "
                        "transaction completion."
                    ),
                    locking=(
                        "No row lock; each decision reflects the canonical settings "
                        "snapshot visible to the caller transaction."
                    ),
                    idempotency=(
                        "The same typed action evidence and visible settings snapshot "
                        "produce the same policy outcome."
                    ),
                    retries=(
                        "Adapters may retry transient setting reads. Invalid or "
                        "incomplete policy evidence is terminal until corrected."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "access.event_policy.invalid_boolean_setting",
                        "access.event_policy.invalid_requested_fup_action",
                        "access.event_policy.invalid_configured_fup_action",
                        "access.event_policy.throttle_profile_required",
                        "access.event_policy.invalid_throttle_profile_id",
                        "access.event_policy.invalid_throttle_decision",
                        "access.event_policy.subscription_required",
                    ),
                    mapping_owner=("event dispatcher and RADIUS projection adapters"),
                    fail_closed_on=(
                        "invalid event action evidence",
                        "invalid canonical boolean or action settings",
                        # An ABSENT global throttle profile is not fail-closed:
                        # it is the fallback, and the per-subscriber derived
                        # profile is the primary path. Only an invalid value,
                        # or a throttle that can be derived from neither, fails.
                        "invalid throttle RADIUS profile evidence",
                        "no derivable throttle rate and no configured fallback",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "string-based enforcement helpers with duplicated setting "
                        "defaults and silent missing-profile no-op behavior"
                    ),
                    new_owner="access.event_policy",
                    verification=(
                        "Typed policy, invalid-input, missing-profile, caller, and "
                        "architecture tests."
                    ),
                    cutover_gate=(
                        "Enforcement event and RADIUS projection callers consume "
                        "typed decisions only."
                    ),
                    fallback_retirement=(
                        "Primitive helper returns, duplicated defaults, permissive "
                        "invalid-action fallback, and silent throttle no-op are "
                        "removed."
                    ),
                ),
                steward="network access",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_enforcement_event_policy.py",
                    "tests/test_events_enforcement_services.py",
                    "tests/test_radius_shadow_handler_integration.py",
                    "tests/architecture/test_enforcement_event_policy_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="access.walled_garden_policy",
            module="app.services.walled_garden_policy",
            owns=(
                "captive account eligibility",
                "captive network readiness",
                "effective hard-reject/captive restriction",
                "most-restrictive-active-lock resolution",
            ),
            depends_on=(
                "access.subscription_lifecycle",
                "control.settings_spec",
                "customer.accounts",
                "customer.identity_scope",
            ),
            notes=(
                "Hard reject is the fail-closed default. Captive access "
                "requires explicit account opt-in, eligible direct-house "
                "residential scope, ready network settings, and no more-"
                "restrictive active lock."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="captive account eligibility",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical subscriber access identity",
                            "canonical reseller scope",
                            "captive restriction protocol",
                        ),
                    ),
                    ConcernContract(
                        name="captive network readiness",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical captive network settings",
                            "captive restriction protocol",
                        ),
                    ),
                    ConcernContract(
                        name="effective hard-reject/captive restriction",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical subscriber access identity",
                            "canonical reseller scope",
                            "canonical captive network settings",
                            "canonical enforcement locks",
                            "captive restriction protocol",
                        ),
                    ),
                    ConcernContract(
                        name="most-restrictive-active-lock resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical subscription lifecycle state",
                            "canonical enforcement locks",
                            "captive restriction protocol",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical subscriber access identity",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Subscriber user type, active and lifecycle state, "
                            "explicit category evidence, and captive opt-in"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical reseller scope",
                        owner="customer.identity_scope",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active Reseller relationship with explicit direct-house "
                            "classification"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical captive network settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed radius.captive_redirect_enabled, captive_portal_ip, "
                            "and captive_portal_url values"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscription lifecycle state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Subscription identity, account, and lifecycle status",
                    ),
                    AuthorityInput(
                        name="canonical enforcement locks",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("active per-subscription EnforcementLock access modes"),
                    ),
                    AuthorityInput(
                        name="captive restriction protocol",
                        owner="access.walled_garden_policy",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed reason vocabulary, eligibility statuses, HTTPS "
                            "portal requirement, and most-restrictive-wins ordering"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Caller creates and closes the session; the policy reads "
                        "canonical account, lock, and setting evidence without writes "
                        "or transaction completion."
                    ),
                    locking=(
                        "No row lock; callers requiring a command decision lock source "
                        "state before invoking the policy. Read projections reflect the "
                        "visible active-lock snapshot."
                    ),
                    idempotency=(
                        "The same requested mode and visible account, reseller, lock, "
                        "and network-setting snapshot produce the same typed decision."
                    ),
                    retries=(
                        "Transient reads may be retried. Missing, stale, invalid, or "
                        "ambiguous captive evidence deterministically resolves to hard "
                        "reject and needs no retry."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner=(
                        "financial, lifecycle, event, RADIUS, and status adapters"
                    ),
                    fail_closed_on=(
                        "missing explicit residential or direct-house evidence",
                        "inactive or ineligible account scope",
                        "disabled or invalid captive network settings",
                        "terminal subscription state or ambiguous active locks",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "raw captive account flags and caller-local restriction "
                        "interpretation"
                    ),
                    new_owner="access.walled_garden_policy",
                    verification=(
                        "Eligibility, opt-in, network readiness, terminal status, "
                        "most-restrictive-lock, RADIUS projection, and architecture "
                        "tests."
                    ),
                    cutover_gate=(
                        "Financial, event, RADIUS, connectivity, and service-status "
                        "callers consume the canonical typed decision."
                    ),
                    fallback_retirement=(
                        "Raw opt-in authority, caller-local captive readiness, and "
                        "permissive missing-evidence fallbacks are removed."
                    ),
                ),
                steward="network access",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/audits/BILLING_SOT_AUDIT_2026-07-12.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_walled_garden_policy.py",
                    "tests/test_radius_shadow_handler_integration.py",
                    "tests/architecture/test_grace_walled_garden_ownership.py",
                    "tests/architecture/test_walled_garden_policy_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="access.radius_state",
            module="app.services.radius_access_state",
            owns=("pure desired RADIUS access-state mapping",),
            depends_on=(
                "access.subscription_lifecycle",
                "access.walled_garden_policy",
                "financial.access_resolution",
            ),
            notes=(
                "Maps canonical lifecycle status and effective restriction to an "
                "AccessState. It writes neither lifecycle rows nor external RADIUS."
            ),
        ),
        SOTService(
            name="access.radius_reject",
            module="app.services.radius_reject",
            owns=("reject address allocation", "reject IP lifecycle"),
            depends_on=("access.radius_state",),
        ),
        SOTService(
            name="access.radius_target_registry",
            module="app.services.external_radius_targets",
            owns=(
                "configured external RADIUS database target selection",
                "per-target capability and schema configuration",
                "legacy environment bootstrap and cutover verification",
            ),
            depends_on=("control.settings_spec", "runtime.db_sessions"),
            notes=(
                "Active RadiusSyncJob and encrypted ConnectorConfig rows are "
                "the runtime authority. The environment DSN is bootstrap and "
                "cutover-shadow input only, never a runtime fallback."
            ),
        ),
        SOTService(
            name="access.radius_projection",
            module="app.services.radius_population",
            owns=(
                "canonical per-login RADIUS projection plan",
                "radcheck/radreply/radusergroup customer projection",
                "radcheck_admin/radreply_admin device-login projection",
                "idempotent per-target advisory-locked RADIUS auth projection",
                "credential-independent hard-reject projection",
                "unbuildable active/captive login classification and preservation",
                "secret-safe exact RADIUS-row projection fingerprint",
                "walled-garden/reject radreply on blocked/suspended access",
                "RADIUS simultaneous-session check/control placement and cutover",
                "bidirectional desired-versus-observed projection drift",
            ),
            depends_on=(
                "access.subscription_lifecycle",
                "access.radius_state",
                "access.radius_reject",
                "access.radius_target_registry",
                "control.settings_spec",
            ),
            notes=(
                "Single writer of the FreeRADIUS auth tables across every "
                "configured runtime target. Event-time and per-user callers "
                "request a full or scoped projection; they do not write auth "
                "tables directly. The permanent account-access reconciler is the "
                "only periodic drift detector and requests the full writer only "
                "when drift exists; the writer is never independently scheduled. "
                "Hard reject does not depend on a recoverable "
                "customer password. An active/captive login that cannot be built "
                "is preserved and reported, never treated as a successful refresh. "
                "The writer and reconciler consume the same exact, secret-safe "
                "per-login fingerprint and therefore cannot reinterpret lifecycle "
                "statuses or silently ignore attribute drift. "
                "Simultaneous-Use is projected to radcheck only after the "
                "database-owned cutover gate is enabled; radacct remains observed "
                "session evidence and never owns credential or service identity."
            ),
        ),
        SOTService(
            name="access.session_enforcement",
            module="app.services.enforcement",
            owns=(
                "typed access-state CoA/disconnect execution",
                "NAS-evidenced accounting-session closure",
                "single-flight access-control recovery execution",
            ),
            depends_on=(
                "access.radius_projection",
                "access.radius_state",
                "sessions.radius_resolution",
            ),
            notes=(
                "Disconnect ACK, RFC 5176 session-not-found, rejection, timeout "
                "and configuration failure remain distinct outcomes. Accounting "
                "closes only when the NAS explicitly reports that the session "
                "context is absent. Exact-old-IP projection repair issues one "
                "disconnect and bounded-polls authoritative radacct for up to "
                "15 seconds; it does not fall back to the lagging imported "
                "accounting mirror, and polling never sends a second customer "
                "interruption. "
                "The periodic recovery loop is single-flight and caps attempts "
                "rather than successes."
            ),
        ),
        SOTService(
            name="access.fup_rule_engine",
            module="app.services.fup",
            owns=(
                "FUP policy and rule definitions (CRUD)",
                "FUP rule evaluation and simulation",
            ),
            depends_on=(
                "access.fup_usage_windows",
                "auth.permission_gate",
                "events.dispatcher",
                "service_intent.catalog_policy",
            ),
            notes=(
                "Canonical policy/rule definitions and a side-effect-free "
                "decision engine shared by enforcement and simulation. It "
                "never writes per-subscription enforcement state."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="FUP policy and rule definitions (CRUD)",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authenticated FUP policy command context",
                            "canonical catalog offer",
                            "FUP policy mutation protocol",
                        ),
                        canonical_writer="access.fup_rule_engine",
                    ),
                    ConcernContract(
                        name="FUP rule evaluation and simulation",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical FUP policy and rule definitions",
                            "period-scoped FUP usage observations",
                            "FUP rule evaluation protocol",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authenticated FUP policy command context",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "authenticated actor, offer scope, reason, command, and "
                            "correlation identifiers"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical catalog offer",
                        owner="service_intent.catalog_policy",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="locked CatalogOffer identity and lifecycle row",
                    ),
                    AuthorityInput(
                        name="canonical FUP policy and rule definitions",
                        owner="access.fup_rule_engine",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "FupPolicy and ordered FupRule rows including windows, "
                            "thresholds, chaining, cooldown, action, and active state"
                        ),
                    ),
                    AuthorityInput(
                        name="period-scoped FUP usage observations",
                        owner="access.fup_usage_windows",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "daily, weekly, and monthly usage windows with source and "
                            "authority evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="FUP policy mutation protocol",
                        owner="access.fup_rule_engine",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed policy/rule commands, positive thresholds, enum, "
                            "day, cooldown, reduction, ordering, and chain invariants"
                        ),
                    ),
                    AuthorityInput(
                        name="FUP rule evaluation protocol",
                        owner="access.fup_rule_engine",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "ordered rule activation, prerequisite, time/day window, "
                            "period usage, threshold, severity, and simulation rules"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each typed FUP policy mutation enters one verified owner "
                        "transaction; evaluation and simulation are read-only."
                    ),
                    locking=(
                        "Policy creation locks CatalogOffer; rule commands lock the "
                        "policy, target rule, and prerequisite rules before mutation."
                    ),
                    idempotency=(
                        "Ensure-policy replays to the offer's existing policy; other "
                        "mutations require explicit source identifiers and command "
                        "evidence."
                    ),
                    retries=(
                        "Validation failures are terminal. Concurrency failures retry "
                        "the complete typed command with the original context."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "access.fup_rule_engine.offer_not_found",
                        "access.fup_rule_engine.policy_not_found",
                        "access.fup_rule_engine.source_policy_not_found",
                        "access.fup_rule_engine.rule_not_found",
                        "access.fup_rule_engine.invalid_rule",
                        "access.fup_rule_engine.invalid_rule_chain",
                        "access.fup_rule_engine.invalid_command_context",
                        "access.fup_rule_engine.command_contract_violation",
                        "access.fup_rule_engine.nested_owner_command",
                        "access.fup_rule_engine.active_caller_transaction",
                        "access.fup_rule_engine.nested_transaction_completion",
                    ),
                    mapping_owner="app.web.admin.catalog",
                    fail_closed_on=(
                        "missing offer, policy, rule, or prerequisite",
                        "invalid enum, threshold, reduction, day, cooldown, or chain",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=("fup_policy.changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and carries action, policy, offer, "
                        "optional rule, command, and correlation identifiers."
                    ),
                    replay=(
                        "Canonical FupPolicy and ordered FupRule rows rebuild current "
                        "policy; events retain mutation evidence."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "FupPolicies helpers with transport exceptions, generic "
                        "attribute mutation, helper commits, and GET-time policy writes"
                    ),
                    new_owner="access.fup_rule_engine",
                    verification=(
                        "Typed command, clean adapter, lock, event, rollback, no-GET-"
                        "write, evaluation, simulation, and architecture tests."
                    ),
                    cutover_gate=(
                        "Admin mutations construct typed commands on clean sessions and "
                        "all enforcement/simulation callers share evaluate_rules."
                    ),
                    fallback_retirement=(
                        "Service HTTP exceptions, generic setattr, helper commits, "
                        "untyped kwargs writers, and get_or_create reads are removed."
                    ),
                ),
                steward="network access",
                design_refs=(
                    "docs/designs/FUP_CONSUMPTION_WINDOWS.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                ),
                test_refs=(
                    "tests/test_fup_ui_gaps.py",
                    "tests/test_fup_period_aware_evaluation.py",
                    "tests/test_fup_submonthly_safeguards.py",
                    "tests/architecture/test_fup_rule_engine_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="access.fup_runtime_state",
            module="app.services.fup_state",
            owns=("FUP per-subscription runtime state rows",),
            depends_on=("events.dispatcher",),
            notes=(
                "State store only: get/apply/clear/list. Decisions live in "
                "the rule engine and the enforcement sweep."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="FUP per-subscription runtime state rows",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "canonical subscription offer state",
                            "resolved FUP enforcement consequence",
                            "applied access consequence evidence",
                        ),
                        canonical_writer="access.fup_runtime_state",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical subscription offer state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="Subscription.id and Subscription.offer_id",
                    ),
                    AuthorityInput(
                        name="resolved FUP enforcement consequence",
                        owner="access.fup_enforcement_sweep",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "rule, action, cap-reset, and evaluation-time "
                            "evidence from the FUP sweep"
                        ),
                    ),
                    AuthorityInput(
                        name="applied access consequence evidence",
                        owner="access.session_enforcement",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "successfully applied throttle, block, suspend, "
                            "restore, or reset consequence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "FUP enforcement owners pass a typed transition. The "
                        "participant locks the Subscription and FupState rows, "
                        "then flushes state and event evidence without commit."
                    ),
                    locking=(
                        "The canonical Subscription row serializes creation and "
                        "the FupState row is selected FOR UPDATE before change."
                    ),
                    idempotency=(
                        "An exact typed transition replay is a no-op; clear is a "
                        "no-op when the runtime projection is already neutral."
                    ),
                    retries=(
                        "The surrounding enforcement owner retries the complete "
                        "consequence transaction; the participant never retries "
                        "or commits independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "access.fup_runtime_state.invalid_subscription_id",
                        "access.fup_runtime_state.invalid_evaluated_at",
                        "access.fup_runtime_state.invalid_cap_resets_at",
                        "access.fup_runtime_state.invalid_before",
                        "access.fup_runtime_state.invalid_event_evidence",
                        "access.fup_runtime_state.subscription_not_found",
                        "access.fup_runtime_state.offer_required",
                        "access.fup_runtime_state.offer_mismatch",
                        "access.fup_runtime_state.state_offer_mismatch",
                    ),
                    mapping_owner=(
                        "access.fup_enforcement_sweep and enforcement event "
                        "consequence owners"
                    ),
                    fail_closed_on=(
                        "missing or mismatched subscription/offer identity",
                        "naive evaluation or reset time",
                        "runtime state persistence failure",
                    ),
                ),
                events=EventContract(
                    event_types=("fup.runtime_state_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and contains subscription, offer, "
                        "transition, and action vocabulary without usage values."
                    ),
                    replay=(
                        "Exact transition replay is idempotent. The current row "
                        "is rebuilt from subscription, rule, usage, and applied "
                        "access consequence evidence."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="current per-subscription FUP enforcement posture",
                        input_names=(
                            "canonical subscription offer state",
                            "resolved FUP enforcement consequence",
                            "applied access consequence evidence",
                        ),
                        writer="access.fup_runtime_state",
                        freshness="FupState.last_evaluated_at",
                        stale_behavior=(
                            "Never relax access from stale state; expose the stale "
                            "projection and request enforcement reconciliation."
                        ),
                        drift_signal=(
                            "Compare runtime action/profile/reset evidence with "
                            "canonical rules, usage window, access locks, and "
                            "RADIUS projection."
                        ),
                        rebuild_operation=(
                            "Run the scoped FUP enforcement reconciliation for "
                            "the subscription and reapply or clear exact state."
                        ),
                        repair_owner="access.fup_enforcement_sweep",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "event-handler and sweep-local free-form FupState writes "
                        "with implicit wall-clock time"
                    ),
                    new_owner="access.fup_runtime_state",
                    verification=(
                        "Typed transition, locking, idempotency, atomic-event, "
                        "reset, lift, sweep, and single-writer tests."
                    ),
                    cutover_gate=(
                        "All FupState mutations pass typed commands with owner-"
                        "supplied evaluation time through the participant."
                    ),
                    fallback_retirement=(
                        "Free-form state mutation, implicit datetime.now, silent "
                        "offer mismatch, and parallel FupState writers are removed."
                    ),
                ),
                steward="network access",
                design_refs=(
                    "docs/designs/FUP_CONSUMPTION_WINDOWS.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                ),
                test_refs=(
                    "tests/test_fup_runtime_state_owner.py",
                    "tests/architecture/test_fup_runtime_state_boundary.py",
                    "tests/test_fup_lift_enforcement.py",
                    "tests/test_fup_evaluate_commits.py",
                ),
            ),
        ),
        SOTService(
            name="access.fup_throttle_rate",
            module="app.services.fup_throttle_profile",
            owns=(
                "derived FUP throttle RADIUS profiles",
                "resolved FUP throttle rate per subscription",
            ),
            depends_on=("access.fup_rule_engine",),
            notes=(
                "How hard a throttle bites. fup_rules.speed_reduction_percent "
                "is the decision; this derives the rate from the subscriber's "
                "effective profile and materialises the fup-throttle-* profile "
                "expressing it. Sole writer of those rows."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="resolved FUP throttle rate per subscription",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "FUP rule throttle depth",
                            "subscriber effective rate",
                        ),
                    ),
                    ConcernContract(
                        name="derived FUP throttle RADIUS profiles",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "FUP rule throttle depth",
                            "subscriber effective rate",
                        ),
                        canonical_writer="access.fup_throttle_rate",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="FUP rule throttle depth",
                        owner="access.fup_rule_engine",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="FupRule.speed_reduction_percent",
                    ),
                    AuthorityInput(
                        name="subscriber effective rate",
                        owner="access.session_enforcement",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "RadiusProfile.download_speed/upload_speed of the "
                            "credential-, subscription-, or offer-effective "
                            "profile"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "Runs inside the enforcement consequence transaction "
                        "and flushes without commit."
                    ),
                    locking=(
                        "None taken. The RadiusProfile.code unique constraint "
                        "serializes concurrent creation of the same rate."
                    ),
                    idempotency=(
                        "Keyed on the derived rate pair, so the same rule and "
                        "rate always resolve the same row rather than forking "
                        "the projection."
                    ),
                    retries=(
                        "The surrounding enforcement owner retries; this never "
                        "retries or commits independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "access.fup_throttle_rate.invalid_reduction_percent",
                        "access.fup_throttle_rate.invalid_full_rate",
                        "access.fup_throttle_rate.no_throttle_profile_available",
                    ),
                    mapping_owner="access.session_enforcement",
                    fail_closed_on=(
                        "a reduction percentage outside 1..99, which would "
                        "produce either a no-op or a disconnection",
                        "a non-positive rate to reduce",
                        "no derivable rate AND no configured fallback profile, "
                        "which would otherwise leave a breaching subscriber at "
                        "full speed while the sweep counted the enforcement as "
                        "done",
                    ),
                ),
                events=EventContract(
                    event_types=("fup.throttle_profile_derived",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 carries the derived profile identity and "
                        "its rate pair; no subscriber or usage values."
                    ),
                    replay=(
                        "Emitted only on first creation of a rate pair; reuse "
                        "is a read. Rows are rebuilt by deleting them and "
                        "letting enforcement recreate the rates in use."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="derived FUP throttle RADIUS profiles",
                        input_names=(
                            "FUP rule throttle depth",
                            "subscriber effective rate",
                        ),
                        writer="access.fup_throttle_rate",
                        freshness="RadiusProfile.updated_at",
                        stale_behavior=(
                            "A rate with no derived profile falls back to the "
                            "globally configured throttle profile and the "
                            "fallback is logged; enforcement is never skipped."
                        ),
                        drift_signal=(
                            "A fup-throttle-* profile whose download/upload "
                            "speed disagrees with its own code, or one edited "
                            "outside this module."
                        ),
                        rebuild_operation=(
                            "Delete the derived profiles; the next enforcement "
                            "recreates exactly the rates in use."
                        ),
                        repair_owner="access.fup_throttle_rate",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "usage.fup_throttle_radius_profile_id, one global "
                        "profile for every plan, plus a decorative "
                        "speed_reduction_percent and a never-read "
                        "usage_allowances.throttle_rate_mbps"
                    ),
                    new_owner="access.fup_throttle_rate",
                    verification=(
                        "Proportionality across tiers, reduction-is-a-cut, "
                        "floor, and rate-limit rx/tx ordering tests."
                    ),
                    cutover_gate=(
                        "Enforcement resolves the profile through this module; "
                        "the global setting is reached only as a logged "
                        "fallback."
                    ),
                    fallback_retirement=(
                        "usage_allowances.throttle_rate_mbps is dropped. The "
                        "global setting is retained deliberately, for offers "
                        "with no rate to reduce."
                    ),
                ),
                steward="network access",
                design_refs=(
                    "docs/PLAN_FAMILY_ARCHITECTURE.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_fup_free_night.py",
                    "tests/test_fup_free_night_release.py",
                ),
            ),
        ),
        SOTService(
            name="access.fup_usage_windows",
            module="app.services.fup_usage",
            owns=(
                "FUP consumption window bounds",
                "windowed FUP usage aggregation",
            ),
            depends_on=("sessions.radius_reconciliation",),
            notes=(
                "Single source of truth for FUP consumption windows and "
                "windowed usage reads; read-only over usage facts."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="FUP consumption window bounds",
                        role=OwnerRole.RESOLVER,
                        input_names=("FUP consumption period policy",),
                    ),
                    ConcernContract(
                        name="windowed FUP usage aggregation",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "FUP consumption period policy",
                            "rated quota and session usage facts",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="FUP consumption period policy",
                        owner="access.fup_usage_windows",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed period argument normalized to the supported "
                            "daily, weekly, or monthly vocabulary"
                        ),
                    ),
                    AuthorityInput(
                        name="rated quota and session usage facts",
                        owner="sessions.radius_reconciliation",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "rated QuotaBucket totals and timestamped RADIUS "
                            "usage samples"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Window resolution and aggregation read usage facts on "
                        "the caller session and never flush or complete a "
                        "transaction."
                    ),
                    locking="Read-only aggregation requires no mutation lock.",
                    idempotency=(
                        "The same period, timezone, timestamp, and usage facts "
                        "produce the same aligned window and total."
                    ),
                    retries=(
                        "Callers may retry reads; unavailable sample evidence "
                        "returns an explicit non-authoritative no-data result."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner="FUP enforcement and usage-summary adapters",
                    fail_closed_on=(
                        "missing or unavailable non-monthly usage evidence",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "call-site-local FUP period arithmetic and direct usage reads"
                    ),
                    new_owner="access.fup_usage_windows",
                    verification=(
                        "Window-boundary, authoritative-source, and no-data "
                        "behavior tests cover every supported period."
                    ),
                    cutover_gate=(
                        "FUP evaluation, usage summaries, and notifications use "
                        "the shared windowed reader."
                    ),
                    fallback_retirement=(
                        "Independent daily, weekly, and monthly window "
                        "calculations are removed from callers."
                    ),
                ),
                steward="network access",
                design_refs=(
                    "docs/designs/FUP_CONSUMPTION_WINDOWS.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_fup_window_bounds.py",
                    "tests/test_fup_usage_reader.py",
                ),
            ),
        ),
        SOTService(
            name="access.fup_enforcement_sweep",
            module="app.services.fup_enforcement",
            owns=(
                "FUP sweep enforce/warn/reset decisions",
                "FUP enforcement transition and cooldown hysteresis",
                "FUP repeat-upsell nudge policy",
                "FUP customer notification fan-out",
            ),
            depends_on=(
                "access.fup_rule_engine",
                "access.fup_runtime_state",
                "access.fup_usage_windows",
                "access.session_enforcement",
                "access.subscription_lifecycle",
                "communications.notification_service",
                "control.settings_spec",
                "events.dispatcher",
            ),
            notes=(
                "Celery tasks keep only the advisory-lock plumbing, task "
                "names, and queue chaining; the sweep owns every "
                "enforce/warn/reset/repeat-upsell decision."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="FUP sweep enforce/warn/reset decisions",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "canonical subscription offer state",
                            "canonical FUP rule decisions",
                            "period-scoped FUP usage observations",
                            "canonical FUP runtime state",
                            "FUP enforcement control settings",
                            "FUP sweep command protocol",
                        ),
                    ),
                    ConcernContract(
                        name="FUP enforcement transition and cooldown hysteresis",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical FUP rule decisions",
                            "canonical FUP runtime state",
                            "FUP sweep command protocol",
                        ),
                    ),
                    ConcernContract(
                        name="FUP repeat-upsell nudge policy",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical FUP rule decisions",
                            "canonical FUP notification history",
                            "period-scoped FUP usage observations",
                        ),
                    ),
                    ConcernContract(
                        name="FUP customer notification fan-out",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "resolved FUP enforcement decision",
                            "FUP communication channel policy",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical subscription offer state",
                        owner="access.subscription_lifecycle",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked Subscription identity, subscriber, offer, and "
                            "lifecycle state"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical FUP rule decisions",
                        owner="access.fup_rule_engine",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "ordered rule evaluation including action, threshold, "
                            "window, usage authority, reset, and cooldown evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="period-scoped FUP usage observations",
                        owner="access.fup_usage_windows",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "metering-owned current QuotaBucket plus daily, weekly, "
                            "and monthly usage-window evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical FUP runtime state",
                        owner="access.fup_runtime_state",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "locked per-subscription action, active rule, cooldown, "
                            "and cap-reset state"
                        ),
                    ),
                    AuthorityInput(
                        name="FUP enforcement control settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "usage warning enablement and thresholds plus canonical "
                            "throttle RADIUS profile configuration"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical FUP notification history",
                        owner="communications.notification_service",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "prior fup_throttled, fup_blocked, and repeat-upsell "
                            "notification evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="FUP communication channel policy",
                        owner="communications.notification_service",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "customer eligibility, recipient, template, channel, and "
                            "suppression policy"
                        ),
                    ),
                    AuthorityInput(
                        name="resolved FUP enforcement decision",
                        owner="access.fup_enforcement_sweep",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "enforce, warn, reset, notify, or repeat-upsell consequence "
                            "selected for one locked subscription"
                        ),
                    ),
                    AuthorityInput(
                        name="FUP sweep command protocol",
                        owner="access.fup_enforcement_sweep",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed per-subscription command, source, actor, evaluation "
                            "time, command, and correlation evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "Discovery is read-only; each candidate enters one verified "
                        "per-subscription coordinator transaction containing decision, "
                        "locked runtime evidence, any participant write, event, and "
                        "communication intent."
                    ),
                    locking=(
                        "Each command locks Subscription and existing FupState before "
                        "evaluating transition, cooldown, warning, or reset state."
                    ),
                    idempotency=(
                        "State/status comparisons suppress repeat transitions; cooldown "
                        "and notification history bound reassertion and upsell frequency; "
                        "expired lifts are idempotent."
                    ),
                    retries=(
                        "One failed subscription rolls back independently and can retry "
                        "with its correlation evidence; earlier subscription commands "
                        "remain committed."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "access.fup_enforcement_sweep.invalid_source",
                        "access.fup_enforcement_sweep.subscription_not_found",
                        "access.fup_enforcement_sweep.invalid_command_context",
                        "access.fup_enforcement_sweep.command_contract_violation",
                        "access.fup_enforcement_sweep.nested_owner_command",
                        "access.fup_enforcement_sweep.active_caller_transaction",
                        "access.fup_enforcement_sweep.nested_transaction_completion",
                    ),
                    mapping_owner="app.tasks.usage",
                    retryable_codes=(),
                    fail_closed_on=(
                        "missing locked subscription or conflicting runtime state",
                        "missing authoritative usage bucket or non-authoritative window",
                        "missing throttle profile for a reduce-speed decision",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=("usage.exhausted",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and carries subscription, offer, rule, "
                        "action, usage, threshold, and cap-reset evidence."
                    ),
                    replay=(
                        "FupPolicy/FupRule, metered usage, FupState, enforcement locks, "
                        "and notification intents reconstruct decisions and outcomes."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "one service-managed loop with implicit session lifecycle, "
                        "quota creation, direct commits, split notification commits, "
                        "and reset decisions in the Celery task"
                    ),
                    new_owner="access.fup_enforcement_sweep",
                    verification=(
                        "Per-subscription command, clean discovery, no quota write, "
                        "hysteresis, missing-profile, notification, reset, rollback, "
                        "task-adapter, and architecture tests."
                    ),
                    cutover_gate=(
                        "Tasks retain only advisory-lock/session plumbing and invoke "
                        "typed sweep requests; all decisions enter owner commands."
                    ),
                    fallback_retirement=(
                        "Direct service/task commits, helper rollbacks, quota-bucket "
                        "creation, split notification commits, and task-side reset loops "
                        "are removed."
                    ),
                ),
                steward="network access",
                design_refs=(
                    "docs/designs/FUP_CONSUMPTION_WINDOWS.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                ),
                test_refs=(
                    "tests/test_fup_evaluate_commits.py",
                    "tests/test_fup_enforcement_hardening.py",
                    "tests/test_fup_hysteresis.py",
                    "tests/test_fup_notifications.py",
                    "tests/architecture/test_fup_enforcement_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.services.events.handlers.enforcement",
        "app.tasks.enforcement",
        "app.services.collections.*",
        "app.services.usage",
    ),
    rule="Billing, FUP, and admin actions resolve the desired access outcome "
    "once, map it to RADIUS state once, then let enforcement apply the "
    "network-side change.",
)
