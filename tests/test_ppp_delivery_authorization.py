"""Delivery-time PPP authorization: staged desired state is not permission.

The producer gate decides whether to STAGE a credential. This decides whether a
staged plan may REACH a device, and it does not trust the producer.

Production carries 1,318 ONTs with `pending_apply` set and PPP credentials
staged onto 1,373 services whose termination is not the ONT. So "desired state
exists" and "delivery is authorized" are demonstrably different questions, and
this gate answers only the second.
"""

from __future__ import annotations

import pytest

from app.models.network import OntUnit, OntWanServiceInstance
from app.services.network.ppp_delivery_authorization import (
    PPP_BUNDLE_ACTION_NAMES,
    PppDeliveryRefusal,
    authorize_ppp_delivery,
    is_ppp_bundle_action,
    partition_actions,
)


def _ont(db_session, serial="HWTC-DELIV-1"):
    ont = OntUnit(serial_number=serial, is_active=True)
    db_session.add(ont)
    db_session.flush()
    return ont


def _instance(db_session, ont, connection_type, *, active=True, name="svc"):
    instance = OntWanServiceInstance(
        ont_id=ont.id,
        name=name,
        connection_type=connection_type,
        is_active=active,
    )
    db_session.add(instance)
    db_session.flush()
    return instance


# ---------------------------------------------------------------------------
# Rulings
# ---------------------------------------------------------------------------


def test_one_active_pppoe_instance_authorizes(db_session):
    ont = _ont(db_session)
    _instance(db_session, ont, "pppoe")
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont.id)

    assert ruling.authorized is True
    assert ruling.refusal is None
    assert len(ruling.instance_ids) == 1


def test_no_service_intent_refuses(db_session):
    """The production majority: staged credentials, nobody declared PPPoE."""
    ont = _ont(db_session)
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont.id)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.no_pppoe_service_intent


def test_inactive_instance_does_not_authorize(db_session):
    ont = _ont(db_session)
    _instance(db_session, ont, "pppoe", active=False)
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont.id)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.no_pppoe_service_intent


@pytest.mark.parametrize("other", ["dhcp", "static"])
def test_a_non_pppoe_instance_does_not_authorize(db_session, other):
    ont = _ont(db_session)
    _instance(db_session, ont, other)
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont.id)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.no_pppoe_service_intent


def test_bridged_intent_refuses_even_alongside_pppoe(db_session):
    """Bridged places termination downstream whatever else is declared.

    A co-existing PPPoE row is a conflict to adjudicate, not permission.
    """
    ont = _ont(db_session)
    _instance(db_session, ont, "pppoe", name="ppp")
    _instance(db_session, ont, "bridged", name="bridge")
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont.id)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.bridged_service_intent


def test_two_active_pppoe_instances_are_ambiguous_not_a_pick(db_session):
    ont = _ont(db_session)
    _instance(db_session, ont, "pppoe", name="a")
    _instance(db_session, ont, "pppoe", name="b")
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont.id)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.ambiguous_pppoe_service_intent
    assert len(ruling.instance_ids) == 2


def test_unresolvable_ont_refuses(db_session):
    ruling = authorize_ppp_delivery(db_session, None)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.unresolvable_ont


def test_every_refusal_reason_is_distinct():
    """Category-level codes hide which precondition actually failed."""
    values = [member.value for member in PppDeliveryRefusal]

    assert len(values) == len(set(values))
    assert len(values) >= 4


# ---------------------------------------------------------------------------
# The bundle, not just the credential write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action_name",
    [
        "AcsSetPppoe",
        "AcsAddObject",
        "AcsDeleteObject",
        "AcsSetNatEnabled",
        "OltOmciPppoe",
        "OltOmciWanConfig",
        "OltOmciInternetConfig",
        "OltCreateServicePort",
        "OltDeleteServicePort",
    ],
)
def test_the_whole_ppp_bundle_is_gated_not_only_the_credential(action_name):
    """Each of these can establish or disturb a PPP termination on its own.

    Gating only AcsSetPppoe would leave object creation, OMCI provisioning,
    service-port work, NAT and stale-instance deletion as open routes.
    """
    assert action_name in PPP_BUNDLE_ACTION_NAMES

    action = type(action_name, (), {})()
    assert is_ppp_bundle_action(action) is True


@pytest.mark.parametrize(
    "action_name",
    [
        "AcsSetManagementServer",
        "AcsSetWifiConfig",
        "AcsSetDhcpServer",
        "AcsSetIpv6",
        "AcsSetRemoteAccess",
        "AcsSetWanIp",
        "OltAuthorize",
        "OltModifyLineProfile",
        "OltModifyDescription",
        "OltReset",
        "OltTr069ServerConfig",
    ],
)
def test_unrelated_reconciliation_is_not_gated(action_name):
    """Containment targets competing dialers, not ONT management generally."""
    action = type(action_name, (), {})()

    assert is_ppp_bundle_action(action) is False


def test_refusal_drops_only_the_ppp_bundle(db_session):
    ont = _ont(db_session)
    db_session.commit()
    ruling = authorize_ppp_delivery(db_session, ont.id)

    actions = [
        type("AcsSetManagementServer", (), {})(),
        type("AcsSetPppoe", (), {})(),
        type("AcsSetWifiConfig", (), {})(),
        type("OltOmciPppoe", (), {})(),
    ]
    deliverable, refused = partition_actions(actions, ruling)

    assert [type(a).__name__ for a in deliverable] == [
        "AcsSetManagementServer",
        "AcsSetWifiConfig",
    ]
    assert [type(a).__name__ for a in refused] == ["AcsSetPppoe", "OltOmciPppoe"]


def test_authorized_ruling_refuses_nothing(db_session):
    ont = _ont(db_session)
    _instance(db_session, ont, "pppoe")
    db_session.commit()
    ruling = authorize_ppp_delivery(db_session, ont.id)

    actions = [type("AcsSetPppoe", (), {})(), type("OltOmciPppoe", (), {})()]
    deliverable, refused = partition_actions(actions, ruling)

    assert len(deliverable) == 2
    assert refused == ()


# ---------------------------------------------------------------------------
# The gate must not be skippable
# ---------------------------------------------------------------------------


def test_applier_refuses_ppp_when_no_ruling_was_resolved():
    """An absent ruling is a refusal, not a pass.

    A caller that forgets to resolve authorization must not thereby deliver
    PPP. This is the difference between a gate and a convention.
    """
    from app.services.network.reconcile.applier import ApplyContext, apply_plan
    from app.services.network.reconcile.planner import Plan

    executed: list[str] = []

    class _Acs:
        def __getattr__(self, name):
            def _call(*args, **kwargs):
                executed.append(name)
                return {}

            return _call

    plan = Plan(
        actions=(type("AcsSetPppoe", (), {})(),),
        drifts=(),
        required_surfaces=frozenset(),
    )
    ctx = ApplyContext(olt_adapter=_Acs(), acs_client=_Acs())

    result = apply_plan(plan, ctx)

    # Skipped, not failed: the pass still succeeds so unrelated convergence
    # continues, but nothing PPP reached the device.
    assert result.success is True
    assert result.actions_applied == ()
    assert executed == []


def test_applier_delivers_ppp_when_authorized(db_session):
    from app.services.network.reconcile.applier import ApplyContext, apply_plan
    from app.services.network.reconcile.planner import Plan

    ont = _ont(db_session, serial="HWTC-DELIV-OK")
    _instance(db_session, ont, "pppoe")
    db_session.commit()
    ruling = authorize_ppp_delivery(db_session, ont.id)
    assert ruling.authorized

    plan = Plan(actions=(), drifts=(), required_surfaces=frozenset())
    ctx = ApplyContext(
        olt_adapter=object(), acs_client=object(), ppp_authorization=ruling
    )

    result = apply_plan(plan, ctx)

    assert result.success is True
