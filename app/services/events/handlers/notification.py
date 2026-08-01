"""Notification handler for the event system."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import (
    NotificationChannel,
    NotificationTemplate,
)
from app.schemas.notification import NotificationCreate
from app.services.communication_intents import (
    CommunicationAttachment,
    CommunicationAttachmentKind,
    CommunicationIntent,
    submit,
)
from app.services.customer_notification_policy import (
    channel_disabled_in_config,
    resolve_subscriber_id_for_recipient,
)
from app.services.events.types import Event, EventType
from app.services.notification import notifications as notification_service
from app.services.notification_channel_policy import resolve_notification_channels
from app.services.notification_template_conditions import (
    NotificationTemplateConditionError,
    conditions_match,
)

logger = logging.getLogger(__name__)
_LOGGED_MISSING_TEMPLATE_CODES: set[str] = set()

CHANNEL_TEMPLATE_SUFFIXES: dict[NotificationChannel, str] = {
    NotificationChannel.email: "email",
    NotificationChannel.sms: "sms",
    NotificationChannel.whatsapp: "whatsapp",
    NotificationChannel.push: "push",
    NotificationChannel.webhook: "webhook",
}

#: The single system-wide fallback channel. Real routing is owned by
#: communications.channel_policy (the admin matrix); this is only the last
#: resort so an unconfigured system still reaches the customer rather than
#: going silent. Specs declare a notification, NOT its channels.
SYSTEM_DEFAULT_CHANNELS: tuple[NotificationChannel, ...] = (NotificationChannel.email,)


_UNRESOLVED_TEMPLATE_RE = re.compile(r"\{\{?\s*[a-zA-Z0-9_]+\s*\}?\}")


def _channel_disabled_in_config(db: Session, channel: NotificationChannel) -> bool:
    return channel_disabled_in_config(db, channel)


def _event_channels(
    db: Session,
    template_code: str,
    category: str,
    default_channels: tuple[NotificationChannel, ...],
) -> tuple[NotificationChannel, ...]:
    return resolve_notification_channels(
        db,
        template_code=template_code,
        category=category,
        default_channels=default_channels,
    )


@dataclass(frozen=True)
class EventNotificationSpec:
    template_code: str
    category: str
    subject: str
    body: str


EVENT_NOTIFICATION_SPECS: dict[EventType, EventNotificationSpec] = {
    # Outage classifier (design docs/designs/OUTAGE_CLASSIFIER.md §P4). The
    # outage notifier supplies the customer-safe {message} as event context; the
    # channels here are the DEFAULT and remain config-overridable per type via
    # the ``notification_event_<template_code>_channels`` setting — channel
    # selection lives with the notification system, never in the outage notifier.
    EventType.outage_area: EventNotificationSpec(
        template_code="outage_area",
        category="service",
        subject="Service interruption in your area",
        body="Dear {subscriber_name},\n\n{message}",
    ),
    EventType.outage_last_mile: EventNotificationSpec(
        template_code="outage_last_mile",
        category="service",
        subject="About your connection",
        body="Dear {subscriber_name},\n\n{message}",
    ),
    # Ticket acknowledgement. `customer_ticket_created` was already emitted by
    # the portal (app/web/customer/routes.py) but no spec consumed it, so a
    # customer raising a ticket got silence — the top driver of "did you get
    # my message?" follow-ups. Later movements are notified from
    # app.services.support (staff reply, status change, resolution).
    EventType.customer_ticket_created: EventNotificationSpec(
        template_code="customer_ticket_created",
        category="support",
        subject="We received your request ({ticket_number})",
        body=(
            "Dear {subscriber_name},\n\n"
            "We've logged your request as ticket {ticket_number} and the "
            "support team has it.\n\n"
            "You can follow it in the portal at any time."
        ),
    ),
    EventType.subscriber_created: EventNotificationSpec(
        template_code="subscriber_created",
        category="account",
        subject="Your customer account is ready",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your customer account has been created successfully. "
            "You can now manage your services and billing through the portal.\n\n"
            "Thank you for choosing us."
        ),
    ),
    EventType.subscriber_updated: EventNotificationSpec(
        template_code="subscriber_updated",
        category="account",
        subject="Your account profile was updated",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your account profile was updated successfully.\n\n"
            "Updated fields: {updated_fields}\n\n"
            "If you did not make this change, please contact support immediately."
        ),
    ),
    EventType.subscription_created: EventNotificationSpec(
        template_code="subscription_created",
        category="service",
        subject="Your new service subscription",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your subscription to {offer_name} has been created. "
            "A service order will be created for installation.\n\n"
            "If you have questions, contact our support team."
        ),
    ),
    EventType.subscription_activated: EventNotificationSpec(
        template_code="subscription_activated",
        category="service",
        subject="Your service is now active",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription is now active and ready to use."
        ),
    ),
    EventType.subscription_suspended: EventNotificationSpec(
        template_code="subscription_suspended",
        category="service",
        subject="Service suspended",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription has been suspended. "
            "Please make payment or contact support to restore service."
        ),
    ),
    EventType.subscription_resumed: EventNotificationSpec(
        template_code="subscription_resumed",
        category="service",
        subject="Your service has been resumed",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription has been resumed successfully."
        ),
    ),
    EventType.subscription_canceled: EventNotificationSpec(
        template_code="subscription_canceled",
        category="service",
        subject="Subscription canceled",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription has been canceled. "
            "If this was unexpected, please contact support."
        ),
    ),
    EventType.subscription_expiring: EventNotificationSpec(
        template_code="subscription_expiring",
        category="service",
        subject="Your subscription is expiring soon",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription will expire soon. "
            "Please renew to avoid interruption."
        ),
    ),
    EventType.subscription_renewal_invoice_ready: EventNotificationSpec(
        template_code="subscription_renewal_invoice_ready",
        category="billing",
        subject="Renewal invoice #{invoice_number} is ready",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your current service remains active until {renewal_period_start}. "
            "Invoice #{invoice_number} for {renewal_period_start} to "
            "{renewal_period_end} is ready. You can pay now to continue without "
            "interruption.\n\nView and pay: {invoice_url}"
        ),
    ),
    EventType.subscription_expired: EventNotificationSpec(
        template_code="subscription_expired",
        category="service",
        subject="Your subscription has expired",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {offer_name} subscription has expired. "
            "Renew your service to restore access."
        ),
    ),
    EventType.subscription_suspension_warning: EventNotificationSpec(
        template_code="suspension_warning",
        category="billing",
        subject="A reminder about invoice #{invoice_number}",
        body=(
            "Dear {subscriber_name},\n\n"
            "Invoice #{invoice_number} for {amount} is currently unpaid. To avoid "
            "any interruption to your service, please arrange payment when you "
            "can. If you have already paid, please disregard this message."
        ),
    ),
    EventType.subscription_upgraded: EventNotificationSpec(
        template_code="subscription_upgraded",
        category="service",
        subject="Your plan has been upgraded",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your service has been upgraded from {old_offer_name} to {new_offer_name}."
        ),
    ),
    EventType.subscription_downgraded: EventNotificationSpec(
        template_code="subscription_downgraded",
        category="service",
        subject="Your plan has been updated",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your service has been changed from {old_offer_name} to {new_offer_name}."
        ),
    ),
    EventType.invoice_created: EventNotificationSpec(
        template_code="invoice_created",
        category="billing",
        subject="New invoice #{invoice_number}",
        body=(
            "Dear {subscriber_name},\n\n"
            "A new invoice #{invoice_number} for {amount} has been generated. "
            "Due date: {due_date}."
        ),
    ),
    EventType.invoice_sent: EventNotificationSpec(
        template_code="invoice_sent",
        category="billing",
        subject="Invoice #{invoice_number} — payment due {due_date}",
        body=(
            "Dear {subscriber_name},\n\n"
            "Invoice #{invoice_number} for {amount} is due on {due_date}. "
            "Please pay before the due date to avoid disruption."
        ),
    ),
    EventType.invoice_paid: EventNotificationSpec(
        template_code="invoice_paid",
        category="billing",
        subject="Invoice #{invoice_number} has been paid",
        body=(
            "Dear {subscriber_name},\n\n"
            "Invoice #{invoice_number} has been paid successfully. "
            "Thank you for your payment."
        ),
    ),
    EventType.invoice_overdue: EventNotificationSpec(
        template_code="invoice_overdue",
        category="billing",
        subject="Invoice #{invoice_number} is now due",
        body=(
            "Dear {subscriber_name},\n\n"
            "Our records show invoice #{invoice_number} for {amount} is now past "
            "its due date. If you have already paid, please disregard this "
            "message. Otherwise you can pay anytime at {portal_url}/billing."
        ),
    ),
    EventType.payment_received: EventNotificationSpec(
        template_code="payment_received",
        category="billing",
        subject="Payment receipt {receipt_number}",
        body=(
            "Dear {subscriber_name},\n\n"
            "We have received your payment of {amount}. Thank you.\n\n"
            "Receipt: {receipt_number}\n"
            "View or download: {receipt_url}"
        ),
    ),
    EventType.prepaid_service_renewed: EventNotificationSpec(
        template_code="prepaid_service_renewed",
        category="service",
        subject="Your {offer_name} service is renewed",
        body=(
            "Dear {subscriber_name},\n\n"
            "We applied {amount} to your {offer_name} service. "
            "Your service is renewed through {renewed_through}.\n\n"
            "This renewal confirmation is separate from your payment receipt."
        ),
    ),
    EventType.payment_failed: EventNotificationSpec(
        template_code="payment_failed",
        category="billing",
        subject="Payment failed — please retry",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your recent payment attempt of {amount} was not successful. "
            "Please try again or use a different payment method."
        ),
    ),
    EventType.payment_refunded: EventNotificationSpec(
        template_code="payment_refunded",
        category="billing",
        subject="Payment refunded",
        body=(
            "Dear {subscriber_name},\n\n"
            "A refund of {amount} has been processed on your account."
        ),
    ),
    EventType.payment_reversed: EventNotificationSpec(
        template_code="payment_reversed",
        category="billing",
        subject="Payment reversed",
        body=(
            "Dear {subscriber_name},\n\n"
            "A settled payment of {amount} was reversed on your account. "
            "Please contact billing if you need more information."
        ),
    ),
    EventType.referral_reward_issued: EventNotificationSpec(
        template_code="referral_reward_issued",
        category="billing",
        subject="You earned a referral reward",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your referral reward of {amount} has been added to your account credit."
        ),
    ),
    EventType.arrangement_defaulted: EventNotificationSpec(
        template_code="arrangement_defaulted",
        category="billing",
        subject="Update on your payment arrangement",
        body=(
            "Dear {subscriber_name},\n\n"
            "It looks like your payment arrangement has fallen behind, and the "
            "outstanding balance of {total_amount} is now due. If you've already "
            "paid, please disregard this message. Otherwise, please contact our "
            "billing team and we'll be happy to help."
        ),
    ),
    EventType.usage_warning: EventNotificationSpec(
        template_code="usage_warning",
        category="usage",
        subject="Data usage warning — {usage_percent}% used",
        body=(
            "Dear {subscriber_name},\n\n"
            "You have used {usage_percent}% of your monthly data allowance on {offer_name}."
        ),
    ),
    EventType.usage_exhausted: EventNotificationSpec(
        template_code="usage_exhausted",
        category="usage",
        subject="Data allowance exhausted",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your monthly data allowance on {offer_name} has been exhausted."
        ),
    ),
    EventType.service_extended: EventNotificationSpec(
        template_code="service_extended",
        category="billing",
        subject="Your service has been extended",
        body=(
            "Dear {subscriber_name},\n\n"
            "We have added {days} day(s) to your service as compensation: "
            "{reason}. Your service now runs until {extended_until}."
        ),
    ),
    EventType.addon_expiring: EventNotificationSpec(
        template_code="addon_expiring",
        category="usage",
        subject="Your data bundle expires soon",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your {addon_name} data bundle expires on {expires_at}. "
            "Unused data lapses with it — top up again to stay connected."
        ),
    ),
    EventType.usage_topped_up: EventNotificationSpec(
        template_code="usage_topped_up",
        category="usage",
        subject="Top-up received",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your account has been topped up with {amount}. Reference: {reference}."
        ),
    ),
    EventType.provisioning_completed: EventNotificationSpec(
        template_code="provisioning_completed",
        category="service",
        subject="Service installation complete",
        body=(
            "Dear {subscriber_name},\n\n"
            "Your service installation has been completed successfully."
        ),
    ),
    EventType.provisioning_failed: EventNotificationSpec(
        template_code="provisioning_failed",
        category="service",
        subject="Service installation issue",
        body=(
            "Dear {subscriber_name},\n\n"
            "We encountered an issue while setting up your service. "
            "Our technical team will follow up shortly."
        ),
    ),
    EventType.service_order_created: EventNotificationSpec(
        template_code="service_order_created",
        category="service",
        subject="Your service order has been created",
        body=(
            "Dear {subscriber_name},\n\n"
            "Service order #{service_order_id} has been created for your account."
        ),
    ),
    EventType.service_order_assigned: EventNotificationSpec(
        template_code="service_order_assigned",
        category="service",
        subject="Your service order is in progress",
        body=(
            "Dear {subscriber_name},\n\n"
            "Service order #{service_order_id} is now being worked on by our team."
        ),
    ),
    EventType.service_order_completed: EventNotificationSpec(
        template_code="service_order_completed",
        category="service",
        subject="Your service order is complete",
        body=(
            "Dear {subscriber_name},\n\n"
            "Service order #{service_order_id} has been completed successfully."
        ),
    ),
    EventType.ont_offline: EventNotificationSpec(
        template_code="ont_offline",
        category="service",
        subject="We've noticed an issue with your connection",
        body=(
            "Dear {subscriber_name},\n\n"
            "We've detected that your service may currently be offline, and our "
            "team is looking into it. If your equipment has lost power, please "
            "check that it is switched on. If the issue continues, please contact "
            "our support team."
        ),
    ),
    EventType.ont_online: EventNotificationSpec(
        template_code="ont_online",
        category="service",
        subject="Your connection is back online",
        body=(
            "Dear {subscriber_name},\n\n"
            "Good news — your service is back online. Thank you for your "
            "patience. If you continue to experience any issues, please contact "
            "our support team."
        ),
    ),
    EventType.ont_signal_degraded: EventNotificationSpec(
        template_code="ont_signal_degraded",
        category="service",
        subject="We're checking on your connection quality",
        body=(
            "Dear {subscriber_name},\n\n"
            "Our monitoring suggests your connection quality may be reduced, and "
            "our team is looking into it. There is nothing you need to do right "
            "now. If you notice any problems, please contact our support team."
        ),
    ),
    EventType.ont_discovered: EventNotificationSpec(
        template_code="ont_discovered",
        category="service",
        subject="New ONT discovered — {device_serial}",
        body="A new ONT has been discovered on the network (internal notification).",
    ),
}

EVENT_TYPE_TO_TEMPLATE = {
    event_type: spec.template_code
    for event_type, spec in EVENT_NOTIFICATION_SPECS.items()
}


@dataclass(frozen=True)
class EventCatalogueEntry:
    """One configurable event, as presented to an operator."""

    event_type: str
    template_code: str
    category: str
    default_channels: tuple[str, ...]
    subject: str


def event_catalogue() -> tuple[EventCatalogueEntry, ...]:
    """Describe every event whose channels an operator can configure.

    The channel policy owner resolves by ``template_code``/``category``; this
    exposes the spec defaults so the admin surface can show what a given event
    falls back to when no override is set.
    """
    return tuple(
        sorted(
            (
                EventCatalogueEntry(
                    event_type=event_type.value,
                    template_code=spec.template_code,
                    category=spec.category,
                    default_channels=tuple(c.value for c in SYSTEM_DEFAULT_CHANNELS),
                    subject=spec.subject,
                )
                for event_type, spec in EVENT_NOTIFICATION_SPECS.items()
            ),
            key=lambda entry: (entry.category, entry.template_code),
        )
    )


def event_categories() -> tuple[str, ...]:
    """Distinct categories, for the category-level override rows."""
    return tuple(sorted({spec.category for spec in EVENT_NOTIFICATION_SPECS.values()}))


class NotificationHandler:
    """Handler that queues customer notifications."""

    def handle(self, db: Session, event: Event) -> None:
        spec = EVENT_NOTIFICATION_SPECS.get(event.event_type)
        if spec is None:
            return
        correction_source = "admin:subscription_correction"
        if (
            event.event_type == EventType.subscription_canceled
            and event.payload.get("source") == correction_source
        ) or (
            event.event_type == EventType.subscription_resumed
            and (
                event.payload.get("resolved_by") == correction_source
                or event.payload.get("source") == correction_source
            )
        ):
            logger.info(
                "Suppressed paired lifecycle notification for reviewed subscription correction"
            )
            return
        if (
            event.event_type == EventType.invoice_created
            and str(event.payload.get("status") or "").lower() == "draft"
        ):
            logger.info(
                "Suppressed customer notification for draft invoice_created event"
            )
            return
        # Back-office bookkeeping (e.g. the cutover credit reconcile) suppresses
        # customer notifications: the activity is not a real-time customer action,
        # so "Payment received"/"Service resumed" mail would be wrong and, in a
        # bulk burst to churned mailboxes, reputation-damaging.
        from app.services.notification_suppression import notifications_suppressed

        if notifications_suppressed():
            logger.info(
                "Suppressed notification for event %s (back-office scope)",
                event.event_type.value,
            )
            return

        templates = self._load_templates(db, spec.template_code)
        if not templates:
            templates = self._seed_and_reload_templates(db, spec.template_code)
        if not templates and spec.template_code not in _LOGGED_MISSING_TEMPLATE_CODES:
            logger.warning(
                "No active notification template for code %s", spec.template_code
            )
            _LOGGED_MISSING_TEMPLATE_CODES.add(spec.template_code)

        context = self._build_render_context(db, event)
        templates_by_channel = {
            (template.channel or NotificationChannel.email): template
            for template in templates
        }
        for channel in _event_channels(
            db,
            spec.template_code,
            spec.category,
            SYSTEM_DEFAULT_CHANNELS,
        ):
            template = templates_by_channel.get(channel)
            if template is None:
                logger.info(
                    "Suppressed notification for event %s on %s: no active template for code %s",
                    event.event_type.value,
                    channel.value,
                    spec.template_code,
                )
                continue
            recipient = self._resolve_recipient(db, event, channel)
            if not recipient:
                logger.debug(
                    "Cannot determine recipient for event %s on channel %s",
                    event.event_type.value,
                    channel.value,
                )
                continue

            subscriber_id = self._resolve_subscriber_id(db, event, recipient)
            try:
                if not conditions_match(
                    db,
                    subscriber_id=subscriber_id,
                    conditions=template.conditions,
                ):
                    logger.info(
                        "Suppressed notification for event %s on %s to %s by template conditions",
                        event.event_type.value,
                        channel.value,
                        recipient,
                    )
                    continue
            except NotificationTemplateConditionError as exc:
                logger.error(
                    "Suppressed notification for event %s on %s: invalid template conditions on %s: %s",
                    event.event_type.value,
                    channel.value,
                    template.id,
                    exc,
                )
                continue
            except Exception:
                logger.exception(
                    "Suppressed notification for event %s on %s: template condition evaluation failed for %s",
                    event.event_type.value,
                    channel.value,
                    template.id,
                )
                continue

            subject = self._render_subject(template, spec, context)
            body = self._render_body(template, spec, context)
            unresolved = sorted(
                {
                    *_UNRESOLVED_TEMPLATE_RE.findall(subject),
                    *_UNRESOLVED_TEMPLATE_RE.findall(body),
                }
            )
            if unresolved:
                logger.error(
                    "Suppressed notification for event %s on %s: unresolved template variable(s) %s",
                    event.event_type.value,
                    channel.value,
                    ", ".join(unresolved),
                )
                continue

            if subscriber_id is None:
                notification_service.queue_internal_notification(
                    db,
                    NotificationCreate(
                        template_id=template.id,
                        channel=channel,
                        event_type=spec.template_code,
                        category=spec.category,
                        recipient=recipient,
                        subject=subject,
                        body=body,
                    ),
                )
                queued_count = 1
            else:
                attachments: tuple[CommunicationAttachment, ...] = ()
                if (
                    channel == NotificationChannel.email
                    and event.event_type
                    in {
                        EventType.invoice_sent,
                        EventType.subscription_renewal_invoice_ready,
                    }
                    and event.invoice_id is not None
                ):
                    from app.models.billing import Invoice
                    from app.services.billing_invoice_pdf import download_filename

                    invoice = db.get(Invoice, event.invoice_id)
                    if invoice is None or invoice.account_id != subscriber_id:
                        logger.error(
                            "Suppressed invoice attachment delivery for event %s: "
                            "invoice scope could not be verified",
                            event.event_id,
                        )
                        continue
                    attachments = (
                        CommunicationAttachment(
                            kind=CommunicationAttachmentKind.invoice_pdf,
                            entity_id=invoice.id,
                            filename=download_filename(invoice),
                        ),
                    )
                result = submit(
                    db,
                    CommunicationIntent(
                        subscriber_id=subscriber_id,
                        event_type=spec.template_code,
                        category=spec.category,
                        template_id=template.id,
                        template_code=spec.template_code,
                        subject=subject,
                        body=body,
                        channels=(channel,),
                        persist_policy_suppressions=False,
                        recipients={channel: recipient},
                        attachments=attachments,
                        dedupe_key=(
                            f"event-notification:{event.event_id}:"
                            f"{spec.template_code}:{channel.value}"
                        ),
                    ),
                )
                queued_count = len(result.queued)
            if queued_count == 0:
                logger.info(
                    "Suppressed notification for event %s on %s to %s by shared policy",
                    event.event_type.value,
                    channel.value,
                    recipient,
                )
                continue

            logger.info(
                "Queued notification for event %s on %s to %s",
                event.event_type.value,
                channel.value,
                recipient,
            )

    def _load_templates(
        self,
        db: Session,
        template_code: str,
    ) -> list[NotificationTemplate]:
        candidate_codes = {
            template_code,
            *(
                f"{template_code}_{suffix}"
                for suffix in CHANNEL_TEMPLATE_SUFFIXES.values()
            ),
        }
        templates = list(
            db.scalars(
                select(NotificationTemplate)
                .where(NotificationTemplate.code.in_(candidate_codes))
                .where(NotificationTemplate.is_active.is_(True))
            ).all()
        )
        return self._order_templates(templates, template_code)

    def _seed_and_reload_templates(
        self,
        db: Session,
        template_code: str,
    ) -> list[NotificationTemplate]:
        try:
            from app.services.settings_seed import _seed_missing_notification_templates

            _seed_missing_notification_templates(db)
        except Exception:
            logger.debug(
                "Notification template reseed failed for code %s",
                template_code,
                exc_info=True,
            )
        return self._load_templates(db, template_code)

    def _order_templates(
        self,
        templates: Iterable[NotificationTemplate],
        template_code: str,
    ) -> list[NotificationTemplate]:
        suffix_to_channel = {
            suffix: channel for channel, suffix in CHANNEL_TEMPLATE_SUFFIXES.items()
        }
        ordered: dict[str, NotificationTemplate] = {}

        for template in templates:
            channel = template.channel or NotificationChannel.email
            expected_code = f"{template_code}_{CHANNEL_TEMPLATE_SUFFIXES[channel]}"
            if template.code == template_code:
                ordered[channel.value] = template
                continue
            if template.code == expected_code:
                ordered[channel.value] = template
                continue
            suffix = template.code.removeprefix(f"{template_code}_")
            mapped_channel = suffix_to_channel.get(suffix)
            if mapped_channel and mapped_channel.value not in ordered:
                ordered[mapped_channel.value] = template

        return list(ordered.values())

    def _resolve_subscriber_id(
        self,
        db: Session,
        event: Event,
        recipient: str | None,
    ):
        if event.account_id:
            return event.account_id
        if event.subscriber_id:
            return event.subscriber_id
        return resolve_subscriber_id_for_recipient(db, recipient)

    def _resolve_recipient(
        self,
        db: Session,
        event: Event,
        channel: NotificationChannel,
    ) -> str | None:
        email = event.payload.get("email")
        phone = event.payload.get("phone") or event.payload.get("phone_number")
        subscriber_id = event.account_id or event.subscriber_id

        if subscriber_id:
            from app.models.subscriber import Subscriber

            subscriber = db.get(Subscriber, subscriber_id)
            if channel == NotificationChannel.email and subscriber and subscriber.email:
                return subscriber.email
            # Push delivery targets the subscriber's device tokens; the
            # recipient string is only the in-app record's address field.
            if channel == NotificationChannel.push and subscriber and subscriber.email:
                return subscriber.email
            if (
                channel in {NotificationChannel.sms, NotificationChannel.whatsapp}
                and subscriber
                and subscriber.phone
            ):
                return subscriber.phone

        if channel == NotificationChannel.email and isinstance(email, str) and email:
            return email
        if (
            channel in {NotificationChannel.sms, NotificationChannel.whatsapp}
            and isinstance(phone, str)
            and phone
        ):
            return phone

        if isinstance(email, str) and email:
            return email
        if isinstance(phone, str) and phone:
            return phone
        return None

    def _build_render_context(self, db: Session, event: Event) -> dict[str, str]:
        context: dict[str, str] = {}
        for key, value in event.payload.items():
            if value is not None:
                context[key] = str(value)

        if "subscriber_name" not in context:
            subscriber_id = event.account_id or event.subscriber_id
            if subscriber_id:
                try:
                    from app.models.subscriber import Subscriber

                    subscriber = db.get(Subscriber, subscriber_id)
                    if subscriber:
                        context["subscriber_name"] = (
                            subscriber.name or "Valued Customer"
                        )
                except Exception:
                    logger.warning(
                        "Failed to resolve subscriber name (subscriber_id=%s)",
                        subscriber_id,
                        exc_info=True,
                    )

        if event.event_type == EventType.payment_received:
            payment_id = event.payload.get("payment_id")
            if payment_id:
                try:
                    from app.models.billing import Payment, PaymentStatus
                    from app.services.billing.payment_receipt_identity import (
                        payment_receipt_path,
                        payment_receipt_reference,
                    )
                    from app.services.branding_config import get_brand
                    from app.services.common import coerce_uuid

                    payment = db.get(Payment, coerce_uuid(payment_id))
                    if (
                        payment is not None
                        and payment.status == PaymentStatus.succeeded
                        and payment.is_active
                        and (
                            event.account_id is None
                            or payment.account_id == event.account_id
                        )
                    ):
                        app_url = str(get_brand().get("app_url") or "").rstrip("/")
                        context.setdefault(
                            "receipt_number",
                            payment_receipt_reference(
                                payment.id, payment.receipt_number
                            ),
                        )
                        context.setdefault(
                            "receipt_url",
                            f"{app_url}{payment_receipt_path(payment.id)}",
                        )
                except Exception:
                    logger.warning(
                        "Failed to resolve payment receipt context (payment_id=%s)",
                        payment_id,
                        exc_info=True,
                    )

        if event.invoice_id and not {
            "invoice_number",
            "amount",
            "due_date",
        }.issubset(context):
            try:
                from app.models.billing import Invoice

                invoice = db.get(Invoice, event.invoice_id)
                if invoice:
                    context.setdefault("invoice_number", invoice.invoice_number or "")
                    if invoice.total is not None:
                        context.setdefault("amount", f"₦{invoice.total:,.2f}")
                    if invoice.due_at:
                        context.setdefault(
                            "due_date", invoice.due_at.strftime("%b %d, %Y")
                        )
            except Exception:
                logger.warning(
                    "Failed to resolve invoice details (invoice_id=%s)",
                    event.invoice_id,
                    exc_info=True,
                )

        if event.invoice_id:
            from app.services.branding_config import get_brand

            app_url = str(get_brand().get("app_url") or "").rstrip("/")
            context.setdefault(
                "invoice_url", f"{app_url}/portal/billing/invoices/{event.invoice_id}"
            )

        if event.subscription_id:
            try:
                from app.models.catalog import CatalogOffer, Subscription

                subscription = db.get(Subscription, event.subscription_id)
                if subscription:
                    offer = db.get(CatalogOffer, subscription.offer_id)
                    if offer:
                        context.setdefault("offer_name", offer.name or "")
                        context.setdefault("plan_name", offer.name or "")
            except Exception:
                logger.warning(
                    "Failed to resolve subscription details (subscription_id=%s)",
                    event.subscription_id,
                    exc_info=True,
                )

        if event.service_order_id:
            context.setdefault("service_order_id", str(event.service_order_id))
        elif "service_order_id" in context:
            context["service_order_id"] = str(context["service_order_id"])

        if "old_offer" in context:
            context.setdefault("old_offer_name", context["old_offer"])
        if "new_offer" in context:
            context.setdefault("new_offer_name", context["new_offer"])

        if "updated_fields" in context:
            context["updated_fields"] = context["updated_fields"].strip("[]")

        if "amount" in context and not context["amount"].startswith("₦"):
            try:
                from decimal import Decimal, InvalidOperation

                amount = Decimal(context["amount"])
                context["amount"] = f"₦{amount:,.2f}"
            except (InvalidOperation, ValueError):
                pass

        if "renewed_through" in context:
            try:
                renewed_through = datetime.fromisoformat(context["renewed_through"])
                context["renewed_through"] = renewed_through.strftime("%B %d, %Y")
            except ValueError:
                pass

        context.setdefault("device_serial", context.get("serial_number", ""))
        context.setdefault("location", context.get("olt_name", ""))
        context.setdefault("portal_url", "/portal")
        context.setdefault("subscriber_name", "Valued Customer")
        context.setdefault("offer_name", context.get("plan_name", "your service"))
        context.setdefault("old_offer_name", "your current plan")
        context.setdefault("new_offer_name", "your updated plan")
        context.setdefault("updated_fields", "profile details")
        return context

    def _render_text(self, text: str, context: dict[str, str]) -> str:
        rendered = text
        for key, value in context.items():
            rendered = rendered.replace(f"{{{key}}}", value)
        return rendered

    def _render_subject(
        self,
        template: NotificationTemplate | None,
        spec: EventNotificationSpec,
        context: dict[str, str],
    ) -> str:
        return self._render_text(
            (template.subject if template and template.subject else spec.subject),
            context,
        )

    def _render_body(
        self,
        template: NotificationTemplate | None,
        spec: EventNotificationSpec,
        context: dict[str, str],
    ) -> str:
        return self._render_text(
            (template.body if template and template.body else spec.body), context
        )
