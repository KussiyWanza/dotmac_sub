"""Canonical SOT declarations for the party_identity domain."""

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
    domain="party_identity",
    services=(
        SOTService(
            name="party.registry",
            module="app.services.party",
            owns=(
                "native person and organization party identity",
                "party data classification and quarantine",
                "party merge policy and canonical redirect",
                "external identity-reference provenance",
                "concurrent party role lifecycle",
                "reseller versus partner role contract",
                "partner agreement type vocabulary",
                "directional person and organization relationships",
                "relationship type and effective-date contract",
                "person-to-organization membership lifecycle",
                "bounded organization membership access scope",
                "canonical party contact-point lifecycle",
                "contact-point verification and consent evidence",
                "provider-scoped immutable social contact identity",
                "subscriber-account canonical party binding",
                "organization role-profile canonical party binding",
                "native Vendor and FieldVendor paired party binding",
                "SystemUser principal to Person Party binding",
                "ResellerUser Person and reseller membership binding",
                "organization membership canonical context binding",
                "FieldVendorUser explicit vendor membership binding",
                "SubscriberContact canonical Person Party binding",
                "reviewed SubscriberContact relationship projection",
                "reviewed SubscriberContact source-field contact-point projection",
            ),
            depends_on=("auth.subscriber_assignments", "auth.permission_gate"),
            notes=(
                "One native owner keeps identity, roles, descriptive "
                "relationships, memberships, and contact evidence coherent. "
                "A reseller is a specific commercial channel role; a partner "
                "is an explicitly typed collaboration agreement with no "
                "implicit permission. CRM identifiers are import provenance "
                "only. Migrations 339 through 344 are additive foundations; "
                "the subscriber binding is nullable and existing domain "
                "reads cut over only in later verified slices."
            ),
        ),
        SOTService(
            name="party.identity_audit",
            module="app.services.party_identity_audit",
            owns=(
                "read-only subscriber identity cleanup classification",
                "duplicate candidate evidence grouping",
                "subscriber cleanup worklist contract",
            ),
            depends_on=(
                "party.registry",
                "sales.service",
                "sales.orders",
                "access.subscription_lifecycle",
                "operations.provisioning_workflow",
                "financial.invoices",
                "financial.payments",
                "support.ticket_lifecycle",
            ),
            notes=(
                "Observes native Sub facts and produces private UUID-only "
                "artifacts. It never writes source state, calls CRM, or "
                "authorizes an automatic merge."
            ),
        ),
        SOTService(
            name="party.identity_adjudication",
            module="app.services.party_identity_adjudication",
            owns=(
                "reviewed subscriber identity decision contract",
                "medium and high duplicate adjudication closure",
                "Party backfill dry-run plan digest",
                "PII-free Party backfill plan artifact contract",
            ),
            depends_on=("party.identity_audit", "party.registry"),
            notes=(
                "Validates explicit decisions against current audit and row "
                "digests, then produces a non-executable plan. It has no DB "
                "writer or apply mode and never authorizes automatic merge."
            ),
        ),
        SOTService(
            name="party.identity_backfill_executor",
            module="app.services.party_identity_backfill",
            owns=(
                "approved Subscriber Party backfill execution gate",
                "Party identity backfill execution receipt",
                "Party identity backfill idempotent replay verification",
            ),
            depends_on=(
                "party.identity_audit",
                "party.identity_adjudication",
                "party.registry",
            ),
            notes=(
                "Consumes one exact, expiring, separately approved plan in a "
                "SERIALIZABLE transaction and calls party.registry for "
                "predetermined Party creation and Subscriber binding. It "
                "records a PII-free receipt, never commits inside the owner, "
                "and cannot merge, repoint, assign roles, copy contacts, or "
                "change lifecycle, billing, access, or authorization state."
            ),
        ),
        SOTService(
            name="party.organization_profile_audit",
            module="app.services.party_organization_audit",
            owns=(
                "read-only organization role-profile convergence audit",
                "Vendor and FieldVendor bridge debt classification",
                "organization profile Party-role coverage report",
            ),
            depends_on=("party.registry",),
            notes=(
                "Reports aggregate schema, binding, role-coverage, and "
                "Vendor/FieldVendor bridge counts without identity values. "
                "It never binds a profile, assigns a role, repairs a twin, "
                "calls CRM, or changes a legacy read path."
            ),
        ),
        SOTService(
            name="party.principal_context_audit",
            module="app.services.party_principal_audit",
            owns=(
                "read-only Person principal convergence audit",
                "reseller and organization membership context audit",
                "FieldVendorUser vendor context debt classification",
            ),
            depends_on=(
                "party.registry",
                "auth.subscriber_assignments",
                "auth.permission_gate",
            ),
            notes=(
                "Reports aggregate schema, principal-binding, membership-"
                "context, and field-vendor-user counts without identity values. "
                "It never binds a principal, creates or activates a membership, "
                "changes a credential or permission, calls CRM, or changes a "
                "login/read path."
            ),
        ),
        SOTService(
            name="party.subscriber_binding_repair",
            module="app.services.subscriber_party_binding_repair",
            owns=("reviewed single-subscriber Party binding repair",),
            depends_on=(
                "party.registry",
                "auth.permission_gate",
                "observability.audit_log",
            ),
            notes=(
                "Applies one attributable administrator's reviewed choice for an "
                "unbound Subscriber. It may bind one exact existing Party or create "
                "one explicitly named Party, but never infers identity, repoints, "
                "copies contacts, changes lifecycle, or grants access."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="reviewed single-subscriber Party binding repair",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "attributable reviewed binding decision",
                            "canonical Subscriber account state",
                            "canonical Party identity",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="attributable reviewed binding decision",
                        owner="party.subscriber_binding_repair",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed administrator command with explicit Party choice "
                            "or explicit Party type/name, review evidence and "
                            "correlation evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Subscriber account state",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="exact Subscriber row locked by UUID",
                    ),
                    AuthorityInput(
                        name="canonical Party identity",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "reviewed existing Party or explicit Party creation and "
                            "guarded Subscriber.party_id binding"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "The public command locks the selected Party before the "
                        "Subscriber where applicable, delegates native writes to "
                        "party.registry, stages PII-free audit evidence and commits "
                        "once before returning."
                    ),
                    locking=(
                        "Lock the exact existing Party before the Subscriber; Party "
                        "creation and Subscriber binding occur in one owner command."
                    ),
                    idempotency=(
                        "An exact existing-Party command replays only with the same "
                        "Party and complete evidence. Existing bindings to another "
                        "Party fail closed; created-Party commands never replay into "
                        "a second Party."
                    ),
                    retries=(
                        "Retry only after rereading the customer binding. A changed "
                        "or already bound customer requires the reviewed merge/repoint "
                        "workflow."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "party.subscriber_binding_repair.invalid_command",
                        "party.subscriber_binding_repair.subscriber_not_found",
                        "party.subscriber_binding_repair.party_binding_refused",
                        *owner_command_boundary_error_codes(
                            "party.subscriber_binding_repair"
                        ),
                    ),
                    mapping_owner="app.web.admin.customers",
                    fail_closed_on=(
                        "unattributable administrator",
                        "missing or unavailable Party",
                        "missing Subscriber",
                        "incomplete review evidence",
                        "existing or conflicting Party binding",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner="unbound Subscriber rows with no runtime repair path",
                    new_owner="party.subscriber_binding_repair",
                    verification=(
                        "Focused command, audit, exact-replay, repoint-refusal and "
                        "admin action tests."
                    ),
                    cutover_gate=(
                        "The customer quote picker consumes only complete reviewed "
                        "Party bindings and focused repair evidence remains green."
                    ),
                    fallback_retirement=(
                        "No direct Subscriber.party_id writer or UI fallback is "
                        "introduced; the existing backfill executor remains available "
                        "for approved batch work."
                    ),
                ),
                steward="identity and customer operations",
                design_refs=(
                    "docs/PARTY_ROLE_RELATIONSHIP_SOT.md",
                    "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=("tests/test_subscriber_party_binding_repair.py",),
            ),
        ),
        SOTService(
            name="party.staff_principal_adoption",
            module="app.services.staff_party_adoption",
            owns=("existing staff Party principal adoption",),
            depends_on=(
                "party.registry",
                "auth.staff_provisioning",
                "app_sessions.auth",
                "observability.audit_log",
            ),
            notes=(
                "Consumes one exact approved UUID-only decision and delegates "
                "the native SystemUser link to party.registry. It creates no "
                "Party, selects no identity, writes no credential projection, "
                "and changes no login, role, permission or active state."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="existing staff Party principal adoption",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "reviewed existing-staff Party binding decision",
                            "canonical staff principal state",
                            "canonical Person Party identity",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="reviewed existing-staff Party binding decision",
                        owner="party.staff_principal_adoption",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed UUID-only plan item, exact decision and approval "
                            "SHA-256 evidence, expiring approval, and attributable "
                            "user CommandContext"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff principal state",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "the exact existing SystemUser selected by reviewed UUID"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Person Party identity",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active or quarantined Person Party and guarded native "
                            "SystemUser.person_party_id binding"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "bind_existing_staff_party enters one owner transaction, "
                        "delegates the native field write to party.registry, stages "
                        "PII-free audit evidence, and commits before return."
                    ),
                    locking=(
                        "Lock the reviewed Person Party before its SystemUser to "
                        "match the credential-projection lock suffix; native Party "
                        "validation repeats under the same transaction."
                    ),
                    idempotency=(
                        "Only the exact Party, source, reason, approval digest and "
                        "complete original timestamp replay; changed evidence or a "
                        "repoint fails closed."
                    ),
                    retries=(
                        "Retry the exact typed command after a transient database "
                        "failure; stale or conflicting identity evidence requires a "
                        "new review and plan."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "party.staff_principal_adoption.invalid_command",
                        "party.staff_principal_adoption.staff_account_not_found",
                        "party.staff_principal_adoption.party_binding_refused",
                        *owner_command_boundary_error_codes(
                            "party.staff_principal_adoption"
                        ),
                    ),
                    mapping_owner=(
                        "scripts.migration.execute_staff_party_credential_adoption"
                    ),
                    fail_closed_on=(
                        "unattributable approver",
                        "missing, non-Person or unavailable Party",
                        "missing SystemUser",
                        "incomplete, changed or conflicting binding evidence",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner="unbound existing SystemUser principal rows",
                    new_owner="party.staff_principal_adoption",
                    verification=(
                        "Exact typed-plan, approval, delegation, audit, replay, "
                        "composition, liveness and boundary canaries."
                    ),
                    cutover_gate=(
                        "Every in-scope staff principal and credential is projected, "
                        "drift cohorts are zero, login shadow parity passes, and the "
                        "tenant GUC/RLS rehearsal is green."
                    ),
                    fallback_retirement=(
                        "Legacy staff principal and credential reads remain until "
                        "the separately approved authentication reader cutover."
                    ),
                ),
                steward="identity and authentication",
                design_refs=(
                    "docs/PARTY_PRINCIPAL_CONTEXT_BINDING.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_staff_party_credential_adoption.py",
                    "tests/architecture/test_credential_party_binding_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="party.staff_session_projection",
            module="app.services.staff_session_party_adoption",
            owns=("approved staff session Party projection",),
            depends_on=(
                "party.registry",
                "auth.staff_provisioning",
                "observability.audit_log",
            ),
            notes=(
                "Projects only active, unrevoked legacy staff sessions from "
                "an exact UUID-only, digest-bound approval plan. Identity is "
                "the deployed SystemUser.person_party_id foreign-key binding; "
                "names, email, usernames and token material are never inputs. "
                "Revoked and non-active historical null rows remain preserved."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="approved staff session Party projection",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "approved staff session projection decision",
                            "canonical staff principal state",
                            "canonical Person Party identity",
                            "legacy staff session state",
                        ),
                        canonical_writer="party.staff_session_projection",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="approved staff session projection decision",
                        owner="party.staff_session_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed UUID-only plan item, exact plan and file "
                            "SHA-256 digests, expiring approval, exact count "
                            "bound and attributable user CommandContext"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff principal state",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active SystemUser and its exact person_party_id "
                            "foreign-key binding"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Person Party identity",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="the exact Person Party named by the staff binding",
                    ),
                    AuthorityInput(
                        name="legacy staff session state",
                        owner="app_sessions.auth",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "the exact active, unrevoked sessions row selected "
                            "by reviewed UUID"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "project_staff_session_party validates and commits one "
                        "sessions.party_id projection plus its audit row before "
                        "return; the operator adapter owns no business write."
                    ),
                    locking=(
                        "Lock the reviewed Person Party, then SystemUser, then "
                        "session; repeat exact binding, eligibility and conflict "
                        "checks while all three locks are held."
                    ),
                    idempotency=(
                        "An already-populated exact Party is a replay; a different "
                        "Party, principal, state or incomplete binding refuses."
                    ),
                    retries=(
                        "Retry only the same unexpired approved plan. The adapter "
                        "revalidates approval before every independently committed "
                        "item and exact owner replay resumes interrupted batches."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "party.staff_session_projection.invalid_command",
                        "party.staff_session_projection.party_binding_refused",
                        "party.staff_session_projection.staff_account_not_found",
                        "party.staff_session_projection.session_not_found",
                        "party.staff_session_projection.session_principal_conflict",
                        "party.staff_session_projection.session_party_conflict",
                        "party.staff_session_projection.session_ineligible",
                        *owner_command_boundary_error_codes(
                            "party.staff_session_projection"
                        ),
                    ),
                    mapping_owner=(
                        "scripts.migration.execute_staff_session_party_projection"
                    ),
                    fail_closed_on=(
                        "unattributable approver",
                        "missing or non-Person Party",
                        "inactive, missing or differently bound SystemUser",
                        "missing, revoked, non-active or differently owned session",
                        "conflicting existing session Party projection",
                        "expired, changed or count-mismatched approval evidence",
                    ),
                ),
                events=EventContract(
                    event_types=("party.staff_session_projected",),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility=(
                        "PII-free audit evidence carries session, SystemUser, Party, "
                        "decision, plan, item-evidence, approval and command ids or "
                        "digests; never contact, credential or token material."
                    ),
                    replay=(
                        "An exact existing session Party returns a replay and does "
                        "not append a second audit event."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="staff session Party projection",
                        input_names=(
                            "approved staff session projection decision",
                            "canonical staff principal state",
                            "canonical Person Party identity",
                            "legacy staff session state",
                        ),
                        writer="party.staff_session_projection",
                        freshness=(
                            "New staff sessions dual-write the bound pair; the "
                            "approved campaign projects each eligible legacy row."
                        ),
                        stale_behavior=(
                            "A null legacy row remains on the deploy-1 bridge; an "
                            "unbound or conflicting active row blocks planning and "
                            "the deploy-2 ratchet."
                        ),
                        drift_signal=(
                            "StaffSessionPartyProjectionReport active-unrevoked "
                            "remaining, unbound and disagreement counts."
                        ),
                        rebuild_operation=(
                            "Generate and separately approve a fresh deterministic "
                            "batch; there is no force-repoint or inferred mapping."
                        ),
                        repair_owner="party.staff_session_projection",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner="legacy active staff sessions with null party_id",
                    new_owner="party.staff_session_projection",
                    verification=(
                        "Typed-contract sensitivity, exact-FK planning, PII-free "
                        "report, approval, owner refusal, audit and replay canaries."
                    ),
                    cutover_gate=(
                        "Every active, unrevoked staff session is projected with "
                        "zero unbound principals or disagreements."
                    ),
                    fallback_retirement=(
                        "The assertion-first reader bridge is deleted only after "
                        "the aggregate report is ratchet-ready in production."
                    ),
                ),
                steward="identity and authentication",
                design_refs=(
                    "docs/PARTY_PRINCIPAL_CONTEXT_BINDING.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=("tests/test_staff_session_party_adoption.py",),
            ),
        ),
        SOTService(
            name="party.credential_authentication_projection",
            module="app.services.credential_party_binding",
            owns=(
                "installed authentication binding registry",
                "credential Party authentication projection",
                "credential principal readiness and projection convergence report",
            ),
            depends_on=(
                "party.registry",
                "tenancy.operator_tenant",
                "observability.audit_log",
            ),
            notes=(
                "Migration 527 is additive. This owner locks and projects one "
                "credential to a Person Party, exact installed verifier binding, "
                "operator tenant and reviewed evidence. Legacy principal foreign "
                "keys remain login authority until a later reader cutover."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="installed authentication binding registry",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=(
                            "owner-declared authentication mechanism vocabulary",
                            "installed verifier configuration evidence",
                        ),
                        canonical_writer="party.credential_authentication_projection",
                    ),
                    ConcernContract(
                        name="credential Party authentication projection",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "legacy credential principal reference",
                            "reviewed Person Party binding",
                            "declared installed authentication binding",
                            "operator tenant identity",
                            "typed credential projection command evidence",
                        ),
                        canonical_writer="party.credential_authentication_projection",
                    ),
                    ConcernContract(
                        name=(
                            "credential principal readiness and projection "
                            "convergence report"
                        ),
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "legacy credential principal reference",
                            "reviewed Person Party binding",
                            "declared installed authentication binding",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="owner-declared authentication mechanism vocabulary",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "DomainSOT authentication_mechanisms declarations, with "
                            "local owned by authorization and radius by network access"
                        ),
                    ),
                    AuthorityInput(
                        name="installed verifier configuration evidence",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Migration 527 deterministic binding key, mechanism and "
                            "operator-facing label; no credential material"
                        ),
                    ),
                    AuthorityInput(
                        name="legacy credential principal reference",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "UserCredential subscriber_id, system_user_id or "
                            "reseller_user_id and provider; retained as login "
                            "authority during R1"
                        ),
                    ),
                    AuthorityInput(
                        name="reviewed Person Party binding",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "Person Party plus the reviewed Party binding already "
                            "carried by the legacy principal"
                        ),
                    ),
                    AuthorityInput(
                        name="declared installed authentication binding",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Active authentication_bindings row whose mechanism is "
                            "declared by exactly one SOT domain and matches provider"
                        ),
                    ),
                    AuthorityInput(
                        name="operator tenant identity",
                        owner="tenancy.operator_tenant",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="The provisioned deterministic Sub operator tenant",
                    ),
                    AuthorityInput(
                        name="typed credential projection command evidence",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "Exact credential, Party, binding and tenant ids plus "
                            "review source, reason and CommandContext"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "bind_credential_party owns one complete root transaction "
                        "and returns only after the projection and audit commit."
                    ),
                    locking=(
                        "Lock the credential, Person Party and verifier binding; the "
                        "Party lock serializes competing credentials for one person "
                        "before the tuple uniqueness backstop."
                    ),
                    idempotency=(
                        "Only the exact same tenant, Party, binding, source and reason "
                        "replays; changed evidence or a repoint fails closed."
                    ),
                    retries=(
                        "Retry the complete command with the same CommandContext; "
                        "database uniqueness or changed source state is not repaired "
                        "inside a partial transaction."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "party.credential_authentication_projection.invalid_command",
                        "party.credential_authentication_projection.credential_missing",
                        "party.credential_authentication_projection.party_missing",
                        "party.credential_authentication_projection.person_required",
                        "party.credential_authentication_projection.party_unavailable",
                        (
                            "party.credential_authentication_projection."
                            "principal_party_missing"
                        ),
                        (
                            "party.credential_authentication_projection."
                            "principal_mismatch"
                        ),
                        (
                            "party.credential_authentication_projection."
                            "organization_administrator_required"
                        ),
                        (
                            "party.credential_authentication_projection."
                            "principal_party_mismatch"
                        ),
                        (
                            "party.credential_authentication_projection."
                            "authentication_binding_missing"
                        ),
                        (
                            "party.credential_authentication_projection."
                            "authentication_binding_inactive"
                        ),
                        "party.credential_authentication_projection.undeclared_mechanism",
                        (
                            "party.credential_authentication_projection."
                            "ambiguous_mechanism_binding"
                        ),
                        "party.credential_authentication_projection.tenant_mismatch",
                        "party.credential_authentication_projection.mechanism_mismatch",
                        "party.credential_authentication_projection.partial_projection",
                        "party.credential_authentication_projection.repoint_refused",
                        "party.credential_authentication_projection.projection_collision",
                        *owner_command_boundary_error_codes(
                            "party.credential_authentication_projection"
                        ),
                    ),
                    mapping_owner=(
                        "scripts.migration.execute_staff_party_credential_adoption"
                    ),
                    fail_closed_on=(
                        "organization Party",
                        "missing or mismatched legacy principal Party binding",
                        "undeclared or mismatched verifier mechanism",
                        "partial or conflicting existing projection",
                        "duplicate tenant-Party-binding tuple",
                    ),
                ),
                events=EventContract(
                    event_types=("credential.party_authentication_projected",),
                    schema_version=1,
                    delivery_owner="observability.audit_log",
                    compatibility=(
                        "PII-free audit evidence carries only ids, immutable binding "
                        "key, source and command correlation; never username, secret "
                        "or display name."
                    ),
                    replay=(
                        "An exact command replay returns the original bound_at and "
                        "does not append a second audit record."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="UserCredential Party authentication projection",
                        input_names=(
                            "legacy credential principal reference",
                            "reviewed Person Party binding",
                            "declared installed authentication binding",
                            "operator tenant identity",
                        ),
                        writer="party.credential_authentication_projection",
                        freshness="Written only by reviewed adoption commands in R1.",
                        stale_behavior=(
                            "Legacy principal references remain login authority; a "
                            "drifted projection blocks cutover and is never preferred."
                        ),
                        drift_signal=(
                            "credential_convergence_report mechanism, Person and "
                            "tuple-collision cohorts"
                        ),
                        rebuild_operation=(
                            "Re-run the separately approved credential adoption plan; "
                            "there is no force-repoint path."
                        ),
                        repair_owner="party.credential_authentication_projection",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    new_owner="party.credential_authentication_projection",
                    old_owner=(
                        "UserCredential subscriber_id, system_user_id and "
                        "reseller_user_id principal references"
                    ),
                    verification=(
                        "The convergence report separates legacy principal readiness "
                        "from the complete new projection and reports no identities."
                    ),
                    cutover_gate=(
                        "Every credential is projected, mismatch/collision cohorts are "
                        "zero, login shadow parity passes, and GUC/RLS prerequisites "
                        "are proven on a production-derived rehearsal."
                    ),
                    fallback_retirement=(
                        "Legacy principal authentication reads and direct projection "
                        "writers are removed only after the later reader cutover."
                    ),
                ),
                steward="identity and authentication",
                design_refs=(
                    "docs/PARTY_PRINCIPAL_CONTEXT_BINDING.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_credential_party_binding.py",
                    "tests/test_credential_party_binding_migration.py",
                    "tests/architecture/test_credential_party_binding_boundary.py",
                    "tests/test_staff_party_credential_adoption.py",
                ),
            ),
        ),
        SOTService(
            name="party.staff_authentication_shadow",
            module="app.services.staff_authentication_shadow",
            owns=(
                "legacy and Party-keyed staff authentication parity",
                "staff Party authentication read-cutover readiness",
            ),
            depends_on=(
                "party.staff_principal_adoption",
                "party.credential_authentication_projection",
                "auth.staff_provisioning",
                "app_sessions.auth",
            ),
            notes=(
                "Read-only migration evidence compares SystemUser-keyed and "
                "Party-keyed credential, MFA, lockout and live-session answers. "
                "It reports aggregate stable reasons only, changes no reader or "
                "authentication state, and cannot authorize cutover by itself. "
                "uq_system_users_person_party_id prevents a Party from owning "
                "multiple SystemUsers today; that report reason remains an "
                "invariant-breach sentinel for lineage drift."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="legacy and Party-keyed staff authentication parity",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical staff identity and credential state",
                            "credential Party authentication projection",
                            "database authentication session state",
                            "legacy staff MFA persistence observation",
                        ),
                    ),
                    ConcernContract(
                        name="staff Party authentication read-cutover readiness",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical staff identity and credential state",
                            "credential Party authentication projection",
                            "database authentication session state",
                            "legacy staff MFA persistence observation",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical staff identity and credential state",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "SystemUser identity plus its active local "
                            "UserCredential and credential lockout state"
                        ),
                    ),
                    AuthorityInput(
                        name="credential Party authentication projection",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source=(
                            "the complete nullable UserCredential Party, binding, "
                            "tenant and evidence projection introduced by migration 527"
                        ),
                    ),
                    AuthorityInput(
                        name="database authentication session state",
                        owner="app_sessions.auth",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "database Session status, revocation and expiry facts "
                            "resolved by the application-session owner"
                        ),
                    ),
                    AuthorityInput(
                        name="legacy staff MFA persistence observation",
                        owner="party.staff_authentication_shadow",
                        kind=AuthorityKind.OBSERVATION,
                        source=(
                            "read-only count and SystemUser association observed "
                            "directly from retained MFAMethod compatibility rows; "
                            "this assigns no MFA lifecycle or writer authority"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "The operator adapter opens one PostgreSQL REPEATABLE READ, "
                        "READ ONLY transaction, resolves one report, then rolls back."
                    ),
                    locking=(
                        "No row locks: one repeatable-read snapshot prevents login, "
                        "MFA or session changes from splitting the report."
                    ),
                    idempotency=(
                        "The sorted aggregate report is deterministic for one "
                        "database snapshot and stores no execution marker."
                    ),
                    retries=(
                        "Retry the complete read-only report on a fresh snapshot; "
                        "never combine cohorts from separate attempts."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(),
                    mapping_owner=(
                        "scripts.migration.staff_authentication_shadow_parity"
                    ),
                    fail_closed_on=(
                        "credential and principal Party disagreement",
                        "an active credential whose staff principal has no Party",
                        "one Party owning multiple SystemUsers",
                        "one principal holding multiple active credentials",
                        "any incomplete credential projection",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner=(
                        "legacy SystemUser-keyed credential, MFA, lockout and "
                        "database-session reads"
                    ),
                    new_owner="party.staff_authentication_shadow",
                    verification=(
                        "Stable PII-free cohorts compare both resolution paths and "
                        "separately expose corruption, ambiguity and projection debt."
                    ),
                    cutover_gate=(
                        "Every staff credential is projected and every blocking "
                        "cohort is zero in production shadow evidence."
                    ),
                    fallback_retirement=(
                        "Retire the shadow verifier only after the separately "
                        "approved authentication reader cutover and rollback window."
                    ),
                ),
                steward="identity and authentication",
                design_refs=(
                    "docs/PARTY_PRINCIPAL_CONTEXT_BINDING.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_staff_authentication_shadow.py",
                    "tests/integration/test_staff_party_identity_constraint.py",
                ),
            ),
        ),
        SOTService(
            name="party.contact_inbox_audit",
            module="app.services.party_contact_audit",
            owns=(
                "read-only SubscriberContact Person convergence audit",
                "legacy contact relationship and contact-point projection audit",
                "Party contact-point verification and consent coverage report",
                "Team Inbox canonical contact-point projection debt report",
            ),
            depends_on=(
                "party.registry",
                "communications.team_inbox_contact_resolution",
            ),
            notes=(
                "Reports only aggregate schema, identity, contact-point, and "
                "Inbox routing-projection counts. It never emits identity "
                "values, creates or binds a Party/relationship/contact point, "
                "changes an Inbox route, copies verification or consent, or "
                "changes authentication or authorization."
            ),
        ),
    ),
    entrypoints=(
        "scripts.migration.audit_subscriber_identity",
        "scripts.migration.plan_subscriber_party_backfill",
        "scripts.migration.execute_subscriber_party_backfill",
        "scripts.migration.audit_party_organization_profiles",
        "scripts.migration.audit_party_principal_contexts",
        "scripts.migration.execute_staff_party_credential_adoption",
        "scripts.migration.staff_authentication_shadow_parity",
        "scripts.migration.audit_party_contact_inbox",
        "future party backfills",
        "future subscriber/reseller/vendor cutovers",
        "future Team Inbox contact resolution",
        "future authentication principal cutovers",
    ),
    rule="One real-world person or organization has one canonical Party and "
    "may hold several independent roles. Domain records and security "
    "principals link to Party; they do not create parallel identity. "
    "No adapter treats the additive foundation as a completed cutover.",
)
