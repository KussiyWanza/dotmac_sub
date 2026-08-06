"""Tests for the live-session IP divergence audit.

Covers the production-readiness rules the detector must not regress on: the
session resolver's own freshness policy, conflict detection that does not
depend on the subscription's own desired state, NAS-scoped duplicate reporting
that still names an off-NAS counterparty, and failing closed on every ambiguity
the audit cannot legitimately resolve — a login bound to several active
subscriptions, and a service with several active assignments.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.models.catalog import NasDevice, Subscription, SubscriptionStatus
from app.models.network import IPAssignment, IPv4Address, IPVersion
from app.models.radius_active_session import RadiusActiveSession
from app.models.subscriber import Subscriber
from app.services.network.radius_sessions import ACTIVE_SESSION_FRESHNESS
from scripts.one_off.audit_nas_session_ip_divergence import audit


def _nas(db_session, name):
    device = NasDevice(name=name, nas_ip=f"10.0.0.{len(name)}")
    db_session.add(device)
    db_session.flush()
    return device


def _subscriber(db_session, tag):
    subscriber = Subscriber(
        first_name="Div",
        last_name="Case",
        email=f"{tag}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    return subscriber


def _assign(db_session, *, subscriber, subscription, ip):
    address = db_session.query(IPv4Address).filter_by(address=ip).one_or_none()
    if address is None:
        address = IPv4Address(address=ip)
        db_session.add(address)
        db_session.flush()
    db_session.add(
        IPAssignment(
            subscriber_id=subscriber.id,
            subscription_id=subscription.id if subscription is not None else None,
            ip_version=IPVersion.ipv4,
            ipv4_address_id=address.id,
            is_active=True,
        )
    )
    db_session.flush()


def _sub(
    db_session,
    catalog_offer,
    *,
    login,
    column_ip=None,
    owned_ips=(),
    subscriber=None,
    status=SubscriptionStatus.active,
):
    subscriber = subscriber or _subscriber(db_session, login)
    sub = Subscription(
        subscriber_id=subscriber.id,
        offer_id=catalog_offer.id,
        status=status,
        login=login,
        ipv4_address=column_ip,
    )
    db_session.add(sub)
    db_session.flush()
    for ip in owned_ips:
        _assign(db_session, subscriber=subscriber, subscription=sub, ip=ip)
    return sub


def _session(db_session, *, nas, sub, framed_ip, age_seconds=60):
    seen = datetime.now(UTC) - timedelta(seconds=age_seconds)
    db_session.add(
        RadiusActiveSession(
            subscriber_id=sub.subscriber_id,
            subscription_id=sub.id,
            nas_device_id=nas.id,
            username=sub.login,
            acct_session_id=uuid.uuid4().hex,
            nas_ip_address=nas.nas_ip,
            framed_ip_address=framed_ip,
            session_start=seen,
            last_update=seen,
        )
    )
    db_session.flush()


def test_freshness_follows_the_session_resolver_policy(db_session, catalog_offer):
    """A dead-but-unclosed radacct ghost is not evidence of a wrong address.

    The boundary is ``network.radius_sessions.ACTIVE_SESSION_FRESHNESS``; the
    audit has no override of its own.
    """
    nas = _nas(db_session, "Eagle Access")
    stale_sub = _sub(db_session, catalog_offer, login="ghost", owned_ips=["172.16.1.1"])
    fresh_sub = _sub(db_session, catalog_offer, login="fresh", owned_ips=["172.16.1.2"])
    beyond = int(ACTIVE_SESSION_FRESHNESS.total_seconds()) + 60
    within = int(ACTIVE_SESSION_FRESHNESS.total_seconds()) - 60
    _session(
        db_session, nas=nas, sub=stale_sub, framed_ip="172.16.9.9", age_seconds=beyond
    )
    _session(
        db_session, nas=nas, sub=fresh_sub, framed_ip="172.16.9.8", age_seconds=within
    )
    db_session.commit()

    result = audit(db_session, "eagle")

    assert result["sessions_stale_skipped"] == 1
    assert result["session_snapshot_status"] == "stale"
    assert result["session_gate_valid"] is False
    assert result["sessions_in_scope"] == 1
    assert (
        str(int(ACTIVE_SESSION_FRESHNESS.total_seconds())) in result["freshness_policy"]
    )
    # Only the fresh session is adjudicated.
    assert result["counts"]["session_ip_mismatch"] == 1
    assert result["findings"]["session_ip_mismatch"][0]["login"] == "fresh"


def test_conflict_detected_when_subscription_has_no_owning_assignment(
    db_session, catalog_offer
):
    """Conflict is decided on the assignment ledger, not on desired state.

    The offending subscription owns nothing and its served column even matches
    the address it is squatting on — a desired-state guard suppresses both.
    """
    nas = _nas(db_session, "Eagle Access")
    victim = _sub(db_session, catalog_offer, login="victim", owned_ips=["172.16.2.5"])
    squatter = _sub(db_session, catalog_offer, login="squatter", column_ip="172.16.2.5")
    _session(db_session, nas=nas, sub=squatter, framed_ip="172.16.2.5")
    db_session.commit()

    result = audit(db_session, "eagle")

    assert result["counts"]["session_ip_conflict"] == 1
    conflict = result["findings"]["session_ip_conflict"][0]
    assert conflict["login"] == "squatter"
    assert conflict["assignment_holder"] == "victim"
    assert conflict["assignment_holder_subscription_id"] == str(victim.id)
    assert result["counts"]["served_projection_unowned"] == 1


def test_legacy_unbound_assignment_still_holds_the_address(db_session, catalog_offer):
    """An assignment with subscription_id IS NULL is an owner, not a gap.

    Excluding legacy rows would make a live collision against one look like an
    untracked address with no holder.
    """
    nas = _nas(db_session, "Eagle Access")
    legacy_owner = _subscriber(db_session, "legacyowner")
    legacy_sub = _sub(
        db_session,
        catalog_offer,
        login="legacy",
        column_ip="172.16.7.4",
        subscriber=legacy_owner,
    )
    _assign(db_session, subscriber=legacy_owner, subscription=None, ip="172.16.7.4")
    squatter = _sub(
        db_session, catalog_offer, login="squatter", owned_ips=["172.16.7.9"]
    )
    _session(db_session, nas=nas, sub=squatter, framed_ip="172.16.7.4")
    _session(db_session, nas=nas, sub=legacy_sub, framed_ip="172.16.7.4")
    db_session.commit()

    result = audit(db_session, "eagle")

    # The squatter collides with the legacy holder, compared at subscriber grain.
    conflicts = result["findings"]["session_ip_conflict"]
    assert [c["login"] for c in conflicts] == ["squatter"]
    assert conflicts[0]["assignment_holder"] == f"legacy subscriber {legacy_owner.id}"
    # The legacy holder's own session is not a conflict, and its served column is
    # backed — so it is migration debt, not an unowned projection.
    assert result["counts"]["served_projection_unowned"] == 0
    assert result["counts"]["legacy_unbound_assignment"] == 1
    assert result["findings"]["legacy_unbound_assignment"][0]["address"] == (
        "172.16.7.4"
    )
    assert result["counts"]["session_ip_untracked"] == 0


def test_every_legacy_assignment_is_gated_even_when_service_grain_is_ambiguous(
    db_session, catalog_offer
):
    subscriber = _subscriber(db_session, "legacy-multi")
    _sub(
        db_session,
        catalog_offer,
        login="legacy-multi-a",
        subscriber=subscriber,
    )
    _sub(
        db_session,
        catalog_offer,
        login="legacy-multi-b",
        subscriber=subscriber,
    )
    _assign(
        db_session,
        subscriber=subscriber,
        subscription=None,
        ip="172.16.7.8",
    )
    db_session.commit()

    result = audit(db_session, None)

    assert result["gate_counts"]["legacy_unbound_assignment"] == 1
    finding = result["findings"]["legacy_unbound_assignment"][0]
    assert finding["address"] == "172.16.7.8"
    assert len(finding["projected_subscriptions"]) == 2


def test_duplicate_login_subscriptions_fail_closed(db_session, catalog_offer):
    """The reconciler's lowest-id binding is a guess; no verdict may rest on it."""
    nas = _nas(db_session, "Eagle Access")
    shared = _subscriber(db_session, "shared")
    first = _sub(
        db_session,
        catalog_offer,
        login="dupe",
        owned_ips=["172.16.8.1"],
        subscriber=shared,
    )
    _sub(
        db_session,
        catalog_offer,
        login="dupe",
        owned_ips=["172.16.8.2"],
        subscriber=shared,
    )
    # Bound to the first subscription, whose assignment does not match the
    # observed address — a naive audit would call this a mismatch.
    _session(db_session, nas=nas, sub=first, framed_ip="172.16.8.2")
    db_session.commit()

    result = audit(db_session, "eagle")

    assert result["counts"]["duplicate_login_subscription"] == 1
    finding = result["findings"]["duplicate_login_subscription"][0]
    assert finding["login"] == "dupe"
    assert len(finding["active_subscription_ids"]) == 2
    # No verdict rests on the unreliable binding.
    assert result["counts"]["session_ip_mismatch"] == 0
    assert result["counts"]["session_ip_conflict"] == 0
    assert result["counts"]["ambiguous_service_assignment"] == 0


def test_duplicate_session_ip_names_off_nas_counterparty(db_session, catalog_offer):
    """A scoped run must still show the collision partner on another NAS."""
    eagle = _nas(db_session, "Eagle Access")
    other = _nas(db_session, "Kubwa Access")
    a = _sub(db_session, catalog_offer, login="onEagle", owned_ips=["172.16.3.1"])
    b = _sub(db_session, catalog_offer, login="onKubwa", owned_ips=["172.16.3.2"])
    _session(db_session, nas=eagle, sub=a, framed_ip="172.16.3.7")
    _session(db_session, nas=other, sub=b, framed_ip="172.16.3.7")
    db_session.commit()

    result = audit(db_session, "eagle")

    assert result["sessions_in_scope"] == 1
    assert result["counts"]["duplicate_session_ip"] == 1
    group = result["findings"]["duplicate_session_ip"][0]
    assert group["observed_ip"] == "172.16.3.7"
    assert {(s["login"], s["in_scope"]) for s in group["sessions"]} == {
        ("onEagle", True),
        ("onKubwa", False),
    }


def test_duplicate_served_projection_is_the_real_duplicate_class(
    db_session, catalog_offer
):
    """The ledger cannot express two holders; the unconstrained column can.

    ``uq_ip_assignments_ipv4_active`` makes a duplicate ACTIVE assignment
    impossible, so the only way one address reaches two logins is through
    ``subscriptions.ipv4_address`` — which populate() prefers over the
    assignment and projects into radreply verbatim.
    """
    eagle = _nas(db_session, "Eagle Access")
    owner = _sub(
        db_session,
        catalog_offer,
        login="rightful",
        column_ip="172.16.4.9",
        owned_ips=["172.16.4.9"],
    )
    _sub(db_session, catalog_offer, login="squatter", column_ip="172.16.4.9")
    _session(db_session, nas=eagle, sub=owner, framed_ip="172.16.4.9")
    db_session.commit()

    result = audit(db_session, "eagle")

    assert result["counts"]["duplicate_served_projection"] == 1
    group = result["findings"]["duplicate_served_projection"][0]
    assert group["served_ip"] == "172.16.4.9"
    assert group["assignment_holder"] == "rightful"
    assert {(s["login"], s["in_scope"]) for s in group["subscriptions"]} == {
        ("rightful", True),
        ("squatter", False),
    }
    assert result["counts"]["ledger_integrity_violation"] == 0


def test_projection_gate_is_fleetwide_even_when_session_audit_is_nas_scoped(
    db_session, catalog_offer
):
    """NAS scope cannot narrow the population used for a cutover gate."""
    eagle = _nas(db_session, "Eagle Access")
    other = _nas(db_session, "Kubwa Access")
    clean = _sub(
        db_session,
        catalog_offer,
        login="clean",
        column_ip="172.16.10.1",
        owned_ips=["172.16.10.1"],
    )
    _session(db_session, nas=eagle, sub=clean, framed_ip="172.16.10.1")
    far_a = _sub(db_session, catalog_offer, login="farA", column_ip="172.16.10.9")
    _sub(db_session, catalog_offer, login="farB", column_ip="172.16.10.9")
    _session(db_session, nas=other, sub=far_a, framed_ip="172.16.10.9")
    db_session.commit()

    scoped = audit(db_session, "eagle")
    assert scoped["counts"]["duplicate_served_projection"] == 1
    assert scoped["counts"]["session_ip_conflict"] == 0
    assert scoped["counts"]["session_ip_mismatch"] == 0
    assert scoped["gate_population"] == "all ACTIVE/BLOCKED projected subscriptions"

    assert audit(db_session, None)["counts"]["duplicate_served_projection"] == 1


def test_projection_gate_does_not_require_a_live_session(db_session, catalog_offer):
    _sub(
        db_session,
        catalog_offer,
        login="offline-stale",
        column_ip="172.16.11.9",
        owned_ips=["172.16.11.1"],
    )
    db_session.commit()

    result = audit(db_session, "eagle")

    assert result["sessions_in_scope"] == 0
    assert result["session_snapshot_status"] == "empty"
    assert result["session_gate_valid"] is False
    assert result["gate_counts"]["served_projection_stale"] == 1


def test_ambiguous_assignments_suppress_mismatch_but_not_conflict(
    db_session, catalog_offer
):
    """Ambiguity blocks only the verdict that needs this sub's desired state.

    Multiple active IPv4 assignments per subscription is a supported product
    shape -- the admin form allocates one per selected block -- so the audit
    must handle it rather than assume it away. No owner can be named, so no
    mismatch verdict is issued. A conflict still is, because it is decided by
    the OTHER party's ownership of the observed address.
    """
    nas = _nas(db_session, "Eagle Access")
    victim = _sub(db_session, catalog_offer, login="victim", owned_ips=["172.16.5.9"])
    ambiguous = _sub(
        db_session,
        catalog_offer,
        login="ambiguous",
        column_ip="172.16.5.1",
        owned_ips=["172.16.5.1", "172.16.5.2"],
    )
    _session(db_session, nas=nas, sub=ambiguous, framed_ip="172.16.5.9")
    db_session.commit()

    result = audit(db_session, "eagle")

    assert result["counts"]["ambiguous_service_assignment"] == 1
    finding = result["findings"]["ambiguous_service_assignment"][0]
    assert finding["owner_ips"] == ["172.16.5.1", "172.16.5.2"]
    assert finding["owner_ip"] == ""
    assert result["counts"]["session_ip_mismatch"] == 0
    assert result["counts"]["served_projection_stale"] == 0
    assert result["counts"]["session_ip_conflict"] == 1
    assert result["findings"]["session_ip_conflict"][0]["assignment_holder"] == "victim"
    assert (
        str(victim.id)
        == (
            result["findings"]["session_ip_conflict"][0][
                "assignment_holder_subscription_id"
            ]
        )
    )


def test_served_projection_stale_when_column_disagrees_with_owner(
    db_session, catalog_offer
):
    nas = _nas(db_session, "Eagle Access")
    sub = _sub(
        db_session,
        catalog_offer,
        login="staleproj",
        column_ip="172.16.6.9",
        owned_ips=["172.16.6.1"],
    )
    _session(db_session, nas=nas, sub=sub, framed_ip="172.16.6.9")
    db_session.commit()

    result = audit(db_session, "eagle")

    assert result["counts"]["served_projection_stale"] == 1
    assert result["counts"]["session_ip_mismatch"] == 1
