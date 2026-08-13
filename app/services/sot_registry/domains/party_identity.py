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
            name="party.staff_principal_adoption",
            module="app.services.staff_party_adoption",
            owns=("existing staff Party principal adoption",),
            depends_on=(
                "party.registry",
                "auth.staff_provisioning",
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
                "Migration 525 is additive. This owner locks and projects one "
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
                            "Migration 525 deterministic binding key, mechanism and "
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
        "scripts.migration.execute_staff_party_credential_adoption",
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
