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

Lock policy. Delivery is the locking path and acquires in one canonical order:
**OntUnit -> intent -> Subscription -> all credentials for the subscription**.
The reconciler already holds the OntUnit row lock when it calls in, so the
reads underneath it use ``for_update``, which also forces a refresh past the
session identity map.

The credential lock deliberately covers the PARENT subscription row and EVERY
credential row for it, unfiltered. Locking only the currently-active
credentials would leave a phantom read: nothing would block an inactive
credential being activated, or a second active one being inserted, and the
schema has no one-active-per-subscription constraint to fall back on. The
active set is decided in memory after the rows are held.

The PRODUCER takes no locks here. It reads intent unlocked and writes the
OntUnit row afterwards, so if it also locked intent the two paths would acquire
OntUnit and the intent row in opposite orders and deadlock. The producer only
stages desired state, and delivery independently reauthorizes before any device
I/O, so the unlocked read costs nothing.

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
    #: No single active AccessCredential for the exact service.
    no_authoritative_credential = "no_authoritative_credential"
    #: More than one active credential; which may dial is unstated.
    ambiguous_authoritative_credential = "ambiguous_authoritative_credential"
    #: The credential exists but its secret cannot be read.
    unreadable_authoritative_credential = "unreadable_authoritative_credential"
    #: No credential-encryption key, so no keyed comparison is possible.
    credential_key_unavailable = "credential_key_unavailable"
    #: Nothing was ever staged, so there is no projection to authorise.
    missing_projection_fingerprint = "missing_projection_fingerprint"
    #: The staged projection is not this service's authoritative credential.
    credential_fingerprint_mismatch = "credential_fingerprint_mismatch"


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


#: Drift fields produced by PPP-bearing repair work. Attribution is by DRIFT,
#: not by action: a non-repairable drift carries no action at all, so an
#: "every action is PPP" verdict would silently exempt unrelated residual drift.
PPP_ATTRIBUTABLE_DRIFT_FIELDS = frozenset(
    {
        "wan_pppoe_username",
        "acs_wan_ppp_instance",
        # `nat_enabled` is emitted alongside the PPP instance in this planner.
        "nat_enabled",
    }
)
#: Deliberately NOT attributable: `wan_ip_mode`. The planner emits it for
#: DHCP and static WAN work too, so exempting it would hide unrelated residual
#: drift behind a PPP refusal.


def is_ppp_attributable_drift(drift: Any) -> bool:
    """Whether this drift exists only because PPP repair was withheld."""
    field = str(getattr(drift, "field", "") or "")
    return field in PPP_ATTRIBUTABLE_DRIFT_FIELDS


@dataclass(frozen=True, slots=True)
class PppDeliveryScope:
    """What is actually being delivered, derived independently of the ruling.

    This exists because the first version of the scope check was circular: the
    caller copied ``subscription_id`` and ``credential_scope`` off the ruling
    and handed them back to be compared against the ruling, and the applier
    checked ``ont_id`` against ``ruling.ont_id``. Every comparison was the
    ruling against itself, so the binding fields were decoration.

    Every field here is recomputed from the ONT being reconciled, the current
    stored intent and the plan actually about to be applied. Equality with the
    ruling is therefore a real check: an intent replaced since the ruling was
    issued bumps ``instance_revision``, and a plan carrying different PPP
    content changes ``plan_fingerprint``.
    """

    ont_id: str
    subscription_id: str
    instance_id: str
    instance_revision: int
    #: Shape of the PPP work in the plan. Non-secret, and deliberately NOT
    #: evidence of credential ownership -- a staged foreign credential
    #: fingerprints identically to a legitimate one.
    plan_fingerprint: str
    #: Keyed fingerprint of the service's authoritative AccessCredential, from
    #: `cpe_dialer_credential_reconcile`. This is what establishes that the
    #: staged payload belongs to this service.
    credential_fingerprint: str


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
    #: Shape of the PPP work authorised. Non-secret; never evidence of
    #: credential ownership.
    plan_fingerprint: str = UNSCOPED
    #: Keyed fingerprint of the service's authoritative credential, matched
    #: against the staged projection before this ruling was granted.
    credential_fingerprint: str = UNSCOPED
    owner: str = OWNER

    @property
    def authorized(self) -> bool:
        return self.decision is PppDeliveryDecision.authorized

    def authorizes(self, scope: PppDeliveryScope | None) -> bool:
        """Whether this ruling authorises THIS exact delivery.

        Exact equality on every binding field, with no optional comparisons.
        An earlier version skipped a field when the caller passed ``None``,
        which meant a caller that supplied nothing was fully authorised -- the
        opposite of a gate. There is deliberately no way to compare "some" of
        the binding.

        A missing scope is a refusal: if the caller cannot say what it is
        delivering, nothing may be delivered.
        """
        if not self.authorized or scope is None:
            return False
        return (
            self.ont_id == scope.ont_id
            and self.subscription_id == scope.subscription_id
            and self.instance_id == scope.instance_id
            and self.instance_revision == scope.instance_revision
            and self.plan_fingerprint == scope.plan_fingerprint
            and self.credential_fingerprint == scope.credential_fingerprint
        )

    def as_log_extra(self) -> dict[str, object]:
        return {
            "ppp_delivery_decision": self.decision.value,
            "ppp_delivery_refusal": self.refusal.value if self.refusal else None,
            "ont_id": self.ont_id,
            "subscription_id": self.subscription_id,
            "ppp_service_instance": self.instance_id,
            "ppp_service_instance_revision": self.instance_revision,
            # Truncated: fingerprints are keyed, but there is no reason to put
            # the full value in every log line.
            "credential_fingerprint": self.credential_fingerprint[:12],
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


#: Fields hashed into the plan fingerprint, per PPP-bearing action class.
#: Password references are deliberately excluded -- the fingerprint is written
#: to logs and audit rows, so it carries only non-secret identity.
_FINGERPRINT_FIELDS = (
    "device_id",
    "object_path",
    "username",
    "vlan",
    "wcd_index",
    "instance_index",
    "ip_index",
    "profile_id",
    "fsp",
    "ont_id",
    "service_port_index",
    "gem_index",
    "slot",
    "enabled",
)


def plan_ppp_fingerprint(actions: Sequence[Any]) -> str:
    """Canonical, non-secret fingerprint of a plan's PPP content.

    Order-independent so an unrelated reordering does not invalidate a ruling,
    but content-sensitive so a changed credential, VLAN or instance index does.
    Deliberately excludes ``*_password_ref`` and any resolved secret.
    """
    import hashlib
    import json

    parts: list[str] = []
    for action in actions:
        if not is_ppp_bundle_action(action):
            continue
        payload = {
            name: str(getattr(action, name))
            for name in _FINGERPRINT_FIELDS
            if hasattr(action, name)
        }
        payload["__action__"] = type(action).__name__
        parts.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    if not parts:
        # A plan with no PPP content still gets a stable, non-empty scope so
        # equality remains meaningful rather than collapsing to "".
        return "no-ppp-content"
    digest = hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()
    return f"ppp-plan:{digest[:32]}"


_CREDENTIAL_REFUSALS = {
    "dialer_credential_missing": PppDeliveryRefusal.no_authoritative_credential,
    "dialer_credential_ambiguous": (
        PppDeliveryRefusal.ambiguous_authoritative_credential
    ),
    "dialer_credential_unreadable": (
        PppDeliveryRefusal.unreadable_authoritative_credential
    ),
    "dialer_credential_key_unavailable": (
        PppDeliveryRefusal.credential_key_unavailable
    ),
}


@dataclass(frozen=True, slots=True)
class _CredentialCheck:
    """Outcome of proving the staged projection is this service's credential."""

    fingerprint: str = ""
    refusal: PppDeliveryRefusal | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None and bool(self.fingerprint)


def _resolve_credential_authority(
    db: Session, ont: Any, subscription_id: UUID
) -> _CredentialCheck:
    """Prove the staged projection carries THIS service's credential.

    The check that stops a staged payload authorising itself. The plan's own
    fingerprint cannot do this: a foreign credential already projected onto the
    ONT hashes exactly like a legitimate one, so fingerprinting the plan and
    comparing it to a ruling derived from the same plan proves only that the
    plan equals itself.

    Authority comes from ``cpe_dialer_credential_reconcile``, which owns the
    credential projection: exactly one active ``AccessCredential`` for the
    exact subscription, keyed-fingerprinted, compared against the fingerprint
    recorded on the ONT's staged projection. Every failure is a refusal.
    """
    from app.services.cpe_dialer_credential_reconcile import (
        authoritative_dialer_fingerprint,
        projected_dialer_fingerprint,
    )

    result = authoritative_dialer_fingerprint(
        db, subscription_id=subscription_id, for_update=True
    )
    if not result.ok or result.authority is None:
        return _CredentialCheck(
            refusal=_CREDENTIAL_REFUSALS.get(
                result.refusal.value if result.refusal else "",
                PppDeliveryRefusal.no_authoritative_credential,
            )
        )
    authority = result.authority

    projected = projected_dialer_fingerprint(ont)
    if projected is None:
        # Nothing staged. There is no projection for this ruling to authorise,
        # and inventing one here would be the producer's job, not this gate's.
        return _CredentialCheck(
            refusal=PppDeliveryRefusal.missing_projection_fingerprint
        )
    if projected != authority.fingerprint:
        # The staged payload is not this service's credential. This is the
        # production failure mode: 1,373 services carry a dialer whose
        # termination is not the ONT.
        return _CredentialCheck(
            refusal=PppDeliveryRefusal.credential_fingerprint_mismatch
        )
    return _CredentialCheck(fingerprint=authority.fingerprint)


def derive_delivery_scope(
    db: Session, ont: Any, actions: Sequence[Any]
) -> PppDeliveryScope | None:
    """Describe the delivery about to happen, from live state and the plan.

    Independent of any ruling. Returns ``None`` when the ONT has no exact
    service, no active intent, or no provable credential authority -- each of
    which makes every comparison fail closed.

    The intent row is re-read under ``for_update`` so the bound ``revision`` is
    the current one: two ORM reads in one session would otherwise both be
    served from the identity map and reproduce a stale revision, leaving the
    TOCTOU window this binding exists to close.
    """
    from app.services.network.ont_wan_service_intent import (
        active_primary_internet_intent,
    )

    ont_id = getattr(ont, "id", ont)
    if ont_id is None:
        return None
    subscription_id, refusal = resolve_exact_assignment_subscription(db, ont_id)
    if refusal is not None or subscription_id is None:
        return None
    instance = active_primary_internet_intent(
        db, ont_id=ont_id, subscription_id=subscription_id, for_update=True
    )
    if instance is None:
        return None
    credential = _resolve_credential_authority(db, ont, subscription_id)
    if not credential.ok:
        return None
    return PppDeliveryScope(
        ont_id=str(ont_id),
        subscription_id=str(subscription_id),
        instance_id=str(instance.id),
        instance_revision=int(getattr(instance, "revision", 0) or 0),
        plan_fingerprint=plan_ppp_fingerprint(actions),
        credential_fingerprint=credential.fingerprint,
    )


def authorize_ppp_termination_intent(
    db: Session, ont: Any, *, for_update: bool = False
) -> PppDeliveryRuling:
    """May this ONT terminate PPP for its exact service at all?

    Intent only: exact assignment, owner-managed active primary Internet
    intent, and a PPPoE connection type. Deliberately does NOT check the
    staged projection, because the PRODUCER asks this question before it
    stages anything -- requiring an existing projection would make the first
    projection impossible and turn the gate into a deadlock.

    Delivery adds the credential and projection checks on top; see
    ``authorize_ppp_delivery``.

    LOCK POLICY. ``for_update`` defaults to FALSE and the producer must leave
    it that way. The producer takes no OntUnit lock before reading intent and
    then writes the OntUnit row afterwards; delivery locks the OntUnit row
    first and reads intent second. If both locked intent, the two paths would
    acquire OntUnit and the intent row in opposite orders and deadlock. The
    producer only stages desired state, and delivery independently
    reauthorizes before any device I/O, so an unlocked read here costs nothing.

    Canonical order, delivery side: OntUnit -> intent -> credential.
    """
    from app.services.network.ont_wan_service_intent import (
        active_primary_internet_intent,
    )

    ont_id = getattr(ont, "id", ont)
    if ont_id is None:
        return _refused(PppDeliveryRefusal.unresolvable_ont)

    subscription_id, refusal = resolve_exact_assignment_subscription(db, ont_id)
    if refusal is not None or subscription_id is None:
        return _refused(
            refusal or PppDeliveryRefusal.no_active_assignment, ont_id=ont_id
        )

    instance = active_primary_internet_intent(
        db, ont_id=ont_id, subscription_id=subscription_id, for_update=for_update
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
    )


def authorize_ppp_delivery(
    db: Session,
    ont: Any,
    *,
    actions: Sequence[Any] = (),
) -> PppDeliveryRuling:
    """Rule on whether PPP may be DELIVERED to this ONT for its exact service.

    Intent plus proof that the staged payload is this service's credential.

    The caller does NOT supply a credential scope. An earlier version accepted
    the plan's own fingerprint as ``credential_scope`` and then re-derived the
    same value from the same plan, so an already-staged foreign credential
    authorised itself and no ``AccessCredential`` was ever consulted.
    """
    # Delivery is the locking path: the caller already holds the OntUnit row
    # lock, so intent then credential completes the canonical
    # OntUnit -> intent -> credential order.
    intent = authorize_ppp_termination_intent(db, ont, for_update=True)
    if not intent.authorized:
        return intent

    # Re-resolve the raw identifier: `intent.subscription_id` is stringified
    # for logging and would not match a UUID column in the credential query.
    ont_id = getattr(ont, "id", ont)
    raw_subscription_id, _ = resolve_exact_assignment_subscription(db, ont_id)
    if raw_subscription_id is None:  # pragma: no cover - intent gate proved one
        return _refused(PppDeliveryRefusal.no_active_assignment, ont_id=intent.ont_id)
    credential = _resolve_credential_authority(db, ont, raw_subscription_id)
    if not credential.ok:
        return _refused(
            credential.refusal or PppDeliveryRefusal.no_authoritative_credential,
            ont_id=intent.ont_id,
            subscription_id=intent.subscription_id,
        )

    return PppDeliveryRuling(
        decision=PppDeliveryDecision.authorized,
        refusal=None,
        ont_id=intent.ont_id,
        subscription_id=intent.subscription_id,
        instance_id=intent.instance_id,
        instance_revision=intent.instance_revision,
        plan_fingerprint=plan_ppp_fingerprint(actions),
        credential_fingerprint=credential.fingerprint,
    )


def partition_actions(
    actions: Sequence[Any],
    ruling: PppDeliveryRuling | None,
    scope: PppDeliveryScope | None = None,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Split a planned action list into (deliverable, refused).

    A ruling that does not authorise THIS delivery refuses the PPP-bearing and
    indeterminate actions and leaves every unrelated action in place, so ONT
    reconciliation continues to converge management, Wi-Fi and LAN state while
    PPP stays blocked.

    ``ruling=None`` or ``scope=None`` refuses: a plan may not deliver PPP
    merely because nobody checked, nor because the caller could not say what it
    was delivering.
    """
    allowed = ruling is not None and ruling.authorizes(scope)
    if allowed:
        return tuple(actions), ()
    deliverable = tuple(a for a in actions if not is_ppp_bundle_action(a))
    refused = tuple(a for a in actions if is_ppp_bundle_action(a))
    return deliverable, refused
