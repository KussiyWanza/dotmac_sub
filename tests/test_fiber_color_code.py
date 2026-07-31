from __future__ import annotations

from app.models.network import FiberColorStandard
from app.services.network.fiber_color_code import (
    EIA_TIA_598_COLORS,
    StrandColorCode,
    derive_strand_colors,
)

_STANDARD = FiberColorStandard.eia_tia_598.value


def test_loose_tube_derivation_follows_eia_tia_598():
    # 48-count, 12 fibers per tube: strand 37 is tube 4 (brown), core 1 (blue).
    colors = derive_strand_colors(
        strand_number=37,
        fiber_count=48,
        fibers_per_tube=12,
        color_standard=_STANDARD,
    )
    assert colors is not None
    assert colors.tube_number == 4
    assert colors.tube_color == "brown"
    assert colors.core_number_in_tube == 1
    assert colors.core_color == "blue"

    # Strand 24 closes tube 2: core 12 (aqua) in the orange tube.
    colors = derive_strand_colors(
        strand_number=24,
        fiber_count=48,
        fibers_per_tube=12,
        color_standard=_STANDARD,
    )
    assert colors is not None
    assert colors.tube_number == 2
    assert colors.tube_color == "orange"
    assert colors.core_number_in_tube == 12
    assert colors.core_color == "aqua"


def test_single_tube_cable_derives_core_color_only():
    colors = derive_strand_colors(
        strand_number=5,
        fiber_count=12,
        fibers_per_tube=None,
        color_standard=_STANDARD,
    )
    assert colors is not None
    assert colors.tube_number is None
    assert colors.tube_color is None
    assert colors.core_color == "slate"
    assert colors.core_number_in_tube == 5


def test_derivation_refuses_ambiguous_or_invalid_construction():
    # No declared standard.
    assert (
        derive_strand_colors(
            strand_number=1, fiber_count=12, fibers_per_tube=None, color_standard=None
        )
        is None
    )
    # Unknown standard value.
    assert (
        derive_strand_colors(
            strand_number=1,
            fiber_count=12,
            fibers_per_tube=None,
            color_standard="custom",
        )
        is None
    )
    # Multi-tube count without declared fibers_per_tube is ambiguous.
    assert (
        derive_strand_colors(
            strand_number=13,
            fiber_count=48,
            fibers_per_tube=None,
            color_standard=_STANDARD,
        )
        is None
    )
    # Strand number outside the declared count.
    assert (
        derive_strand_colors(
            strand_number=49,
            fiber_count=48,
            fibers_per_tube=12,
            color_standard=_STANDARD,
        )
        is None
    )
    # Invalid strand number.
    assert (
        derive_strand_colors(
            strand_number=0,
            fiber_count=48,
            fibers_per_tube=12,
            color_standard=_STANDARD,
        )
        is None
    )


def test_color_cycle_wraps_beyond_twelve_tubes():
    # 288-count, 12 per tube: tube 13 wraps back to blue.
    colors = derive_strand_colors(
        strand_number=145,
        fiber_count=288,
        fibers_per_tube=12,
        color_standard=_STANDARD,
    )
    assert colors is not None
    assert colors.tube_number == 13
    assert colors.tube_color == EIA_TIA_598_COLORS[0]


def test_payload_round_trip():
    colors = derive_strand_colors(
        strand_number=7,
        fiber_count=24,
        fibers_per_tube=12,
        color_standard=_STANDARD,
    )
    assert colors is not None
    assert StrandColorCode.from_payload(colors.to_dict()) == colors
    assert StrandColorCode.from_payload(None) is None
    assert StrandColorCode.from_payload({"strand_number": "bad"}) is None
