"""The one definition of which ONTs Huawei reconciliation may touch.

The predicate below decides, for the whole system, whether a device is inside
automatic reconciliation. It had been hand-copied into every caller that needed
it -- the sweeper, the expired remote-access cleanup, and each piece of
analysis tooling written against them. Copies of a population rule do not stay
equal: one caller gains a condition, the others silently keep answering a
different question, and a "the fleet converged" statement quietly stops being
true of the same fleet the sweeper walks.

Callers supply their own ``select`` shape -- the sweeper wants ids in a
staleness order, the cleanup wants whole rows -- so this applies the predicate
rather than owning the query. Anything that needs to reason about the sweep's
population, including read-only auditing, consumes this instead of restating
it.
"""

from __future__ import annotations

from sqlalchemy import Select, func

from app.models.network import DeviceStatus, OLTDevice, OntUnit

__all__ = ["restrict_to_reconcile_candidates"]


def restrict_to_reconcile_candidates(
    stmt: Select, *, only_active: bool = True
) -> Select:
    """Narrow ``stmt`` to the ONTs Huawei reconciliation is allowed to drive.

    Joins ``OLTDevice`` and applies every eligibility condition:

    * the ONT is active (unless ``only_active`` is False, which exists for
      callers that deliberately inspect deactivated inventory);
    * neither the ONT nor its OLT is UISP-managed -- ownership is explicit so
      UFiber ONUs can never enter Huawei SSH/ACS paths;
    * the OLT is active, has active status, and is a Huawei OLT.

    The vendor test is on the **OLT**, not the ONT. An ONT with a blank or
    inconsistent vendor string is still a candidate when its OLT is Huawei;
    conversely an ONT with no OLT association at all is not a candidate at any
    point, because the join removes it. That exclusion is invisible in the
    resulting count -- see issue #1964.
    """
    stmt = stmt.join(OLTDevice, OLTDevice.id == OntUnit.olt_device_id)
    if only_active:
        stmt = stmt.where(OntUnit.is_active.is_(True))
    return (
        stmt.where(OntUnit.uisp_device_id.is_(None))
        .where(OLTDevice.uisp_device_id.is_(None))
        .where(OLTDevice.is_active.is_(True))
        .where(OLTDevice.status == DeviceStatus.active)
        .where(func.lower(OLTDevice.vendor) == "huawei")
    )
