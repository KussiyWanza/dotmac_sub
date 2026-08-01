"""Only an explicit managed-ONT PPPoE intent authorises a dialer projection.

This reconciler staged a PPPoE credential into desired state for 1,374 of 1,523
production ONTs. 1,372 of those services are `routing` + `dhcp` and one is
`bridging` -- none of which places PPPoE termination on the ONT. It selected
every subscriber-assigned ONT without consulting WAN mode, and resolved the
credential at subscriber grain by creation order.

The projection is a device write. Absent or unrecognised intent must therefore
be ineligible, not defaulted to the one value that authorises writing.
"""

from __future__ import annotations

import pytest

from app.services.cpe_dialer_credential_reconcile import termination_intent


@pytest.mark.parametrize(
    ("wan_mode", "ip_mode"),
    [("routing", "dhcp"), ("routing", None), (None, None), ("routing", "static_ip")],
)
def test_operator_entered_intent_is_the_only_thing_that_authorises(wan_mode, ip_mode):
    """Neither WAN field can say PPPoE, so the username is the whole signal."""
    eligible, reason = termination_intent(wan_mode, ip_mode, "acct-1")

    assert eligible is True
    assert reason == "managed_ont_pppoe"


def test_routing_without_operator_intent_is_ineligible():
    """The production majority: 1,372 ONTs carried a dialer on this shape.

    `routing` says only that the ONT is not bridging. It is not consent to dial.
    """
    eligible, reason = termination_intent("routing", "dhcp", None)

    assert eligible is False
    assert "no_ont_pppoe_intent" in reason


@pytest.mark.parametrize(
    ("wan_mode", "ip_mode"),
    [("bridging", "dhcp"), ("bridge", None), (None, "bridged"), ("setup_via_onu", "")],
)
def test_bridge_signal_in_either_field_is_ineligible(wan_mode, ip_mode):
    eligible, reason = termination_intent(wan_mode, ip_mode, "acct-1")

    assert eligible is False
    assert reason == "bridge_termination"


def test_bridge_beats_a_conflicting_pppoe_signal():
    """Contradictory intent must not resolve toward writing.

    Bridging places termination on a downstream router whatever the other
    field claims, so the conflict is refused rather than averaged.
    """
    eligible, reason = termination_intent("bridging", "dhcp", "acct-1")

    assert eligible is False
    assert reason == "bridge_termination"


def test_absent_intent_is_ineligible_not_assumed_pppoe():
    """The fail-open this replaces.

    `_normalise_wan_mode` ends with `return "pppoe"`, so an unset mode read as
    PPPoE intent. Defaulting an unknown to the value that authorises a device
    write is the wrong direction.
    """
    eligible, reason = termination_intent(None, None, None)

    assert eligible is False
    assert "no_ont_pppoe_intent" in reason


@pytest.mark.parametrize("ip_mode", ["static_ip", "dhcp", "inactive"])
def test_no_ip_mode_authorises_a_dial_on_its_own(ip_mode):
    eligible, _ = termination_intent("routing", ip_mode, None)

    assert eligible is False


def test_the_gate_is_not_the_permissive_helper():
    """Guard against someone 'simplifying' this back onto _normalise_wan_mode."""
    from app.services.network.reconcile.adapters import _normalise_wan_mode

    # The old helper calls an absent mode PPPoE...
    assert _normalise_wan_mode(None, None) == "pppoe"
    # ...and the gate must not.
    assert termination_intent(None, None, None)[0] is False
