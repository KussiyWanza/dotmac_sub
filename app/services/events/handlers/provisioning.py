"""Provisioning handler for event-driven automation."""

import logging

from sqlalchemy.orm import Session

from app.models.provisioning import (
    ProvisioningRun,
    ProvisioningRunStatus,
    ServiceOrder,
)
from app.schemas.provisioning import ProvisioningRunStart
from app.services import provisioning as provisioning_service
from app.services.common import coerce_uuid
from app.services.db_session_adapter import db_session_adapter
from app.services.events.types import Event, EventType
from app.services.owner_commands import CommandContext
from app.services.provisioning_lifecycle import (
    ConfirmActivationCommand,
    EvaluateReadinessCommand,
    confirm_activation,
    evaluate_readiness,
)

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.subscription_activated,
        EventType.subscription_resumed,
        EventType.service_order_assigned,
        EventType.service_order_activation_requested,
        EventType.provisioning_completed,
        EventType.provisioning_failed,
    }
)


class ProvisioningHandler:
    """Handler that triggers provisioning workflows on key events."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.subscription_activated:
            self._handle_subscription_activated(db, event)
        elif event.event_type == EventType.subscription_resumed:
            self._handle_subscription_resumed(db, event)
        elif event.event_type == EventType.service_order_assigned:
            self._handle_service_order_assigned(db, event)
        elif event.event_type == EventType.service_order_activation_requested:
            self._handle_service_order_activation_requested(db, event)
        elif event.event_type == EventType.provisioning_completed:
            self._evaluate_run_readiness(event)
        elif event.event_type == EventType.provisioning_failed:
            self._evaluate_run_readiness(event)

    def _evaluate_run_readiness(self, event: Event) -> None:
        """Delegate terminal run observations to the lifecycle decision owner."""
        service_order_id = event.service_order_id or event.payload.get(
            "service_order_id"
        )
        provisioning_run_id = event.payload.get("provisioning_run_id")
        if not service_order_id or not provisioning_run_id:
            return
        try:
            order_uuid = coerce_uuid(service_order_id)
            run_uuid = coerce_uuid(provisioning_run_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid provisioning lifecycle event scope") from exc
        with db_session_adapter.owner_command_session() as owner_db:
            outcome = evaluate_readiness(
                owner_db,
                EvaluateReadinessCommand(
                    context=CommandContext.system(
                        actor="system:provisioning_event",
                        scope=str(order_uuid),
                        reason=event.event_type.value,
                        command_id=event.event_id,
                        correlation_id=event.event_id,
                        causation_id=event.event_id,
                        idempotency_key=f"event:{event.event_id}",
                    ),
                    service_order_id=order_uuid,
                    provisioning_run_id=run_uuid,
                ),
            )
        logger.info(
            "Provisioning readiness for service order %s decided %s",
            order_uuid,
            outcome.status.value,
        )

    def _confirm_service_order_activation(self, event: Event) -> None:
        service_order_id = event.service_order_id or event.payload.get(
            "service_order_id"
        )
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not service_order_id or not subscription_id:
            return
        try:
            order_uuid = coerce_uuid(service_order_id)
            subscription_uuid = coerce_uuid(subscription_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid activation confirmation event scope") from exc
        with db_session_adapter.owner_command_session() as owner_db:
            confirm_activation(
                owner_db,
                ConfirmActivationCommand(
                    context=CommandContext.system(
                        actor="system:connectivity_projection",
                        scope=str(order_uuid),
                        reason="subscription activation projections succeeded",
                        command_id=event.event_id,
                        correlation_id=event.event_id,
                        causation_id=event.event_id,
                        idempotency_key=f"event:{event.event_id}",
                    ),
                    service_order_id=order_uuid,
                    subscription_id=subscription_uuid,
                ),
            )

    def _handle_subscription_resumed(self, db: Session, event: Event) -> None:
        """Re-provision IP on reactivation.

        ``restore_subscription`` (payment-reactivation) emits
        ``subscription_resumed``, NOT ``subscription_activated`` — so without this
        the IPv4 assignment and ``subscriptions.ipv4_address`` are never restored
        on reactivation. The RADIUS refresh then rebuilds the reply WITHOUT
        Framed-IP-Address and the BNG tears the session down ~130ms after auth
        (the "paid -> went offline" 30s flap). ``ensure_ip_assignments_for_subscription``
        reactivates the inactive IPv4 assignment and re-sets ipv4_address; the
        RADIUS refresh enqueued by the enforcement handler on the same event then
        regenerates the reply WITH Framed-IP-Address.
        """
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            return
        try:
            provisioning_service.ensure_ip_assignments_for_subscription(
                db, str(subscription_id)
            )
        except Exception as exc:
            logger.warning(
                "IP re-provision on resume failed for subscription %s: %s",
                subscription_id,
                exc,
            )

    def _handle_subscription_activated(self, db: Session, event: Event) -> None:
        if event.payload.get("projections_confirmed") is True:
            return
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            logger.debug(
                "Skipping auto IP allocation: event %s missing subscription_id",
                event.event_type.value if event.event_type else "unknown",
            )
            return
        # Projection failures propagate so the event remains retryable and the
        # exact service order cannot be confirmed prematurely.
        provisioning_service.ensure_ip_assignments_for_subscription(
            db, str(subscription_id)
        )
        # Step 2: Sync RADIUS credentials so subscriber can authenticate
        self._sync_radius_on_activation(db, str(subscription_id))
        # Step 3: Push NAS provisioning commands
        self._push_nas_provisioning(db, str(subscription_id))

    def _handle_service_order_activation_requested(
        self, db: Session, event: Event
    ) -> None:
        """Project connectivity, then confirm the exact readiness decision."""

        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            raise ValueError("Activation request is missing subscription_id")
        provisioning_service.ensure_ip_assignments_for_subscription(
            db, str(subscription_id)
        )
        self._sync_radius_on_activation(db, str(subscription_id))
        self._push_nas_provisioning(db, str(subscription_id))
        self._confirm_service_order_activation(event)

    def _sync_radius_on_activation(self, db: Session, subscription_id: str) -> None:
        """Reconcile RADIUS state for the activated subscription."""
        from app.models.catalog import Subscription
        from app.services.radius import (
            reconcile_subscription_connectivity,
            sync_account_credentials_to_radius,
        )

        subscription = db.get(Subscription, coerce_uuid(subscription_id))
        if not subscription:
            raise ValueError(f"Subscription {subscription_id} not found")

        sync_account_credentials_to_radius(db, str(subscription.subscriber_id))
        result = reconcile_subscription_connectivity(db, subscription_id)
        if not result.ok:
            raise RuntimeError(f"RADIUS connectivity reconciliation failed: {result}")
        logger.info(
            "Reconciled RADIUS state for subscription %s: %s",
            subscription_id,
            result,
        )

    def _push_nas_provisioning(self, db: Session, subscription_id: str) -> None:
        """Push NAS provisioning commands on subscription activation."""
        from app.models.catalog import NasDevice, ProvisioningAction, Subscription
        from app.services.connection_type_provisioning import (
            build_nas_provisioning_commands,
        )
        from app.services.enforcement import _resolve_effective_profile
        from app.services.nas import DeviceProvisioner

        subscription = db.get(Subscription, coerce_uuid(subscription_id))
        if not subscription or not subscription.provisioning_nas_device_id:
            return
        nas_device = db.get(NasDevice, subscription.provisioning_nas_device_id)
        if not nas_device:
            raise ValueError("Provisioning NAS device was not found")
        profile = _resolve_effective_profile(db, subscription)
        commands = build_nas_provisioning_commands(
            db,
            subscription,
            nas_device,
            profile=profile,
            action="create",
        )
        for command in commands:
            DeviceProvisioner._execute_ssh(nas_device, command)
        DeviceProvisioner._handle_queue_mapping(
            db,
            nas_device,
            ProvisioningAction.create_user,
            {
                "subscription_id": str(subscription.id),
                "username": subscription.login or "",
            },
        )
        logger.info(
            "Pushed %d NAS provisioning commands for subscription %s.",
            len(commands),
            subscription_id,
        )

    def _handle_service_order_assigned(self, db: Session, event: Event) -> None:
        service_order_id = event.service_order_id or event.payload.get(
            "service_order_id"
        )
        if not service_order_id:
            logger.debug(
                "Skipping provisioning run: event %s missing service_order_id",
                event.event_type.value if event.event_type else "unknown",
            )
            return
        try:
            order_uuid = coerce_uuid(service_order_id)
        except (TypeError, ValueError):
            logger.warning(
                "Skipping provisioning run: invalid service_order_id=%r",
                service_order_id,
            )
            return
        # Lock the order row so two concurrent ``service_order_assigned``
        # events serialize through the existing-run check below — otherwise
        # both see zero runs and both kick off a run against the same OLT/NAS
        # (double-provisioning). On SQLite (tests) FOR UPDATE is a harmless
        # no-op.
        from sqlalchemy import select as _select

        service_order = db.execute(
            _select(ServiceOrder).where(ServiceOrder.id == order_uuid).with_for_update()
        ).scalar_one_or_none()
        if not service_order:
            logger.warning(
                "Skipping provisioning run: service order %s not found.",
                service_order_id,
            )
            return
        existing = (
            db.query(ProvisioningRun)
            .filter(ProvisioningRun.service_order_id == service_order.id)
            .filter(ProvisioningRun.status != ProvisioningRunStatus.failed)
            .first()
        )
        if existing:
            logger.info(
                "Skipping provisioning run for service order %s: existing run %s with status %s.",
                service_order_id,
                existing.id,
                existing.status.value,
            )
            return
        workflow = provisioning_service.resolve_workflow_for_service_order(
            db, service_order
        )
        if not workflow:
            logger.warning(
                "Skipping provisioning run for service order %s: no active workflow found.",
                service_order_id,
            )
            return
        try:
            provisioning_service.provisioning_runs.run(
                db,
                str(workflow.id),
                ProvisioningRunStart(
                    service_order_id=service_order.id,
                    subscription_id=service_order.subscription_id,
                ),
            )
        except Exception as exc:
            # Propagate so the event delivery stays failed and retryable —
            # a swallowed start would leave the assigned order with no run
            # and no visible pending work.
            logger.exception(
                "Provisioning run failed for service order %s: %s",
                service_order_id,
                exc,
            )
            raise
