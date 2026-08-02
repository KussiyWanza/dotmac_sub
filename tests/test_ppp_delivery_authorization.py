"""Delivery-time PPP authorization: staged desired state is not permission.

The producer gate decides whether to STAGE a credential. This decides whether a
staged plan may REACH a device, and it does not trust the producer.

Production carries 1,318 ONTs with `pending_apply` set and PPP credentials
staged onto 1,373 services whose termination is not the ONT. So "desired state
exists" and "delivery is authorized" are demonstrably different questions, and
this gate answers only the second.

Disabling the producer does not retire this gate: `network.ont_reconcile` stays
enabled and keeps consuming the projections already staged.
"""

from __future__ import annotations

import pytest

from app.models.network import (
    OntAssignment,
    OntUnit,
    OntWanServiceInstance,
    OntWanServiceLifecycle,
)
from app.services.network.ppp_delivery_authorization import (
    PppActionPurpose,
    PppDeliveryDecision,
    PppDeliveryRefusal,
    PppDeliveryRuling,
    PppDeliveryScope,
    authorize_ppp_delivery,
    classify_action,
    derive_delivery_scope,
    is_ppp_bundle_action,
    partition_actions,
    plan_ppp_fingerprint,
)


def _ont(db_session, serial="HWTC-DELIV-1"):
    ont = OntUnit(serial_number=serial, is_active=True)
    db_session.add(ont)
    db_session.flush()
    return ont


def _assign(db_session, ont, subscription_id, *, active=True):
    row = OntAssignment(
        ont_unit_id=ont.id, subscription_id=subscription_id, active=active
    )
    db_session.add(row)
    db_session.flush()
    return row


def _instance(
    db_session,
    ont,
    connection_type,
    *,
    subscription_id=None,
    lifecycle=OntWanServiceLifecycle.active,
    is_primary=True,
    service_type="internet",
    is_active=True,
    name="svc",
):
    instance = OntWanServiceInstance(
        ont_id=ont.id,
        subscription_id=subscription_id,
        name=name,
        service_type=service_type,
        connection_type=connection_type,
        is_primary=is_primary,
        lifecycle_state=lifecycle,
        is_active=is_active,
    )
    db_session.add(instance)
    db_session.flush()
    return instance


DIALER_SECRET = "s3cret-pppoe"  # noqa: S105 - test fixture value


def _authorize_ready(db_session, ont, subscription, *, secret=DIALER_SECRET):
    """Give the service a real credential and stage the matching projection.

    Delivery authorization no longer trusts the staged payload to describe
    itself: it resolves the one active AccessCredential for the exact service,
    keyed-fingerprints it, and requires the ONT's recorded projection
    fingerprint to match. Both halves are therefore part of a legitimate
    fixture.
    """
    from app.models.catalog import AccessCredential
    from app.services.cpe_dialer_credential_reconcile import dialer_fingerprint
    from app.services.credential_crypto import encrypt_credential
    from app.services.network.ont_desired_config import set_desired_config_values

    username = "100024456"
    db_session.add(
        AccessCredential(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            username=username,
            secret_hash=encrypt_credential(secret),
            is_active=True,
        )
    )
    db_session.flush()
    set_desired_config_values(
        ont,
        {
            "delivery.dialer_credential_fingerprint": dialer_fingerprint(
                username, secret
            )
        },
    )
    db_session.add(ont)
    db_session.flush()
    return username


def _fake(name, **attrs):
    """A stand-in planner action; only its class name and fields are read."""
    return type(name, (), attrs)()


# ---------------------------------------------------------------------------
# Grain: a ruling is bound to one exact service
# ---------------------------------------------------------------------------


def test_active_primary_intent_for_the_exact_service_authorizes(
    db_session, subscription
):
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    instance = _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorized is True
    assert ruling.refusal is None
    assert ruling.subscription_id == str(subscription.id)
    assert ruling.instance_id == str(instance.id)
    assert ruling.instance_revision == instance.revision


def test_a_ruling_for_service_a_is_unusable_for_service_b(db_session, subscription):
    """The whole point of exact grain.

    An ONT-grain ruling would authorise whichever service happens to share the
    device. This asserts the ruling refuses to travel.
    """
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()

    actions = [_fake("AcsSetPppoe", username="100024456", vlan=203)]
    fingerprint = plan_ppp_fingerprint(actions)
    ruling = authorize_ppp_delivery(db_session, ont, actions=actions)
    scope = derive_delivery_scope(db_session, ont, actions)

    assert ruling.authorizes(scope) is True
    assert (
        ruling.authorizes(
            PppDeliveryScope(
                ont_id=scope.ont_id,
                subscription_id="00000000-0000-0000-0000-0000000000ff",
                instance_id=scope.instance_id,
                instance_revision=scope.instance_revision,
                plan_fingerprint=scope.plan_fingerprint,
                credential_fingerprint=scope.credential_fingerprint,
            )
        )
        is False
    )


def test_a_ruling_does_not_travel_to_another_ont(db_session, subscription):
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()

    actions = [_fake("AcsSetPppoe", username="u", vlan=203)]
    ruling = authorize_ppp_delivery(db_session, ont, actions=actions)
    scope = derive_delivery_scope(db_session, ont, actions)

    assert (
        ruling.authorizes(
            PppDeliveryScope(
                ont_id="a-different-ont",
                subscription_id=scope.subscription_id,
                instance_id=scope.instance_id,
                instance_revision=scope.instance_revision,
                plan_fingerprint=scope.plan_fingerprint,
                credential_fingerprint=scope.credential_fingerprint,
            )
        )
        is False
    )


def test_a_superseded_instance_revision_does_not_authorize(db_session, subscription):
    """A ruling taken before a replace cannot authorise a write after it."""
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()

    actions = [_fake("AcsSetPppoe", username="u", vlan=203)]
    ruling = authorize_ppp_delivery(db_session, ont, actions=actions)
    scope = derive_delivery_scope(db_session, ont, actions)
    assert ruling.authorizes(scope) is True

    bumped = PppDeliveryScope(
        ont_id=scope.ont_id,
        subscription_id=scope.subscription_id,
        instance_id=scope.instance_id,
        instance_revision=scope.instance_revision + 1,
        plan_fingerprint=scope.plan_fingerprint,
        credential_fingerprint=scope.credential_fingerprint,
    )
    assert ruling.authorizes(bumped) is False


def test_a_changed_plan_invalidates_the_ruling(db_session, subscription):
    """A ruling granted for one credential does not authorise another."""
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()

    granted = [_fake("AcsSetPppoe", username="LOGIN-A", vlan=203)]
    ruling = authorize_ppp_delivery(db_session, ont, actions=granted)

    swapped = [_fake("AcsSetPppoe", username="LOGIN-B", vlan=203)]
    scope = derive_delivery_scope(db_session, ont, swapped)

    assert ruling.authorizes(scope) is False


def test_a_missing_scope_is_a_refusal(db_session, subscription):
    """A caller that cannot say what it is delivering delivers nothing.

    The earlier version skipped a comparison when the caller passed None,
    so supplying nothing was full authorisation -- the opposite of a gate.
    """
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorizes(None) is False


def test_the_fingerprint_excludes_secrets(db_session):
    """The fingerprint reaches logs and audit rows; it carries no secret."""
    a = _fake("AcsSetPppoe", username="u", vlan=203, password_ref="bao://secret-a")
    b = _fake("AcsSetPppoe", username="u", vlan=203, password_ref="bao://secret-b")

    assert plan_ppp_fingerprint([a]) == plan_ppp_fingerprint([b])
    assert "secret" not in plan_ppp_fingerprint([a])


def test_the_fingerprint_is_order_independent():
    a = _fake("AcsSetPppoe", username="u", vlan=203)
    b = _fake("OltOmciPppoe", username="u", vlan=203, ip_index=1)

    assert plan_ppp_fingerprint([a, b]) == plan_ppp_fingerprint([b, a])


# ---------------------------------------------------------------------------
# Authority: lifecycle_state, never legacy is_active
# ---------------------------------------------------------------------------


def test_unverified_legacy_row_does_not_authorize_even_when_is_active(
    db_session, subscription
):
    """The defect this slice exists to close.

    Migration 456 deliberately leaves legacy `is_active` untouched, so every
    pre-owner row can still be is_active=True while sitting in `unverified`.
    The first version of this gate read `is_active` at ONT grain and would have
    authorised exactly the rows the owner slice quarantined.
    """
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(
        db_session,
        ont,
        "pppoe",
        subscription_id=subscription.id,
        lifecycle=OntWanServiceLifecycle.unverified,
        is_active=True,
    )
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.no_active_service_intent


def test_planned_intent_does_not_authorize(db_session, subscription):
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(
        db_session,
        ont,
        "pppoe",
        subscription_id=subscription.id,
        lifecycle=OntWanServiceLifecycle.planned,
    )
    db_session.commit()

    assert authorize_ppp_delivery(db_session, ont).authorized is False


def test_retired_intent_does_not_authorize(db_session, subscription):
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(
        db_session,
        ont,
        "pppoe",
        subscription_id=subscription.id,
        lifecycle=OntWanServiceLifecycle.retired,
    )
    db_session.commit()

    assert authorize_ppp_delivery(db_session, ont).authorized is False


def test_bridged_intent_refuses(db_session, subscription):
    """Bridged places termination on a downstream router."""
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "bridged", subscription_id=subscription.id)
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.bridged_service_intent


# ---------------------------------------------------------------------------
# Assignment resolution
# ---------------------------------------------------------------------------


def test_no_active_assignment_refuses(db_session):
    ont = _ont(db_session)
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.no_active_assignment


def test_multiple_active_assignments_are_refused_not_picked(db_session, subscription):
    """Ambiguity is a refusal, never a selection.

    Picking the first would hand one service's credential to another. The
    schema already prevents this state -- `ont_assignments` carries a unique
    index on the active assignment per ONT -- so this exercises the resolver
    directly rather than constructing a row pair the database rejects. The
    branch is kept because that index is partial and Postgres-only, and a
    relaxation must not silently become a pick.
    """
    from app.services.network import ppp_delivery_authorization as mod

    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    db_session.commit()

    class _TwoRows:
        def execute(self, *_args, **_kwargs):
            class _R:
                def scalars(self):
                    class _S:
                        def all(self):
                            return ["sub-a", "sub-b"]

                    return _S()

            return _R()

    subscription_id, refusal = mod.resolve_exact_assignment_subscription(
        _TwoRows(), ont.id
    )

    assert subscription_id is None
    assert refusal is PppDeliveryRefusal.ambiguous_assignment


def test_unresolvable_ont_refuses(db_session):
    ruling = authorize_ppp_delivery(db_session, None)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.unresolvable_ont


def test_every_refusal_reason_is_distinct():
    values = [member.value for member in PppDeliveryRefusal]
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# Purpose-aware classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        _fake("AcsSetPppoe"),
        _fake("OltOmciPppoe"),
        _fake("OltOmciInternetConfig"),
        _fake("OltOmciWanConfig"),
        _fake("AcsSetNatEnabled"),
        _fake("AcsAddObject", object_path="...WANConnectionDevice.1.WANPPPConnection"),
        _fake("AcsDeleteObject", object_path="...WANPPPConnection.3."),
        _fake("OltCreateServicePort", slot="wan"),
        _fake("OltDeleteServicePort", slot="wan"),
    ],
)
def test_ppp_bearing_actions_are_gated(action):
    """Each can establish or disturb a PPP termination on its own."""
    assert classify_action(action) is PppActionPurpose.ppp_bearing
    assert is_ppp_bundle_action(action) is True


@pytest.mark.parametrize(
    "action",
    [
        _fake("AcsSetManagementServer"),
        _fake("AcsSetWifiConfig"),
        _fake("AcsSetDhcpServer"),
        _fake("AcsSetIpv6"),
        _fake("AcsSetRemoteAccess"),
        _fake("AcsSetWanIp"),
        _fake("OltAuthorize"),
        _fake("OltModifyLineProfile"),
        _fake("OltModifyDescription"),
        _fake("OltReset"),
        _fake("OltTr069ServerConfig"),
        # Management service ports are not dialer work.
        _fake("OltCreateServicePort", slot="mgmt"),
        _fake("OltDeleteServicePort", slot="mgmt"),
        # Object lifecycle on a non-PPP object.
        _fake("AcsAddObject", object_path="...WANDevice.1.WANIPConnection"),
    ],
)
def test_management_work_is_not_gated(action):
    """Containment targets competing dialers, not ONT management generally."""
    assert classify_action(action) is PppActionPurpose.not_ppp
    assert is_ppp_bundle_action(action) is False


def test_management_service_port_survives_a_refusal(db_session):
    """The concrete regression: OltCreateServicePort is both mgmt and wan.

    Gating it by class name blocked management convergence that has nothing to
    do with a dialer.
    """
    ont = _ont(db_session)
    db_session.commit()
    ruling = authorize_ppp_delivery(db_session, ont)
    assert not ruling.authorized

    actions = [
        _fake("OltCreateServicePort", slot="mgmt"),
        _fake("OltCreateServicePort", slot="wan"),
    ]
    deliverable, refused = partition_actions(actions, ruling, None)

    assert [a.slot for a in deliverable] == ["mgmt"]
    assert [a.slot for a in refused] == ["wan"]


def test_an_unknown_slot_fails_closed():
    """A stale port whose VLAN did not resolve. Unknown purpose is withheld."""
    action = _fake("OltDeleteServicePort", slot="unknown")

    assert classify_action(action) is PppActionPurpose.indeterminate
    assert is_ppp_bundle_action(action) is True


# ---------------------------------------------------------------------------
# Partitioning
# ---------------------------------------------------------------------------


def test_refusal_drops_only_ppp_bearing_actions(db_session):
    ont = _ont(db_session)
    db_session.commit()
    ruling = authorize_ppp_delivery(db_session, ont)

    actions = [
        _fake("AcsSetManagementServer"),
        _fake("AcsSetPppoe"),
        _fake("AcsSetWifiConfig"),
        _fake("OltOmciPppoe"),
    ]
    deliverable, refused = partition_actions(actions, ruling, None)

    assert [type(a).__name__ for a in deliverable] == [
        "AcsSetManagementServer",
        "AcsSetWifiConfig",
    ]
    assert [type(a).__name__ for a in refused] == ["AcsSetPppoe", "OltOmciPppoe"]


def test_authorized_ruling_refuses_nothing(db_session, subscription):
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()
    actions = [_fake("AcsSetPppoe"), _fake("OltOmciPppoe")]
    ruling = authorize_ppp_delivery(db_session, ont, actions=actions)
    scope = derive_delivery_scope(db_session, ont, actions)
    deliverable, refused = partition_actions(actions, ruling, scope)

    assert len(deliverable) == 2
    assert refused == ()


def test_a_none_ruling_refuses():
    deliverable, refused = partition_actions([_fake("AcsSetPppoe")], None, None)

    assert deliverable == ()
    assert len(refused) == 1


def test_an_authorized_ruling_for_the_wrong_service_still_refuses(
    db_session, subscription
):
    """A stale ruling is worse than none: it looks like diligence."""
    ont = _ont(db_session)
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()
    ruling = authorize_ppp_delivery(db_session, ont)
    assert ruling.authorized

    deliverable, refused = partition_actions(
        [_fake("AcsSetPppoe")],
        ruling,
        PppDeliveryScope(
            ont_id=str(ont.id),
            subscription_id="00000000-0000-0000-0000-0000000000bb",
            instance_id=ruling.instance_id,
            instance_revision=ruling.instance_revision,
            plan_fingerprint=ruling.plan_fingerprint,
            credential_fingerprint=ruling.credential_fingerprint,
        ),
    )

    assert deliverable == ()
    assert len(refused) == 1


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
        actions=(_fake("AcsSetPppoe"),), drifts=(), required_surfaces=frozenset()
    )
    ctx = ApplyContext(olt_adapter=_Acs(), acs_client=_Acs())

    result = apply_plan(plan, ctx)

    # Skipped, not failed: the pass still succeeds so unrelated convergence
    # continues, but nothing PPP reached the device.
    assert result.success is True
    assert result.actions_applied == ()
    assert executed == []
    # ...and it is recorded as residual drift rather than silently dropped.
    assert [r.action_name for r in result.refused_ppp] == ["AcsSetPppoe"]


def test_refused_work_is_recorded_as_residual_drift():
    """Refusals must stay visible; a containment that reports nothing is one
    that nobody can audit."""
    from app.services.network.reconcile.applier import ApplyContext, apply_plan
    from app.services.network.reconcile.planner import Plan

    plan = Plan(
        actions=(_fake("AcsSetPppoe"), _fake("OltCreateServicePort", slot="mgmt")),
        drifts=(),
        required_surfaces=frozenset(),
    )

    class _Adapter:
        def __getattr__(self, name):
            def _call(*args, **kwargs):
                return {}

            return _call

    ctx = ApplyContext(olt_adapter=_Adapter(), acs_client=_Adapter())
    result = apply_plan(plan, ctx)

    refused = {r.action_name for r in result.refused_ppp}
    assert refused == {"AcsSetPppoe"}
    assert all(r.refusal for r in result.refused_ppp), "each refusal carries a reason"


def test_a_wrong_scope_ruling_does_not_deliver_through_the_applier(
    db_session, subscription
):
    """Enforcement is at the point of use, not only the point of issue."""
    from app.services.network.reconcile.applier import ApplyContext, apply_plan
    from app.services.network.reconcile.planner import Plan

    ont = _ont(db_session, serial="HWTC-DELIV-SCOPE")
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()
    ruling = authorize_ppp_delivery(db_session, ont)
    assert ruling.authorized

    executed: list[str] = []

    class _Acs:
        def __getattr__(self, name):
            def _call(*args, **kwargs):
                executed.append(name)
                return {}

            return _call

    plan = Plan(
        actions=(_fake("AcsSetPppoe"),), drifts=(), required_surfaces=frozenset()
    )
    ctx = ApplyContext(
        olt_adapter=_Acs(),
        acs_client=_Acs(),
        ppp_authorization=ruling,
        # A scope for a different service than the ruling was granted for.
        ppp_scope=PppDeliveryScope(
            ont_id=str(ont.id),
            subscription_id="00000000-0000-0000-0000-0000000000cc",
            instance_id=ruling.instance_id,
            instance_revision=ruling.instance_revision,
            plan_fingerprint=ruling.plan_fingerprint,
            credential_fingerprint=ruling.credential_fingerprint,
        ),
    )

    result = apply_plan(plan, ctx)

    assert executed == []
    assert [r.action_name for r in result.refused_ppp] == ["AcsSetPppoe"]


def test_ruling_is_typed_not_duck_typed():
    """`ApplyContext.ppp_authorization` is a PppDeliveryRuling or None.

    The first version accepted `Any` and probed it with getattr, so any object
    with a truthy `authorized` attribute would have been believed.
    """
    from app.services.network.reconcile.applier import ApplyContext

    ctx = ApplyContext(olt_adapter=object(), acs_client=object())
    assert ctx.ppp_authorization is None

    ruling = PppDeliveryRuling(
        decision=PppDeliveryDecision.refused,
        refusal=PppDeliveryRefusal.no_active_service_intent,
        ont_id="x",
    )
    assert ruling.authorized is False


# ---------------------------------------------------------------------------
# Credential authority: the staged payload may not authorise itself
# ---------------------------------------------------------------------------


def test_a_staged_payload_cannot_authorise_itself(db_session, subscription):
    """The defect this correction closes.

    An earlier version fingerprinted the plan and compared it to a ruling
    derived from the same plan, so a foreign credential already staged onto the
    ONT proved only that it equalled itself. Authority now comes from the
    AccessCredential record: with no credential, a fully staged projection
    authorises nothing.
    """
    from app.services.network.ont_desired_config import set_desired_config_values

    ont = _ont(db_session, serial="HWTC-SELFAUTH")
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    # A projection exists and looks entirely plausible...
    set_desired_config_values(
        ont, {"delivery.dialer_credential_fingerprint": "ppp-fp:whatever-was-staged"}
    )
    db_session.add(ont)
    db_session.commit()

    ruling = authorize_ppp_delivery(
        db_session, ont, actions=[_fake("AcsSetPppoe", username="x", vlan=203)]
    )

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.no_authoritative_credential


def test_a_foreign_credential_projection_is_refused(db_session, subscription):
    """The production failure mode: a dialer staged for a different service."""
    from app.services.network.ont_desired_config import set_desired_config_values

    ont = _ont(db_session, serial="HWTC-FOREIGN")
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    # ...then someone else's credential lands in the projection.
    set_desired_config_values(
        ont, {"delivery.dialer_credential_fingerprint": "ppp-fp:someone-elses"}
    )
    db_session.add(ont)
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.credential_fingerprint_mismatch


def test_no_staged_projection_is_refused(db_session, subscription):
    """Nothing staged means there is no projection for a ruling to authorise."""
    ont = _ont(db_session, serial="HWTC-NOPROJ")
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    # Credential exists, but nothing was ever projected onto the ONT.
    from app.models.catalog import AccessCredential
    from app.services.credential_crypto import encrypt_credential

    db_session.add(
        AccessCredential(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            username="100024456",
            secret_hash=encrypt_credential("s3cret-pppoe"),
            is_active=True,
        )
    )
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.missing_projection_fingerprint


def test_an_unreadable_credential_is_refused(db_session, subscription):
    """An undecryptable secret cannot be keyed, so it cannot authorise.

    Note the 'enc:' prefix: a bare value is a legacy PLAINTEXT credential and
    decrypts to itself, so it would not exercise this path at all.
    """
    from app.models.catalog import AccessCredential

    ont = _ont(db_session, serial="HWTC-UNREADABLE")
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    db_session.add(
        AccessCredential(
            subscriber_id=subscription.subscriber_id,
            subscription_id=subscription.id,
            username="100024456",
            secret_hash="enc:this-is-not-valid-ciphertext",
            is_active=True,
        )
    )
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.unreadable_authoritative_credential


def test_two_active_credentials_are_refused_not_picked(db_session, subscription):
    """Which credential may dial is unstated; a device write must not pick."""
    from app.models.catalog import AccessCredential
    from app.services.credential_crypto import encrypt_credential

    ont = _ont(db_session, serial="HWTC-AMBIG-CRED")
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    for username in ("100024456", "100099999"):
        db_session.add(
            AccessCredential(
                subscriber_id=subscription.subscriber_id,
                subscription_id=subscription.id,
                username=username,
                secret_hash=encrypt_credential("s3cret-pppoe"),
                is_active=True,
            )
        )
    db_session.commit()

    ruling = authorize_ppp_delivery(db_session, ont)

    assert ruling.authorized is False
    assert ruling.refusal is PppDeliveryRefusal.ambiguous_authoritative_credential


def test_the_plan_fingerprint_is_not_credential_authority(db_session, subscription):
    """Plan shape and credential ownership are separate fields on purpose."""
    ont = _ont(db_session, serial="HWTC-SEPARATE")
    _assign(db_session, ont, subscription.id)
    _instance(db_session, ont, "pppoe", subscription_id=subscription.id)
    _authorize_ready(db_session, ont, subscription)
    db_session.commit()

    actions = [_fake("AcsSetPppoe", username="100024456", vlan=203)]
    ruling = authorize_ppp_delivery(db_session, ont, actions=actions)

    assert ruling.authorized is True
    assert ruling.plan_fingerprint == plan_ppp_fingerprint(actions)
    assert ruling.credential_fingerprint != ruling.plan_fingerprint
    assert ruling.credential_fingerprint


# ---------------------------------------------------------------------------
# Lock policy
# ---------------------------------------------------------------------------


def test_the_producer_intent_gate_does_not_lock(db_session, subscription):
    """Lock inversion guard.

    The producer reads intent and then writes the OntUnit row. Delivery locks
    the OntUnit row first and reads intent second. If the producer also locked
    intent, the two paths would take OntUnit and the intent row in opposite
    orders and deadlock under concurrency.

    Pinned as a default rather than a comment: `for_update` must stay False
    unless a caller opts in.
    """
    import inspect

    from app.services.network.ppp_delivery_authorization import (
        authorize_ppp_termination_intent,
    )

    signature = inspect.signature(authorize_ppp_termination_intent)
    assert signature.parameters["for_update"].default is False


def test_the_producer_gate_never_opts_into_locking():
    """The producer must call the intent gate without `for_update`."""
    from pathlib import Path

    source = Path("app/services/cpe_dialer_credential_reconcile.py").read_text(
        encoding="utf-8"
    )
    gate = source[source.index("def termination_intent(") :]
    gate = gate[: gate.index("\ndef ", 1)]
    body = gate.split('"""')[-1]

    assert "authorize_ppp_termination_intent" in body
    assert "for_update" not in body, (
        "the producer must not lock intent: delivery locks OntUnit first, so "
        "locking intent here inverts the order and deadlocks"
    )


def test_delivery_locks_intent_and_credential(db_session, subscription):
    """Delivery is the locking path, in OntUnit -> intent -> credential order.

    The OntUnit lock is held by the reconciler before this runs; this asserts
    the two reads underneath it opt into locking.
    """
    from pathlib import Path

    source = Path("app/services/network/ppp_delivery_authorization.py").read_text(
        encoding="utf-8"
    )
    delivery = source[source.index("def authorize_ppp_delivery(") :]
    delivery = delivery[: delivery.index("\ndef ", 1)]
    assert "for_update=True" in delivery

    credential = source[source.index("def _resolve_credential_authority(") :]
    credential = credential[: credential.index("\ndef ", 1)]
    assert "for_update=True" in credential


def test_the_credential_lock_covers_the_parent_and_all_children():
    """Phantom-read guard.

    Filtering `is_active` before FOR UPDATE locks only the rows that are
    already active, so nothing blocks an inactive credential being activated
    or a second active one being inserted -- and the schema has no
    one-active-per-subscription constraint to fall back on.
    """
    from pathlib import Path

    source = Path("app/services/cpe_dialer_credential_reconcile.py").read_text(
        encoding="utf-8"
    )
    fn = source[source.index("def _authoritative_credentials(") :]
    fn = fn[: fn.index("\ndef ", 1)]
    locking = fn[fn.index("if for_update:") : fn.index("else:")]

    # The parent subscription row is locked...
    assert "Subscription" in locking
    # ...and the credential lock carries NO active filter. The in-memory
    # filter uses `row.is_active`; a SQL pre-filter would say
    # `AccessCredential.is_active`, which is the phantom-read bug.
    assert "AccessCredential.is_active" not in locking, (
        "locking must not pre-filter on is_active"
    )
    # The active set is decided after the rows are held.
    assert "if row.is_active" in fn
