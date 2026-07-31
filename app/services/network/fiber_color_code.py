"""EIA/TIA-598 tube and core color derivation from declared construction.

Colors are derived, never stored per strand: the cable's declared
construction (``fiber_count``, ``fibers_per_tube``, ``color_standard`` on
``FiberSegment``) plus the exact ``strand_number`` determine the tube and
core colors deterministically. Unknown or ambiguous construction derives
nothing — the resolver refuses to guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models.network import FiberColorStandard

EIA_TIA_598_COLORS: tuple[str, ...] = (
    "blue",
    "orange",
    "green",
    "brown",
    "slate",
    "white",
    "red",
    "black",
    "yellow",
    "violet",
    "rose",
    "aqua",
)


@dataclass(frozen=True)
class StrandColorCode:
    """Derived field color identity for one exact numbered strand."""

    strand_number: int
    color_standard: str
    tube_number: int | None
    tube_color: str | None
    core_number_in_tube: int
    core_color: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strand_number": self.strand_number,
            "color_standard": self.color_standard,
            "tube_number": self.tube_number,
            "tube_color": self.tube_color,
            "core_number_in_tube": self.core_number_in_tube,
            "core_color": self.core_color,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> StrandColorCode | None:
        if not payload:
            return None
        try:
            return cls(
                strand_number=int(payload["strand_number"]),
                color_standard=str(payload["color_standard"]),
                tube_number=int(payload["tube_number"])
                if payload.get("tube_number") is not None
                else None,
                tube_color=payload.get("tube_color"),
                core_number_in_tube=int(payload["core_number_in_tube"]),
                core_color=str(payload["core_color"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def derive_strand_colors(
    *,
    strand_number: int,
    fiber_count: int | None,
    fibers_per_tube: int | None,
    color_standard: str | None,
) -> StrandColorCode | None:
    """Derive tube/core colors for a 1-based strand number, or refuse.

    Derivation requires the declared standard and enough construction to be
    unambiguous: either ``fibers_per_tube`` (loose tube) or a single-tube
    cable (``fiber_count`` within one color cycle).
    """

    if color_standard != FiberColorStandard.eia_tia_598.value:
        return None
    if strand_number < 1:
        return None
    if fiber_count is not None and strand_number > fiber_count:
        return None

    colors = EIA_TIA_598_COLORS
    if fibers_per_tube is not None and fibers_per_tube > 0:
        tube_number = (strand_number - 1) // fibers_per_tube + 1
        core_number_in_tube = (strand_number - 1) % fibers_per_tube + 1
        return StrandColorCode(
            strand_number=strand_number,
            color_standard=color_standard,
            tube_number=tube_number,
            tube_color=colors[(tube_number - 1) % len(colors)],
            core_number_in_tube=core_number_in_tube,
            core_color=colors[(core_number_in_tube - 1) % len(colors)],
        )

    if fiber_count is not None and fiber_count <= len(colors):
        return StrandColorCode(
            strand_number=strand_number,
            color_standard=color_standard,
            tube_number=None,
            tube_color=None,
            core_number_in_tube=strand_number,
            core_color=colors[(strand_number - 1) % len(colors)],
        )

    return None


def derive_segment_strand_colors(segment, strand_number: int) -> StrandColorCode | None:
    """Derive colors for one strand of a ``FiberSegment``-shaped record."""

    if segment is None:
        return None
    return derive_strand_colors(
        strand_number=strand_number,
        fiber_count=segment.fiber_count,
        fibers_per_tube=segment.fibers_per_tube,
        color_standard=segment.color_standard,
    )
