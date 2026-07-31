from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.billing import Invoice, InvoiceLine, TaxApplication
from app.models.catalog import BillingCycle, SubscriptionStatus
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.durable_timer import DurableTimer, TimerStatus
from app.models.subscription_engine import SettingValueType
from app.services import advance_renewal_invoicing as renewal
from app.services.billing_automation import (
    PostpaidChargeComponentPreview,
    PostpaidChargePreview,
    PostpaidChargePreviewDisposition,
    RecurringChargeComponentKind,
)
from app.services.owner_commands import CommandContext
from app.services.settings_cache import SettingsCache
from app.services.web_system_config import save_billing_config


def _set_config(db, *, enabled: bool, days: int | None) -> None:
    for key in ("renewal_invoice_notice_enabled", "renewal_invoice_notice_days"):
        row = (
            db.query(DomainSetting)
            .filter_by(domain=SettingDomain.billing, key=key)
            .first()
        )
        if row:
            db.delete(row)
    db.flush()
    db.add(
        DomainSetting(
            domain=SettingDomain.billing,
            key="renewal_invoice_notice_enabled",
            value_type=SettingValueType.boolean,
            value_text="true" if enabled else "false",
            value_json=enabled,
        )
    )
    if days is not None:
        db.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="renewal_invoice_notice_days",
                value_type=SettingValueType.integer,
                value_text=str(days),
                value_json=days,
            )
        )
    db.commit()
    SettingsCache.invalidate_domain(SettingDomain.billing.value)


def test_advance_renewal_is_disabled_with_no_default_day(db_session):
    config = renewal.resolve_notice_configuration(db_session)

    assert config.state is renewal.RenewalNoticeConfigurationState.disabled
    assert config.days_before is None


def test_enabled_advance_renewal_requires_explicit_days(db_session):
    _set_config(db_session, enabled=True, days=None)

    config = renewal.resolve_notice_configuration(db_session)

    assert config.state is renewal.RenewalNoticeConfigurationState.invalid
    assert config.days_before is None


def test_billing_settings_reject_enabled_notice_without_days(db_session):
    with pytest.raises(ValueError, match="Notice Days is required"):
        save_billing_config(
            db_session,
            {
                "renewal_invoice_notice_enabled": "true",
                "renewal_invoice_notice_days": "",
            },
        )


def test_advance_invoice_uses_future_boundary_and_replays(
    db_session, subscription, monkeypatch
):
    evaluated_at = datetime(2026, 8, 25, 9, tzinfo=UTC)
    period_start = datetime(2026, 9, 1, tzinfo=UTC)
    period_end = datetime(2026, 10, 1, tzinfo=UTC)
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = period_start
    subscription.end_at = None
    subscription_id = subscription.id
    account_id = subscription.subscriber_id
    db_session.commit()
    _set_config(db_session, enabled=True, days=7)
    timer_command_id = uuid4()
    timer = DurableTimer(
        owner=renewal.ADVANCE_RENEWAL_TIMER_OWNER,
        entity_kind="subscription",
        entity_id=subscription_id,
        purpose=renewal.ADVANCE_RENEWAL_TIMER_PURPOSE,
        generation=1,
        due_at=evaluated_at,
        output_event_type=renewal.ADVANCE_RENEWAL_TIMER_TRIGGER,
        status=TimerStatus.fired,
        fired_at=evaluated_at,
        command_id=timer_command_id,
        correlation_id=timer_command_id,
    )
    db_session.add(timer)
    db_session.flush()
    timer_id = timer.id
    db_session.commit()

    preview = PostpaidChargePreview(
        subscription_id=subscription_id,
        account_id=account_id,
        period_start=period_start,
        period_end=period_end,
        currency="NGN",
        net_amount=Decimal("1000.00"),
        tax_amount=Decimal("0.00"),
        gross_amount=Decimal("1000.00"),
        billing_cycle=BillingCycle.monthly,
        tax_application=TaxApplication.exempt,
        tax_rate_percent=Decimal("0"),
        tax_rate_id=None,
        disposition=PostpaidChargePreviewDisposition.comparable,
        components=(
            PostpaidChargeComponentPreview(
                kind=RecurringChargeComponentKind.base_service,
                component_key="base_service",
                quantity=Decimal("1"),
                unit_price=Decimal("1000.00"),
                net_amount=Decimal("1000.00"),
                tax_amount=Decimal("0.00"),
                gross_amount=Decimal("1000.00"),
            ),
        ),
        issues=(),
    )
    monkeypatch.setattr(
        renewal,
        "preview_postpaid_recurring_charge",
        lambda *_args, **_kwargs: preview,
    )
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(
        renewal,
        "emit_event",
        lambda *_args, **kwargs: emitted.append(kwargs),
    )

    def _command() -> renewal.GenerateAdvanceRenewalInvoiceCommand:
        command_id = uuid4()
        return renewal.GenerateAdvanceRenewalInvoiceCommand(
            context=CommandContext.system(
                actor="test:scheduler",
                scope=renewal.ADVANCE_RENEWAL_WRITE_SCOPE,
                reason="test advance renewal",
                command_id=command_id,
                correlation_id=command_id,
                idempotency_key=f"advance:{subscription_id}:{period_start.isoformat()}",
            ),
            subscription_id=subscription_id,
            evaluated_at=evaluated_at,
            timer_id=timer_id,
            timer_generation=1,
        )

    first_command = _command()
    first = renewal.generate_advance_renewal_invoice(db_session, first_command)
    second_command = _command()
    second = renewal.generate_advance_renewal_invoice(db_session, second_command)

    invoice = db_session.get(Invoice, first.invoice_id)
    assert first.disposition is renewal.AdvanceRenewalInvoiceDisposition.created
    assert second.disposition is renewal.AdvanceRenewalInvoiceDisposition.replayed
    assert second.invoice_id == first.invoice_id
    assert invoice.issued_at.replace(tzinfo=UTC) == evaluated_at
    assert invoice.due_at.replace(tzinfo=UTC) == period_start
    assert invoice.billing_period_start.replace(tzinfo=UTC) == period_start
    assert invoice.billing_period_end.replace(tzinfo=UTC) == period_end
    persisted_boundary = db_session.get(
        type(subscription), subscription_id
    ).next_billing_at
    assert persisted_boundary.replace(tzinfo=UTC) == period_start
    assert (
        db_session.query(InvoiceLine).filter_by(subscription_id=subscription.id).count()
        == 1
    )
    assert len(emitted) == 1


def test_subscription_timer_uses_exact_configured_boundary(db_session, subscription):
    boundary = datetime(2026, 9, 1, 9, tzinfo=UTC)
    subscription.status = SubscriptionStatus.active
    subscription.next_billing_at = boundary
    subscription_id = subscription.id
    db_session.commit()
    _set_config(db_session, enabled=True, days=7)

    command_id = uuid4()
    outcome = renewal.schedule_advance_renewal_timer(
        db_session,
        renewal.ScheduleAdvanceRenewalTimerCommand(
            context=CommandContext.system(
                actor="test:lifecycle",
                scope=renewal.ADVANCE_RENEWAL_WRITE_SCOPE,
                reason="test subscription timer",
                command_id=command_id,
                correlation_id=command_id,
                idempotency_key=f"advance-timer:{subscription_id}:{boundary.isoformat()}",
            ),
            subscription_id=subscription_id,
        ),
    )

    assert outcome.scheduled is True
    assert outcome.due_at == boundary - timedelta(days=7)
