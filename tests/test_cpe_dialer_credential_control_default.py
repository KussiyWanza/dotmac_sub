"""The dialer credential projection must be opted into, not out of.

This control was `default=True, on_missing=True`, so an environment that had
never configured it ran the projection anyway. On production that staged a
PPPoE dialer onto 1,374 of 1,523 ONTs; 1,373 of them belong to services whose
WAN intent is routing+DHCP or bridging, which does not authorise ONT PPPoE.

The projection can manufacture a second dialer on a service that terminates
PPPoE elsewhere, so absent configuration must mean OFF. Production containment
is currently a database row; this makes the safe state survive a fresh
environment that has no such row.
"""

from __future__ import annotations

from app.services.control_registry import all_controls


def control_for_key(key: str):
    return next((c for c in all_controls() if c.key == key), None)


KEY = "network.cpe_dialer_credential_sync"


def test_absent_configuration_means_disabled() -> None:
    control = control_for_key(KEY)

    assert control is not None
    # on_missing is what a fresh environment resolves to.
    assert control.on_missing is False, (
        "an environment with no setting row must not run the dialer projection"
    )
    assert control.default is False


def test_general_ont_reconciliation_is_untouched() -> None:
    """Containment must not disable ONT reconciliation as a side effect.

    The two were conflated during the incident: disabling the producer is the
    narrow lever, and reconciliation carries unrelated device configuration.
    """
    control = control_for_key("network.ont_reconcile")

    assert control is not None
    assert control.on_missing is True
    assert control.default is True
