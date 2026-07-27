"""Architecture guards for exact-service IPAM ownership migration."""

import ast
from pathlib import Path

from app.services.sot_manifest import (
    AuthorityMigrationState,
    OwnerRole,
    TransactionMode,
)
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
OWNER = ROOT / "app/services/ip_assignment_lifecycle.py"
SCRIPT = ROOT / "scripts/one_off/repair_ipam_to_served.py"
LIFECYCLE_SCRIPT = ROOT / "scripts/one_off/repair_service_ipv4_assignment.py"
PROJECTION_SCRIPT = ROOT / "scripts/one_off/repair_service_ipv4_projection.py"

# The complete set of modules allowed to construct an IPAssignment while the
# lifecycle owner is in its SHADOWING phase. Everything other than the owner is
# declared migration debt in docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md, to be
# migrated behind the owner command at the runtime cutover.
#
# This list may SHRINK without discussion. Growing it means a fifth parallel
# writer to the address-allocation authority, which is the exact drift the
# shadowing phase exists to end -- so a new entry needs an architecture
# decision recording the alternative owner and how drift is prevented, not a
# green diff.
_IP_ASSIGNMENT_WRITERS = {
    "app/services/ip_assignment_lifecycle.py",  # the owner
    "app/services/network/ip.py",  # debt: provisioning allocation/reactivation
    "app/services/network/subscriber_wan_ipam.py",  # debt: ONT WAN claims
    "app/services/web_network_ip.py",  # debt: admin assignment + bulk import
}

# Modules that retire an allocation (`assignment.is_active = False`). This set
# is LARGER than the constructor set and only partly overlaps it: four modules
# retire an address without ever creating one. Pinning constructors alone would
# have left retirement -- reclaiming a live customer address -- unguarded.
_IP_ASSIGNMENT_DEACTIVATORS = {
    "app/services/ip_assignment_lifecycle.py",  # the owner
    "app/services/ip_lifecycle.py",  # debt: terminal lifecycle release
    "app/services/network/ip.py",  # debt: provisioning allocation/reactivation
    "app/services/network/subscriber_wan_ipam.py",  # debt: ONT WAN claims
    "app/services/provisioning_helpers.py",  # debt: provisioning reclaim
    "app/services/subscriber.py",  # debt: subscriber deletion cleanup
    "app/services/web_system_restore_tool.py",  # debt: restore tooling
}


def test_ipam_ownership_owner_has_complete_transaction_contract() -> None:
    service = service_relationship("network.ip_assignment_lifecycle")

    assert service.module == "app.services.ip_assignment_lifecycle"
    assert service.contract is not None
    assert service.contract.transaction.mode is TransactionMode.OWNER_MANAGED
    assert service.contract.migration.state is AuthorityMigrationState.SHADOWING
    assert service.contract.concerns[0].role is OwnerRole.RECONCILER
    assert service.contract.concerns[1].role is OwnerRole.COMMAND_WRITER
    assert service.contract.concerns[2].role is OwnerRole.COMMAND_WRITER


def test_legacy_projection_authority_and_partial_commit_paths_are_retired() -> None:
    source = OWNER.read_text(encoding="utf-8")

    assert "backfill_create" not in source
    assert "reclaim_stale" not in source
    assert "dedupe_active" not in source
    assert "db.commit()" not in source
    assert "db.rollback()" not in source
    assert "execute_owner_command(" in source
    assert source.count("subscription.ipv4_address =") == 1


def test_operator_adapter_is_dry_run_first_and_fingerprint_gated() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert "reconcile_ip_assignment_service_ownership(" in source
    assert ".commit(" not in source


def test_lifecycle_operator_adapter_is_dry_run_first_and_fingerprint_gated() -> None:
    source = LIFECYCLE_SCRIPT.read_text(encoding="utf-8")

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert '"--desired-address-id"' in source
    assert '"--deactivate-assignment-id"' in source
    assert "preview_service_ipv4_assignment_repair(" in source
    assert "repair_service_ipv4_assignment(" in source
    assert ".commit(" not in source


def test_projection_operator_adapter_is_dry_run_first_and_fingerprint_gated() -> None:
    source = PROJECTION_SCRIPT.read_text(encoding="utf-8")

    assert '"--apply"' in source
    assert '"--fingerprint"' in source
    assert '"--idempotency-key"' in source
    assert '"--assignment-id"' in source
    assert "preview_service_ipv4_projection_repair(" in source
    assert "repair_service_ipv4_projection(" in source
    assert ".commit(" not in source


def test_ip_assignment_writers_are_pinned_during_shadowing() -> None:
    """The declared migration debt must not grow while the owner shadows.

    The design doc says the legacy writers "must not be treated as precedent or
    expanded". Prose does not stop a fifth writer landing in a green diff, so
    pin the set the same way the prepaid owners are pinned (see
    test_prepaid_timer_fields_have_one_canonical_writer). Deleting an entry as
    a caller migrates behind the owner command is the expected direction.
    """
    writers: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/models/network.py":
            continue  # the model's own definition, not a writer
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "IPAssignment"
            ):
                writers.add(relative)

    assert writers == _IP_ASSIGNMENT_WRITERS


def test_ip_assignment_deactivators_are_pinned_during_shadowing() -> None:
    """`is_active = False` retires an allocation, so pin it separately.

    Creation and retirement are different consequences with different blast
    radii: a parallel deactivator silently reclaims a live customer address
    without entering the fingerprint-gated command. Seven modules retire an
    allocation against four that create one, so pinning constructors alone
    would have left the larger surface unguarded.
    """
    deactivators: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/models/network.py":
            continue
        source = path.read_text(encoding="utf-8")
        # Name-based AST matching cannot tell an IPAssignment from a team-inbox
        # assignment, so require the module to reference the model at all.
        if "IPAssignment" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "is_active"
                    and isinstance(target.value, ast.Name)
                    and "assignment" in target.value.id.lower()
                ):
                    deactivators.add(relative)

    assert deactivators == _IP_ASSIGNMENT_DEACTIVATORS
