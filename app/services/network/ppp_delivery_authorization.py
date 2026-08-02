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

Intent source. ``OntWanServiceInstance`` already models service intent and
already expresses ``connection_type=pppoe``, so it is read rather than
introducing another parallel field. Two fields that look like intent are NOT
used:

* ``OntAssignment.wan_mode`` / ``ip_mode`` — migration 084 copied these into
  desired config and then explicitly set them ``NULL``, so surviving values are
  unexplained residue.
* ``OntAssignment.pppoe_username`` — cleared by the same migration. The 12
  production survivors cannot be distinguished from residue, so they must not
  authorize a device write.

Scope. Only the PPP bundle is gated. Unrelated ONT reconciliation — management
server, Wi-Fi, LAN, DHCP-server, IPv6, remote access, line/service profile,
descriptions, authorization, reset — is untouched, because the containment is
aimed at competing PPPoE dialers and not at ONT management generally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

OWNER = "network.ppp_delivery_authorization"


class PppDeliveryRefusal(StrEnum):
    """Stable reasons a PPP delivery is refused. One per distinct cause.

    Category-level codes hide which precondition failed, so an operator cannot
    tell "nobody ever declared PPP for this service" from "two instances
    disagree" without reading prose.
    """

    #: No active service instance declares PPPoE for this ONT.
    no_pppoe_service_intent = "no_pppoe_service_intent"
    #: More than one active PPPoE instance; which one is authoritative is
    #: unstated, and a device write may not resolve it by picking.
    ambiguous_pppoe_service_intent = "ambiguous_pppoe_service_intent"
    #: An active instance declares bridged termination, which places PPP on a
    #: downstream router regardless of anything else staged.
    bridged_service_intent = "bridged_service_intent"
    #: The caller could not supply an ONT identity, so nothing can be checked.
    unresolvable_ont = "unresolvable_ont"


class PppDeliveryDecision(StrEnum):
    authorized = "authorized"
    refused = "refused"


@dataclass(frozen=True, slots=True)
class PppDeliveryRuling:
    """Typed, provenanced delivery ruling.

    Emitted whether or not anything is blocked: a plan that legitimately
    carries no PPP actions still records why delivery was or was not
    authorized, so "nothing was sent" is distinguishable from "the gate never
    ran".
    """

    decision: PppDeliveryDecision
    refusal: PppDeliveryRefusal | None
    ont_id: str
    instance_ids: tuple[str, ...]
    owner: str = OWNER

    @property
    def authorized(self) -> bool:
        return self.decision is PppDeliveryDecision.authorized

    def as_log_extra(self) -> dict[str, object]:
        return {
            "ppp_delivery_decision": self.decision.value,
            "ppp_delivery_refusal": self.refusal.value if self.refusal else None,
            "ont_id": self.ont_id,
            "ppp_service_instances": len(self.instance_ids),
            "owner": self.owner,
        }


#: Action classes that deliver, mutate or remove PPP termination on a device.
#:
#: Deliberately the whole bundle, not just the credential write. Creating the
#: WANPPPConnection object, provisioning it over OMCI, opening the OLT service
#: port, flipping NAT, and deleting a stale instance are each capable of
#: establishing or disturbing a PPP termination on their own; gating only
#: ``AcsSetPppoe`` would leave every other route open.
PPP_BUNDLE_ACTION_NAMES = frozenset(
    {
        # ACS: credential write, object lifecycle, NAT on the PPP instance
        "AcsSetPppoe",
        "AcsAddObject",
        "AcsDeleteObject",
        "AcsSetNatEnabled",
        # OMCI provisioning of the PPP/WAN service
        "OltOmciPppoe",
        "OltOmciWanConfig",
        "OltOmciInternetConfig",
        # OLT service-port work carrying the PPP service
        "OltCreateServicePort",
        "OltDeleteServicePort",
    }
)


def is_ppp_bundle_action(action: Any) -> bool:
    """Whether an action belongs to the gated PPP delivery bundle."""
    return type(action).__name__ in PPP_BUNDLE_ACTION_NAMES


def authorize_ppp_delivery(db: Session, ont_id: Any) -> PppDeliveryRuling:
    """Rule on whether PPP may be delivered to this ONT.

    Fails closed in every unclear case. Absent intent, ambiguous intent and an
    unresolvable ONT are all refusals, because the failure mode being prevented
    is a device acquiring a PPP dialer nobody asked it to have.
    """
    from app.models.network import OntWanServiceInstance

    if ont_id is None:
        return PppDeliveryRuling(
            decision=PppDeliveryDecision.refused,
            refusal=PppDeliveryRefusal.unresolvable_ont,
            ont_id="",
            instance_ids=(),
        )

    rows = list(
        db.execute(
            select(
                OntWanServiceInstance.id,
                OntWanServiceInstance.connection_type,
            )
            .where(OntWanServiceInstance.ont_id == ont_id)
            .where(OntWanServiceInstance.is_active.is_(True))
        ).all()
    )

    def _type_name(value: Any) -> str:
        return str(getattr(value, "value", value) or "").strip().lower()

    bridged = [row for row in rows if _type_name(row[1]) == "bridged"]
    pppoe = [row for row in rows if _type_name(row[1]) == "pppoe"]

    # A bridged declaration wins outright: it places termination downstream
    # whatever else is also declared, so a co-existing PPPoE row is a conflict
    # to adjudicate rather than a permission to deliver.
    if bridged:
        return PppDeliveryRuling(
            decision=PppDeliveryDecision.refused,
            refusal=PppDeliveryRefusal.bridged_service_intent,
            ont_id=str(ont_id),
            instance_ids=tuple(str(row[0]) for row in bridged),
        )
    if not pppoe:
        return PppDeliveryRuling(
            decision=PppDeliveryDecision.refused,
            refusal=PppDeliveryRefusal.no_pppoe_service_intent,
            ont_id=str(ont_id),
            instance_ids=(),
        )
    if len(pppoe) > 1:
        return PppDeliveryRuling(
            decision=PppDeliveryDecision.refused,
            refusal=PppDeliveryRefusal.ambiguous_pppoe_service_intent,
            ont_id=str(ont_id),
            instance_ids=tuple(str(row[0]) for row in pppoe),
        )

    return PppDeliveryRuling(
        decision=PppDeliveryDecision.authorized,
        refusal=None,
        ont_id=str(ont_id),
        instance_ids=(str(pppoe[0][0]),),
    )


def partition_actions(
    actions: Sequence[Any], ruling: PppDeliveryRuling
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Split a planned action list into (deliverable, refused).

    An authorized ruling refuses nothing. A refusal drops only the PPP bundle
    and leaves every unrelated action in place, so ONT reconciliation continues
    to converge management, Wi-Fi and LAN state while PPP stays blocked.
    """
    if ruling.authorized:
        return tuple(actions), ()
    deliverable = tuple(a for a in actions if not is_ppp_bundle_action(a))
    refused = tuple(a for a in actions if is_ppp_bundle_action(a))
    return deliverable, refused
