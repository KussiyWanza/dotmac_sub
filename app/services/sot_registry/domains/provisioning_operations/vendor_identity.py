"""provisioning_operations SOT declarations: vendor identity."""

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
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="auth.vendor_user_provisioning",
        module="app.services.vendor_user_provisioning",
        owns=(
            "vendor portal login provisioning and revocation",
            "vendor organisation role assignment",
        ),
        depends_on=("auth.permission_gate",),
        notes=(
            "One vendor login is a SystemUser marked UserType.vendor, a "
            "must-change local credential, and the FieldVendorUser "
            "membership auth resolves through; any subset is a broken "
            "identity, so all three are staged together. This owner never "
            "mints or delivers a usable secret. Capability for the "
            "assigned role is declared by field.vendor_capabilities; this "
            "owner stores the role and never decides what it may do."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="vendor portal login provisioning and revocation",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("vendor portal login command",),
                    canonical_writer="auth.vendor_user_provisioning",
                ),
                ConcernContract(
                    name="vendor organisation role assignment",
                    role=OwnerRole.COMMAND_WRITER,
                    input_names=("vendor portal login command",),
                    canonical_writer="auth.vendor_user_provisioning",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="vendor portal login command",
                    owner="auth.permission_gate",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "staff-authorized vendor, identity, and role "
                        "identifiers from the admin vendor surface"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.OWNER_MANAGED,
                boundary=(
                    "Principal, credential, and membership commit together "
                    "or not at all."
                ),
                locking="Email uniqueness is rechecked before the write.",
                idempotency=(
                    "Email uniqueness on system_users makes a repeated "
                    "provisioning attempt a refusal, never a duplicate "
                    "principal."
                ),
                retries=(
                    "No automatic retry: a collision is an identity "
                    "question for staff to resolve."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "auth.vendor_user_provisioning.vendor_not_found",
                    "auth.vendor_user_provisioning.vendor_inactive",
                    "auth.vendor_user_provisioning.email_in_use",
                    "auth.vendor_user_provisioning.email_required",
                    "auth.vendor_user_provisioning.name_required",
                    "auth.vendor_user_provisioning.unknown_role",
                    "auth.vendor_user_provisioning.membership_not_found",
                    "auth.vendor_user_provisioning.membership_inactive",
                    "auth.vendor_user_provisioning.principal_not_vendor",
                    "auth.vendor_user_provisioning.credential_not_found",
                    "auth.vendor_user_provisioning.credential_projection_conflict",
                    "auth.vendor_user_provisioning.active_caller_transaction",
                    "auth.vendor_user_provisioning.command_contract_violation",
                    "auth.vendor_user_provisioning.invalid_command_context",
                    "auth.vendor_user_provisioning.nested_owner_command",
                    "auth.vendor_user_provisioning.nested_transaction_completion",
                ),
                mapping_owner="app.web.admin.vendors",
            ),
            events=EventContract(
                event_types=(
                    "vendor_user.provisioned",
                    "vendor_user.revoked",
                    "vendor_user.role_changed",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Version 1 is additive and carries vendor-user, "
                    "field-vendor, principal, and role identity. It never "
                    "carries a credential."
                ),
                replay=(
                    "FieldVendorUser and its SystemUser rebuild membership "
                    "and capability; the credential is never replayed."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="auth.vendor_user_provisioning",
                verification=(
                    "Provisioning, identity-collision, inactive-vendor, "
                    "role, revocation, and route-capability tests."
                ),
            ),
            steward="vendor operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=(
                "tests/test_vendor_identity.py",
                "tests/test_vendor_portal_auth.py",
            ),
        ),
    ),
)
