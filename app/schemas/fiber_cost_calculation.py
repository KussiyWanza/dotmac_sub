"""Stable typed calculation vocabulary for composable fiber cost lines."""

from __future__ import annotations

import enum


class FiberCostUnit(str, enum.Enum):
    """Closed calculation operators shared by contracts and persistence.

    Cost components remain open-ended rows. This vocabulary is closed because
    every member names arithmetic the owner can execute, rather than a kind of
    material or charge configured by an operator.
    """

    PER_METER = "per_meter"
    FLAT = "flat"
