"""The WAN service intent owner: declaring is not authorising.

`OntWanServiceInstance` had no application writer — no constructor outside
tests, and 8 production rows against 1,523 ONTs. Rows written by nothing cannot
authorise anything, so this owner is what makes them mean something.

The properties worth protecting are the ones that were missing: exact service
grain, lifecycle as the single authority, `is_primary` selecting where
`priority` only orders, and retirement preserving history.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.catalog import Subscription
from app.models.network import (
    OntUnit,
    OntWanServiceInstance,
    OntWanServiceLifecycle,
)
from app.models.subscriber import Subscriber
from app.services.network.ont_wan_service_intent import (
    IntentRefusal,
    WanServiceIntentError,
    WanServiceIntentSpec,
    activate_wan_service_intent,
    active_primary_internet_intent,
    declare_wan_service_intent,
    replace_wan_service_intent,
    retire_wan_service_intent,
)
from app.services.owner_commands import CommandContext


def _ctx(reason="declared at install", actor="michael"):
    return CommandContext(
        command_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        actor=actor,
        scope="network.ont_wan_service_intent",
        reason=reason,
        idempotency_key=f"wo-{uuid.uuid4().hex[:8]}",
    )


def _ont(db_session, serial=None):
    ont = OntUnit(serial_number=serial or f"HWTC{uuid.uuid4().hex[:8]}", is_active=True)
    db_session.add(ont)
    db_session.flush()
    return ont


def _subscription(db_session, catalog_offer, tag):
    subscriber = Subscriber(
        first_name="Intent", last_name="Case", email=f"{tag}@example.com"
    )
    db_session.add(subscriber)
    db_session.flush()
    sub = Subscription(subscriber_id=subscriber.id, offer_id=catalog_offer.id)
    db_session.add(sub)
    db_session.flush()
    return sub


def _spec(ont, sub, *, primary=True, service_type="internet", conn="pppoe"):
    return WanServiceIntentSpec(
        ont_id=ont.id,
        subscription_id=sub.id,
        service_type=service_type,
        connection_type=conn,
        is_primary=primary,
    )


# Owner commands demand a transaction-free session at entry, which is how a
# real caller arrives (one command per request/session). Any read after the
# previous command opens a fresh transaction, so these wrappers close it first
# rather than every test remembering to -- the same `commit()` before the call
# that tests/test_ip_assignment_lifecycle.py uses.


def _declare_cmd(db, spec, ctx):
    db.commit()
    return declare_wan_service_intent(db, spec=spec, context=ctx)


def _activate_cmd(db, instance_id, ctx, expected_revision=None):
    db.commit()
    return activate_wan_service_intent(
        db, instance_id=instance_id, context=ctx, expected_revision=expected_revision
    )


def _replace_cmd(db, outgoing_id, spec, ctx):
    db.commit()
    return replace_wan_service_intent(
        db, outgoing_instance_id=outgoing_id, spec=spec, context=ctx
    )


def _retire_cmd(db, instance_id, ctx):
    db.commit()
    return retire_wan_service_intent(db, instance_id=instance_id, context=ctx)


# ---------------------------------------------------------------------------
# Declaring is not authorising
# ---------------------------------------------------------------------------


def test_declaring_leaves_the_intent_non_authorising(db_session, catalog_offer):
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "declare")
    db_session.commit()

    outcome = _declare_cmd(db_session, _spec(ont, sub), _ctx())

    instance = db_session.get(OntWanServiceInstance, outcome.instance_id)
    assert instance.lifecycle_state is OntWanServiceLifecycle.planned
    assert instance.is_active is False
    # Nothing may treat a declaration as permission.
    assert (
        active_primary_internet_intent(
            db_session, ont_id=ont.id, subscription_id=sub.id
        )
        is None
    )


def test_activation_makes_it_authoritative(db_session, catalog_offer):
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "activate")
    db_session.commit()
    declared = _declare_cmd(db_session, _spec(ont, sub), _ctx())

    activated = _activate_cmd(db_session, declared.instance_id, _ctx())

    assert activated.lifecycle_state is OntWanServiceLifecycle.active
    assert activated.revision > declared.revision
    found = active_primary_internet_intent(
        db_session, ont_id=ont.id, subscription_id=sub.id
    )
    assert found is not None and found.id == declared.instance_id


def test_is_active_is_derived_not_a_second_authority(db_session, catalog_offer):
    """A row can be is_active=True and still non-authorising.

    That is exactly the state every pre-owner row starts in, so the read must
    key on lifecycle_state rather than the legacy flag.
    """
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "derived")
    instance = OntWanServiceInstance(
        ont_id=ont.id,
        subscription_id=sub.id,
        name="legacy",
        service_type="internet",
        connection_type="pppoe",
        is_primary=True,
        is_active=True,
        lifecycle_state=OntWanServiceLifecycle.unverified,
    )
    db_session.add(instance)
    db_session.commit()

    assert (
        active_primary_internet_intent(
            db_session, ont_id=ont.id, subscription_id=sub.id
        )
        is None
    )


# ---------------------------------------------------------------------------
# Exact service grain
# ---------------------------------------------------------------------------


def test_intent_is_scoped_to_one_service_not_the_device(db_session, catalog_offer):
    """An ONT-grain answer would authorise another service's credential."""
    ont = _ont(db_session)
    served = _subscription(db_session, catalog_offer, "served")
    other = _subscription(db_session, catalog_offer, "other")
    db_session.commit()
    declared = _declare_cmd(db_session, _spec(ont, served), _ctx())
    _activate_cmd(db_session, declared.instance_id, _ctx())

    assert (
        active_primary_internet_intent(
            db_session, ont_id=ont.id, subscription_id=served.id
        )
        is not None
    )
    # Same device, different service: not authorised.
    assert (
        active_primary_internet_intent(
            db_session, ont_id=ont.id, subscription_id=other.id
        )
        is None
    )


def test_a_row_without_a_subscription_cannot_be_activated(db_session, catalog_offer):
    """Pre-owner rows must be adjudicated, never adopted."""
    ont = _ont(db_session)
    instance = OntWanServiceInstance(
        ont_id=ont.id,
        name="pre-owner",
        service_type="internet",
        connection_type="pppoe",
        is_primary=True,
        lifecycle_state=OntWanServiceLifecycle.unverified,
    )
    db_session.add(instance)
    db_session.commit()

    with pytest.raises(WanServiceIntentError) as excinfo:
        _activate_cmd(db_session, instance.id, _ctx())

    assert excinfo.value.code == IntentRefusal.missing_subscription.value


# ---------------------------------------------------------------------------
# One active primary, per service and per ONT
# ---------------------------------------------------------------------------


def test_a_service_cannot_have_two_active_primaries(db_session, catalog_offer):
    ont_a = _ont(db_session)
    ont_b = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "twoprim")
    db_session.commit()
    first = _declare_cmd(db_session, _spec(ont_a, sub), _ctx())
    _activate_cmd(db_session, first.instance_id, _ctx())
    second = _declare_cmd(db_session, _spec(ont_b, sub), _ctx())

    with pytest.raises(WanServiceIntentError) as excinfo:
        _activate_cmd(db_session, second.instance_id, _ctx())

    assert excinfo.value.code == IntentRefusal.duplicate_primary_for_subscription.value


def test_an_ont_cannot_carry_two_services_primary_terminations(
    db_session, catalog_offer
):
    ont = _ont(db_session)
    first_sub = _subscription(db_session, catalog_offer, "ontprim1")
    second_sub = _subscription(db_session, catalog_offer, "ontprim2")
    db_session.commit()
    first = _declare_cmd(db_session, _spec(ont, first_sub), _ctx())
    _activate_cmd(db_session, first.instance_id, _ctx())
    second = _declare_cmd(db_session, _spec(ont, second_sub), _ctx())

    with pytest.raises(WanServiceIntentError) as excinfo:
        _activate_cmd(db_session, second.instance_id, _ctx())

    assert excinfo.value.code == IntentRefusal.duplicate_primary_for_ont.value


def test_non_internet_services_stay_multi_wan_capable(db_session, catalog_offer):
    """The singular-primary rule is Internet-only; IPTV and VoIP coexist."""
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "multiwan")
    db_session.commit()

    for service_type in ("iptv", "voip"):
        declared = _declare_cmd(
            db_session, _spec(ont, sub, service_type=service_type, conn="dhcp"), _ctx()
        )
        _activate_cmd(db_session, declared.instance_id, _ctx())

    active = (
        db_session.query(OntWanServiceInstance)
        .filter(
            OntWanServiceInstance.ont_id == ont.id,
            OntWanServiceInstance.lifecycle_state == OntWanServiceLifecycle.active,
        )
        .count()
    )
    assert active == 2


# ---------------------------------------------------------------------------
# Replace and retire
# ---------------------------------------------------------------------------


def test_replace_hands_over_atomically_and_keeps_history(db_session, catalog_offer):
    old_ont = _ont(db_session)
    new_ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "replace")
    db_session.commit()
    outgoing = _declare_cmd(db_session, _spec(old_ont, sub), _ctx())
    _activate_cmd(db_session, outgoing.instance_id, _ctx())

    result = _replace_cmd(
        db_session,
        outgoing.instance_id,
        _spec(new_ont, sub),
        _ctx(reason="CPE swap, work order 41"),
    )

    retired = db_session.get(OntWanServiceInstance, outgoing.instance_id)
    assert retired.lifecycle_state is OntWanServiceLifecycle.retired
    assert retired.replaced_by_id == result.instance_id
    assert retired.retired_reason == "CPE swap, work order 41"
    # History preserved, not deleted.
    assert db_session.get(OntWanServiceInstance, outgoing.instance_id) is not None
    # And the service now terminates on the new ONT only.
    assert (
        active_primary_internet_intent(
            db_session, ont_id=new_ont.id, subscription_id=sub.id
        )
        is not None
    )
    assert (
        active_primary_internet_intent(
            db_session, ont_id=old_ont.id, subscription_id=sub.id
        )
        is None
    )


def test_retire_preserves_the_row(db_session, catalog_offer):
    """Release, movement, cancellation and return-to-inventory route here.

    Deleting the row would destroy the record of what a service was declared to
    be, which is the evidence a later adjudication depends on.
    """
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "retire")
    db_session.commit()
    declared = _declare_cmd(db_session, _spec(ont, sub), _ctx())
    _activate_cmd(db_session, declared.instance_id, _ctx())

    _retire_cmd(db_session, declared.instance_id, _ctx(reason="cancelled"))

    row = db_session.get(OntWanServiceInstance, declared.instance_id)
    assert row is not None
    assert row.lifecycle_state is OntWanServiceLifecycle.retired
    assert row.is_active is False
    assert row.retired_at is not None
    assert (
        active_primary_internet_intent(
            db_session, ont_id=ont.id, subscription_id=sub.id
        )
        is None
    )


def test_retiring_twice_is_idempotent(db_session, catalog_offer):
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "retire2")
    db_session.commit()
    declared = _declare_cmd(db_session, _spec(ont, sub), _ctx())

    first = _retire_cmd(db_session, declared.instance_id, _ctx(reason="x"))
    second = _retire_cmd(db_session, declared.instance_id, _ctx(reason="x"))

    assert first.lifecycle_state is OntWanServiceLifecycle.retired
    assert second.revision == first.revision


def test_a_retired_intent_cannot_be_reactivated(db_session, catalog_offer):
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "reactivate")
    db_session.commit()
    declared = _declare_cmd(db_session, _spec(ont, sub), _ctx())
    _retire_cmd(db_session, declared.instance_id, _ctx(reason="done"))

    with pytest.raises(WanServiceIntentError) as excinfo:
        _activate_cmd(db_session, declared.instance_id, _ctx())

    assert excinfo.value.code == IntentRefusal.already_retired.value


# ---------------------------------------------------------------------------
# Provenance and revision
# ---------------------------------------------------------------------------


def test_every_transition_requires_actor_and_reason(db_session, catalog_offer):
    """Refused at whichever layer sees it first.

    `execute_owner_command` already rejects a blank actor or reason, and the
    owner repeats the check so a future direct caller of the private helpers
    cannot slip past. Asserting on the base DomainError keeps the test honest
    about there being two layers rather than pinning one.
    """
    from app.services.domain_errors import DomainError

    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "prov")
    db_session.commit()

    for bad in (_ctx(actor="  "), _ctx(reason="")):
        with pytest.raises(DomainError):
            _declare_cmd(db_session, _spec(ont, sub), bad)


def test_the_owner_also_checks_provenance_itself(db_session, catalog_offer):
    """Defence in depth: the private helper refuses too."""
    from app.services.network.ont_wan_service_intent import _validate_context

    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "prov2")
    db_session.commit()
    assert ont is not None and sub is not None

    blank = CommandContext(
        command_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        actor="michael",
        scope="network.ont_wan_service_intent",
        reason="   ",
    )
    with pytest.raises(WanServiceIntentError) as excinfo:
        _validate_context(blank)

    assert excinfo.value.code == IntentRefusal.missing_evidence.value


def test_revision_advances_so_a_ruling_can_bind_it(db_session, catalog_offer):
    """A ruling taken before a change must not authorise a write after it."""
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "rev")
    db_session.commit()
    declared = _declare_cmd(db_session, _spec(ont, sub), _ctx())
    activated = _activate_cmd(db_session, declared.instance_id, _ctx())

    assert activated.revision == declared.revision + 1

    with pytest.raises(WanServiceIntentError) as excinfo:
        _activate_cmd(
            db_session,
            declared.instance_id,
            _ctx(),
            expected_revision=declared.revision,
        )

    assert excinfo.value.code == IntentRefusal.revision_conflict.value


def test_priority_orders_but_never_selects_authority(db_session, catalog_offer):
    """A high-priority non-primary instance must not become the answer."""
    ont = _ont(db_session)
    sub = _subscription(db_session, catalog_offer, "prio")
    db_session.commit()
    secondary = _declare_cmd(
        db_session,
        WanServiceIntentSpec(
            ont_id=ont.id,
            subscription_id=sub.id,
            service_type="internet",
            connection_type="pppoe",
            is_primary=False,
            priority=99,
        ),
        _ctx(),
    )
    _activate_cmd(db_session, secondary.instance_id, _ctx())

    assert (
        active_primary_internet_intent(
            db_session, ont_id=ont.id, subscription_id=sub.id
        )
        is None
    )
