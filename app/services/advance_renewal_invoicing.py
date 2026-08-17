"""Advance renewal invoices anchored to existing service coverage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing import (
    Invoice,
    InvoiceDueDateBasis,
    InvoiceLine,
    InvoiceStatus,
    TaxApplication,
)
from app.models.catalog import BillingMode, Subscription, SubscriptionStatus
from app.models.domain_settings import SettingDomain
from app.models.durable_timer import DurableTimer, TimerStatus
from app.schemas.billing import InvoiceCreate, SystemInvoiceLineCreate
from app.services import settings_spec
from app.services.billing.invoices import InvoiceLines, Invoices
from app.services.billing_automation import (
    RecurringChargeComponentKind,
    preview_postpaid_recurring_charge,
)
from app.services.domain_errors import DomainError
from app.services.enforcement_window import to_local
from app.services.events import emit_event
from app.services.events.types import EventType
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.prepaid_service_coverage import (
    PrepaidCoverageStatus,
    resolve_prepaid_service_coverage,
)
from app.services.prepaid_service_renewals import preview_prepaid_recurring_charge
from app.services.runtime_durable_timers import (
    ScheduleTimerCommand,
    cancel_timer,
    schedule_timer,
)

ADVANCE_RENEWAL_WRITE_SCOPE = "billing:advance-renewal:write"
ADVANCE_RENEWAL_TIMER_PURPOSE = "advance_renewal_invoice_due"
ADVANCE_RENEWAL_TIMER_TRIGGER = "financial.advance_renewal_invoice_due"
ADVANCE_RENEWAL_TIMER_OWNER = "financial.advance_renewal_invoicing"
_GENERATE_COMMAND = OwnerCommandDefinition(
    owner="financial.advance_renewal_invoicing",
    concern="idempotent advance renewal invoice and notification request",
    name="generate_advance_renewal_invoice",
)
_SCHEDULE_COMMAND = OwnerCommandDefinition(
    owner=ADVANCE_RENEWAL_TIMER_OWNER,
    concern="per-subscription advance renewal timer",
    name="schedule_advance_renewal_timer",
)


class RenewalNoticeConfigurationState(StrEnum):
    disabled = "disabled"
    configured = "configured"
    invalid = "invalid"


@dataclass(frozen=True, slots=True)
class RenewalNoticeConfiguration:
    state: RenewalNoticeConfigurationState
    days_before: int | None


class AdvanceRenewalInvoiceDisposition(StrEnum):
    created = "created"
    replayed = "replayed"


@dataclass(frozen=True, slots=True)
class GenerateAdvanceRenewalInvoiceCommand:
    context: CommandContext
    subscription_id: UUID
    evaluated_at: datetime
    timer_id: UUID
    timer_generation: int


@dataclass(frozen=True, slots=True)
class AdvanceRenewalInvoiceOutcome:
    subscription_id: UUID
    invoice_id: UUID
    period_start: datetime
    period_end: datetime
    disposition: AdvanceRenewalInvoiceDisposition
    notification_requested: bool


@dataclass(frozen=True, slots=True)
class ScheduleAdvanceRenewalTimerCommand:
    context: CommandContext
    subscription_id: UUID


@dataclass(frozen=True, slots=True)
class AdvanceRenewalTimerOutcome:
    subscription_id: UUID
    timer_id: UUID | None
    due_at: datetime | None
    scheduled: bool


class AdvanceRenewalInvoiceError(DomainError):
    """Stable failure from the advance renewal owner."""


def _error(suffix: str, message: str, **details: object) -> AdvanceRenewalInvoiceError:
    return AdvanceRenewalInvoiceError(
        code=f"financial.advance_renewal_invoicing.{suffix}",
        message=message,
        details=details,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve_notice_configuration(db: Session) -> RenewalNoticeConfiguration:
    enabled = settings_spec.resolve_value(
        db, SettingDomain.billing, "renewal_invoice_notice_enabled"
    )
    if enabled is not True:
        return RenewalNoticeConfiguration(
            state=RenewalNoticeConfigurationState.disabled, days_before=None
        )
    raw_days = settings_spec.resolve_value(
        db, SettingDomain.billing, "renewal_invoice_notice_days"
    )
    if not isinstance(raw_days, int) or not 0 <= raw_days <= 90:
        return RenewalNoticeConfiguration(
            state=RenewalNoticeConfigurationState.invalid, days_before=None
        )
    return RenewalNoticeConfiguration(
        state=RenewalNoticeConfigurationState.configured,
        days_before=raw_days,
    )


def _renewal_boundary(
    db: Session, subscription: Subscription, *, evaluated_at: datetime
) -> datetime:
    projected = subscription.next_billing_at
    if projected is None:
        raise _error(
            "missing_renewal_boundary",
            "Advance renewal invoicing requires a billing anchor.",
            subscription_id=str(subscription.id),
        )
    boundary = _utc(projected)
    if subscription.billing_mode is BillingMode.prepaid:
        decision = resolve_prepaid_service_coverage(
            db, [subscription], as_of=evaluated_at
        )[subscription.id]
        if (
            decision.status is not PrepaidCoverageStatus.covered
            or decision.evidence is None
        ):
            raise _error(
                "coverage_ambiguous",
                "Prepaid renewal requires current authoritative coverage evidence.",
                subscription_id=str(subscription.id),
            )
        evidence_end = _utc(decision.evidence.ends_at)
        if evidence_end != boundary:
            raise _error(
                "coverage_anchor_drift",
                "Coverage evidence and the billing anchor disagree.",
                subscription_id=str(subscription.id),
                evidence_end=evidence_end.isoformat(),
                projected_boundary=boundary.isoformat(),
            )
    return boundary


def schedule_advance_renewal_timer(
    db: Session, command: ScheduleAdvanceRenewalTimerCommand
) -> AdvanceRenewalTimerOutcome:
    """Create or cancel one exact subscription's durable notice timer."""

    def operation() -> AdvanceRenewalTimerOutcome:
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        configuration = resolve_notice_configuration(db)
        if (
            subscription is None
            or subscription.status is not SubscriptionStatus.active
            or subscription.next_billing_at is None
            or configuration.state is not RenewalNoticeConfigurationState.configured
        ):
            cancel_timer(
                db,
                owner=ADVANCE_RENEWAL_TIMER_OWNER,
                entity_kind="subscription",
                entity_id=command.subscription_id,
                purpose=ADVANCE_RENEWAL_TIMER_PURPOSE,
            )
            return AdvanceRenewalTimerOutcome(
                subscription_id=command.subscription_id,
                timer_id=None,
                due_at=None,
                scheduled=False,
            )
        assert configuration.days_before is not None
        boundary = _utc(subscription.next_billing_at)
        due_at = boundary - timedelta(days=configuration.days_before)
        timer = schedule_timer(
            db,
            ScheduleTimerCommand(
                owner=ADVANCE_RENEWAL_TIMER_OWNER,
                entity_kind="subscription",
                entity_id=subscription.id,
                purpose=ADVANCE_RENEWAL_TIMER_PURPOSE,
                due_at=due_at,
                output_event_type=ADVANCE_RENEWAL_TIMER_TRIGGER,
            ),
            context=command.context,
        )
        return AdvanceRenewalTimerOutcome(
            subscription_id=subscription.id,
            timer_id=timer.id,
            due_at=due_at,
            scheduled=True,
        )

    return execute_owner_command(
        db,
        definition=_SCHEDULE_COMMAND,
        context=command.context,
        operation=operation,
    )


def _billing_line_key(
    subscription_id: UUID, period_start: datetime, period_end: datetime, component: str
) -> str:
    return (
        f"subscription:{subscription_id}:{period_start.isoformat()}:"
        f"{period_end.isoformat()}:advance-renewal:{component}"
    )


def generate_advance_renewal_invoice(
    db: Session, command: GenerateAdvanceRenewalInvoiceCommand
) -> AdvanceRenewalInvoiceOutcome:
    evaluated_at = _utc(command.evaluated_at)

    def operation() -> AdvanceRenewalInvoiceOutcome:
        configuration = resolve_notice_configuration(db)
        if configuration.state is not RenewalNoticeConfigurationState.configured:
            raise _error(
                "configuration_unavailable",
                "Advance renewal invoicing is disabled or unconfigured.",
            )
        subscription = db.scalar(
            select(Subscription)
            .where(Subscription.id == command.subscription_id)
            .with_for_update()
        )
        if subscription is None or subscription.status is not SubscriptionStatus.active:
            raise _error(
                "subscription_not_eligible",
                "Advance renewal requires an active subscription.",
                subscription_id=str(command.subscription_id),
            )
        timer = db.get(DurableTimer, command.timer_id)
        if (
            timer is None
            or timer.owner != ADVANCE_RENEWAL_TIMER_OWNER
            or timer.entity_kind != "subscription"
            or timer.entity_id != subscription.id
            or timer.purpose != ADVANCE_RENEWAL_TIMER_PURPOSE
            or timer.output_event_type != ADVANCE_RENEWAL_TIMER_TRIGGER
            or timer.generation != command.timer_generation
            or timer.status is not TimerStatus.fired
        ):
            raise _error(
                "stale_timer",
                "Advance renewal invoicing requires the current fired subscription timer.",
                subscription_id=str(subscription.id),
                timer_id=str(command.timer_id),
            )
        period_start = _renewal_boundary(db, subscription, evaluated_at=evaluated_at)
        assert configuration.days_before is not None
        expected_notice_date = to_local(db, period_start).date() - timedelta(
            days=configuration.days_before
        )
        if expected_notice_date != to_local(db, evaluated_at).date():
            raise _error(
                "outside_notice_date",
                "Subscription is outside the configured renewal notice date.",
                subscription_id=str(subscription.id),
            )
        terminal_end = _utc(subscription.end_at) if subscription.end_at else None
        if terminal_end is not None and terminal_end <= period_start:
            raise _error(
                "terminal_subscription",
                "A terminal subscription cannot receive a renewal invoice.",
                subscription_id=str(subscription.id),
            )

        if subscription.billing_mode is BillingMode.prepaid:
            prepaid_preview = preview_prepaid_recurring_charge(
                db, subscription_id=subscription.id, as_of=evaluated_at
            )
            preview_period_start = prepaid_preview.period_start
            period_end = prepaid_preview.period_end
            currency = prepaid_preview.currency
            components: tuple[
                tuple[str, str, Decimal, Decimal, TaxApplication, UUID | None], ...
            ] = (
                (
                    "base",
                    "base_subscription",
                    Decimal("1"),
                    prepaid_preview.gross_amount,
                    TaxApplication.exempt,
                    None,
                ),
            )
        else:
            postpaid_preview = preview_postpaid_recurring_charge(
                db, subscription_id=subscription.id, as_of=evaluated_at
            )
            preview_period_start = postpaid_preview.period_start
            period_end = postpaid_preview.period_end
            currency = postpaid_preview.currency
            components = tuple(
                (
                    "base"
                    if component.kind is RecurringChargeComponentKind.base_service
                    else component.component_key,
                    component.kind.value,
                    component.quantity,
                    component.unit_price,
                    postpaid_preview.tax_application,
                    postpaid_preview.tax_rate_id,
                )
                for component in postpaid_preview.components
            )
        if period_start != preview_period_start:
            raise _error(
                "period_drift",
                "The charge owner returned a different renewal boundary.",
                subscription_id=str(subscription.id),
            )

        base_key = _billing_line_key(subscription.id, period_start, period_end, "base")
        existing_line = db.scalar(
            select(InvoiceLine).where(
                InvoiceLine.billing_line_key == base_key,
                InvoiceLine.is_active.is_(True),
            )
        )
        if existing_line is not None:
            existing_invoice = db.get(Invoice, existing_line.invoice_id)
            if existing_invoice is None:
                raise _error("invoice_drift", "Existing billing line has no invoice.")
            return AdvanceRenewalInvoiceOutcome(
                subscription_id=subscription.id,
                invoice_id=existing_invoice.id,
                period_start=period_start,
                period_end=period_end,
                disposition=AdvanceRenewalInvoiceDisposition.replayed,
                notification_requested=False,
            )

        invoice = db.scalar(
            select(Invoice)
            .where(
                Invoice.account_id == subscription.subscriber_id,
                Invoice.billing_period_start == period_start,
                Invoice.billing_period_end == period_end,
                Invoice.is_active.is_(True),
            )
            .with_for_update()
        )
        if invoice is None:
            invoice = Invoices.stage_system_invoice(
                db,
                InvoiceCreate(
                    account_id=subscription.subscriber_id,
                    status=InvoiceStatus.issued,
                    currency=currency,
                    issued_at=evaluated_at,
                    due_at=period_start,
                    due_date_basis=InvoiceDueDateBasis.contract_terms,
                    due_date_basis_ref=(
                        f"subscription:{subscription.id}:period:{period_start.isoformat()}"
                    ),
                    due_date_policy_version="advance-renewal-terms-v1",
                    billing_period_start=period_start,
                    billing_period_end=period_end,
                ),
                reason="advance_renewal_invoice",
            )
        elif invoice.currency != currency:
            raise _error(
                "currency_conflict",
                "Existing future invoice has a different currency.",
                invoice_id=str(invoice.id),
            )

        offer_name = subscription.offer.name if subscription.offer else "Service"
        for (
            component_key,
            kind,
            quantity,
            unit_price,
            tax_application,
            tax_rate_id,
        ) in components:
            key = _billing_line_key(
                subscription.id, period_start, period_end, str(component_key)
            )
            description = (
                f"{offer_name} ({period_start.date()} - {period_end.date()})"
                if kind == RecurringChargeComponentKind.base_service.value
                or kind == "base_subscription"
                else f"{offer_name} add-on ({period_start.date()} - {period_end.date()})"
            )
            InvoiceLines.stage_system_line(
                db,
                SystemInvoiceLineCreate(
                    invoice_id=invoice.id,
                    subscription_id=subscription.id,
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                    tax_rate_id=tax_rate_id,
                    tax_application=tax_application,
                    metadata_={
                        "kind": kind,
                        "billing_period_start": period_start.isoformat(),
                        "billing_period_end": period_end.isoformat(),
                        "advance_renewal": True,
                    },
                    billing_line_key=key,
                ),
                reason="advance_renewal_invoice",
            )
        db.flush()
        emit_event(
            db,
            EventType.subscription_renewal_invoice_ready,
            {
                "subscription_id": str(subscription.id),
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number or "",
                "amount": str(invoice.total),
                "currency": invoice.currency,
                "due_date": period_start.date().isoformat(),
                "renewal_period_start": period_start.date().isoformat(),
                "renewal_period_end": period_end.date().isoformat(),
            },
            subscription_id=subscription.id,
            invoice_id=invoice.id,
            account_id=subscription.subscriber_id,
        )
        return AdvanceRenewalInvoiceOutcome(
            subscription_id=subscription.id,
            invoice_id=invoice.id,
            period_start=period_start,
            period_end=period_end,
            disposition=AdvanceRenewalInvoiceDisposition.created,
            notification_requested=True,
        )

    return execute_owner_command(
        db,
        definition=_GENERATE_COMMAND,
        context=command.context,
        operation=operation,
    )
