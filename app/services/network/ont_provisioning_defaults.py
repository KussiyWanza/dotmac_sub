"""Declared device-layout defaults used by ONT provisioning.

Owner: ``network.ont_provisioning_defaults``.

This owner is intentionally narrower than the stateful provisioning executor.
It decides only approved, model-family layout defaults that may become device
write targets when per-ONT intent omits a value.  It performs no I/O and owns
no transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

OWNER = "network.ont_provisioning_defaults"


@dataclass(frozen=True, slots=True)
class PppoeInstanceLayout:
    """Approved first TR-069 WAN PPP connection layout."""

    instance_index: int
    declared_by: str


FIRST_TR069_PPPOE_INSTANCE = PppoeInstanceLayout(
    instance_index=1,
    declared_by=OWNER,
)


def default_pppoe_instance_index() -> int:
    """Return the approved first WANPPPConnection instance index."""
    return FIRST_TR069_PPPOE_INSTANCE.instance_index
