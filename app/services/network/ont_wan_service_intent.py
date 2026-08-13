"""Owner of declared ONT WAN service intent.

Owner: ``network.ont_wan_service_intent``.

``OntWanServiceInstance`` modelled service intent but nothing wrote it. The
repository contained no constructor outside tests and production held 8 rows
against 1,523 ONTs, so the table recorded that something once wrote a value,
not that anyone declared intent. A delivery gate built on it would have been a
blanket denial with a handful of unexplained exceptions -- the same shape as
the 12 surviving ``OntAssignment.pppoe_username`` values that migration 084 had
already cleared.

This is the writer that makes those rows mean something.

What authority looks like here:

* **Exact service grain.** ``ont_id`` AND ``subscription_id``. An ONT-grain row
  claims "this device may terminate PPP", which is not "this SERVICE terminates
  here" -- and a ruling built on the weaker claim can hand one service's
  credential to another.
* **``lifecycle_state`` is the only authority.** ``planned``/``unverified`` do
  not authorise. ``is_active`` is derived and kept in step here; it is not a
  second opinion.
* **``is_primary`` selects, ``priority`` orders.** Ordering is presentation;
  authority is an ownership decision, so a delivery ruling never reads
  ``priority``.
* **One active primary Internet instance per subscription AND per ONT.**
  Non-Internet service types stay multi-WAN capable. Enforced here because the
  partial unique indexes land only after inventory, backfill and verification.
* **Retirement preserves history.** Rows are retired, never deleted, and a
  replacement records what it replaced.

Every transition carries actor, reason, evidence reference and a revision bump,
so a delivery ruling can bind the exact revision it was granted against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.network import (
    OntWanServiceInstance,
    OntWanServiceLifecycle,
)
from app.services.audit_adapter import stage_audit_event
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)

logger = logging.getLogger(__name__)

OWNER = "network.ont_wan_service_intent"
_CONCERN = "declared ONT WAN service intent lifecycle"

DECLARE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=_CONCERN, name="declare_wan_service_intent"
)
ACTIVATE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=_CONCERN, name="activate_wan_service_intent"
)
REPLACE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=_CONCERN, name="replace_wan_service_intent"
)
RETIRE_COMMAND = OwnerCommandDefinition(
    owner=OWNER, concern=_CONCERN, name="retire_wan_service_intent"
)

_AUDIT_ACTION = "ont_wan_service_intent"

#: The service type whose primary instance is singular per service and per ONT.
#: Other types stay multi-WAN capable.
PRIMARY_CONSTRAINED_SERVICE_TYPE = "internet"


class WanServiceIntentError(DomainError):
    """Fail-closed refusal from the WAN service intent owner."""

    def __init__(
        self, message: str, *, code: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(code=code, message=message, details=details)


class IntentRefusal(StrEnum):
    """One stable code per distinct refusal."""

    missing_subscription = "wan_intent_missing_subscription"
    missing_ont = "wan_intent_missing_ont"
    missing_evidence = "wan_intent_missing_evidence"
    instance_not_found = "wan_intent_instance_not_found"
    already_retired = "wan_intent_already_retired"
    not_activatable = "wan_intent_not_activatable"
    duplicate_primary_for_subscription = "wan_intent_duplicate_primary_subscription"
    duplicate_primary_for_ont = "wan_intent_duplicate_primary_ont"
    revision_conflict = "wan_intent_revision_conflict"


@dataclass(frozen=True, slots=True)
class WanServiceIntentSpec:
    """Typed declaration of one WAN service intent."""

    ont_id: UUID
    subscription_id: UUID
    service_type: str
    connection_type: str
    is_primary: bool
    name: str | None = None
    priority: int = 1
    s_vlan: int | None = None
    c_vlan: int | None = None


@dataclass(frozen=True, slots=True)
class WanServiceIntentOutcome:
    """Result of one lifecycle transition."""

    instance_id: UUID
    lifecycle_state: OntWanServiceLifecycle
    revision: int
    is_primary: bool
    replaced_instance_id: UUID | None = None
    replayed: bool = False


def _enum_value(value: Any) -> str:
    """Normalise an enum-or-string column to its lowercase value.

    ``str(SomeEnum.internet)`` yields ``"OntServiceType.internet"``, so a naive
    comparison against ``"internet"`` silently treats every Internet instance
    as unconstrained -- exactly the invariant this owner exists to hold.
    """
    return str(getattr(value, "value", value) or "").strip().lower()


def _require(value: Any, *, code: IntentRefusal, message: str) -> None:
    if value in (None, "") or (isinstance(value, str) and not value.strip()):
        raise WanServiceIntentError(message, code=code.value)


def _validate_context(context: CommandContext) -> None:
    _require(
        context.actor,
        code=IntentRefusal.missing_evidence,
        message="A WAN service intent transition requires an actor.",
    )
    _require(
        context.reason,
        code=IntentRefusal.missing_evidence,
        message="A WAN service intent transition requires a recorded reason.",
    )


def _active_primary_conflicts(
    db: Session,
    *,
    ont_id: UUID,
    subscription_id: UUID,
    service_type: str,
    exclude_id: UUID | None,
) -> tuple[list[OntWanServiceInstance], list[OntWanServiceInstance]]:
    """Active primary Internet instances that would collide.

    Returned separately by scope so the refusal can say WHICH invariant broke:
    a service already terminating elsewhere is a different operational fact
    from an ONT already carrying another service's primary.
    """
    if _enum_value(service_type) != PRIMARY_CONSTRAINED_SERVICE_TYPE:
        return [], []

    rows = (
        db.execute(
            select(OntWanServiceInstance)
            .where(OntWanServiceInstance.is_primary.is_(True))
            .where(
                OntWanServiceInstance.lifecycle_state == OntWanServiceLifecycle.active
            )
        )
        .scalars()
        .all()
    )
    by_subscription = [
        row
        for row in rows
        if row.subscription_id == subscription_id and row.id != exclude_id
    ]
    by_ont = [
        row
        for row in rows
        if row.ont_id == ont_id
        and row.subscription_id != subscription_id
        and row.id != exclude_id
    ]
    return by_subscription, by_ont


def _stage(
    db: Session,
    *,
    context: CommandContext,
    instance: OntWanServiceInstance,
    action: str,
) -> None:
    stage_audit_event(
        db,
        action=f"{_AUDIT_ACTION}.{action}",
        entity_type="ont_wan_service_instance",
        entity_id=str(instance.id),
        actor_type=AuditActorType.service,
        actor_id=context.actor,
        metadata={
            "ont_id": str(instance.ont_id),
            "subscription_id": str(instance.subscription_id or ""),
            "service_type": str(instance.service_type),
            "connection_type": str(instance.connection_type),
            "is_primary": bool(instance.is_primary),
            "lifecycle_state": str(
                getattr(instance.lifecycle_state, "value", instance.lifecycle_state)
            ),
            "revision": int(instance.revision),
            "reason": context.reason,
            "evidence_ref": instance.evidence_ref,
        },
    )


def _declare(
    db: Session, spec: WanServiceIntentSpec, context: CommandContext
) -> WanServiceIntentOutcome:
    _validate_context(context)
    _require(
        spec.subscription_id,
        code=IntentRefusal.missing_subscription,
        message="WAN service intent is declared at exact service grain.",
    )
    _require(
        spec.ont_id,
        code=IntentRefusal.missing_ont,
        message="WAN service intent requires the ONT it terminates on.",
    )

    instance = OntWanServiceInstance(
        ont_id=spec.ont_id,
        subscription_id=spec.subscription_id,
        name=spec.name or spec.service_type,
        service_type=spec.service_type,
        connection_type=spec.connection_type,
        priority=spec.priority,
        is_primary=spec.is_primary,
        s_vlan=spec.s_vlan,
        c_vlan=spec.c_vlan,
        # Declared, not authorised. Activation is a separate, evidenced step so
        # that recording an intention can never by itself permit a device write.
        lifecycle_state=OntWanServiceLifecycle.planned,
        is_active=False,
        revision=1,
        declared_by=context.actor,
        declared_reason=context.reason,
        evidence_ref=context.idempotency_key,
    )
    db.add(instance)
    db.flush()
    _stage(db, context=context, instance=instance, action="declared")
    return WanServiceIntentOutcome(
        instance_id=instance.id,
        lifecycle_state=instance.lifecycle_state,
        revision=instance.revision,
        is_primary=instance.is_primary,
    )


def _load(db: Session, instance_id: UUID) -> OntWanServiceInstance:
    instance = db.get(OntWanServiceInstance, instance_id)
    if instance is None:
        raise WanServiceIntentError(
            f"WAN service intent {instance_id} was not found.",
            code=IntentRefusal.instance_not_found.value,
        )
    return instance


def _activate(
    db: Session,
    instance_id: UUID,
    context: CommandContext,
    expected_revision: int | None,
) -> WanServiceIntentOutcome:
    _validate_context(context)
    instance = _load(db, instance_id)

    if instance.lifecycle_state is OntWanServiceLifecycle.retired:
        raise WanServiceIntentError(
            "A retired WAN service intent cannot be activated; declare a new one.",
            code=IntentRefusal.already_retired.value,
        )
    if expected_revision is not None and expected_revision != instance.revision:
        raise WanServiceIntentError(
            f"WAN service intent {instance_id} moved to revision "
            f"{instance.revision}; re-read before activating.",
            code=IntentRefusal.revision_conflict.value,
        )
    if instance.subscription_id is None:
        # A pre-owner row. It cannot be adopted without being told which service
        # it belongs to, and guessing is the failure this owner exists to stop.
        raise WanServiceIntentError(
            "WAN service intent has no subscription; adjudicate it before activating.",
            code=IntentRefusal.missing_subscription.value,
        )

    if instance.is_primary:
        by_subscription, by_ont = _active_primary_conflicts(
            db,
            ont_id=instance.ont_id,
            subscription_id=instance.subscription_id,
            service_type=_enum_value(instance.service_type),
            exclude_id=instance.id,
        )
        if by_subscription:
            raise WanServiceIntentError(
                "This service already has an active primary Internet "
                "termination; replace it rather than adding a second.",
                code=IntentRefusal.duplicate_primary_for_subscription.value,
                details={"existing": [str(row.id) for row in by_subscription]},
            )
        if by_ont:
            raise WanServiceIntentError(
                "This ONT already carries another service's active primary "
                "Internet termination.",
                code=IntentRefusal.duplicate_primary_for_ont.value,
                details={"existing": [str(row.id) for row in by_ont]},
            )

    instance.lifecycle_state = OntWanServiceLifecycle.active
    instance.is_active = True  # derived, kept in step by the owner only
    instance.activated_at = datetime.now(UTC)
    instance.revision = int(instance.revision) + 1
    instance.declared_by = context.actor
    instance.declared_reason = context.reason
    if context.idempotency_key:
        instance.evidence_ref = context.idempotency_key
    db.flush()
    _stage(db, context=context, instance=instance, action="activated")
    return WanServiceIntentOutcome(
        instance_id=instance.id,
        lifecycle_state=instance.lifecycle_state,
        revision=instance.revision,
        is_primary=instance.is_primary,
    )


def _retire(
    db: Session, instance_id: UUID, context: CommandContext, *, replaced_by: UUID | None
) -> WanServiceIntentOutcome:
    _validate_context(context)
    instance = _load(db, instance_id)
    if instance.lifecycle_state is OntWanServiceLifecycle.retired:
        return WanServiceIntentOutcome(
            instance_id=instance.id,
            lifecycle_state=instance.lifecycle_state,
            revision=instance.revision,
            is_primary=instance.is_primary,
        )

    instance.lifecycle_state = OntWanServiceLifecycle.retired
    instance.is_active = False
    instance.retired_at = datetime.now(UTC)
    instance.retired_reason = context.reason
    instance.replaced_by_id = replaced_by
    instance.revision = int(instance.revision) + 1
    db.flush()
    _stage(db, context=context, instance=instance, action="retired")
    return WanServiceIntentOutcome(
        instance_id=instance.id,
        lifecycle_state=instance.lifecycle_state,
        revision=instance.revision,
        is_primary=instance.is_primary,
    )


def _replace(
    db: Session,
    outgoing_id: UUID,
    spec: WanServiceIntentSpec,
    context: CommandContext,
) -> WanServiceIntentOutcome:
    """Atomic hand-over: retire the outgoing intent, then activate the new one.

    Retire-then-activate, not the reverse: the primary invariant permits one
    active primary per service, so activating first would collide with the row
    being replaced. Both happen inside one owned transaction, so a failure
    leaves neither half applied.
    """
    _validate_context(context)
    outgoing = _load(db, outgoing_id)

    declared = _declare(db, spec, context)
    _retire(db, outgoing.id, context, replaced_by=declared.instance_id)
    activated = _activate(db, declared.instance_id, context, None)
    return WanServiceIntentOutcome(
        instance_id=activated.instance_id,
        lifecycle_state=activated.lifecycle_state,
        revision=activated.revision,
        is_primary=activated.is_primary,
        replaced_instance_id=outgoing.id,
    )


# ---------------------------------------------------------------------------
# Public commands
# ---------------------------------------------------------------------------


def declare_wan_service_intent(
    db: Session, *, spec: WanServiceIntentSpec, context: CommandContext
) -> WanServiceIntentOutcome:
    """Record an intended WAN service. Declaring never authorises delivery."""
    return execute_owner_command(
        db,
        definition=DECLARE_COMMAND,
        context=context,
        operation=lambda: _declare(db, spec, context),
    )


def activate_wan_service_intent(
    db: Session,
    *,
    instance_id: UUID,
    context: CommandContext,
    expected_revision: int | None = None,
) -> WanServiceIntentOutcome:
    """Make a declared intent authoritative, enforcing the primary invariants."""
    return execute_owner_command(
        db,
        definition=ACTIVATE_COMMAND,
        context=context,
        operation=lambda: _activate(db, instance_id, context, expected_revision),
    )


def replace_wan_service_intent(
    db: Session,
    *,
    outgoing_instance_id: UUID,
    spec: WanServiceIntentSpec,
    context: CommandContext,
) -> WanServiceIntentOutcome:
    """Hand a service's termination from one instance to another, atomically."""
    return execute_owner_command(
        db,
        definition=REPLACE_COMMAND,
        context=context,
        operation=lambda: _replace(db, outgoing_instance_id, spec, context),
    )


def retire_wan_service_intent(
    db: Session, *, instance_id: UUID, context: CommandContext
) -> WanServiceIntentOutcome:
    """Retire an intent, preserving it as history.

    The single path for assignment release, service movement, cancellation and
    return-to-inventory. Those flows must stop deleting service-instance rows:
    deletion destroys the record of what a service was once declared to be, and
    that record is the evidence a later adjudication depends on.
    """
    return execute_owner_command(
        db,
        definition=RETIRE_COMMAND,
        context=context,
        operation=lambda: _retire(db, instance_id, context, replaced_by=None),
    )


def retire_ont_intents_in_transaction(
    db: Session, *, ont_id: UUID, context: CommandContext
) -> tuple[WanServiceIntentOutcome, ...]:
    """Retire every live intent on an ONT from inside a caller's transaction.

    ONT lifecycle flows -- return-to-inventory, decommission, assignment release
    -- reset many facets of a device as one unit of work, and they are already
    inside a transaction when they reach this point. ``execute_owner_command``
    requires a transaction-free session at entry, so those callers cannot use
    the command form; without this they would keep deleting the rows outright.

    This is still the owner deciding: the same ``_retire`` transition, the same
    provenance validation, the same staged event. Only the transaction boundary
    moves to the caller, whose atomicity requirement is the stronger one -- a
    device must not end up half-returned with its intents still active.

    Provenance note: these entry points do not yet thread an operator identity,
    so ``context.actor`` names the owning system rather than a person. That is
    accurate rather than flattering; attributing a machine reset to a human
    would be the worse record.
    """
    _validate_context(context)
    live = (
        db.execute(
            select(OntWanServiceInstance)
            .where(OntWanServiceInstance.ont_id == ont_id)
            .where(
                OntWanServiceInstance.lifecycle_state != OntWanServiceLifecycle.retired
            )
            .order_by(OntWanServiceInstance.priority, OntWanServiceInstance.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    return tuple(
        _retire(db, instance.id, context, replaced_by=None) for instance in live
    )


def ensure_active_wan_service_intent_in_transaction(
    db: Session,
    *,
    spec: WanServiceIntentSpec,
    context: CommandContext,
) -> WanServiceIntentOutcome:
    """Flush-only participant for a coordinator-owned configuration command.

    Preserves an exact matching declaration. A material WAN change creates a
    replacement so the previous declaration remains historical evidence.
    """

    _validate_context(context)
    current = active_primary_internet_intent(
        db,
        ont_id=spec.ont_id,
        subscription_id=spec.subscription_id,
        for_update=True,
    )
    if current is not None:
        matches = (
            _enum_value(current.service_type) == _enum_value(spec.service_type)
            and _enum_value(current.connection_type)
            == _enum_value(spec.connection_type)
            and bool(current.is_primary) is bool(spec.is_primary)
            and int(current.priority or 0) == int(spec.priority)
            and current.s_vlan == spec.s_vlan
            and current.c_vlan == spec.c_vlan
        )
        if matches:
            return WanServiceIntentOutcome(
                instance_id=current.id,
                lifecycle_state=current.lifecycle_state,
                revision=current.revision,
                is_primary=current.is_primary,
                replayed=True,
            )
        return _replace(db, current.id, spec, context)
    declared = _declare(db, spec, context)
    return _activate(db, declared.instance_id, context, declared.revision)


def active_primary_internet_intent(
    db: Session,
    *,
    ont_id: UUID,
    subscription_id: UUID,
    for_update: bool = False,
) -> OntWanServiceInstance | None:
    """The one active primary Internet intent binding this ONT to this service.

    The read a delivery ruling is built on. Both identifiers are required: an
    ONT-grain answer would authorise a credential belonging to whichever
    service happens to share the device.
    """
    statement = (
        select(OntWanServiceInstance)
        .where(OntWanServiceInstance.ont_id == ont_id)
        .where(OntWanServiceInstance.subscription_id == subscription_id)
        .where(OntWanServiceInstance.is_primary.is_(True))
        .where(OntWanServiceInstance.lifecycle_state == OntWanServiceLifecycle.active)
    )
    if for_update:
        # A delivery ruling binds `revision`, so the row must not change
        # between the ruling and the write. Two ORM reads in one session can
        # both be served from the identity map, which would return the stale
        # revision and leave the TOCTOU window open -- `populate_existing`
        # forces the refresh and `with_for_update` holds the row.
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    rows = [
        row
        for row in db.execute(statement).scalars().all()
        # Internet-scoped: the singular-primary invariant is Internet-only, so
        # a primary IPTV or VoIP instance must not answer this question.
        if _enum_value(row.service_type) == PRIMARY_CONSTRAINED_SERVICE_TYPE
    ]
    # More than one is unadjudicated ambiguity, and the caller must fail closed
    # rather than receive an arbitrary pick.
    return rows[0] if len(rows) == 1 else None
