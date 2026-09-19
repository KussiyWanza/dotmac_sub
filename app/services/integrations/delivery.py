"""Canonical owner for capability-bound outbound event deliveries."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.integration_platform import (
    IntegrationBindingState,
    IntegrationCapabilityBinding,
    IntegrationDelivery,
    IntegrationEventSubscription,
    IntegrationInstallationState,
)
from app.services.events.types import Event
from app.services.integrations.connectors.http_webhook import (
    EVENT_DELIVERY_CAPABILITY,
    HttpWebhookRunner,
)
from app.services.integrations.runtime import OperationStatus, OperationTrigger
from app.services.integrations.runtime_execution import (
    build_execution_context,
    make_operation_executor,
)


class DeliveryError(ValueError):
    """Raised when a delivery transition violates its binding or lifecycle."""


CommandResultT = TypeVar("CommandResultT")


def execute_command(
    db: Session,
    command: Callable[[], CommandResultT],
) -> CommandResultT:
    """Complete one delivery-owned unit of work."""

    try:
        result = command()
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_enabled_delivery_binding(
    db: Session, capability_binding_id: UUID
) -> IntegrationCapabilityBinding:
    binding = db.get(IntegrationCapabilityBinding, capability_binding_id)
    if binding is None:
        raise DeliveryError("capability binding not found")
    if binding.capability_id != EVENT_DELIVERY_CAPABILITY:
        raise DeliveryError("binding does not implement events.deliver.v1")
    if binding.state != IntegrationBindingState.enabled.value:
        raise DeliveryError("delivery capability binding is not enabled")
    if binding.installation.state != IntegrationInstallationState.enabled.value:
        raise DeliveryError("delivery installation is not enabled")
    return binding


def create_event_subscription(
    db: Session,
    *,
    capability_binding_id: UUID,
    event_type: str,
    filter_json: dict[str, Any] | None = None,
    payload_policy_json: dict[str, Any] | None = None,
    actor: str | None = None,
) -> IntegrationEventSubscription:
    binding = _require_enabled_delivery_binding(db, capability_binding_id)
    normalized_event_type = event_type.strip()
    if not normalized_event_type:
        raise DeliveryError("event type is required")
    subscription = (
        db.query(IntegrationEventSubscription)
        .filter(
            IntegrationEventSubscription.capability_binding_id == binding.id,
            IntegrationEventSubscription.event_type == normalized_event_type,
        )
        .one_or_none()
    )
    if subscription is None:
        subscription = IntegrationEventSubscription(
            capability_binding_id=binding.id,
            event_type=normalized_event_type,
            state="enabled",
            filter_json=dict(filter_json or {}),
            payload_policy_json=dict(payload_policy_json or {}),
            created_by=actor,
            updated_by=actor,
        )
        db.add(subscription)
    else:
        subscription.state = "enabled"
        subscription.filter_json = dict(filter_json or {})
        subscription.payload_policy_json = dict(payload_policy_json or {})
        subscription.updated_by = actor
    db.flush()
    return subscription


def set_event_subscription_enabled(
    db: Session,
    *,
    subscription_id: UUID,
    enabled: bool,
    actor: str | None = None,
) -> IntegrationEventSubscription:
    subscription = db.get(IntegrationEventSubscription, subscription_id)
    if subscription is None:
        raise DeliveryError("integration event subscription not found")
    if enabled:
        _require_enabled_delivery_binding(db, subscription.capability_binding_id)
    subscription.state = "enabled" if enabled else "disabled"
    subscription.updated_by = actor
    db.flush()
    return subscription


def sync_event_subscriptions(
    db: Session,
    *,
    binding: IntegrationCapabilityBinding,
    event_types: tuple[str, ...],
    actor: str | None = None,
) -> list[IntegrationEventSubscription]:
    """Make the selected event set authoritative for one delivery binding."""

    if binding.capability_id != EVENT_DELIVERY_CAPABILITY:
        raise DeliveryError("binding does not implement events.deliver.v1")
    if binding.installation.state == IntegrationInstallationState.retired.value:
        raise DeliveryError("retired installation cannot receive subscriptions")
    selected = set(event_types)
    existing = {
        item.event_type: item
        for item in db.query(IntegrationEventSubscription)
        .filter(IntegrationEventSubscription.capability_binding_id == binding.id)
        .all()
    }
    for event_type in selected:
        subscription = existing.get(event_type)
        if subscription is None:
            subscription = IntegrationEventSubscription(
                capability_binding_id=binding.id,
                event_type=event_type,
                state="enabled",
                filter_json={},
                payload_policy_json={"projection": "domain_event.v1"},
                created_by=actor,
                updated_by=actor,
            )
            db.add(subscription)
            existing[event_type] = subscription
        else:
            subscription.state = "enabled"
            subscription.updated_by = actor
    for event_type, subscription in existing.items():
        if event_type not in selected:
            subscription.state = "disabled"
            subscription.updated_by = actor
    db.flush()
    return [existing[event_type] for event_type in event_types]


def list_deliveries(
    db: Session,
    *,
    state: str | None = None,
    capability_binding_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[IntegrationDelivery]:
    query = db.query(IntegrationDelivery)
    if state:
        query = query.filter(IntegrationDelivery.state == state)
    if capability_binding_id:
        query = query.filter(
            IntegrationDelivery.capability_binding_id == capability_binding_id
        )
    return (
        query.order_by(IntegrationDelivery.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def create_platform_deliveries_for_event(
    db: Session,
    *,
    event: Event,
    event_type: str,
) -> list[IntegrationDelivery]:
    subscriptions = (
        db.query(IntegrationEventSubscription)
        .join(IntegrationCapabilityBinding)
        .filter(
            IntegrationEventSubscription.event_type == event_type,
            IntegrationEventSubscription.state == "enabled",
            IntegrationCapabilityBinding.state == IntegrationBindingState.enabled.value,
            IntegrationCapabilityBinding.capability_id == EVENT_DELIVERY_CAPABILITY,
        )
        .all()
    )
    deliveries: list[IntegrationDelivery] = []
    for subscription in subscriptions:
        binding = subscription.capability_binding
        if binding.installation.state != IntegrationInstallationState.enabled.value:
            continue
        projection = str(
            (subscription.payload_policy_json or {}).get("projection") or ""
        )
        payload = (
            dict(event.payload) if projection == "event_payload.v1" else event.to_dict()
        )
        digest = payload_digest(payload)
        key = f"event:{event.event_id}:binding:{binding.id}"
        existing = (
            db.query(IntegrationDelivery)
            .filter(IntegrationDelivery.idempotency_key == key)
            .one_or_none()
        )
        if existing is not None:
            deliveries.append(existing)
            continue
        delivery = IntegrationDelivery(
            subscription_id=subscription.id,
            capability_binding_id=binding.id,
            source_event_id=str(event.event_id),
            event_type=event_type,
            destination_key=f"capability-binding:{binding.id}",
            idempotency_key=key,
            payload_digest=digest,
            payload_json=payload,
            state="pending",
        )
        db.add(delivery)
        db.flush()
        deliveries.append(delivery)
    return deliveries


def queue_platform_deliveries(
    deliveries: list[IntegrationDelivery], *, event: Event
) -> None:
    if not deliveries:
        return
    from app.services.queue_adapter import enqueue_task
    from app.tasks.integration_delivery import deliver_integration_event

    for delivery in deliveries:
        if delivery.state != "pending":
            continue
        enqueue_task(
            deliver_integration_event,
            args=[str(delivery.id)],
            correlation_id=f"integration-delivery:{event.event_id}",
            source="integration.delivery",
        )


def create_manual_test_delivery(
    db: Session, *, capability_binding_id: UUID
) -> IntegrationDelivery:
    """Persist and queue an operator-requested transport test."""

    binding = _require_enabled_delivery_binding(db, capability_binding_id)
    nonce = uuid4()
    payload = {
        "event_id": str(nonce),
        "event_type": "custom",
        "payload": {
            "kind": "integration.test",
            "installation_id": str(binding.installation_id),
        },
    }
    record = IntegrationDelivery(
        capability_binding_id=binding.id,
        source_event_id=str(nonce),
        event_type="custom",
        destination_key=f"capability-binding:{binding.id}",
        idempotency_key=f"manual:{binding.id}:{nonce}",
        payload_digest=payload_digest(payload),
        payload_json=payload,
        state="pending",
    )
    db.add(record)
    db.commit()

    from app.services.queue_adapter import enqueue_task
    from app.tasks.integration_delivery import deliver_integration_event

    enqueue_task(
        deliver_integration_event,
        args=[str(record.id)],
        correlation_id=f"integration-delivery:{record.id}",
        source="admin.integrations.webhook_test",
    )
    return record


def execute_delivery(
    db: Session,
    *,
    delivery_id: UUID,
    runner_override: HttpWebhookRunner | None = None,
    secret_resolver=None,
) -> IntegrationDelivery:
    delivery = db.get(IntegrationDelivery, delivery_id)
    if delivery is None:
        raise DeliveryError("integration delivery not found")
    if delivery.state in {"delivered", "canceled"}:
        return delivery
    _require_enabled_delivery_binding(db, delivery.capability_binding_id)
    context_kwargs: dict[str, Any] = {
        "capability_binding_id": delivery.capability_binding_id,
        "runner_override": runner_override,
    }
    if secret_resolver is not None:
        context_kwargs["secret_resolver"] = secret_resolver
    context = build_execution_context(db, **context_kwargs)
    executor = make_operation_executor(
        context,
        correlation_id=f"integration-delivery:{delivery.id}",
        trigger=OperationTrigger.event,
        actor="integration.delivery",
    )
    delivery.state = "leased"
    delivery.leased_until = datetime.now(UTC) + timedelta(minutes=2)
    delivery.last_attempt_at = datetime.now(UTC)
    delivery.attempt_count += 1
    db.flush()
    result = executor(
        "deliver_event",
        {"event_type": delivery.event_type, "payload": delivery.payload_json},
    )
    delivery.external_receipt_json = dict(result.external_receipt)
    delivery.response_status = result.external_receipt.get("response_status")
    delivery.error_code = result.error_code
    delivery.leased_until = None
    if result.status == OperationStatus.succeeded:
        delivery.state = "delivered"
        delivery.delivered_at = datetime.now(UTC)
        delivery.error_detail = None
    elif result.status == OperationStatus.retryable:
        max_attempts = int(context.config.get("max_attempts") or 10)
        if delivery.attempt_count >= max(1, min(max_attempts, 20)):
            delivery.state = "dead_letter"
        else:
            delivery.state = "retryable"
            delay = result.retry_after_seconds or min(
                8 * 60 * 60, 60 * (2 ** max(delivery.attempt_count - 1, 0))
            )
            delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
    elif result.status == OperationStatus.reconciliation_required:
        delivery.state = "reconciliation_required"
    else:
        delivery.state = "dead_letter"
    db.flush()
    return delivery


def replay_delivery(db: Session, *, delivery_id: UUID) -> IntegrationDelivery:
    delivery = db.get(IntegrationDelivery, delivery_id)
    if delivery is None:
        raise DeliveryError("integration delivery not found")
    if delivery.state not in {
        "dead_letter",
        "reconciliation_required",
        "retryable",
    }:
        raise DeliveryError("delivery is not replayable")
    _require_enabled_delivery_binding(db, delivery.capability_binding_id)
    delivery.state = "pending"
    delivery.next_attempt_at = None
    delivery.error_code = None
    delivery.error_detail = None
    db.flush()
    return delivery
