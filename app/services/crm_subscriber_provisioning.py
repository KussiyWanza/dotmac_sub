"""Explicit CRM command admission for canonical Subscriber creation.

The verified CRM customer webhook remains an observation-only boundary. This
owner admits a separately authenticated, idempotent command and delegates the
actual account initialization to ``customer.accounts``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.idempotency import IdempotencyKey
from app.models.subscriber import Subscriber, SubscriberStatus
from app.schemas.crm_provisioning import CRMSubscriberProvisionRequest
from app.schemas.subscriber import SubscriberCreate
from app.services.audit_adapter import stage_audit_event
from app.services.crm_customers import (
    CRMCustomerObservation,
    CRMCustomerObservationStatus,
    observe_customer,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.subscriber import SubscriberAccountPreparationError
from app.services.subscriber import subscribers as subscriber_service

CRM_PROVISIONING_SCOPE = "integration:crm:subscriber:create"
_IDEMPOTENCY_SCOPE = "crm_subscriber_provision"
_COMMAND = OwnerCommandDefinition(
    owner="customer.crm_subscriber_provisioning",
    concern="authenticated CRM Subscriber provisioning coordination",
    name="provision_crm_subscriber",
)

ProvisioningOutcome = Literal["created", "reused"]


class CRMSubscriberProvisioningError(DomainError):
    """Stable failure returned by the CRM provisioning owner."""


def _error(
    code: str, message: str, **details: object
) -> CRMSubscriberProvisioningError:
    return CRMSubscriberProvisioningError(
        code=f"customer.crm_subscriber_provisioning.{code}",
        message=message,
        details=details,
    )


@dataclass(frozen=True)
class ProvisionCRMSubscriberCommand:
    context: CommandContext
    payload: CRMSubscriberProvisionRequest


@dataclass(frozen=True)
class CRMSubscriberProvisioningResult:
    subscriber_id: UUID
    subscriber_number: str | None
    account_number: str | None
    outcome: ProvisioningOutcome
    replayed: bool
    command_id: UUID
    correlation_id: UUID


def _normalized_optional(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _validate_context(context: CommandContext) -> str:
    if context.scope != CRM_PROVISIONING_SCOPE:
        raise _error("invalid_command", "CRM provisioning scope is invalid.")
    key = str(context.idempotency_key or "").strip()
    if not key:
        raise _error("missing_idempotency_key", "Idempotency-Key is required.")
    if len(key) > 120:
        raise _error(
            "invalid_command",
            "Idempotency-Key must be at most 120 characters.",
        )
    return key


def _fingerprint(payload: CRMSubscriberProvisionRequest) -> str:
    material = payload.model_dump(mode="json", exclude_none=False)
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _serialize_key(db: Session, key: str) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


def _reservation(db: Session, key: str) -> IdempotencyKey | None:
    return db.scalars(
        select(IdempotencyKey)
        .where(
            IdempotencyKey.scope == _IDEMPOTENCY_SCOPE,
            IdempotencyKey.key == key,
        )
        .with_for_update()
    ).one_or_none()


def _lock_subscriber(db: Session, subscriber_id: UUID) -> Subscriber:
    subscriber = db.scalars(
        select(Subscriber).where(Subscriber.id == subscriber_id).with_for_update()
    ).one_or_none()
    if subscriber is None:
        raise _error(
            "idempotency_conflict",
            "The prior provisioning result is no longer available.",
        )
    return subscriber


def _result(
    subscriber: Subscriber,
    *,
    outcome: ProvisioningOutcome,
    replayed: bool,
    context: CommandContext,
) -> CRMSubscriberProvisioningResult:
    return CRMSubscriberProvisioningResult(
        subscriber_id=subscriber.id,
        subscriber_number=subscriber.subscriber_number,
        account_number=subscriber.account_number,
        outcome=outcome,
        replayed=replayed,
        command_id=context.command_id,
        correlation_id=context.correlation_id,
    )


def _replay(
    db: Session,
    reservation: IdempotencyKey,
    *,
    fingerprint: str,
    context: CommandContext,
) -> CRMSubscriberProvisioningResult:
    if reservation.ref_id != fingerprint or reservation.account_id is None:
        raise _error(
            "idempotency_conflict",
            "Idempotency-Key was used with a different provisioning request.",
        )
    subscriber = _lock_subscriber(db, reservation.account_id)
    return _result(subscriber, outcome="reused", replayed=True, context=context)


def _subscriber_payload(
    payload: CRMSubscriberProvisionRequest,
) -> SubscriberCreate:
    metadata = {
        "source": "dotmac_crm",
        "crm_person_id": payload.crm_person_id.strip(),
        "crm_project_id": _normalized_optional(payload.crm_project_id),
        "crm_quote_id": _normalized_optional(payload.crm_quote_id),
        "crm_sales_order_id": _normalized_optional(payload.crm_sales_order_id),
    }
    return SubscriberCreate(
        first_name=payload.first_name,
        last_name=payload.last_name,
        display_name=payload.display_name,
        email=payload.email,
        phone=payload.phone,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        nin=payload.nin,
        address_line1=payload.address_line1,
        address_line2=payload.address_line2,
        city=payload.city,
        region=payload.region,
        postal_code=payload.postal_code,
        country_code=payload.country_code,
        status=SubscriberStatus.new,
        metadata_=metadata,
    )


def _stage_audit(
    db: Session,
    subscriber: Subscriber,
    *,
    outcome: ProvisioningOutcome,
    command: ProvisionCRMSubscriberCommand,
) -> None:
    stage_audit_event(
        db,
        action="customer.crm_subscriber_provisioned",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        actor_type=AuditActorType.system,
        actor_id=command.context.actor,
        metadata={
            "owner": "customer.crm_subscriber_provisioning",
            "outcome": outcome,
            "crm_person_id": command.payload.crm_person_id,
            "crm_sales_order_id": _normalized_optional(
                command.payload.crm_sales_order_id
            ),
            "command_id": str(command.context.command_id),
            "correlation_id": str(command.context.correlation_id),
        },
    )


def _operate(
    db: Session, command: ProvisionCRMSubscriberCommand
) -> CRMSubscriberProvisioningResult:
    key = _validate_context(command.context)
    fingerprint = _fingerprint(command.payload)
    _serialize_key(db, f"{_IDEMPOTENCY_SCOPE}:{key}")
    existing_reservation = _reservation(db, key)
    if existing_reservation is not None:
        return _replay(
            db,
            existing_reservation,
            fingerprint=fingerprint,
            context=command.context,
        )

    observation = CRMCustomerObservation(
        crm_person_id=command.payload.crm_person_id.strip(),
        crm_quote_id=_normalized_optional(command.payload.crm_quote_id),
        crm_sales_order_id=_normalized_optional(command.payload.crm_sales_order_id),
    )
    observed = observe_customer(db, observation)
    if observed.status is CRMCustomerObservationStatus.AMBIGUOUS:
        raise _error(
            "ambiguous_identity",
            "CRM identity matches more than one Subscriber.",
        )

    outcome: ProvisioningOutcome
    if observed.status is CRMCustomerObservationStatus.MATCHED:
        if observed.subscriber_id is None:
            raise _error(
                "identity_conflict",
                "Matched CRM identity has no Subscriber identifier.",
            )
        subscriber = _lock_subscriber(db, UUID(observed.subscriber_id))
        outcome = "reused"
    else:
        subscriber = subscriber_service.prepare_new_account(
            db, _subscriber_payload(command.payload)
        )
        subscriber_service.stage_prepared_account_created_event(
            db, subscriber, actor=command.context.actor
        )
        outcome = "created"

    db.add(
        IdempotencyKey(
            scope=_IDEMPOTENCY_SCOPE,
            key=key,
            account_id=subscriber.id,
            ref_id=fingerprint,
        )
    )
    db.flush()
    _stage_audit(db, subscriber, outcome=outcome, command=command)
    return _result(
        subscriber,
        outcome=outcome,
        replayed=False,
        context=command.context,
    )


def provision_crm_subscriber(
    db: Session, command: ProvisionCRMSubscriberCommand
) -> CRMSubscriberProvisioningResult:
    """Create or exactly reuse one CRM-provenance Subscriber atomically."""

    try:
        return execute_owner_command(
            db,
            definition=_COMMAND,
            context=command.context,
            operation=lambda: _operate(db, command),
        )
    except SubscriberAccountPreparationError as exc:
        raise _error("invalid_command", str(exc)) from exc
    except IntegrityError as exc:
        raise _error(
            "identity_conflict",
            "CRM provisioning conflicts with canonical customer state.",
        ) from exc
