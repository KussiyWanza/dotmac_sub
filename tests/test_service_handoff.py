"""Service handoff — how a dedicated circuit reaches the customer edge.

Transit (BGP) and layer-2 clear channel are delivery variants of dedicated, not
separate products. These tests pin the invariants that let one offer carry all
three without the provisioning facts going untyped.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.catalog import ServiceHandoff, ServiceHandoffType


def _handoff(db, subscription, **kwargs):
    row = ServiceHandoff(subscription_id=subscription.id, **kwargs)
    db.add(row)
    db.flush()
    return row


def test_static_ip_is_the_default_handoff(db_session, subscription):
    """Ordinary internet access carries no BGP or endpoint detail."""
    row = _handoff(db_session, subscription)

    assert row.handoff_type is ServiceHandoffType.static_ip
    assert row.customer_asn is None
    assert row.a_end_description is None


def test_bgp_handoff_carries_the_customer_asn_and_prefixes(db_session, subscription):
    """Transit is dedicated delivered over BGP — the NOC needs the ASN and
    what the customer will announce."""
    row = _handoff(
        db_session,
        subscription,
        handoff_type=ServiceHandoffType.bgp,
        customer_asn=328160,
        announced_prefixes="102.89.0.0/22\n197.210.0.0/24",
        peer_ip="169.254.10.1",
    )

    assert row.customer_asn == 328160
    assert "102.89.0.0/22" in row.announced_prefixes


def test_bgp_without_an_asn_is_rejected(db_session, subscription):
    """A BGP handoff with no ASN cannot be provisioned; letting it persist
    would push the failure to the NOC at turn-up time instead of order time."""
    with pytest.raises(IntegrityError):
        _handoff(
            db_session,
            subscription,
            handoff_type=ServiceHandoffType.bgp,
            customer_asn=None,
        )


def test_layer2_requires_both_endpoints(db_session, subscription):
    """A point-to-point circuit with one end is not a circuit."""
    with pytest.raises(IntegrityError):
        _handoff(
            db_session,
            subscription,
            handoff_type=ServiceHandoffType.layer2_clear_channel,
            a_end_description="Garki POP rack 4",
        )


def test_layer2_clear_channel_carries_endpoints_and_vlan(db_session, subscription):
    """No IP is provided — the customer pushes their own across the channel."""
    row = _handoff(
        db_session,
        subscription,
        handoff_type=ServiceHandoffType.layer2_clear_channel,
        a_end_description="Garki POP rack 4, port 12",
        b_end_description="Lagos Allen POP rack 1, port 3",
        vlan_id=203,
    )

    assert row.vlan_id == 203
    assert row.customer_asn is None, "a clear channel carries no IP layer"


def test_a_clear_channel_cannot_also_carry_an_asn(db_session, subscription):
    """Layer 2 has no IP layer, so an ASN on it is a contradiction — the row
    would claim we route for a customer we only hand a channel to."""
    with pytest.raises(IntegrityError):
        _handoff(
            db_session,
            subscription,
            handoff_type=ServiceHandoffType.layer2_clear_channel,
            a_end_description="Garki POP",
            b_end_description="Jabi POP",
            customer_asn=328160,
        )


def test_static_ip_cannot_carry_bgp_or_endpoint_fields(db_session, subscription):
    with pytest.raises(IntegrityError):
        _handoff(
            db_session,
            subscription,
            handoff_type=ServiceHandoffType.static_ip,
            customer_asn=328160,
        )


def test_a_subscription_has_at_most_one_handoff(db_session, subscription):
    """Two handoffs would leave the NOC with no single answer for how to
    deliver the service."""
    _handoff(db_session, subscription)

    with pytest.raises(IntegrityError):
        _handoff(db_session, subscription, handoff_type=ServiceHandoffType.static_ip)


@pytest.mark.parametrize("asn", [0, 4294967295])
def test_asn_outside_usable_space_is_rejected(db_session, subscription, asn):
    with pytest.raises(IntegrityError):
        _handoff(
            db_session,
            subscription,
            handoff_type=ServiceHandoffType.bgp,
            customer_asn=asn,
        )


@pytest.mark.parametrize("vlan", [0, 4095])
def test_vlan_outside_usable_range_is_rejected(db_session, subscription, vlan):
    with pytest.raises(IntegrityError):
        _handoff(
            db_session,
            subscription,
            handoff_type=ServiceHandoffType.layer2_clear_channel,
            a_end_description="A",
            b_end_description="B",
            vlan_id=vlan,
        )
