"""A well-formed MAC is not automatically a device identity.

The distinction is not academic. An OLT reported the same locally administered
address for 227 ONTs, and because device lookup matches on MAC with a limit of
one, a search for it resolved to an arbitrary unit. Storing that value was
worse than storing nothing, because NULL cannot be mistaken for a match.
"""

from __future__ import annotations

import pytest

from app.services.network._common import (
    is_identifying_mac_address,
    normalize_mac_address,
)


@pytest.mark.parametrize(
    "value",
    [
        "DC:C6:4B:B2:B1:6D",
        "d4:01:c3:bf:0c:e1",
        "10c17250 94c9",
        "F492BF3ECBB5",
    ],
)
def test_globally_unique_addresses_are_identities(value):
    assert is_identifying_mac_address(value) is True


def test_the_production_placeholder_is_refused():
    """The exact value 227 ONTs shared."""
    assert normalize_mac_address("BE:CF:AE:AB:EF:AE") == "BE:CF:AE:AB:EF:AE"
    assert is_identifying_mac_address("BE:CF:AE:AB:EF:AE") is False


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("02:00:00:00:00:01", "locally administered"),
        ("BE:CF:AE:AB:EF:AE", "locally administered"),
        ("01:00:5E:00:00:01", "multicast"),
        ("FF:FF:FF:FF:FF:FF", "broadcast"),
        ("00:00:00:00:00:00", "all zero"),
    ],
)
def test_non_identifying_addresses_are_refused(value, why):
    assert is_identifying_mac_address(value) is False, why


@pytest.mark.parametrize(
    "value", ["", None, "not-a-mac", "AA:BB:CC", "zz:zz:zz:zz:zz:zz"]
)
def test_malformed_values_are_refused(value):
    assert is_identifying_mac_address(value) is False


def test_refusal_is_narrower_than_normalisation():
    """A value can normalise cleanly and still not be an identity.

    This is the whole point: the old code accepted anything twelve hex digits
    long, which is why a placeholder reached the identity column.
    """
    placeholder = "BE:CF:AE:AB:EF:AE"

    assert normalize_mac_address(placeholder) is not None
    assert is_identifying_mac_address(placeholder) is False
