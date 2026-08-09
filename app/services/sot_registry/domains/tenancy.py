"""Canonical SOT declarations for the tenancy domain."""

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
from app.services.sot_registry.model import DomainSOT

DOMAIN = DomainSOT(
    domain="tenancy",
    services=(
        SOTService(
            name="tenancy.operator_tenant",
            module="app.services.operator_tenant",
            owns=(
                "operator tenant identity",
                "operator tenant provisioning",
                "single-tenant deployment invariant",
            ),
            depends_on=(),
            notes=(
                "ADR-0009. Sub is a dedicated single-operator deployment and "
                "the ISP operator IS the tenant, per starter ADR-0003: a "
                "single-tenant deployment provisions exactly one tenant, and "
                "single tenancy is a topology rather than a second "
                "architecture. This owner exposes identity, idempotent "
                "provisioning, and lookup, and deliberately no update, delete "
                "or list — there is one tenant and it is not an "
                "operator-editable resource. Provisioning is an existence "
                "check, never an upsert, so a name an operator changed "
                "survives the next restart. It runs before every settings "
                "seed because settings are tenant-scoped. `Tenant` is the "
                "kernel's model, admitted by name only through the adoption "
                "ledger; importing it constructs no engine and `app/db.py` "
                "remains the session and transaction authority."
            ),
            contract=ServiceContract(
                concerns=(
                    ConcernContract(
                        name="operator tenant identity",
                        role=OwnerRole.AUTHORITATIVE_RECORD,
                        input_names=("deterministic operator tenant id",),
                        canonical_writer="tenancy.operator_tenant",
                    ),
                    ConcernContract(
                        name="operator tenant provisioning",
                        role=OwnerRole.COMMAND_WRITER,
                        input_names=("deterministic operator tenant id",),
                        canonical_writer="tenancy.operator_tenant",
                    ),
                    ConcernContract(
                        name="single-tenant deployment invariant",
                        role=OwnerRole.POLICY,
                        input_names=("deterministic operator tenant id",),
                    ),
                ),
                authoritative_inputs=(
                    AuthorityInput(
                        name="deterministic operator tenant id",
                        owner="tenancy.operator_tenant",
                        kind=AuthorityKind.CONTROL_INPUT,
                        source=(
                            "OPERATOR_TENANT_ID, a fixed uuid5 of "
                            "operator.sub.dotmac, copied into migration 509 so "
                            "the backfill and the runtime agree without "
                            "importing application code into a migration"
                        ),
                    ),
                ),
                transaction=TransactionContract(
                    mode=TransactionMode.OWNER_MANAGED,
                    boundary=(
                        "provision_operator_tenant commits its own insert; it "
                        "runs at startup before any settings seed, not inside "
                        "another owner's command."
                    ),
                    locking=(
                        "The tenants primary key serialises a concurrent "
                        "provision; the loser observes the existing row."
                    ),
                    idempotency=(
                        "An existence check on the deterministic id, never an "
                        "upsert, so a name an operator changed survives every "
                        "later boot."
                    ),
                    retries=(
                        "Safe to re-run without limit; a second call returns "
                        "the existing row untouched."
                    ),
                ),
                errors=ErrorContract(
                    domain_codes=(
                        "tenancy.operator_tenant.missing",
                        # Owner-command boundary codes, required of any
                        # owner-managed writer by the manifest contract.
                        "tenancy.operator_tenant.active_caller_transaction",
                        "tenancy.operator_tenant.command_contract_violation",
                        "tenancy.operator_tenant.invalid_command_context",
                        "tenancy.operator_tenant.nested_owner_command",
                        "tenancy.operator_tenant.nested_transaction_completion",
                    ),
                    mapping_owner="app.main startup and migration 509",
                    fail_closed_on=(
                        "reading the operator tenant before it is provisioned, "
                        "because a tenant-scoped write would otherwise be "
                        "attributed to nothing",
                    ),
                ),
                events=EventContract(
                    event_types=("operator_tenant.provisioned",),
                    schema_version=1,
                    delivery_owner="events.dispatcher",
                    compatibility=(
                        "Version 1 carries the tenant id only. The operator "
                        "tenant's id is deterministic, so a subscriber needs "
                        "no payload beyond the fact that provisioning ran."
                    ),
                    replay=(
                        "At most one provisioning per deployment. A replay "
                        "observes the tenant already exists and is a no-op, "
                        "because provisioning is an existence check rather "
                        "than an upsert."
                    ),
                ),
                migration=MigrationContract(
                    state=AuthorityMigrationState.NATIVE,
                    new_owner="tenancy.operator_tenant",
                    old_owner=None,
                    verification=(
                        "tests/test_operator_tenant.py proves exactly one "
                        "tenant, idempotence across boots, that provisioning "
                        "never reverts an operator edit, and that migration "
                        "509's copy of the id still matches the runtime."
                    ),
                    cutover_gate=(
                        "Migration 509 inserts the tenant and moves every "
                        "domain_settings row from platform to tenant scope in "
                        "one transaction."
                    ),
                    fallback_retirement=(
                        "The platform-scope default introduced by migration "
                        "507 is replaced; no fallback survives."
                    ),
                ),
                steward="platform",
                design_refs=(
                    "docs/adr/0009-operator-tenant-bridge.md",
                    "docs/PLATFORM_ADOPTION_LEDGER.md",
                ),
                test_refs=(
                    "tests/test_operator_tenant.py",
                    "tests/architecture/test_kernel_import_boundary.py",
                ),
            ),
        ),
    ),
    entrypoints=(
        "app.main",
        "startup settings seed",
        "migration 509 backfill",
        "future tenant-scoped kernel module adoptions",
    ),
    rule="Exactly one tenant exists and it is the ISP operator. Every "
    "tenant-scoped row carries its id; nothing Sub owns is deployment-wide "
    "above the operator. A second tenant row is a defect until an ADR "
    "supersedes ADR-0009.",
)
