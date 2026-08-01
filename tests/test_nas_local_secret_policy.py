"""Rulings and command-text admissibility for the NAS-local secret boundary.

Retirement behaviour lives in ``tests/test_nas_local_secret_retirement.py``;
the structural prohibition is pinned in
``tests/architecture/test_nas_local_secret_prohibition.py``.
"""

from __future__ import annotations

import pytest

from app.models.catalog import ConnectionType, NasVendor, ProvisioningAction
from app.services.nas import local_secret_policy as policy


@pytest.mark.parametrize(
    "action",
    [
        policy.LocalSecretAction.create,
        policy.LocalSecretAction.suspend,
        policy.LocalSecretAction.unsuspend,
        policy.LocalSecretAction.change_ip,
        policy.LocalSecretAction.change_speed,
    ],
)
def test_mikrotik_pppoe_mutations_are_prohibited(action):
    ruling = policy.decide(
        vendor=NasVendor.mikrotik,
        connection_type=ConnectionType.pppoe,
        action=action,
    )

    assert ruling.decision is policy.LocalSecretDecision.prohibited
    assert not ruling.emits_commands
    assert "RADIUS owned" in ruling.reason


def test_change_speed_is_prohibited_even_though_speed_migration_is_deferred():
    """It mutates the same shadow record, so it cannot stay permitted."""
    ruling = policy.decide(
        vendor=NasVendor.mikrotik,
        connection_type=ConnectionType.pppoe,
        action=policy.LocalSecretAction.change_speed,
    )

    assert ruling.decision is policy.LocalSecretDecision.prohibited
    assert ruling.owner == policy.RADIUS_OWNER


def test_suspension_is_attributed_to_session_enforcement():
    """Retiring the local toggle must not leave suspension ownerless."""
    ruling = policy.decide(
        vendor=NasVendor.mikrotik,
        connection_type=ConnectionType.pppoe,
        action=policy.LocalSecretAction.suspend,
    )

    assert ruling.owner == policy.SESSION_ENFORCEMENT_OWNER


def test_deletion_is_cleanup_only_not_prohibited():
    ruling = policy.decide(
        vendor=NasVendor.mikrotik,
        connection_type=ConnectionType.pppoe,
        action=policy.LocalSecretAction.delete,
    )

    assert ruling.decision is policy.LocalSecretDecision.cleanup_only
    assert not ruling.emits_commands
    assert "typed cleanup operation" in ruling.reason


@pytest.mark.parametrize(
    ("vendor", "connection_type"),
    [
        (NasVendor.mikrotik, ConnectionType.dhcp),
        (NasVendor.mikrotik, ConnectionType.hotspot),
        (NasVendor.mikrotik, ConnectionType.static),
        (NasVendor.cisco, ConnectionType.pppoe),
        (NasVendor.huawei, ConnectionType.pppoe),
    ],
)
def test_other_vendors_and_connection_types_are_untouched(vendor, connection_type):
    ruling = policy.decide(
        vendor=vendor,
        connection_type=connection_type,
        action=policy.LocalSecretAction.create,
    )

    assert ruling.decision is policy.LocalSecretDecision.not_applicable
    assert ruling.emits_commands


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ProvisioningAction.create_user, policy.LocalSecretDecision.prohibited),
        (ProvisioningAction.suspend_user, policy.LocalSecretDecision.prohibited),
        (ProvisioningAction.unsuspend_user, policy.LocalSecretDecision.prohibited),
        (ProvisioningAction.change_ip, policy.LocalSecretDecision.prohibited),
        (ProvisioningAction.change_speed, policy.LocalSecretDecision.prohibited),
        (ProvisioningAction.delete_user, policy.LocalSecretDecision.cleanup_only),
        (ProvisioningAction.backup_config, policy.LocalSecretDecision.not_applicable),
        (ProvisioningAction.reset_session, policy.LocalSecretDecision.not_applicable),
    ],
)
def test_template_actions_map_onto_the_same_rulings(action, expected):
    ruling = policy.decide_for_provisioning_action(
        vendor=NasVendor.mikrotik,
        connection_type=ConnectionType.pppoe,
        action=action,
    )

    assert ruling.decision is expected


def test_ruling_log_extra_is_provenanced():
    ruling = policy.decide(
        vendor=NasVendor.mikrotik,
        connection_type=ConnectionType.pppoe,
        action=policy.LocalSecretAction.create,
    )

    extra = ruling.as_log_extra()

    assert extra["nas_local_secret_decision"] == "prohibited"
    assert extra["nas_local_secret_action"] == "create"
    assert extra["owner"] == policy.RADIUS_OWNER


# ---------------------------------------------------------------------------
# Command-text admissibility — the action label is not a boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('/ppp secret add name="a" remote-address=10.0.0.1', "ppp-secret-add"),
        ('/ppp secret set [find name="a"] disabled=yes', "ppp-secret-set"),
        ('/ppp secret remove [find name="a"]', "ppp-secret-remove"),
        ("/ppp/secret/add name=a", "ppp-secret-path"),
        ("/ip dhcp-server lease add remote-address=10.0.0.2", "remote-address"),
        ('/PPP  SECRET  ADD name="a"', "ppp-secret-add"),
    ],
)
def test_prohibited_command_text_is_detected(text, expected):
    assert expected in policy.scan_command_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "/export",
        '/ppp active remove [find name="a"]',
        "/ip dhcp-server lease add address=10.0.0.2 mac-address=AA:BB",
        "/ip hotspot user add name=a",
        "",
        None,
    ],
)
def test_permitted_command_text_passes(text):
    assert policy.scan_command_text(text) == ()
    policy.assert_command_text_allowed(text, context="test")


def test_benign_action_label_cannot_smuggle_a_local_secret_mutation():
    """The exact bypass the label check alone would miss.

    A template filed under reset_session rules ``not_applicable``, so only
    inspecting the rendered text catches it.
    """
    label_ruling = policy.decide_for_provisioning_action(
        vendor=NasVendor.mikrotik,
        connection_type=ConnectionType.pppoe,
        action=ProvisioningAction.reset_session,
    )
    assert label_ruling.emits_commands  # label says "nothing to see here"

    smuggled = '/ppp active remove [find name="a"]\n/ppp secret set [find] disabled=no'
    with pytest.raises(policy.LocalSecretCommandRejected) as excinfo:
        policy.assert_command_text_allowed(smuggled, context="template 7")

    assert excinfo.value.code == policy.COMMAND_TEXT_REJECTED
    assert "ppp-secret-set" in excinfo.value.details["matches"]


def test_rejection_names_the_owning_services():
    with pytest.raises(policy.LocalSecretCommandRejected) as excinfo:
        policy.assert_command_text_allowed(
            "/ppp secret add name=a", context="template 9"
        )

    assert policy.RADIUS_OWNER in excinfo.value.message
    assert policy.BOUNDARY_OWNER in excinfo.value.message
