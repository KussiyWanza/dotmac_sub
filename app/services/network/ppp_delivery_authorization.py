"""Delivery-time authorization for the PPP action bundle.

Owner: ``network.ppp_delivery_authorization``.

This is the second, independent half of the CPE dialer containment. The
producer gate (``cpe_dialer_credential_reconcile``) decides whether to STAGE a
credential. This decides whether a staged plan may REACH a device, and it does
not trust the producer to have been right.

Why a separate gate at all: ``delivery.pending_apply``, stored desired values
and credential fingerprints are all evidence that something once wrote desired
state. None of them is authorization to deliver it. Production carries 1,318
ONTs with ``pending_apply`` set and PPP credentials staged onto 1,373 services
whose termination is not the ONT, so "desired state exists" and "delivery is
authorized" are demonstrably different questions.

Disabling the producer does NOT make this gate unnecessary. ``network.
ont_reconcile`` stays enabled and keeps consuming the 1,318 projections already
staged; the producer flag only stops NEW staging.

Intent source: the owner, not a column. The first version of this gate read
``OntWanServiceInstance.is_active`` at ONT grain. That was wrong twice over:

* **Grain.** An ONT-grain answer authorises whichever service happens to share
  the device. A ruling for service A must be unusable for service B.
* **Authority.** ``is_active`` is a legacy derived flag. Migration 456 left it
  untouched on purpose, so a pre-owner row can still be ``is_active=True``
  while sitting in ``unverified`` — non-authorising by construction. Reading it
  would let exactly the unadjudicated rows the owner slice quarantined
  authorise a staged payload.

Authority is now ``network.ont_wan_service_intent.active_primary_internet_intent``,
which requires ``lifecycle_state == active`` at exact ``ont_id`` +
``subscription_id`` grain. Because every legacy row starts ``unverified``, this
gate refuses all of them until they are adjudicated through owner commands.

Two fields that look like intent remain unused: ``OntAssignment.wan_mode`` /
``ip_mode`` and ``OntAssignment.pppoe_username`` were copied into desired config
and then explicitly ``NULL``ed by migration 084, so survivors are residue.

Scope. Only PPP-bearing actions are gated, classified by typed purpose rather
than class name. Management service ports, Wi-Fi, LAN, DHCP-server, IPv6,
remote access, line/service profile, descriptions, authorization and reset are
untouched: the containment targets competing PPPoE dialers, not ONT management.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

OWNER = "network.ppp_delivery_authorization"

#: Marks a ruling that carries no credential/plan scope. A ruling granted
#: without scope may not authorise a scoped delivery -- see ``authorizes``.
UNSCOPED = ""


class PppDeliveryRefusal(StrEnum):
    """Stable reasons a PPP delivery is refused. One per distinct cause.

    Category-level codes hide which precondition failed, so an operator cannot
    tell "nobody ever declared PPP for this service" from "two instances
    disagree" without reading prose.
    """

    #: No ACTIVE declared intent binds this ONT to this service. Covers the
    #: adjudication backlog: every pre-owner row is ``unverified``.
    no_active_service_intent = "no_active_service_intent"
    #: An active instance declares bridged termination, which places PPP on a
    #: downstream router regardless of anything else staged.
    bridged_service_intent = "bridged_service_intent"
    #: The ONT carries no active subscriber assignment, so no exact service can
    #: be resolved to check intent against.
    no_active_assignment = "no_active_assignment"
    #: More than one active assignment. Which service the staged payload
    #: belongs to is unstated, and a device write may not resolve it by picking.
    ambiguous_assignment = "ambiguous_assignment"
    #: The caller could not supply an ONT identity, so nothing can be checked.
    unresolvable_ont = "unresolvable_ont"
    #: A ruling was presented for a different ONT, service, instance revision or
    #: credential scope than the one being delivered.
    scope_mismatch = "scope_mismatch"


class PppDeliveryDecision(StrEnum):
    authorized = "authorized"
    refused = "refused"


class PppActionPurpose(StrEnum):
    """What a planned action does to PPP termination.

    Classification is by typed purpose, never by class name alone. The same
    class can be management work or PPP work: ``OltCreateServicePort`` carries
    both the mgmt (VLAN 201) and WAN (VLAN 203) slot, and gating it wholesale
    blocked management convergence that has nothing to do with a dialer.
    """

    #: Cannot establish or disturb a PPP termination. Never gated.
    not_ppp = "not_ppp"
    #: Establishes, mutates or removes a PPP termination. Gated.
    ppp_bearing = "ppp_bearing"
    #: Purpose not determinable from the action. Gated, because the failure
    #: being prevented is a device silently acquiring a dialer.
    indeterminate = "indeterminate"


@dataclass(frozen=True, slots=True)
class PppDeliveryRuling:
    """Typed, provenanced delivery ruling bound to one exact service.

    Emitted whether or not anything is blocked: a plan that legitimately
    carries no PPP actions still records why delivery was or was not
    authorized, so "nothing was sent" is distinguishable from "the gate never
    ran".

    The binding fields are what make a ruling non-transferable. A ruling
    granted for (ONT, subscription, instance revision, credential scope) cannot
    authorise a delivery for any other combination -- including the same ONT
    after a ``replace_wan_service_intent`` bumped the revision.
    """

    decision: PppDeliveryDecision
    refusal: PppDeliveryRefusal | None
    ont_id: str
    subscription_id: str = ""
    instance_id: str = ""
    instance_revision: int = 0
    credential_scope: str = UNSCOPED
    owner: str = OWNER

    @property
    def authorized(self) -> bool:
        return self.decision is PppDeliveryDecision.authorized

    def authorizes(
        self,
        *,
        ont_id: Any,
        subscription_id: Any = None,
        credential_scope: str | None = None,
    ) -> bool:
        """Whether this ruling authorises THIS delivery.

        Checked at the point of use, not just at the point of issue. A ruling
        that authorises something is still the wrong ruling if it was granted
        for a different service, and a stale ruling is worse than none because
        it looks like diligence.
        """
        if not self.authorized:
            return False
        if str(ont_id or "") != self.ont_id:
            return False
        if subscription_id is not None and str(subscription_id) != self.subscription_id:
            return False
        if credential_scope is not None and credential_scope != self.credential_scope:
            return False
        return True

    def as_log_extra(self) -> dict[str, object]:
        return {
            "ppp_delivery_decision": self.decision.value,
            "ppp_delivery_refusal": self.refusal.value if self.refusal else None,
            "ont_id": self.ont_id,
            "subscription_id": self.subscription_id,
            "ppp_service_instance": self.instance_id,
            "ppp_service_instance_revision": self.instance_revision,
            "owner": self.owner,
        }


def _refused(
    refusal: PppDeliveryRefusal,
    *,
    ont_id: Any = "",
    subscription_id: Any = "",
) -> PppDeliveryRuling:
    return PppDeliveryRuling(
        decision=PppDeliveryDecision.refused,
        refusal=refusal,
        ont_id=str(ont_id or ""),
        subscription_id=str(subscription_id or ""),
    )


def _enum_value(value: Any) -> str:
    """Normalise an enum-or-string column to its lowercase value.

    ``str(SomeEnum.pppoe)`` yields ``"OntConnectionType.pppoe"``, so a naive
    comparison against ``"pppoe"`` never matches and every instance reads as
    non-PPP -- silently converting this gate into a blanket refusal.
    """
    return str(getattr(value, "value", value) or "").strip().lower()


def _ppp_object_path(path: Any) -> bool:
    """Whether a TR-069 object path targets the PPP connection object."""
    return "wanpppconnection" in str(path or "").strip().lower()


def classify_action(action: Any) -> PppActionPurpose:
    """Classify one planned action by what it does to PPP termination."""
    name = type(action).__name__

    # Unconditionally PPP: the credential write and the OMCI PPPoE step name
    # the protocol outright.
    if name in {"AcsSetPppoe", "OltOmciPppoe"}:
        return PppActionPurpose.ppp_bearing

    # OMCI steps 2 and 3 complete the WAN sequence on the ip-index that step 1
    # provisioned. They are part of the same termination even though neither
    # mentions PPP.
    if name in {"OltOmciInternetConfig", "OltOmciWanConfig"}:
        return PppActionPurpose.ppp_bearing

    # NAT is set on a specific WANConnectionDevice/instance pair, which is the
    # PPP instance in this planner.
    if name == "AcsSetNatEnabled":
        return PppActionPurpose.ppp_bearing

    # Object lifecycle is PPP only when the path is the PPP object. Creating or
    # deleting a WANIPConnection or a device sub-object is not dialer work.
    if name in {"AcsAddObject", "AcsDeleteObject"}:
        return (
            PppActionPurpose.ppp_bearing
            if _ppp_object_path(getattr(action, "object_path", None))
            else PppActionPurpose.not_ppp
        )

    # Service ports carry their slot. mgmt (VLAN 201) is management work and
    # must keep converging while PPP is blocked; wan (VLAN 203) carries the
    # subscriber service.
    if name in {"OltCreateServicePort", "OltDeleteServicePort"}:
        slot = str(getattr(action, "slot", "") or "").strip().lower()
        if slot == "mgmt":
            return PppActionPurpose.not_ppp
        if slot == "wan":
            return PppActionPurpose.ppp_bearing
        # A stale port with no recoverable slot: unknown purpose, fail closed.
        return PppActionPurpose.indeterminate

    return PppActionPurpose.not_ppp


def is_ppp_bundle_action(action: Any) -> bool:
    """Whether an action must be withheld unless PPP delivery is authorized."""
    return classify_action(action) is not PppActionPurpose.not_ppp


def resolve_exact_assignment_subscription(
    db: Session, ont_id: Any
) -> tuple[UUID | None, PppDeliveryRefusal | None]:
    """The single active subscriber assignment for this ONT.

    Returns ``(subscription_id, None)`` only when exactly one active assignment
    carries a subscription. Zero and many are distinct refusals: "this device
    serves nobody" and "this device serves several and the payload does not say
    which" need different repairs.
    """
    from app.models.network import OntAssignment

    rows = [
        row
        for row in db.execute(
            select(OntAssignment.subscription_id)
            .where(OntAssignment.ont_unit_id == ont_id)
            .where(OntAssignment.active.is_(True))
        )
        .scalars()
        .all()
        if row is not None
    ]
    distinct = {str(row) for row in rows}
    if not distinct:
        return None, PppDeliveryRefusal.no_active_assignment
    if len(distinct) > 1:
        return None, PppDeliveryRefusal.ambiguous_assignment
    return rows[0], None


def authorize_ppp_delivery(
    db: Session,
    ont_id: Any,
    *,
    credential_scope: str | None = None,
) -> PppDeliveryRuling:
    """Rule on whether PPP may be delivered to this ONT for its exact service.

    Fails closed in every unclear case. Absent intent, unverified intent,
    ambiguous assignment and an unresolvable ONT are all refusals, because the
    failure mode being prevented is a device acquiring a PPP dialer nobody
    asked it to have.

    ``credential_scope`` binds the ruling to the specific credential or plan
    being delivered. Pass the same value at the point of use; a ruling issued
    for one credential does not authorise another.
    """
    from app.services.network.ont_wan_service_intent import (
        active_primary_internet_intent,
    )

    if ont_id is None:
        return _refused(PppDeliveryRefusal.unresolvable_ont)

    subscription_id, refusal = resolve_exact_assignment_subscription(db, ont_id)
    if refusal is not None or subscription_id is None:
        return _refused(
            refusal or PppDeliveryRefusal.no_active_assignment, ont_id=ont_id
        )

    instance = active_primary_internet_intent(
        db, ont_id=ont_id, subscription_id=subscription_id
    )
    if instance is None:
        # Includes every pre-owner row: migration 456 lands them all in
        # `unverified`, which this query excludes by design.
        return _refused(
            PppDeliveryRefusal.no_active_service_intent,
            ont_id=ont_id,
            subscription_id=subscription_id,
        )

    connection = _enum_value(getattr(instance, "connection_type", None))
    if connection == "bridged":
        # Bridged wins outright: it places termination on a downstream router,
        # so a co-existing PPPoE declaration is a conflict to adjudicate rather
        # than permission to deliver.
        return _refused(
            PppDeliveryRefusal.bridged_service_intent,
            ont_id=ont_id,
            subscription_id=subscription_id,
        )
    if connection != "pppoe":
        return _refused(
            PppDeliveryRefusal.no_active_service_intent,
            ont_id=ont_id,
            subscription_id=subscription_id,
        )

    return PppDeliveryRuling(
        decision=PppDeliveryDecision.authorized,
        refusal=None,
        ont_id=str(ont_id),
        subscription_id=str(subscription_id),
        instance_id=str(instance.id),
        instance_revision=int(getattr(instance, "revision", 0) or 0),
        credential_scope=credential_scope if credential_scope is not None else UNSCOPED,
    )


def partition_actions(
    actions: Sequence[Any],
    ruling: PppDeliveryRuling | None,
    *,
    ont_id: Any = None,
    subscription_id: Any = None,
    credential_scope: str | None = None,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Split a planned action list into (deliverable, refused).

    A ruling that does not authorise THIS delivery refuses the PPP-bearing and
    indeterminate actions and leaves every unrelated action in place, so ONT
    reconciliation continues to converge management, Wi-Fi and LAN state while
    PPP stays blocked.

    ``ruling=None`` refuses: a plan may not deliver PPP merely because nobody
    checked.
    """
    allowed = ruling is not None and (
        ruling.authorizes(
            ont_id=ont_id if ont_id is not None else ruling.ont_id,
            subscription_id=subscription_id,
            credential_scope=credential_scope,
        )
    )
    if allowed:
        return tuple(actions), ()
    deliverable = tuple(a for a in actions if not is_ppp_bundle_action(a))
    refused = tuple(a for a in actions if is_ppp_bundle_action(a))
    return deliverable, refused
