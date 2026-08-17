"""Canonical SOT declarations for the authorization_control_plane domain."""

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
)
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="authorization_control_plane",
    authentication_mechanisms=("local",),
    setting_domains=("auth",),
    services=(
        SOTService(
            name="auth.permission_gate",
            module="app.services.auth_dependencies",
            owns=(
                "route permission dependencies",
                "request principal permission checks",
            ),
            depends_on=(
                "auth.rbac_catalog",
                "auth.subscriber_assignments",
            ),
        ),
        SOTService(
            name="auth.subscriber_assignments",
            module="app.services.subscriber_assignments",
            owns=("subscriber role and direct-permission assignments",),
            depends_on=(
                "auth.rbac_catalog",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "This is the only application and seed writer for "
                "subscriber_roles and subscriber_permissions. Public "
                "commands own their complete transaction; reseller "
                "onboarding and seed workflows use only flush-only "
                "collaborators inside their coordinator transaction."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=("subscriber role and direct-permission assignments"),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized subscriber assignment principal",
                            "active role and permission catalog",
                            "canonical subscriber assignment state",
                        ),
                        canonical_writer="auth.subscriber_assignments",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized subscriber assignment principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=("rbac:assign evidence carried in CommandContext"),
                    ),
                    AuthorityInput(
                        name="active role and permission catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("active roles and active UI-assignable permissions"),
                    ),
                    AuthorityInput(
                        name="canonical subscriber assignment state",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscriber_roles and subscriber_permissions",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public assignment command enters "
                        "execute_owner_command on a transaction-free session; "
                        "the grants, audit evidence, and versioned event commit "
                        "or roll back together. Reseller onboarding and seed "
                        "collaborators flush only."
                    ),
                    locking=(
                        "Target subscribers, active catalog references, and "
                        "existing grants are selected FOR UPDATE. Unique "
                        "constraints arbitrate concurrent duplicate grants."
                    ),
                    idempotency=(
                        "Duplicate grant and desired-state replacement "
                        "commands converge without parallel writes; adapter "
                        "intent keys are stored only as SHA-256 evidence."
                    ),
                    retries=(
                        "Adapters may retry a failed desired-state command "
                        "with the same intent key. Invalid scope, inactive "
                        "catalog, and conflict failures require changed input."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.subscriber_assignments.invalid_command",
                        "auth.subscriber_assignments.invalid_scope",
                        "auth.subscriber_assignments.subscriber_not_found",
                        "auth.subscriber_assignments.role_not_found",
                        "auth.subscriber_assignments.permission_not_found",
                        "auth.subscriber_assignments.role_grant_not_found",
                        "auth.subscriber_assignments.permission_grant_not_found",
                        "auth.subscriber_assignments.assignment_conflict",
                        "auth.subscriber_assignments.invalid_command_context",
                        "auth.subscriber_assignments.command_contract_violation",
                        "auth.subscriber_assignments.nested_owner_command",
                        "auth.subscriber_assignments.active_caller_transaction",
                        ("auth.subscriber_assignments.nested_transaction_completion"),
                    ),
                    mapping_owner=("app.api.rbac and app.web.admin.resellers"),
                    fail_closed_on=(
                        "missing rbac:assign evidence",
                        "inactive or non-assignable catalog references",
                        "invalid region or reseller grant scope",
                        "concurrent assignment conflicts",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=("subscriber.assignments_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and contains subscriber, role, "
                        "scope, and permission identifiers but no PII or raw "
                        "idempotency key."
                    ),
                    replay=(
                        "Events are immutable assignment-change evidence. "
                        "Canonical assignment and catalog tables remain the "
                        "rebuild inputs."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.rbac subscriber assignment CRUD, "
                        "reseller onboarding role writes, and direct "
                        "scripts.seed.seed_rbac subscriber grant writes"
                    ),
                    new_owner="auth.subscriber_assignments",
                    verification=(
                        "Focused atomicity, scope, catalog-safety, API, "
                        "reseller, seed, cache, and architecture tests."
                    ),
                    cutover_gate=(
                        "Every application and seed subscriber assignment "
                        "write delegates to auth.subscriber_assignments."
                    ),
                    fallback_retirement=(
                        "The legacy app.services.rbac module and all direct "
                        "subscriber assignment writers are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_subscriber_assignments.py",
                    ("tests/architecture/test_subscriber_assignment_boundary.py"),
                ),
            ),
        ),
        SOTService(
            name="auth.rbac_catalog",
            module="app.services.rbac_catalog",
            owns=(
                "role catalog and role-permission policy",
                "permission catalog",
                "kernel Role identity projection",
            ),
            depends_on=(
                "tenancy.operator_tenant",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "This is the only application and seed writer for roles, "
                "permissions, and role_permissions. Catalog identities are "
                "case-normalized and protected by functional unique indexes. "
                "Permission-policy updates preserve an unchanged legacy role "
                "name, while new and genuinely renamed roles must use the "
                "canonical lowercase identifier syntax. "
                "Migration 528 adds the nullable kernel Role identity on the "
                "same row; this owner alone writes the operator tenant and "
                "deterministic slug while roles.name remains authoritative. "
                "The service-level migration state is SHADOWING for that new "
                "projection only; established catalog command ownership remains "
                "complete. "
                "Assigned identities cannot be renamed or deactivated, and "
                "non-assignable permissions may be granted only to admin."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="role catalog and role-permission policy",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized RBAC catalog principal",
                            "canonical role and role-permission catalog",
                            "system-user role grant references",
                            "subscriber role grant references",
                        ),
                        canonical_writer="auth.rbac_catalog",
                    ),
                    ConcernContract(
                        name="permission catalog",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized RBAC catalog principal",
                            "canonical permission catalog",
                            "system-user permission grant references",
                            "subscriber permission grant references",
                        ),
                        canonical_writer="auth.rbac_catalog",
                    ),
                    ConcernContract(
                        name="kernel Role identity projection",
                        role=OwnerRole.PROJECTION_WRITER,
                        input_names=(
                            "canonical role and role-permission catalog",
                            "operator tenant identity",
                            "deterministic role slug derivation policy",
                        ),
                        canonical_writer="auth.rbac_catalog",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized RBAC catalog principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "rbac role/permission write or delete scope "
                            "evidence carried in CommandContext"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical role and role-permission catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="roles and role_permissions",
                    ),
                    AuthorityInput(
                        name="canonical permission catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="permissions",
                    ),
                    AuthorityInput(
                        name="operator tenant identity",
                        owner="tenancy.operator_tenant",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="The provisioned deterministic Sub operator tenant",
                    ),
                    AuthorityInput(
                        name="deterministic role slug derivation policy",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "derive_role_slug and the typed collision report; no "
                            "counter or insertion-order disambiguation"
                        ),
                    ),
                    AuthorityInput(
                        name="system-user role grant references",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="system_user_roles references used by catalog safety policy",
                    ),
                    AuthorityInput(
                        name="subscriber role grant references",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscriber_roles references used by catalog safety policy",
                    ),
                    AuthorityInput(
                        name="system-user permission grant references",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "system_user_permissions references used by "
                            "catalog identity and deactivation safety policy"
                        ),
                    ),
                    AuthorityInput(
                        name="subscriber permission grant references",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "subscriber_permissions references used by "
                            "catalog identity and deactivation safety policy"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public catalog command enters "
                        "execute_owner_command on a transaction-free session; "
                        "the catalog row, nullable kernel identity projection, "
                        "complete role-permission policy, "
                        "audit evidence, and versioned event commit or roll "
                        "back together. Seed collaborators flush only."
                    ),
                    locking=(
                        "Existing catalog rows and relationship sets are "
                        "selected FOR UPDATE. Case-normalized PostgreSQL unique "
                        "indexes arbitrate concurrent natural-key creation, "
                        "kernel tenant/slug uniqueness arbitrates projection "
                        "collisions, "
                        "while grant-reference checks fail closed before rename "
                        "or deactivation."
                    ),
                    idempotency=(
                        "Role-permission replacement and seed convergence use "
                        "desired sets; duplicate grants are no-ops. Adapter "
                        "intent keys are stored only as SHA-256 evidence."
                    ),
                    retries=(
                        "Adapters may retry failed desired-state commands with "
                        "the same intent key. Validation, protected catalog, "
                        "and in-use failures require changed authoritative input."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.rbac_catalog.invalid_command",
                        "auth.rbac_catalog.invalid_role_name",
                        "auth.rbac_catalog.invalid_permission_key",
                        "auth.rbac_catalog.invalid_permissions",
                        "auth.rbac_catalog.role_not_found",
                        "auth.rbac_catalog.permission_not_found",
                        "auth.rbac_catalog.role_permission_not_found",
                        "auth.rbac_catalog.role_conflict",
                        "auth.rbac_catalog.permission_conflict",
                        "auth.rbac_catalog.catalog_conflict",
                        "auth.rbac_catalog.role_in_use",
                        "auth.rbac_catalog.permission_in_use",
                        "auth.rbac_catalog.protected_role",
                        "auth.rbac_catalog.protected_permission",
                        "auth.rbac_catalog.invalid_command_context",
                        "auth.rbac_catalog.command_contract_violation",
                        "auth.rbac_catalog.nested_owner_command",
                        "auth.rbac_catalog.active_caller_transaction",
                        "auth.rbac_catalog.nested_transaction_completion",
                    ),
                    mapping_owner=("app.api.rbac and app.web.admin.system"),
                    fail_closed_on=(
                        "missing catalog authorization evidence",
                        "case-normalized catalog collisions",
                        "kernel tenant/slug identity collisions",
                        "rename or deactivation of assigned identities",
                        "protected admin role or permission changes",
                        "non-assignable permission grants outside admin",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "rbac.role_catalog_changed",
                        "rbac.permission_catalog_changed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and contains authorization "
                        "identifiers but no PII or raw idempotency key."
                    ),
                    replay=(
                        "Events are immutable policy-change evidence. Canonical "
                        "catalog tables and checked-in seed desired sets remain "
                        "the rebuild inputs."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="Role kernel identity projection",
                        input_names=(
                            "canonical role and role-permission catalog",
                            "operator tenant identity",
                            "deterministic role slug derivation policy",
                        ),
                        writer="auth.rbac_catalog",
                        freshness=(
                            "Written on every canonical role mutation in R1; legacy "
                            "rows remain unprojected until touched or reviewed later."
                        ),
                        stale_behavior=(
                            "roles.name remains authorization authority. A NULL, "
                            "partial, colliding or mismatched projection blocks "
                            "kernel reader and lineage cutover."
                        ),
                        drift_signal=(
                            "ck_roles_kernel_identity_projection, kernel composite "
                            "unique keys, sole-writer architecture guard and the "
                            "typed role-slug collision report"
                        ),
                        rebuild_operation=(
                            "Re-run a separately reviewed future role-adoption "
                            "command through auth.rbac_catalog; R1 has no backfill."
                        ),
                        repair_owner="auth.rbac_catalog",
                    ),
                ),
                migration=MigrationContract(
                    # Role/permission command ownership was already complete.
                    # The service's newest authority expansion is the kernel
                    # identity projection, and that migration is only shadowing:
                    # it is nullable, unpopulated for untouched rows and unread.
                    state=AuthorityMigrationState.SHADOWING,
                    old_owner=(
                        "roles.name-only global identity read by every existing "
                        "authorization path"
                    ),
                    new_owner="auth.rbac_catalog",
                    verification=(
                        "Typed deterministic slug and collision reports, sole-writer "
                        "guard, dual-write behavior, and PostgreSQL predecessor/fresh "
                        "migration canaries."
                    ),
                    cutover_gate=(
                        "The reviewed collision and mismatch cohorts are zero, every "
                        "role has a complete projection, kernel grant semantics have "
                        "shadow parity, and the atomic revision-0001 rehearsal passes."
                    ),
                    fallback_retirement=(
                        "R1 keeps roles.name authoritative and the kernel identity "
                        "nullable and unread; legacy identity reads retire only in a "
                        "later approved cutover."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/PLATFORM_ADOPTION_LEDGER.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_rbac_catalog_owner.py",
                    "tests/test_roles_r1_kernel_identity.py",
                    "tests/test_roles_r1_migration.py",
                    "tests/integration/test_roles_r1_migration.py",
                    "tests/architecture/test_rbac_catalog_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.system_user_assignments",
            module="app.services.system_user_assignments",
            owns=(
                "system-user role and direct-permission assignments",
                "source-scoped managed system-user role convergence",
            ),
            depends_on=(
                "auth.rbac_catalog",
                "auth.permission_gate",
                "events.dispatcher",
                "observability.audit_log",
                "party.registry",
            ),
            notes=(
                "This is the only application writer for system_user_roles and "
                "system_user_permissions. Local replacement preserves grants "
                "owned by ERP or another source. Every role convergence locks "
                "the active admin role before checking the final-active-admin "
                "invariant. Public administrative replacement owns its complete "
                "transaction; staff provisioning uses only flush-only "
                "source-scoped collaborators inside its coordinator transaction."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name=("system-user role and direct-permission assignments"),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized system-user assignment principal",
                            "active role and permission catalog",
                            "canonical system-user assignment state",
                            "canonical staff Party binding",
                        ),
                        canonical_writer="auth.system_user_assignments",
                    ),
                    ConcernContract(
                        name=("source-scoped managed system-user role convergence"),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "active role and permission catalog",
                            "canonical system-user assignment state",
                        ),
                        canonical_writer="auth.system_user_assignments",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized system-user assignment principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "rbac:assign authorization evidence in the typed "
                            "CommandContext"
                        ),
                    ),
                    AuthorityInput(
                        name="active role and permission catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("active roles and active UI-assignable permissions"),
                    ),
                    AuthorityInput(
                        name="canonical system-user assignment state",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="system_user_roles and system_user_permissions",
                    ),
                    AuthorityInput(
                        name="canonical staff Party binding",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "the reviewed SystemUser.person_party_id projection; "
                            "names and email addresses are never identity evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "The public replacement command enters "
                        "execute_owner_command on a transaction-free session; "
                        "roles, direct permissions, audit, and event evidence "
                        "commit or roll back together. Audit actor enrichment reads "
                        "the canonical staff Party binding in that transaction. "
                        "Collaborator methods "
                        "flush but never complete a coordinator transaction."
                    ),
                    locking=(
                        "The target principal and existing grants are selected "
                        "FOR UPDATE. Every role change locks the active admin "
                        "role row before evaluating the final-active-admin "
                        "invariant, serializing competing removals and disables."
                    ),
                    idempotency=(
                        "Each source converges only its own global role grants; "
                        "direct permissions converge to the requested set. "
                        "Repeated desired state is a no-op and adapters carry a "
                        "stable intent key recorded only as a digest."
                    ),
                    retries=(
                        "Adapters may retry failed commands with the same intent "
                        "key. Validation and final-admin failures are not "
                        "retryable without a changed authoritative input."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.system_user_assignments.invalid_command",
                        "auth.system_user_assignments.invalid_roles",
                        "auth.system_user_assignments.invalid_permissions",
                        "auth.system_user_assignments.system_user_not_found",
                        "auth.system_user_assignments.last_admin_required",
                        "auth.system_user_assignments.invalid_command_context",
                        "auth.system_user_assignments.command_contract_violation",
                        "auth.system_user_assignments.nested_owner_command",
                        "auth.system_user_assignments.active_caller_transaction",
                        "auth.system_user_assignments.nested_transaction_completion",
                    ),
                    mapping_owner="app.web.admin.system",
                    fail_closed_on=(
                        "missing assignment authorization evidence",
                        "inactive or unknown roles",
                        "inactive or non-assignable direct permissions",
                        "removal or deactivation of the final active admin",
                        "active caller transaction or nested completion",
                        "manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=("system_user.assignments_changed",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and contains identifiers and "
                        "authorization keys but no raw idempotency key or PII."
                    ),
                    replay=(
                        "Events are immutable decision evidence; authoritative "
                        "assignment tables remain repairable by replaying the "
                        "source-specific desired grant command."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.web_system_user_edit, "
                        "app.services.web_system_user_mutations, and legacy "
                        "app.services.rbac system-user grant helpers"
                    ),
                    new_owner="auth.system_user_assignments",
                    verification=(
                        "Focused atomicity, source preservation, final-admin, "
                        "adapter, and architecture boundary tests."
                    ),
                    cutover_gate=(
                        "All application-level system-user assignment writes "
                        "delegate to this owner and managed roles are read-only "
                        "in the local administrative editor."
                    ),
                    fallback_retirement=(
                        "Profile edits no longer write grants or active state; "
                        "legacy create and direct assignment mutation helpers "
                        "are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_system_user_assignments.py",
                    "tests/architecture/test_system_user_assignment_boundary.py",
                    "tests/architecture/test_audit_actor_provenance.py",
                ),
            ),
        ),
        SOTService(
            name="auth.entitlement_revocation",
            module="app.services.entitlement_revocation",
            owns=("session revocation for entitlement reductions",),
            depends_on=(
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Current login and refresh tokens omit roles and scopes and "
                "reload RBAC through require_user_auth, while that dependency "
                "still accepts compatibility tokens carrying embedded claims. "
                "Those claims remain valid until expiry and cannot be changed by "
                "cache invalidation. require_user_auth re-reads the authoritative "
                "sessions row on every request, so revoking that row is the one "
                "fail-closed next-request rule for both token forms. This owner "
                "revokes inside the reducing owner's "
                "transaction so revocation and reduction commit or roll back "
                "together, emits durable projection work, and registers strict "
                "cache invalidation for after commit — never before, or a "
                "concurrent read would repopulate the cache from uncommitted "
                "rows. A failed invalidation is counted and logged but cannot "
                "preserve authorization, because the database already denies. "
                "It does not decide whether a reduction occurred: that "
                "judgement belongs to the reducing owner, which alone knows the "
                "principal's effective access before and after."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="session revocation for entitlement reductions",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("reduced effective entitlement decision",),
                        canonical_writer="auth.entitlement_revocation",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="reduced effective entitlement decision",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "the reducing owner's before/after effective access "
                            "comparison, computed inside its own transaction"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.PARTICIPANT,
                    boundary=(
                        "Runs inside the reducing owner's transaction and never "
                        "commits. Revocation and reduction commit or roll back "
                        "together; a half-applied pair would either strand a "
                        "live session on withdrawn access or log a principal "
                        "out for a change that was abandoned."
                    ),
                    locking=(
                        "Live sessions for the principal are selected FOR "
                        "UPDATE, serializing against a concurrent login or "
                        "refresh touching the same rows."
                    ),
                    idempotency=(
                        "Already-revoked and expired sessions are excluded, so "
                        "a replay revokes nothing further and preserves the "
                        "original revoked_at."
                    ),
                    retries=(
                        "Retry belongs to the reducing owner's command. The "
                        "post-commit cache invalidation is not retried inline; "
                        "the emitted event is the replay handle, and "
                        "authorization is already denied without it."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.entitlement_revocation.unknown_principal_type",
                    ),
                    mapping_owner="auth.system_user_assignments",
                    fail_closed_on=(
                        "auth.entitlement_revocation.unknown_principal_type",
                    ),
                ),
                events=EventContract(
                    event_types=("rbac.entitlement_reduction_revoked",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Additive payload only. Consumers must tolerate unknown "
                        "keys; revoked_session_ids is sorted and stable."
                    ),
                    replay=(
                        "Replayable as the record of a completed revocation. "
                        "Replay re-invalidates caches; it never re-grants."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="auth.entitlement_revocation",
                    old_owner=None,
                    verification=(
                        "Canaries assert the revoked session no longer "
                        "satisfies the predicate require_user_auth applies, and "
                        "that widening, no-op and equivalent-regrant changes "
                        "revoke nothing."
                    ),
                ),
                steward="auth",
                design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
                test_refs=("tests/test_entitlement_revocation.py",),
            ),
        ),
        SOTService(
            name="auth.token_signing",
            module="app.services.context_signing",
            owns=(
                "configured JWT signing key and algorithm resolution",
                "cryptographic signing and verification of typed capability envelopes",
            ),
            notes=(
                "Calling domain owners define token purpose, claims, lifetime, "
                "and authorization consequences. Auth owns only the signed "
                "envelope and never turns a domain capability into identity proof."
            ),
        ),
        SOTService(
            name="auth.access_invitations",
            module="app.services.access_invitations",
            owns=("access invitation lifecycle",),
            depends_on=(
                "auth.credential_recovery",
                "runtime.durable_timers",
                "events.dispatcher",
                "events.owner_outputs",
            ),
            notes=(
                "Records issued/accepted/expired/revoked evidence for the "
                "staff, reseller, user, and subscriber invitation "
                "capabilities, with a durable per-invitation expiry timer "
                "and a receipted expiry consumer. Rows are lifecycle "
                "evidence, never an access grant: the capability's "
                "redeem-time TTL check in the issuing domain remains the "
                "fail-closed gate, and a completed reset stamps "
                "acceptance."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="access invitation lifecycle",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("issued invitation capabilities",),
                        canonical_writer="auth.access_invitations",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="issued invitation capabilities",
                        owner="auth.credential_recovery",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "exact reset capabilities minted for invite "
                            "purposes with their principal and TTL"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "record_issued participates in the caller's active "
                        "owner command or roots its own; the expiry "
                        "consumer enters execute_owner_command once on a "
                        "transaction-free session."
                    ),
                    locking=(
                        "Reissue supersedes the principal's prior issued "
                        "rows inside one transaction; expiry reloads the "
                        "row and state-guards the transition."
                    ),
                    idempotency=(
                        "Reissue replaces the expiry timer by generation; "
                        "consumer receipts make redelivery an exact no-op."
                    ),
                    retries=(
                        "A failed expiry consequence leaves no receipt; "
                        "the outbox redelivers until it commits."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.access_invitations.active_caller_transaction",
                        "auth.access_invitations.command_contract_violation",
                        "auth.access_invitations.invalid_command_context",
                        "auth.access_invitations.nested_owner_command",
                        "auth.access_invitations.nested_transaction_completion",
                    ),
                    mapping_owner="auth and admin web adapters",
                    fail_closed_on=("expiring an accepted or revoked invitation",),
                ),
                events=EventContract(
                    event_types=(
                        "access_invitation.issued",
                        "access_invitation.accepted",
                        "access_invitation.expired",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 carries invitation, principal, purpose, "
                        "and deadline identities; no email addresses, only "
                        "digests on the row."
                    ),
                    replay=(
                        "Invitation rows are the durable state; outputs are "
                        "evidence and consumer receipts reject replays."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "stateless capability TTLs with redeem-time-only "
                        "expiry and no lifecycle evidence"
                    ),
                    new_owner="auth.access_invitations",
                    verification=(
                        "Invitation lifecycle behavior tests and the "
                        "identity chain boundary test."
                    ),
                    cutover_gate=(
                        "Every invite issuance path records its invitation "
                        "and stages the expiry timer."
                    ),
                    fallback_retirement=(
                        "Redeem-time TTL checks are retained deliberately "
                        "as the fail-closed gate; no parallel lifecycle "
                        "writer exists."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/designs/IDENTITY_ONBOARDING_CHAIN.md",
                    "docs/SOT_RELATIONSHIP_MAP.md",
                ),
                test_refs=(
                    "tests/test_access_invitations.py",
                    "tests/architecture/test_identity_onboarding_chain_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.credential_recovery",
            module="app.services.credential_recovery",
            owns=(
                "password recovery request and delivery intent",
                "password reset credential transition",
                "credential recovery session projection invalidation",
            ),
            depends_on=(
                "auth.token_signing",
                "communications.intents",
                "communications.ephemeral_actions",
                "control.settings_spec",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Recovery requests persist only PII-safe event context. "
                "The communication consequence revalidates an exact local "
                "principal and mints the bearer only at delivery time. "
                "Capability redemption is the only password-reset writer "
                "and atomically changes credentials, revokes database "
                "sessions, stages audit evidence, and emits an event."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="password recovery request and delivery intent",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential recovery command evidence",
                            "canonical recoverable principal state",
                            "credential recovery policy settings",
                            "durable recovery delivery boundary",
                        ),
                        canonical_writer="auth.credential_recovery",
                    ),
                    ConcernContract(
                        name="password reset credential transition",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential recovery command evidence",
                            "canonical recoverable principal state",
                            "credential recovery policy settings",
                            "verified recovery capability envelope",
                        ),
                        canonical_writer="auth.credential_recovery",
                    ),
                    ConcernContract(
                        name=("credential recovery session projection invalidation"),
                        role=OwnerRole.RECONCILER,
                        input_names=("canonical recoverable principal state",),
                        canonical_writer="auth.credential_recovery",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="credential recovery command evidence",
                        owner="auth.credential_recovery",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed CommandContext carrying public-auth or "
                            "authorized-administrator actor, scope, reason, "
                            "correlation, and idempotency evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical recoverable principal state",
                        owner="auth.credential_recovery",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active Subscriber, SystemUser, or ResellerUser "
                            "identity and its active local user_credential, "
                            "password marker, and auth_sessions"
                        ),
                    ),
                    AuthorityInput(
                        name="credential recovery policy settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "declared password minimum and recovery lifetime "
                            "settings plus the owner-defined request rate policy"
                        ),
                    ),
                    AuthorityInput(
                        name="durable recovery delivery boundary",
                        owner="communications.intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "deduplicated communication intent and notification "
                            "outbox state created from the request event"
                        ),
                    ),
                    AuthorityInput(
                        name="verified recovery capability envelope",
                        owner="auth.token_signing",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "signature and expiry verified password_reset "
                            "claims minted for one exact principal"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each public request or redemption enters "
                        "execute_owner_command on a transaction-free adapter "
                        "session. Request audit and outbox event, or credential "
                        "change, session revocation, audit, and completion "
                        "event commit or roll back together."
                    ),
                    locking=(
                        "Redemption selects the exact active principal and "
                        "local credential FOR UPDATE before comparing the "
                        "single-use password marker. Request rate limiting "
                        "precedes principal lookup."
                    ),
                    idempotency=(
                        "Each accepted request has one immutable event id and "
                        "its communication intent deduplicates on that id. A "
                        "redeemed capability is spent by password_updated_at, "
                        "so replay fails closed."
                    ),
                    retries=(
                        "Rolled-back commands may be retried with the same "
                        "intent evidence. Invalid or spent capabilities and "
                        "invalid passwords require changed input; event and "
                        "notification delivery retry independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.credential_recovery.invalid_command",
                        "auth.credential_recovery.invalid_password",
                        "auth.credential_recovery.invalid_reset_capability",
                        "auth.credential_recovery.credential_not_found",
                        ("auth.credential_recovery.invalid_command_context"),
                        ("auth.credential_recovery.command_contract_violation"),
                        "auth.credential_recovery.nested_owner_command",
                        "auth.credential_recovery.active_caller_transaction",
                        ("auth.credential_recovery.nested_transaction_completion"),
                    ),
                    mapping_owner=(
                        "app.api.auth_flow, app.services.web_auth, and portal "
                        "or administrative web adapters"
                    ),
                    fail_closed_on=(
                        "invalid, expired, or spent capability",
                        "principal or recipient drift",
                        "inactive or missing local credential",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "password_recovery.requested",
                        "password_recovery.completed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 contains identifiers, correlation evidence, "
                        "an email digest, and safe redirect context but never "
                        "raw email, password, hash, or bearer capability."
                    ),
                    replay=(
                        "Request-event replay converges on one communication "
                        "intent by event id. Completion events are immutable "
                        "evidence; credential state remains authoritative."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name=(
                            "recovery-invalidated authentication session projections"
                        ),
                        input_names=("canonical recoverable principal state",),
                        writer="auth.credential_recovery",
                        freshness=(
                            "Completion-event dispatch invalidates auth and "
                            "portal session projections immediately after the "
                            "credential transaction commits."
                        ),
                        stale_behavior=(
                            "The event handler attempt remains failed and "
                            "retriable; durable auth_sessions revocation stays "
                            "authoritative while projection repair is pending."
                        ),
                        drift_signal=(
                            "A failed credential-session projection handler "
                            "attempt on the password_recovery.completed event."
                        ),
                        rebuild_operation=(
                            "Replay password_recovery.completed for the exact "
                            "principal to idempotently invalidate auth cache and "
                            "revoke customer or reseller portal sessions."
                        ),
                        repair_owner="auth.credential_recovery",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.auth_flow forgot_password_flow, "
                        "request_password_reset, and reset_password plus "
                        "synchronous web and administrative email helpers"
                    ),
                    new_owner="auth.credential_recovery",
                    verification=(
                        "Focused request, materialization, redemption, replay, "
                        "session-revocation, adapter, and architecture tests."
                    ),
                    cutover_gate=(
                        "Public API, shared web, customer, reseller, admin, "
                        "staff-invite, and reseller-invite paths call only the "
                        "contracted owner or exact in-memory materializer."
                    ),
                    fallback_retirement=(
                        "Synchronous recovery email delivery, persisted bearer "
                        "content, adapter-owned credential mutation, service "
                        "HTTP exceptions, and service commits are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_credential_recovery.py",
                    "tests/architecture/test_credential_recovery_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.customer_credential_enrollment",
            module="app.services.customer_credential_enrollment",
            owns=(
                "credential enrollment delivery request",
                "referral-created customer local credential enrollment",
                "credential enrollment capability purpose claims and lifetime",
                "single-use enrollment and account email verification consequence",
                "credential enrollment authentication cache projection",
            ),
            depends_on=(
                "auth.token_signing",
                "communications.intents",
                "customer.accounts",
                "referrals.account_conversion",
                "communications.ephemeral_actions",
                "control.settings_spec",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Creates no placeholder credential. The local credential and "
                "Subscriber email verification are committed together only "
                "after the emailed capability is redeemed. Party quarantine, "
                "Party contact verification, and account/subscription state "
                "remain with their existing owners."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="credential enrollment delivery request",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential enrollment command evidence",
                            "canonical referral account context",
                            "canonical customer credential state",
                            "credential enrollment policy settings",
                            "durable enrollment delivery intent",
                        ),
                        canonical_writer="auth.customer_credential_enrollment",
                    ),
                    ConcernContract(
                        name=("referral-created customer local credential enrollment"),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential enrollment command evidence",
                            "canonical referral account context",
                            "canonical customer credential state",
                            "credential enrollment policy settings",
                            "verified enrollment capability envelope",
                        ),
                        canonical_writer="auth.customer_credential_enrollment",
                    ),
                    ConcernContract(
                        name=(
                            "credential enrollment capability purpose claims "
                            "and lifetime"
                        ),
                        role=OwnerRole.POLICY,
                        input_names=(
                            "canonical referral account context",
                            "canonical customer credential state",
                            "credential enrollment policy settings",
                            "verified enrollment capability envelope",
                        ),
                    ),
                    ConcernContract(
                        name=(
                            "single-use enrollment and account email "
                            "verification consequence"
                        ),
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "credential enrollment command evidence",
                            "canonical customer credential state",
                            "verified enrollment capability envelope",
                        ),
                        canonical_writer="auth.customer_credential_enrollment",
                    ),
                    ConcernContract(
                        name=("credential enrollment authentication cache projection"),
                        role=OwnerRole.RECONCILER,
                        input_names=("canonical customer credential state",),
                        canonical_writer="auth.customer_credential_enrollment",
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="credential enrollment command evidence",
                        owner="auth.customer_credential_enrollment",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "typed CommandContext carrying the public referral "
                            "or capability actor, scope, reason, command, "
                            "correlation, and idempotency evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical referral account context",
                        owner="referrals.account_conversion",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active Referral, referred Party and Lead binding, "
                            "and the exact converted Subscriber identifier"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical customer credential state",
                        owner="auth.customer_credential_enrollment",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "active converted Subscriber identity, email and "
                            "email_verified state plus its local user_credential"
                        ),
                    ),
                    AuthorityInput(
                        name="credential enrollment policy settings",
                        owner="control.settings_spec",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "database-authoritative password minimum, user invite "
                            "lifetime, credential enrollment request limit, and "
                            "request window settings"
                        ),
                    ),
                    AuthorityInput(
                        name="verified enrollment capability envelope",
                        owner="auth.token_signing",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "signature and expiry verified referral enrollment "
                            "claims for one exact referral, Party, Lead, "
                            "Subscriber, and email digest"
                        ),
                    ),
                    AuthorityInput(
                        name="durable enrollment delivery intent",
                        owner="communications.intents",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "referral-deduplicated communication intent and "
                            "notification outbox with a non-secret ephemeral "
                            "action descriptor"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "Each request or redemption enters "
                        "execute_owner_command on a transaction-free adapter "
                        "session. Request intent, audit, and event, or local "
                        "credential, Subscriber email verification, audit, and "
                        "completion event commit or roll back together."
                    ),
                    locking=(
                        "Requests and redemption lock the exact Referral, Lead, "
                        "and Subscriber in canonical order. Redemption rechecks "
                        "the absence of a local credential; the normalized local "
                        "username unique index arbitrates cross-principal races."
                    ),
                    idempotency=(
                        "A referral has one communication intent dedupe key. "
                        "Delivery retries remint the bearer from canonical "
                        "context. Credential existence spends every outstanding "
                        "capability, so replay fails closed."
                    ),
                    retries=(
                        "Rolled-back commands may retry after transient database "
                        "failures. Rate-limited requests return a typed outcome; "
                        "delivery and cache projection retry independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.customer_credential_enrollment.invalid_command",
                        ("auth.customer_credential_enrollment.invalid_configuration"),
                        "auth.customer_credential_enrollment.context_not_found",
                        "auth.customer_credential_enrollment.stale_context",
                        "auth.customer_credential_enrollment.inactive_account",
                        "auth.customer_credential_enrollment.invalid_capability",
                        "auth.customer_credential_enrollment.invalid_password",
                        "auth.customer_credential_enrollment.invalid_username",
                        ("auth.customer_credential_enrollment.username_unavailable"),
                        ("auth.customer_credential_enrollment.invalid_command_context"),
                        (
                            "auth.customer_credential_enrollment."
                            "command_contract_violation"
                        ),
                        ("auth.customer_credential_enrollment.nested_owner_command"),
                        (
                            "auth.customer_credential_enrollment."
                            "active_caller_transaction"
                        ),
                        (
                            "auth.customer_credential_enrollment."
                            "nested_transaction_completion"
                        ),
                    ),
                    mapping_owner=(
                        "app.api.crm_referrals, app.api.auth_flow, and "
                        "app.services.web_customer_auth adapters"
                    ),
                    fail_closed_on=(
                        "invalid, expired, or spent capability",
                        "referral, Party, Lead, Subscriber, or recipient drift",
                        "inactive account or existing local credential",
                        "username collision",
                        "missing or invalid canonical policy configuration",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "customer_credential_enrollment.requested",
                        "customer_credential_enrollment.completed",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 contains canonical identifiers, command and "
                        "correlation evidence, delivery outcome, and an email "
                        "digest but never raw email, password, hash, rendered "
                        "content, or bearer capability."
                    ),
                    replay=(
                        "Request replay converges on the referral-deduplicated "
                        "communication intent. Completion replay leaves the "
                        "existing credential authoritative and repairs its auth "
                        "cache projection idempotently."
                    ),
                ),
                projections=(
                    ProjectionContract(
                        name="enrolled customer authentication cache",
                        input_names=("canonical customer credential state",),
                        writer="auth.customer_credential_enrollment",
                        freshness=(
                            "Completion-event dispatch invalidates the exact "
                            "subscriber authentication cache immediately after "
                            "the credential transaction commits."
                        ),
                        stale_behavior=(
                            "The handler attempt remains failed and retriable; "
                            "the committed credential and Subscriber email "
                            "verification remain authoritative."
                        ),
                        drift_signal=(
                            "A failed CredentialSessionProjectionHandler attempt "
                            "on customer_credential_enrollment.completed."
                        ),
                        rebuild_operation=(
                            "Replay customer_credential_enrollment.completed for "
                            "the exact subscriber to invalidate its auth cache."
                        ),
                        repair_owner="auth.customer_credential_enrollment",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "uncontracted request_referral_enrollment and "
                        "complete_referral_enrollment service functions with "
                        "helper commits, nested transactions, transport-coded "
                        "errors, and best-effort cache invalidation"
                    ),
                    new_owner="auth.customer_credential_enrollment",
                    verification=(
                        "Focused request, suppression, dedupe, materialization, "
                        "redemption, replay, drift, event, projection, adapter, "
                        "and architecture tests."
                    ),
                    cutover_gate=(
                        "Referral signup, public auth API, and customer portal "
                        "form submit only typed commands on transaction-free "
                        "sessions; materialization remains transport-only."
                    ),
                    fallback_retirement=(
                        "Service commits, savepoints, status-coded domain errors, "
                        "adapter keyword mutation calls, direct best-effort cache "
                        "invalidation, and duplicate delivery intents are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/REFERRAL_CREDENTIAL_ENROLLMENT.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_referral_credential_enrollment.py",
                    (
                        "tests/architecture/"
                        "test_customer_credential_enrollment_boundary.py"
                    ),
                ),
            ),
        ),
        SOTService(
            name="party.staff_authentication_reader",
            module="app.services.staff_party_authentication",
            owns=(
                "Party-keyed staff principal resolution",
                "staff authentication projection refusal",
            ),
            depends_on=(
                "party.registry",
                "party.staff_session_projection",
                "auth.staff_provisioning",
            ),
            notes=(
                "The single owner of staff principal resolution for "
                "authentication. Four consumers delegate: login, refresh, "
                "per-request session validation, and vendor login "
                "eligibility. Vendor ACCESS eligibility stays owned by the "
                "vendor module; only identity resolution moved here. "
                "resolve_staff_principal_by_party is the canonical "
                "primitive and the query direction is the contract: it "
                "starts at the Party and finds the principal, never the "
                "reverse. system_user_id is compared as the Sub-owned staff "
                "context assertion and never used to resolve, because "
                "resolving from it and checking Party afterwards would agree "
                "on healthy data while leaving the legacy key authoritative. "
                "Fails closed with typed refusals and no legacy fallback. "
                "The deploy-1 assertion-first resolver has been deleted: a "
                "staff session without party_id is unusable, while revoked and "
                "non-active historical rows remain preserved. Refresh resolves "
                "identity before token "
                "rotation, and every new staff session is minted from an "
                "explicit typed Party/context binding. Rollback floor is "
                "migration 534: never roll back below it, or new sessions "
                "mint without party_id."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="Party-keyed staff principal resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=(
                            "canonical Person Party identity",
                            "credential Party authentication projection",
                            "staff session Party projection",
                            "canonical staff context state",
                        ),
                    ),
                    ConcernContract(
                        name="staff authentication projection refusal",
                        role=OwnerRole.POLICY,
                        input_names=(
                            "credential Party authentication projection",
                            "staff session Party projection",
                            "canonical staff context state",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="canonical Person Party identity",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="parties Person identity records",
                    ),
                    AuthorityInput(
                        name="credential Party authentication projection",
                        owner="party.credential_authentication_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="user_credentials.party_id",
                    ),
                    AuthorityInput(
                        name="staff session Party projection",
                        owner="party.staff_session_projection",
                        kind=AuthorityKind.DERIVED_PROJECTION,
                        source="sessions.party_id",
                    ),
                    AuthorityInput(
                        name="canonical staff context state",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="system_users.person_party_id",
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.READ_ONLY,
                    boundary=(
                        "Authentication adapters supply a Session; the reader "
                        "performs no writes or transaction completion."
                    ),
                    locking="No locks; resolution reads one committed projection.",
                    idempotency="Repeated reads over one snapshot return one result.",
                    retries="Callers may retry only with a fresh transaction snapshot.",
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "staff_projection_missing",
                        "staff_party_has_no_principal",
                        "staff_party_owns_multiple_principals",
                        "staff_projection_conflict",
                    ),
                    mapping_owner="authentication adapters",
                    fail_closed_on=(
                        "missing Party projection",
                        "missing or ambiguous staff principal",
                        "Party and legacy assertion conflict",
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.CUT_OVER,
                    old_owner="direct credential and session system_user_id lookup",
                    new_owner="party.staff_authentication_reader",
                    verification=(
                        "Focused behavior, PostgreSQL projection, direction-sensitive "
                        "architecture, SOT contract tests, and the production "
                        "ratchet-readiness report."
                    ),
                    cutover_gate=(
                        "Every live staff session has an approved Party projection, "
                        "all readers require it, and the assertion-first bridge is "
                        "deleted."
                    ),
                    fallback_retirement=(
                        "The assertion-first compatibility bridge is deleted. "
                        "Rollback is limited to source "
                        "121e1592db795d339c1bc6279277797891d41064 at image digest "
                        "sha256:27b5324e765add48214b3668d39bb19557acbfac4c8a7edd"
                        "98a4fb22b6e0c19a, retaining sessions.party_id evidence and "
                        "never crossing migration 534."
                    ),
                ),
                steward="platform security",
                design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
                test_refs=(
                    "tests/test_staff_party_authentication.py",
                    "tests/integration/test_session_party_projection.py",
                    "tests/architecture/test_staff_party_authentication_owner.py",
                ),
            ),
        ),
        SOTService(
            name="auth.staff_provisioning",
            module="app.services.staff_provisioning",
            owns=(
                "staff account provisioning",
                "staff identity bootstrap",
                "staff identity maintenance",
                "staff login identity resolution",
                "staff field technician profile binding",
            ),
            depends_on=(
                "auth.rbac_catalog",
                "auth.system_user_assignments",
                "auth.permission_gate",
                "party.registry",
                "communications.intents",
                "communications.ephemeral_actions",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "ERP HR commands enter one verified coordinator transaction. "
                "This owner writes staff identity and credential bootstrap, "
                "keeps the canonical staff email and the one local credential "
                "username aligned even while access is inactive, prepares "
                "credential recovery, resolves credential drift and recovery "
                "eligibility for adapters, "
                "creates and binds one Person Party for every new principal, "
                "delegates managed grants to auth.system_user_assignments, "
                "stages audit and "
                "versioned events atomically, and leaves invite delivery to a "
                "deduplicated communication consequence. Invite capabilities "
                "are minted only at transport time and are never stored in the "
                "event or notification outbox."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="staff account provisioning",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "ERP HR staff lifecycle request",
                            "authorized RBAC assignment principal",
                            "active role catalog",
                            "managed role grant state",
                            "canonical staff identity and credential state",
                            "canonical Person Party identity",
                        ),
                    ),
                    ConcernContract(
                        name="staff identity bootstrap",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "ERP HR staff lifecycle request",
                            "canonical staff identity and credential state",
                            "canonical Person Party identity",
                        ),
                        canonical_writer="auth.staff_provisioning",
                    ),
                    ConcernContract(
                        name="staff identity maintenance",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authorized staff identity principal",
                            "canonical staff identity and credential state",
                            "staff-linked field technician profile",
                        ),
                    ),
                    ConcernContract(
                        name="staff field technician profile binding",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=(
                            "authorized staff identity principal",
                            "canonical staff identity and credential state",
                            "staff-linked field technician profile",
                        ),
                        canonical_writer="auth.staff_provisioning",
                    ),
                    ConcernContract(
                        name="staff login identity resolution",
                        role=OwnerRole.RESOLVER,
                        input_names=("canonical staff identity and credential state",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="ERP HR staff lifecycle request",
                        owner="external:dotmac_erp",
                        kind=AuthorityKind.EXTERNAL_OBSERVATION,
                        source=(
                            "typed provision, managed-role, and active-state "
                            "commands received by app.api.staff_sync"
                        ),
                    ),
                    AuthorityInput(
                        name="authorized RBAC assignment principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "rbac:assign authorization result carried in "
                            "CommandContext actor and scope evidence"
                        ),
                    ),
                    AuthorityInput(
                        name="authorized staff identity principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "rbac:assign administrator evidence or an authenticated "
                            "profile:self principal targeting its own SystemUser"
                        ),
                    ),
                    AuthorityInput(
                        name="active role catalog",
                        owner="auth.rbac_catalog",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="active roles",
                    ),
                    AuthorityInput(
                        name="managed role grant state",
                        owner="auth.system_user_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "source-scoped rows in system_user_roles and the "
                            "final-active-admin invariant"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical staff identity and credential state",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=("system_users and staff-bound local user_credentials"),
                    ),
                    AuthorityInput(
                        name="staff-linked field technician profile",
                        owner="auth.staff_provisioning",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "technician_profiles rows whose system_user_id/person_id "
                            "bind directly to the native staff SystemUser"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical Person Party identity",
                        owner="party.registry",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "new staff Person Party and complete "
                            "SystemUser.person_party_id binding evidence"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "Each public staff write enters execute_owner_command "
                        "on a transaction-free adapter session; identity, "
                        "credentials, profile and login-identity changes, RBAC "
                        "grants, session revocation, audit, "
                        "Person Party identity, and the outbox event commit "
                        "together before return."
                    ),
                    locking=(
                        "A PostgreSQL advisory transaction lock serializes "
                        "provisioning and identity maintenance by normalized "
                        "email in sorted old/new order; existing principals and "
                        "their local credentials "
                        "are selected FOR UPDATE, and database unique constraints "
                        "arbitrate identity and grant keys."
                    ),
                    idempotency=(
                        "Email is the provision natural key; managed roles, active "
                        "state, and the local credential username converge to "
                        "canonical staff state. Adapters carry a stable intent "
                        "key, and invite expansion deduplicates on the immutable "
                        "provisioning event id."
                    ),
                    retries=(
                        "Adapters may retry a failed request with the same "
                        "idempotency key. Domain validation is not retryable; "
                        "a concurrent identity change requires a fresh read and "
                        "reviewed retry; "
                        "event-store delivery retries consequences independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.staff_provisioning.invalid_command",
                        "auth.staff_provisioning.unknown_roles",
                        "auth.staff_provisioning.staff_account_not_found",
                        "auth.staff_provisioning.identity_conflict",
                        "auth.staff_provisioning.credential_not_found",
                        "auth.staff_provisioning.credential_ambiguous",
                        "auth.staff_provisioning.inactive_staff_account",
                        "auth.staff_provisioning.password_update_forbidden",
                        "auth.staff_provisioning.invalid_password",
                        "auth.staff_provisioning.stale_identity_evidence",
                        "auth.staff_provisioning.concurrent_identity_change",
                        "auth.system_user_assignments.last_admin_required",
                        "auth.staff_provisioning.invalid_command_context",
                        "auth.staff_provisioning.command_contract_violation",
                        "auth.staff_provisioning.nested_owner_command",
                        "auth.staff_provisioning.active_caller_transaction",
                        "auth.staff_provisioning.nested_transaction_completion",
                    ),
                    mapping_owner=(
                        "app.api.staff_sync, app.api.auth_flow, and "
                        "app.web.admin.system"
                    ),
                    fail_closed_on=(
                        "missing authorization evidence",
                        "unknown or inactive roles",
                        "identity conflict",
                        "missing or ambiguous local credential state",
                        "stale reviewed repair evidence",
                        "concurrent identity change",
                        "new principal without a complete Person Party binding",
                        "active caller transaction",
                        "nested command or transaction completion",
                        "manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "staff_account.provisioned",
                        "staff_account.roles_changed",
                        "staff_account.activated",
                        "staff_account.deactivated",
                        "staff_account.identity_changed",
                        "staff_account.credential_reconciled",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 is additive and PII-safe; breaking payload "
                        "changes require a new schema version."
                    ),
                    replay=(
                        "State events are immutable evidence. Staff invitation "
                        "intent expansion is idempotent by event_id and mints a "
                        "fresh short-lived capability only at delivery time."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.web_system_user_edit, "
                        "app.services.web_system_user_mutations, direct profile "
                        "writers, and the legacy multi-commit staff provisioning path"
                    ),
                    new_owner="auth.staff_provisioning",
                    verification=(
                        "Focused API, transaction, event, audit, RBAC, identity "
                        "reconciliation, recovery, and ephemeral-delivery tests "
                        "plus architecture guards."
                    ),
                    cutover_gate=(
                        "All staff-sync, administrative identity, self-profile, "
                        "activation, invitation, and password-recovery routes call "
                        "only typed owner commands for staff identity state."
                    ),
                    fallback_retirement=(
                        "Staff sync no longer calls web_system_user_mutations or "
                        "synchronous email delivery; web adapters no longer mutate "
                        "staff profile or local credential identity directly."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/designs/STAFF_LOGIN_IDENTITY_RECONCILIATION.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_api_staff_sync.py",
                    "tests/test_staff_provisioning_owner.py",
                    "tests/test_staff_login_identity_admin.py",
                    "tests/test_staff_login_identity_reconciliation_script.py",
                    "tests/architecture/test_staff_provisioning_boundary.py",
                ),
            ),
        ),
        SOTService(
            name="auth.reseller_onboarding",
            module="app.services.reseller_onboarding",
            owns=("reseller portal principal onboarding",),
            depends_on=(
                "customer.accounts",
                "auth.subscriber_assignments",
                "auth.permission_gate",
                "communications.intents",
                "communications.ephemeral_actions",
                "control.feature_registry",
                "events.dispatcher",
                "observability.audit_log",
            ),
            notes=(
                "Administrative reseller onboarding enters one verified "
                "coordinator transaction. Canonical reseller and fallback "
                "Subscriber initialization, portal identity and credential "
                "bootstrap, assignment-owner grants, audit, and events commit "
                "atomically. Invitations are deduplicated event consequences; "
                "reset capabilities are minted only at transport time for the "
                "exact principal and never persisted in the outbox."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="reseller portal principal onboarding",
                        role=OwnerRole.APPLICATION_COORDINATOR,
                        input_names=(
                            "authorized reseller onboarding principal",
                            "canonical reseller and subscriber account state",
                            "canonical subscriber assignment state",
                            "reseller principal cutover gate",
                            "canonical reseller onboarding state",
                        ),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="authorized reseller onboarding principal",
                        owner="auth.permission_gate",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "reseller:write and, when needed, rbac:assign "
                            "evidence carried in correlated CommandContexts"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical reseller and subscriber account state",
                        owner="customer.accounts",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "resellers, subscribers, and transaction-neutral "
                            "canonical initialization collaborators"
                        ),
                    ),
                    AuthorityInput(
                        name="canonical subscriber assignment state",
                        owner="auth.subscriber_assignments",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source="subscriber_roles and active role catalog references",
                    ),
                    AuthorityInput(
                        name="reseller principal cutover gate",
                        owner="control.feature_registry",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source="reseller_user_principal_enabled feature setting",
                    ),
                    AuthorityInput(
                        name="canonical reseller onboarding state",
                        owner="auth.reseller_onboarding",
                        kind=AuthorityKind.AUTHORITATIVE_RECORD,
                        source=(
                            "reseller_users and reseller-bound local user_credentials"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.COORDINATOR_MANAGED,
                    boundary=(
                        "Each public onboarding command enters "
                        "execute_owner_command on a transaction-free adapter "
                        "session; every record, grant, audit event, and outbox "
                        "event commits or rolls back together."
                    ),
                    locking=(
                        "Existing resellers and active role references are "
                        "selected FOR UPDATE. PostgreSQL advisory transaction "
                        "locks serialize normalized email and username keys, "
                        "with database constraints arbitrating remaining races."
                    ),
                    idempotency=(
                        "Adapters carry stable intent keys as hashed evidence. "
                        "Identity conflicts fail closed, assignment grants "
                        "converge, and invite expansion deduplicates by event id."
                    ),
                    retries=(
                        "Adapters may retry a rolled-back command with the same "
                        "intent key. Validation and identity conflicts require "
                        "changed input; event delivery retries independently."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "auth.reseller_onboarding.invalid_command",
                        ("auth.reseller_onboarding.assignment_authorization_required"),
                        "auth.reseller_onboarding.identity_conflict",
                        "auth.reseller_onboarding.reseller_not_found",
                        "auth.reseller_onboarding.inactive_reseller",
                        "auth.reseller_onboarding.role_not_found",
                        "auth.reseller_onboarding.unsupported_role_target",
                        ("auth.reseller_onboarding.invalid_command_context"),
                        ("auth.reseller_onboarding.command_contract_violation"),
                        "auth.reseller_onboarding.nested_owner_command",
                        "auth.reseller_onboarding.active_caller_transaction",
                        ("auth.reseller_onboarding.nested_transaction_completion"),
                    ),
                    mapping_owner="app.web.admin.resellers",
                    fail_closed_on=(
                        "missing or mismatched authorization evidence",
                        "inactive reseller or role",
                        "identity collision",
                        "unsupported first-class principal role assignment",
                        "active caller transaction or manifest mismatch",
                    ),
                ),
                events=EventContract(
                    event_types=(
                        "reseller.created",
                        "reseller_user.provisioned",
                        "subscriber.created",
                    ),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 onboarding events contain identifiers, "
                        "role names, and an email digest but no PII, password, "
                        "or reset capability."
                    ),
                    replay=(
                        "Events are immutable evidence. Invitation expansion "
                        "is idempotent by event id and revalidates the exact "
                        "canonical principal before minting a capability."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.COMPLETE,
                    old_owner=(
                        "app.services.web_admin_resellers and "
                        "app.services.reseller_portal multi-commit onboarding"
                    ),
                    new_owner="auth.reseller_onboarding",
                    verification=(
                        "Focused atomicity, delivery, reset, adapter, manifest, "
                        "and architecture-boundary tests."
                    ),
                    cutover_gate=(
                        "Admin reseller creation and add-user routes call only "
                        "typed coordinator commands."
                    ),
                    fallback_retirement=(
                        "Compensating deletion, direct onboarding commits, and "
                        "synchronous invite delivery are removed."
                    ),
                ),
                steward="platform security",
                design_refs=(
                    "docs/SOT_RELATIONSHIP_MAP.md",
                    "docs/adr/0002-owner-command-transaction-boundary.md",
                    "docs/designs/SOT_CODING_STANDARDS_REFACTOR.md",
                ),
                test_refs=(
                    "tests/test_reseller_onboarding.py",
                    "tests/architecture/test_reseller_onboarding_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.api.*",
        "app.web.admin.*",
        "app.web.auth.*",
        "app.web.customer.auth",
    ),
    rule="Routes declare permission requirements; RBAC services own role and "
    "permission mutation. Business services should receive an authorized "
    "principal, not perform route-level permission wiring.",
)
