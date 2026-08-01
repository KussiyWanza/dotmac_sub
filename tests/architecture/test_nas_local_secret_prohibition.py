"""Architecture guard: the NAS carries no per-customer PPPoE record.

RouterOS consults RADIUS only when the username is absent from ``/ppp secret``
(https://help.mikrotik.com/docs/spaces/ROS/pages/132350049/PPP+AAA). A local
secret therefore BYPASSES ``access.radius_projection`` rather than overriding an
attribute of it, which is how an activation-time ``remote-address`` kept serving
a released IPv4 long after IPAM, the served projection and RADIUS had all moved
on — and how that address could be reallocated to a second subscriber.

These tests pin the retirement so it cannot come back as a "small fix":

* neither execution surface may emit a local-secret create/enable/disable, and
* the prohibition must be enforced on the operator-editable template runner as
  well as the hard-coded command builder, or a database row alone reintroduces
  the defect with a green diff.

Restoring an update path is NOT the correction. Keeping a parallel authority in
sync preserves it; only removal retires it. See
``docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.sot_manifest import AuthorityMigrationState, OwnerRole
from app.services.sot_relationships import service_relationship

ROOT = Path(__file__).resolve().parents[2]
BOUNDARY = ROOT / "app/services/nas/local_secret_policy.py"
COMMAND_BUILDER = ROOT / "app/services/connection_type_provisioning.py"
VENDOR_ADAPTER = ROOT / "app/services/nas/vendor_adapter.py"
TEMPLATE_RUNNER = ROOT / "app/services/nas/provisioner.py"
ACTIVATION_HANDLER = ROOT / "app/services/events/handlers/provisioning.py"
TEMPLATE_STORE = ROOT / "app/services/nas/templates.py"
CANCEL_HANDLER = ROOT / "app/services/events/handlers/enforcement.py"

# Every module that composes RouterOS command text for a subscriber. The
# boundary owner is excluded: it is the one place allowed to name the verbs,
# because removal is how the parallel authority is retired.
_COMMAND_EMITTING_MODULES = (
    COMMAND_BUILDER,
    VENDOR_ADAPTER,
    TEMPLATE_RUNNER,
    ACTIVATION_HANDLER,
)

# `/ppp secret add` establishes the shadowing record; `/ppp secret set ...
# disabled=` toggles access on it. Both are the authority we retired.
_CREATE_PATTERN = re.compile(r"/ppp\s+secret\s+add")
_TOGGLE_PATTERN = re.compile(r"/ppp\s+secret\s+set[^\n]*disabled=")
_ADDRESS_PATTERN = re.compile(r"remote-address=")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_no_module_can_create_a_local_pppoe_secret() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _COMMAND_EMITTING_MODULES
        if _CREATE_PATTERN.search(_source(path))
    ]

    assert not offenders, (
        "`/ppp secret add` reintroduces a NAS-local authority that bypasses "
        "access.radius_projection:\n  " + "\n  ".join(offenders)
    )


def test_no_module_can_enable_or_disable_a_local_pppoe_secret() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _COMMAND_EMITTING_MODULES
        if _TOGGLE_PATTERN.search(_source(path))
    ]

    assert not offenders, (
        "Local-secret enable/disable is parallel access enforcement; CoA through "
        "access.session_enforcement owns suspension:\n  " + "\n  ".join(offenders)
    )


def test_no_module_can_write_a_customer_address_onto_the_nas() -> None:
    """The update path the lifecycle decision explicitly rejected."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _COMMAND_EMITTING_MODULES
        if _ADDRESS_PATTERN.search(_source(path))
    ]

    assert not offenders, (
        "`remote-address=` gives the NAS an independent customer IPv4, which "
        "docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md prohibits:\n  "
        + "\n  ".join(offenders)
    )


def test_removal_lives_only_in_the_reviewed_cleanup_owner() -> None:
    """Deletion is corrective, so exactly one module may express it."""
    assert "/ppp secret remove" in _source(BOUNDARY)

    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _COMMAND_EMITTING_MODULES
        if "/ppp secret remove" in _source(path)
    ]

    assert not offenders, (
        "Local-secret removal must run through the reviewed cleanup operation "
        "with its shared-login check and device readback, not from a command "
        "builder:\n  " + "\n  ".join(offenders)
    )


def test_both_execution_surfaces_consult_the_boundary() -> None:
    """A database template must not be able to route around the prohibition."""
    builder = _source(COMMAND_BUILDER)
    runner = _source(TEMPLATE_RUNNER)

    assert "local_secret_policy" in builder
    assert "local_secret_policy.decide(" in builder

    assert "local_secret_policy" in runner
    assert "decide_for_provisioning_action(" in runner
    # The template runner must rule BEFORE it looks a template up, or a missing
    # template would 404 while a present one would execute.
    assert runner.index("decide_for_provisioning_action(") < runner.index(
        "ProvisioningTemplates.find_template("
    )


def test_cleanup_refuses_shared_logins_and_verifies_the_device() -> None:
    source = _source(BOUNDARY)

    assert "_subscriptions_for_login(" in source
    assert "_radius_projects_login(" in source
    # A removal that cannot be proven is a failure, never a success.
    assert "verified_absent" in source
    assert "raise LocalSecretCleanupError(" in source


def test_readback_is_count_only_so_it_cannot_echo_a_pppoe_password() -> None:
    """`/ppp secret print detail` returns the stored password. Never use it."""
    source = _source(BOUNDARY)

    assert "/ppp secret print" not in source
    assert "print detail" not in source
    assert ":len [/ppp secret find" in source


def test_every_refusal_has_its_own_stable_code() -> None:
    """Category-level codes hide which precondition actually failed."""
    from app.services.nas import local_secret_policy

    codes = set(local_secret_policy.CLEANUP_ERROR_CODES)

    assert len(codes) == len(local_secret_policy.CLEANUP_ERROR_CODES)
    assert local_secret_policy.CLEANUP_SHARED_LOGIN in codes
    assert local_secret_policy.CLEANUP_RADIUS_NOT_SERVING in codes
    assert local_secret_policy.CLEANUP_RADIUS_STILL_SERVING in codes
    assert local_secret_policy.CLEANUP_DEPENDENT_SUBSCRIPTION in codes
    # The retired catch-all must not come back.
    assert "nas_local_secret_cleanup_refused" not in codes


def test_command_text_guard_runs_on_render_and_on_save() -> None:
    """The action label is not a boundary; the rendered body must be checked."""
    runner = _source(TEMPLATE_RUNNER)
    templates = _source(TEMPLATE_STORE)

    assert "assert_command_text_allowed(" in runner
    # ...and it must run AFTER render, or it inspects nothing.
    assert runner.index("ProvisioningTemplates.render(") < runner.index(
        "assert_command_text_allowed("
    )
    # Saving a bad template must fail at authoring time too.
    assert "_assert_template_body_allowed(" in templates
    assert templates.count("_assert_template_body_allowed(") >= 3


def test_change_speed_is_prohibited_not_merely_deferred() -> None:
    from app.models.catalog import ConnectionType, NasVendor
    from app.services.nas import local_secret_policy

    ruling = local_secret_policy.decide(
        vendor=NasVendor.mikrotik,
        connection_type=ConnectionType.pppoe,
        action=local_secret_policy.LocalSecretAction.change_speed,
    )

    assert ruling.decision is local_secret_policy.LocalSecretDecision.prohibited


def test_retirement_is_durably_recorded_not_just_logged() -> None:
    """The registry declares an event; a log line would not satisfy it."""
    source = _source(BOUNDARY)

    assert "network_operations.start(" in source
    assert "mark_failed(" in source
    assert "mark_succeeded(" in source
    assert "correlation_key=" in source


def test_cancellation_stages_retirement_without_being_able_to_roll_it_back() -> None:
    handler = _source(CANCEL_HANDLER)
    boundary = _source(BOUNDARY)

    assert "stage_terminal_retirement(" in handler
    # Staged AFTER the block/projection call, so RADIUS has stopped serving.
    assert handler.index('_handle_subscription_block(db, event, "canceled")') < (
        handler.index("self._stage_local_secret_retirement(db, event)")
    )
    # The stager swallows failure into the operation ledger rather than raising
    # into an authoritative lifecycle transition.
    staged = boundary[boundary.index("def stage_terminal_retirement(") :]
    assert "return None" in staged


def test_non_pppoe_provisioning_is_untouched() -> None:
    """DHCP and hotspot keep their own device state; only PPPoE is retired."""
    builder = _source(COMMAND_BUILDER)

    assert "/ip dhcp-server lease add" in builder
    assert "/ip hotspot user add" in builder


def test_boundary_is_registered_as_a_contracted_owner() -> None:
    service = service_relationship("network.nas_local_secret_boundary")

    assert service.module == "app.services.nas.local_secret_policy"
    assert service.contract is not None
    roles = [concern.role for concern in service.contract.concerns]
    # Two policy concerns (action ruling, command-text admissibility) plus the
    # single command writer that may touch a device.
    assert roles.count(OwnerRole.POLICY) == 2
    assert roles.count(OwnerRole.COMMAND_WRITER) == 1
    assert service.contract.migration.state is AuthorityMigrationState.CUT_OVER
    assert service.contract.migration.old_owner == (
        "NAS-local per-customer PPPoE secret"
    )
    # Access itself moved to RADIUS; this owner keeps only the prohibition and
    # the corrective removal.
    assert "access.radius_projection" in service.contract.migration.verification
    assert service.contract.errors.fail_closed_on
