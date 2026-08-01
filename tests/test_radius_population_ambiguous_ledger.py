"""The RADIUS projection must not invent an address owner.

`docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md` says consumers fail closed on an
ambiguous ledger. `populate()` previously resolved "which of this service's
active assignments do we serve?" with `setdefault` over an unordered query,
which is both an ownership decision it does not hold and a nondeterministic one:
two runs over identical data could emit different Framed-IPs.
"""

from __future__ import annotations

import pytest

from app.services.radius_population import _single_active_ipv4


def test_no_candidates_resolves_to_nothing_and_is_not_ambiguous():
    address, ambiguous = _single_active_ipv4(None)
    assert address is None
    assert ambiguous == ()

    address, ambiguous = _single_active_ipv4(set())
    assert address is None
    assert ambiguous == ()


def test_exactly_one_candidate_resolves():
    address, ambiguous = _single_active_ipv4({"172.16.1.5"})
    assert address == "172.16.1.5"
    assert ambiguous == ()


def test_multiple_candidates_refuse_rather_than_choose():
    address, ambiguous = _single_active_ipv4({"172.16.1.5", "172.16.9.9"})

    assert address is None
    # The caller needs the offending set to report it, not to pick from it.
    assert ambiguous == ("172.16.1.5", "172.16.9.9")


def test_refusal_is_order_independent():
    """The old bug was that the answer depended on row order."""
    forward = _single_active_ipv4({"10.0.0.1", "10.0.0.2", "10.0.0.3"})
    reverse = _single_active_ipv4({"10.0.0.3", "10.0.0.2", "10.0.0.1"})

    assert forward == reverse
    assert forward[0] is None


@pytest.mark.parametrize("count", [2, 3, 7])
def test_any_multiplicity_refuses(count):
    candidates = {f"10.0.0.{index}" for index in range(1, count + 1)}
    address, ambiguous = _single_active_ipv4(candidates)

    assert address is None
    assert len(ambiguous) == count


def test_populate_reports_ambiguity_as_a_distinct_stat():
    """The refusal must be countable, not just logged."""
    import inspect

    from app.services import radius_population

    source = inspect.getsource(radius_population.populate)

    assert "skipped_ambiguous_ipv4_ledger" in source
    # Preserved, not deleted: an ambiguous ledger degrades to "no change,
    # reported", never to "customer loses their Framed-IP".
    assert "preserve_usernames.add(login)" in source
    assert "setdefault(sub.id" not in source
