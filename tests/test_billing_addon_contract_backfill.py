"""Recurring add-on structural capture and owner-output chain (ADR 0007)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.billing import LedgerEntry, LedgerEntryType, LedgerSource
from app.models.billing_contract import (
    AccountingTreatment,
    BillingContractLine,
    BillingContractSourceKind,
    BillingContractVersion,
    BillingContractVersionStatus,
    BillingObligation,
    BillingRecordAuthority,
    CadenceAlignment,
    ChargeComponent,
    CollectionTiming,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.models.billing_shadow_verification import BillingShadowDeliveryEvidence
from app.models.catalog import (
    AddOn,
    AddOnPrice,
    BillingCycle,
    OfferAddOn,
    PriceType,
    SubscriptionAddOn,
    SubscriptionStatus,
)
from app.models.durable_timer import DurableTimer, TimerStatus
from app.models.event_store import EventStore
from app.models.owner_output import OwnerOutputReceipt
from app.models.sales import SalesOrder, SalesOrderLine
from app.services import customer_portal_flow_addons as customer_addons
from app.services.billing.addon_contract_backfill import (
    AddonContractBackfillError,
    BillingAddonContractBackfill,
    CaptureRecurringAddonBackfillCommand,
)
from app.services.billing.cadence import BillingCadence
from app.services.billing.contracts import (
    BillingContracts,
    ContractLineInput,
    RecordContractVersionCommand,
    RecurringAddonPurchaseTermSnapshot,
)
from app.services.events.handlers.owner_session import owner_session
from app.services.owner_commands import CommandContext
from app.services.runtime_durable_timers import fire_due_timers


def _context(key: str) -> CommandContext:
    command_id = uuid4()
    return CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor="user:pytest",
        scope="billing:addon-contract-backfill",
        reason="pytest recurring add-on contract capture",
        idempotency_key=key,
    )


def _cadence() -> BillingCadence:
    return BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=IntervalUnit.month,
        rate_quantity=Decimal("1"),
        service_interval_unit=IntervalUnit.month,
        service_interval_count=1,
        invoice_interval_unit=IntervalUnit.month,
        invoice_interval_count=1,
        collection_timing=CollectionTiming.arrears,
        alignment=CadenceAlignment.contract_anniversary,
        timezone_name="Africa/Lagos",
        proration_policy=ProrationPolicy.none,
    )


def _source_rows(
    db,
    *,
    subscriber_id,
    subscription_id,
    addon_started_at: datetime,
    addon_ends_at: datetime | None = None,
    second_price: bool = False,
):
    order = SalesOrder(
        subscriber_id=subscriber_id,
        order_number=f"SO-ADDON-{uuid4().hex[:10]}",
    )
    db.add(order)
    db.flush()
    line = SalesOrderLine(
        sales_order_id=order.id,
        description="Fiber service",
        quantity=Decimal("1"),
        unit_price=Decimal("25000.00"),
        amount=Decimal("25000.00"),
    )
    addon = AddOn(name="Static public IP", is_active=True)
    db.add_all((line, addon))
    db.flush()
    price = AddOnPrice(
        add_on_id=addon.id,
        price_type=PriceType.recurring,
        amount=Decimal("2000.00"),
        currency="NGN",
        billing_cycle=BillingCycle.monthly,
        is_active=True,
    )
    subscription_addon = SubscriptionAddOn(
        subscription_id=subscription_id,
        add_on_id=addon.id,
        quantity=2,
        start_at=addon_started_at,
        end_at=addon_ends_at,
    )
    db.add_all((price, subscription_addon))
    if second_price:
        db.add(
            AddOnPrice(
                add_on_id=addon.id,
                price_type=PriceType.recurring,
                amount=Decimal("2500.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            )
        )
    db.flush()
    captured = (order.id, line.id, subscription_addon.id)
    db.commit()
    return captured


def _record_base_contract(
    db,
    *,
    subscriber_id,
    subscription_id,
    sales_order_line_id,
):
    return BillingContracts.record_version(
        db,
        RecordContractVersionCommand(
            account_id=subscriber_id,
            subscription_id=subscription_id,
            source_kind=BillingContractSourceKind.sales_order_line,
            source_id=sales_order_line_id,
            starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            contracted_price=Decimal("25000.00"),
            currency="NGN",
            cadence=_cadence(),
            lines=(
                ContractLineInput(
                    charge_component=ChargeComponent.recurring_service,
                    component_key=str(sales_order_line_id),
                    description="Fiber service",
                    quantity=Decimal("1"),
                    unit_price=Decimal("25000.00"),
                    currency="NGN",
                    accounting_treatment=AccountingTreatment.receivable,
                ),
            ),
        ),
        context=_context(f"pytest:base:{subscription_id}"),
    )


def test_capture_versions_exact_addon_and_drives_shadow_obligations(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    sales_order_id, sales_order_line_id, subscription_addon_id = _source_rows(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        addon_started_at=datetime(2026, 1, 15, tzinfo=UTC),
    )
    base = _record_base_contract(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        sales_order_line_id=sales_order_line_id,
    )

    preview = BillingAddonContractBackfill.preview(
        db_session,
        subscription_id=subscription_id,
        period_index=1,
    )
    assert preview.sales_order_id == sales_order_id
    assert preview.current_contract_version_id == base.version_id
    assert preview.change_required is True
    assert [item.subscription_add_on_id for item in preview.terms] == [
        subscription_addon_id
    ]
    assert preview.terms[0].quantity == Decimal("2")
    assert preview.terms[0].unit_price == Decimal("2000.00")
    db_session.commit()

    key = f"pytest:addon-capture:{subscription_id}"
    result = BillingAddonContractBackfill.capture(
        db_session,
        CaptureRecurringAddonBackfillCommand(
            subscription_id=subscription_id,
            period_index=1,
            preview_fingerprint=preview.fingerprint,
        ),
        context=_context(key),
    )

    assert result.replayed is False
    assert result.recurring_addon_count == 1
    versions = list(
        db_session.execute(
            select(BillingContractVersion)
            .where(BillingContractVersion.contract_id == base.contract_id)
            .order_by(BillingContractVersion.version)
        )
        .scalars()
        .all()
    )
    assert len(versions) == 2
    assert versions[0].status is BillingContractVersionStatus.effective
    assert versions[0].ends_at is None
    assert versions[1].status is BillingContractVersionStatus.draft
    assert versions[1].authority is BillingRecordAuthority.shadow
    assert versions[1].source_kind is BillingContractSourceKind.migration_backfill
    assert versions[1].starts_at.replace(tzinfo=UTC) == preview.target_period.starts_at

    lines = list(
        db_session.execute(
            select(BillingContractLine).where(
                BillingContractLine.contract_version_id == versions[1].id
            )
        )
        .scalars()
        .all()
    )
    assert {(line.charge_component, line.component_key) for line in lines} == {
        (ChargeComponent.recurring_service, str(sales_order_line_id)),
        (ChargeComponent.addon, str(subscription_addon_id)),
    }
    addon_line = next(
        line for line in lines if line.charge_component is ChargeComponent.addon
    )
    assert addon_line.quantity == Decimal("2")
    assert addon_line.unit_price == Decimal("2000.00")
    assert addon_line.accounting_treatment is AccountingTreatment.receivable
    timer = db_session.execute(
        select(DurableTimer).where(
            DurableTimer.owner == "billing.contracts",
            DurableTimer.entity_id == base.contract_id,
            DurableTimer.purpose == "pending_terms_effective",
            DurableTimer.status == TimerStatus.scheduled,
        )
    ).scalar_one()
    assert timer.due_at.replace(tzinfo=UTC) == preview.target_period.starts_at
    assert db_session.query(BillingObligation).count() == 0
    assert db_session.query(BillingShadowDeliveryEvidence).count() == 0

    captured_event = db_session.execute(
        select(EventStore).where(EventStore.event_id == result.event_id)
    ).scalar_one()
    assert captured_event.payload["output"] == (
        "billing.addon_contract_backfill.captured"
    )
    assert (
        db_session.query(OwnerOutputReceipt)
        .filter(
            OwnerOutputReceipt.consumer == "billing.contracts",
            OwnerOutputReceipt.event_id == result.event_id,
        )
        .count()
        == 1
    )

    due_at = timer.due_at.replace(tzinfo=UTC)
    previous_version_id = versions[0].id
    draft_version_id = versions[1].id
    db_session.commit()
    fired = fire_due_timers(
        db_session,
        now=due_at + timedelta(seconds=1),
        context=_context(f"pytest:fire-backfill:{subscription_id}"),
    )
    assert len(fired) == 1

    db_session.expire_all()
    previous = db_session.get(BillingContractVersion, previous_version_id)
    effective = db_session.get(BillingContractVersion, draft_version_id)
    assert previous.status is BillingContractVersionStatus.superseded
    assert effective.status is BillingContractVersionStatus.effective
    assert previous.ends_at.replace(tzinfo=UTC) == preview.target_period.starts_at
    obligations = list(db_session.execute(select(BillingObligation)).scalars())
    assert len(obligations) == 2
    assert {obligation.charge_component for obligation in obligations} == {
        ChargeComponent.recurring_service,
        ChargeComponent.addon,
    }
    assert all(
        obligation.authority is BillingRecordAuthority.shadow
        for obligation in obligations
    )
    assert db_session.query(BillingShadowDeliveryEvidence).count() == 1
    contract_outputs = [
        event
        for event in db_session.query(EventStore).all()
        if event.payload.get("output") == "billing.contracts.shadow_recorded"
        and event.payload.get("contract_change_kind") == "recurring_addon_backfill"
    ]
    assert len(contract_outputs) == 1
    assert len(contract_outputs[0].payload["obligations"]) == 2

    db_session.commit()
    replay = BillingAddonContractBackfill.capture(
        db_session,
        CaptureRecurringAddonBackfillCommand(
            subscription_id=subscription_id,
            period_index=1,
            preview_fingerprint=preview.fingerprint,
        ),
        context=_context(key),
    )
    assert replay.replayed is True
    assert replay.event_id == result.event_id
    assert (
        db_session.query(EventStore)
        .filter(EventStore.event_id == result.event_id)
        .count()
        == 1
    )
    db_session.commit()
    already_captured = BillingAddonContractBackfill.preview(
        db_session,
        subscription_id=subscription_id,
        period_index=1,
    )
    assert already_captured.change_required is False


def test_partial_period_addon_fails_before_capture(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    _sales_order_id, sales_order_line_id, _subscription_addon_id = _source_rows(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        addon_started_at=datetime(2026, 2, 15, tzinfo=UTC),
    )
    _record_base_contract(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        sales_order_line_id=sales_order_line_id,
    )

    with pytest.raises(AddonContractBackfillError) as raised:
        BillingAddonContractBackfill.preview(
            db_session,
            subscription_id=subscription_id,
            period_index=1,
        )

    assert raised.value.code == ("billing.addon_contract_backfill.partial_period_addon")


def test_live_purchase_joins_the_existing_backfill_draft_before_boundary(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    _sales_order_id, sales_order_line_id, legacy_subscription_addon_id = _source_rows(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        addon_started_at=datetime(2026, 1, 15, tzinfo=UTC),
    )
    base = _record_base_contract(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        sales_order_line_id=sales_order_line_id,
    )
    preview = BillingAddonContractBackfill.preview(
        db_session,
        subscription_id=subscription_id,
        period_index=1,
    )
    db_session.commit()
    BillingAddonContractBackfill.capture(
        db_session,
        CaptureRecurringAddonBackfillCommand(
            subscription_id=subscription_id,
            period_index=1,
            preview_fingerprint=preview.fingerprint,
        ),
        context=_context(f"pytest:mixed-draft:backfill:{subscription_id}"),
    )

    backfill_draft = db_session.execute(
        select(BillingContractVersion).where(
            BillingContractVersion.contract_id == base.contract_id,
            BillingContractVersion.status == BillingContractVersionStatus.draft,
        )
    ).scalar_one()
    first_timer = db_session.execute(
        select(DurableTimer).where(
            DurableTimer.owner == "billing.contracts",
            DurableTimer.entity_id == base.contract_id,
            DurableTimer.status == TimerStatus.scheduled,
        )
    ).scalar_one()
    draft_id = backfill_draft.id
    first_timer_generation = first_timer.generation
    db_session.commit()

    live_subscription_addon_id = uuid4()
    live = BillingContracts.consume_recurring_addon_purchase(
        db_session,
        term=RecurringAddonPurchaseTermSnapshot(
            account_id=account_id,
            subscription_id=subscription_id,
            subscription_add_on_id=live_subscription_addon_id,
            add_on_id=uuid4(),
            add_on_price_id=uuid4(),
            description="Managed router",
            quantity=Decimal("1"),
            unit_price=Decimal("3500.00"),
            currency="NGN",
            purchased_at=datetime(2026, 1, 20, tzinfo=UTC),
            billing_cycle=BillingCycle.monthly,
        ),
        event_id=uuid4(),
        context=_context(f"pytest:mixed-draft:live:{subscription_id}"),
    )
    assert live is not None
    assert live.draft_version_id == draft_id
    assert live.timer_generation == first_timer_generation + 1

    draft = db_session.get(BillingContractVersion, draft_id)
    assert draft.source_kind is BillingContractSourceKind.plan_change
    component_keys = set(
        db_session.execute(
            select(BillingContractLine.component_key).where(
                BillingContractLine.contract_version_id == draft_id
            )
        ).scalars()
    )
    assert component_keys == {
        str(sales_order_line_id),
        str(legacy_subscription_addon_id),
        str(live_subscription_addon_id),
    }

    effective_at = live.effective_at
    db_session.commit()
    fired = fire_due_timers(
        db_session,
        now=effective_at + timedelta(seconds=1),
        context=_context(f"pytest:mixed-draft:fire:{subscription_id}"),
    )
    assert len(fired) == 1
    assert (
        db_session.query(BillingObligation)
        .filter(BillingObligation.contract_version_id == draft_id)
        .count()
        == 3
    )
    live_outputs = [
        event
        for event in db_session.query(EventStore).all()
        if event.payload.get("output") == "billing.contracts.shadow_recorded"
        and event.payload.get("contract_change_kind") == "recurring_addon_purchase"
    ]
    assert len(live_outputs) == 1
    assert live_outputs[0].payload["envelope"]["source_kind"] == "subscription"


def test_ambiguous_recurring_price_fails_closed(db_session, subscriber, subscription):
    account_id = subscriber.id
    subscription_id = subscription.id
    _sales_order_id, sales_order_line_id, _subscription_addon_id = _source_rows(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        addon_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        second_price=True,
    )
    _record_base_contract(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        sales_order_line_id=sales_order_line_id,
    )

    with pytest.raises(AddonContractBackfillError) as raised:
        BillingAddonContractBackfill.preview(
            db_session,
            subscription_id=subscription_id,
            period_index=1,
        )

    assert raised.value.code == (
        "billing.addon_contract_backfill.ambiguous_recurring_price"
    )


def test_live_purchase_reaches_effective_shadow_obligations_through_timer(
    db_session, subscriber, subscription
):
    """One owner output drives every later owner; no comparison repairs drift."""

    account_id = subscriber.id
    subscription_id = subscription.id
    subscription.status = SubscriptionStatus.active
    subscription.start_at = datetime(2026, 1, 1, tzinfo=UTC)

    order = SalesOrder(
        subscriber_id=account_id,
        order_number=f"SO-LIVE-ADDON-{uuid4().hex[:10]}",
    )
    db_session.add(order)
    db_session.flush()
    sale_line = SalesOrderLine(
        sales_order_id=order.id,
        description="Fiber service",
        quantity=Decimal("1"),
        unit_price=Decimal("25000.00"),
        amount=Decimal("25000.00"),
    )
    add_on = AddOn(name="Static public IP", is_active=True)
    db_session.add_all((sale_line, add_on))
    db_session.flush()
    db_session.add_all(
        (
            AddOnPrice(
                add_on_id=add_on.id,
                price_type=PriceType.recurring,
                amount=Decimal("2000.00"),
                currency="NGN",
                billing_cycle=BillingCycle.monthly,
                is_active=True,
            ),
            OfferAddOn(
                offer_id=subscription.offer_id,
                add_on_id=add_on.id,
                min_quantity=1,
                max_quantity=3,
                is_required=False,
            ),
            LedgerEntry(
                account_id=account_id,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("5000.00"),
                currency="NGN",
                memo="Live add-on test funding",
            ),
        )
    )
    sales_order_line_id = sale_line.id
    add_on_id = add_on.id
    db_session.commit()

    base = _record_base_contract(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        sales_order_line_id=sales_order_line_id,
    )
    customer = {
        "account_id": str(account_id),
        "subscriber_id": str(account_id),
    }
    quote = customer_addons.get_addon_quote(
        db_session,
        customer,
        str(subscription_id),
        str(add_on_id),
        1,
    )
    assert quote is not None
    with owner_session(db_session) as owner_db:
        purchase = customer_addons.confirm_addon_purchase(
            owner_db,
            customer_addons.PurchaseAddonCommand(
                account_id=account_id,
                subscription_id=subscription_id,
                add_on_id=add_on_id,
                quantity=1,
                preview_fingerprint=str(quote["preview_fingerprint"]),
            ),
            context=_context(f"pytest:live-addon:{subscription_id}"),
        )

    assert purchase.recurring_terms_event_id is not None
    purchase_receipt = db_session.execute(
        select(OwnerOutputReceipt).where(
            OwnerOutputReceipt.consumer == "billing.contracts",
            OwnerOutputReceipt.event_id == purchase.recurring_terms_event_id,
        )
    ).scalar_one()
    assert purchase_receipt.producer_owner == "financial.addon_purchases"

    versions = list(
        db_session.execute(
            select(BillingContractVersion)
            .where(BillingContractVersion.contract_id == base.contract_id)
            .order_by(BillingContractVersion.version)
        )
        .scalars()
        .all()
    )
    assert [version.status for version in versions] == [
        BillingContractVersionStatus.effective,
        BillingContractVersionStatus.draft,
    ]
    draft = versions[1]
    assert draft.supersedes_id == base.version_id
    draft_lines = list(
        db_session.execute(
            select(BillingContractLine).where(
                BillingContractLine.contract_version_id == draft.id
            )
        ).scalars()
    )
    assert {(line.charge_component, line.component_key) for line in draft_lines} == {
        (ChargeComponent.recurring_service, str(sales_order_line_id)),
        (ChargeComponent.addon, str(purchase.subscription_add_on_id)),
    }
    timer = db_session.execute(
        select(DurableTimer).where(
            DurableTimer.owner == "billing.contracts",
            DurableTimer.entity_id == base.contract_id,
            DurableTimer.purpose == "pending_terms_effective",
            DurableTimer.status == TimerStatus.scheduled,
        )
    ).scalar_one()
    assert timer.expected_source_version == draft.version

    due_at = timer.due_at.replace(tzinfo=UTC)
    db_session.commit()
    fired = fire_due_timers(
        db_session,
        now=due_at + timedelta(seconds=1),
        context=_context(f"pytest:fire-live-addon:{subscription_id}"),
    )
    assert len(fired) == 1

    db_session.expire_all()
    previous = db_session.get(BillingContractVersion, base.version_id)
    effective = db_session.get(BillingContractVersion, draft.id)
    assert previous.status is BillingContractVersionStatus.superseded
    assert effective.status is BillingContractVersionStatus.effective
    assert previous.ends_at.replace(tzinfo=UTC) == due_at
    obligations = list(
        db_session.execute(
            select(BillingObligation).where(
                BillingObligation.contract_version_id == effective.id
            )
        ).scalars()
    )
    assert len(obligations) == 2
    assert {item.charge_component for item in obligations} == {
        ChargeComponent.recurring_service,
        ChargeComponent.addon,
    }
    assert all(item.authority is BillingRecordAuthority.shadow for item in obligations)

    fired_event_id = fired[0].event_id
    assert (
        db_session.query(OwnerOutputReceipt)
        .filter(
            OwnerOutputReceipt.consumer == "billing.contracts",
            OwnerOutputReceipt.event_id == fired_event_id,
        )
        .count()
        == 1
    )
    live_contract_output = next(
        event
        for event in db_session.query(EventStore).all()
        if event.payload.get("output") == "billing.contracts.shadow_recorded"
        and event.payload.get("contract_change_kind") == "recurring_addon_purchase"
    )
    assert live_contract_output.payload["subscription_id"] == str(subscription_id)
    assert live_contract_output.payload["envelope"]["source_kind"] == "subscription"
    assert db_session.query(BillingShadowDeliveryEvidence).count() == 1


def test_changed_quantity_rejects_confirmed_preview(
    db_session, subscriber, subscription
):
    account_id = subscriber.id
    subscription_id = subscription.id
    _sales_order_id, sales_order_line_id, subscription_addon_id = _source_rows(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        addon_started_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    base = _record_base_contract(
        db_session,
        subscriber_id=account_id,
        subscription_id=subscription_id,
        sales_order_line_id=sales_order_line_id,
    )
    preview = BillingAddonContractBackfill.preview(
        db_session,
        subscription_id=subscription_id,
        period_index=1,
    )
    db_session.commit()
    row = db_session.get(SubscriptionAddOn, subscription_addon_id)
    row.quantity = 3
    db_session.commit()

    with pytest.raises(AddonContractBackfillError) as raised:
        BillingAddonContractBackfill.capture(
            db_session,
            CaptureRecurringAddonBackfillCommand(
                subscription_id=subscription_id,
                period_index=1,
                preview_fingerprint=preview.fingerprint,
            ),
            context=_context(f"pytest:stale:{subscription_id}"),
        )

    assert raised.value.code == "billing.addon_contract_backfill.stale_preview"
    versions = (
        db_session.query(BillingContractVersion)
        .filter(BillingContractVersion.contract_id == base.contract_id)
        .all()
    )
    assert len(versions) == 1
