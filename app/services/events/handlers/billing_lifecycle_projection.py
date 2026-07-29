"""Deliver the Phase 1 shadow billing owner-output chain.

This adapter owns no billing decision. It converts versioned payloads into the
typed commands of ``billing.contracts`` and ``billing.obligations``; each owner
commits its effect with a unique delivery receipt and emits the next output.
All records remain shadow while ADR 0007's cutover gates are open.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import BillingCycle, BillingMode
from app.services.events.handlers.owner_session import owner_session as _owner_session
from app.services.events.owner_outputs import (
    invalid_output_payload,
    require_output_records,
    require_output_text,
)
from app.services.events.types import Event, EventType

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.custom,
        EventType.account_credit_deposited,
        EventType.payment_received,
    }
)

_FULFILLMENT_OUTPUT = "sales.fulfillment.funding_applied"
_ADDON_BACKFILL_OUTPUT = "billing.addon_contract_backfill.captured"
_LIVE_ADDON_PURCHASE_OUTPUT = "billing.contract_terms.recurring_addon_added"
_PENDING_TERMS_EFFECTIVE_TRIGGER = "billing.contracts.pending_terms_effective_due"
_CONTRACT_OUTPUT = "billing.contracts.shadow_recorded"
_OBLIGATION_OUTPUT = "billing.obligations.shadow_scheduled"
_WALLED_HEALING_TRIGGER = "financial.walled_account_healing_due"


class BillingLifecycleProjectionHandler:
    """Route shadow billing outputs to their exact receipted owners."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type in {
            EventType.account_credit_deposited,
            EventType.payment_received,
        }:
            self._schedule_walled_account_healing(db, event)
            return
        if (
            event.event_type is EventType.custom
            and event.payload.get("trigger") == _WALLED_HEALING_TRIGGER
        ):
            self._consume_walled_account_healing(db, event)
            return
        output = event.payload.get("output")
        trigger = event.payload.get("trigger")
        if output == _FULFILLMENT_OUTPUT:
            self._record_contracts(db, event)
        elif output == _ADDON_BACKFILL_OUTPUT:
            self._record_addon_contract(db, event)
        elif output == _LIVE_ADDON_PURCHASE_OUTPUT:
            self._record_live_addon_purchase(db, event)
        elif trigger == _PENDING_TERMS_EFFECTIVE_TRIGGER:
            self._activate_pending_terms(db, event)
        elif output == _CONTRACT_OUTPUT:
            self._schedule_obligations(db, event)
        elif output == _OBLIGATION_OUTPUT:
            self._record_verification_evidence(db, event)
        # Every other custom payload belongs to another adapter.

    @staticmethod
    def _context(event: Event, scope: str, *, reason: str | None = None):
        from app.services.owner_commands import CommandContext

        return CommandContext.system(
            actor=str(event.actor or "billing.lifecycle_projection"),
            scope=scope,
            reason=str(
                reason
                or event.payload.get("output")
                or event.payload.get("trigger")
                or event.event_type.value
            ),
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
            idempotency_key=f"event:{event.event_id}",
        )

    def _schedule_walled_account_healing(self, db: Session, event: Event) -> None:
        if event.account_id is None:
            raise invalid_output_payload(
                consumer="financial.walled_account_healing",
                event_id=event.event_id,
                event_type=event.event_type.value,
                field="account_id",
                reason="required_uuid",
            )
        from app.services.billing.unwall_paid_accounts import (
            schedule_walled_account_healing,
        )

        with _owner_session(db) as owner_db:
            schedule_walled_account_healing(
                owner_db,
                account_id=event.account_id,
                due_at=datetime.now(UTC) + timedelta(minutes=2),
                context=self._context(
                    event,
                    str(event.account_id),
                    reason="recheck exact access state after settled funding",
                ),
            )

    def _consume_walled_account_healing(self, db: Session, event: Event) -> None:
        consumer = "financial.walled_account_healing"
        account_text = require_output_text(
            event.payload,
            "entity_id",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_WALLED_HEALING_TRIGGER,
        )
        timer_text = require_output_text(
            event.payload,
            "timer_id",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_WALLED_HEALING_TRIGGER,
        )
        generation_raw = event.payload.get("generation")
        try:
            account_id = UUID(account_text)
            timer_id = UUID(timer_text)
        except ValueError as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_WALLED_HEALING_TRIGGER,
                field="timer_identity",
                reason="invalid_uuid_or_generation",
            ) from exc
        if not isinstance(generation_raw, int) or isinstance(generation_raw, bool):
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_WALLED_HEALING_TRIGGER,
                field="generation",
                reason="expected_integer",
            )
        generation = generation_raw

        from app.services.billing.unwall_paid_accounts import (
            consume_walled_account_healing_due,
        )

        with _owner_session(db) as owner_db:
            consume_walled_account_healing_due(
                owner_db,
                account_id=account_id,
                timer_id=timer_id,
                generation=generation,
                event_id=event.event_id,
                context=self._context(
                    event,
                    account_text,
                    reason=_WALLED_HEALING_TRIGGER,
                ),
            )

    @staticmethod
    def _uuid(
        value: str,
        *,
        field: str,
        event: Event,
        consumer: str,
    ) -> UUID:
        try:
            return UUID(value)
        except (TypeError, ValueError) as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=str(event.payload.get("output") or event.event_type.value),
                field=field,
                reason="invalid_uuid",
            ) from exc

    @staticmethod
    def _decimal(
        value: str,
        *,
        field: str,
        event: Event,
        consumer: str,
    ) -> Decimal:
        try:
            return Decimal(value)
        except Exception as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=str(event.payload.get("output") or event.event_type.value),
                field=field,
                reason="invalid_decimal",
            ) from exc

    @staticmethod
    def _integer(
        value: object,
        *,
        field: str,
        event: Event,
        consumer: str,
    ) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError) as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=str(event.payload.get("output") or event.event_type.value),
                field=field,
                reason="invalid_integer",
            ) from exc

    @staticmethod
    def _producer(
        event: Event,
        expected: str,
        *,
        source_id: UUID,
        consumer: str,
        schema_versions: tuple[int, ...] = (1,),
        source_kind: str = "sales_order",
    ) -> None:
        envelope = event.payload.get("envelope")
        if not isinstance(envelope, Mapping):
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=str(event.payload.get("output") or event.event_type.value),
                field="envelope",
                reason="expected_record",
            )
        schema_version = envelope.get("schema_version")
        if schema_version not in schema_versions:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=str(event.payload.get("output") or event.event_type.value),
                field="envelope.schema_version",
                reason=f"expected_one_of:{schema_versions}",
            )
        expected_fields: tuple[tuple[str, object], ...] = (
            ("producer_owner", expected),
            ("source_kind", source_kind),
            ("source_id", str(source_id)),
        )
        for field, expected_value in expected_fields:
            if envelope.get(field) != expected_value:
                raise invalid_output_payload(
                    consumer=consumer,
                    event_id=event.event_id,
                    event_type=str(
                        event.payload.get("output") or event.event_type.value
                    ),
                    field=f"envelope.{field}",
                    reason=f"expected:{expected_value}",
                )

    def _record_contracts(self, db: Session, event: Event) -> None:
        from app.services.billing.contracts import (
            BillingContracts,
        )

        consumer = "billing.contracts"
        sales_order_text = require_output_text(
            event.payload,
            "sales_order_id",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_FULFILLMENT_OUTPUT,
        )
        sales_order_id = self._uuid(
            sales_order_text,
            field="sales_order_id",
            event=event,
            consumer=consumer,
        )
        self._producer(
            event,
            "sales.fulfillment",
            source_id=sales_order_id,
            consumer=consumer,
        )
        records = require_output_records(
            event.payload,
            "contracts",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_FULFILLMENT_OUTPUT,
        )
        snapshots = tuple(
            self._contract_snapshot(record, event=event, consumer=consumer)
            for record in records
        )
        with _owner_session(db) as owner_db:
            BillingContracts.consume_sales_funding(
                owner_db,
                sales_order_id=sales_order_id,
                snapshots=snapshots,
                event_id=event.event_id,
                context=self._context(event, sales_order_text),
            )

    def _contract_snapshot(
        self,
        record,
        *,
        event: Event,
        consumer: str,
    ):
        from app.services.billing.contracts import SalesFundingContractSnapshot

        def text(field: str) -> str:
            return require_output_text(
                record,
                field,
                consumer=consumer,
                event_id=event.event_id,
                event_type=_FULFILLMENT_OUTPUT,
            )

        starts_at_text = text("starts_at")
        try:
            starts_at = datetime.fromisoformat(starts_at_text)
        except ValueError as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_FULFILLMENT_OUTPUT,
                field="contracts.starts_at",
                reason="invalid_datetime",
            ) from exc
        if starts_at.tzinfo is None:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_FULFILLMENT_OUTPUT,
                field="contracts.starts_at",
                reason="timezone_required",
            )
        try:
            cycle = BillingCycle(text("billing_cycle"))
            mode = BillingMode(text("billing_mode"))
        except ValueError as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_FULFILLMENT_OUTPUT,
                field="contracts.billing_terms",
                reason="unsupported_billing_term",
            ) from exc
        return SalesFundingContractSnapshot(
            sales_order_line_id=self._uuid(
                text("sales_order_line_id"),
                field="contracts.sales_order_line_id",
                event=event,
                consumer=consumer,
            ),
            account_id=self._uuid(
                text("account_id"),
                field="contracts.account_id",
                event=event,
                consumer=consumer,
            ),
            subscription_id=self._uuid(
                text("subscription_id"),
                field="contracts.subscription_id",
                event=event,
                consumer=consumer,
            ),
            starts_at=starts_at,
            description=text("description"),
            quantity=self._decimal(
                text("quantity"),
                field="contracts.quantity",
                event=event,
                consumer=consumer,
            ),
            unit_price=self._decimal(
                text("unit_price"),
                field="contracts.unit_price",
                event=event,
                consumer=consumer,
            ),
            currency=text("currency"),
            billing_cycle=cycle,
            billing_mode=mode,
        )

    def _record_addon_contract(self, db: Session, event: Event) -> None:
        from app.services.billing.cadence import Interval
        from app.services.billing.contracts import (
            BillingContracts,
            RecurringAddonContractTermSnapshot,
        )

        consumer = "billing.contracts"

        def text(record: Mapping[str, object], field: str) -> str:
            return require_output_text(
                record,
                field,
                consumer=consumer,
                event_id=event.event_id,
                event_type=_ADDON_BACKFILL_OUTPUT,
            )

        sales_order_text = text(event.payload, "sales_order_id")
        sales_order_id = self._uuid(
            sales_order_text,
            field="sales_order_id",
            event=event,
            consumer=consumer,
        )
        self._producer(
            event,
            "billing.addon_contract_backfill",
            source_id=sales_order_id,
            consumer=consumer,
        )
        try:
            period_start = datetime.fromisoformat(
                text(event.payload, "target_period_start")
            )
            period_end = datetime.fromisoformat(
                text(event.payload, "target_period_end")
            )
            target_period = Interval(
                starts_at=period_start,
                ends_at=period_end,
            )
        except (TypeError, ValueError) as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_ADDON_BACKFILL_OUTPUT,
                field="target_period",
                reason="invalid_datetime_interval",
            ) from exc
        if period_start.tzinfo is None or period_end.tzinfo is None:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_ADDON_BACKFILL_OUTPUT,
                field="target_period",
                reason="timezone_required",
            )

        def optional_datetime(
            record: Mapping[str, object], field: str
        ) -> datetime | None:
            value = record.get(field)
            if value is None:
                return None
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError as exc:
                raise invalid_output_payload(
                    consumer=consumer,
                    event_id=event.event_id,
                    event_type=_ADDON_BACKFILL_OUTPUT,
                    field=f"terms.{field}",
                    reason="invalid_datetime",
                ) from exc
            if parsed.tzinfo is None:
                raise invalid_output_payload(
                    consumer=consumer,
                    event_id=event.event_id,
                    event_type=_ADDON_BACKFILL_OUTPUT,
                    field=f"terms.{field}",
                    reason="timezone_required",
                )
            return parsed

        records = require_output_records(
            event.payload,
            "terms",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_ADDON_BACKFILL_OUTPUT,
        )
        terms = tuple(
            RecurringAddonContractTermSnapshot(
                subscription_add_on_id=self._uuid(
                    text(record, "subscription_add_on_id"),
                    field="terms.subscription_add_on_id",
                    event=event,
                    consumer=consumer,
                ),
                add_on_id=self._uuid(
                    text(record, "add_on_id"),
                    field="terms.add_on_id",
                    event=event,
                    consumer=consumer,
                ),
                add_on_price_id=self._uuid(
                    text(record, "add_on_price_id"),
                    field="terms.add_on_price_id",
                    event=event,
                    consumer=consumer,
                ),
                description=text(record, "description"),
                quantity=self._decimal(
                    text(record, "quantity"),
                    field="terms.quantity",
                    event=event,
                    consumer=consumer,
                ),
                unit_price=self._decimal(
                    text(record, "unit_price"),
                    field="terms.unit_price",
                    event=event,
                    consumer=consumer,
                ),
                currency=text(record, "currency"),
                source_started_at=optional_datetime(record, "source_started_at"),
                source_ends_at=optional_datetime(record, "source_ends_at"),
            )
            for record in records
        )
        with _owner_session(db) as owner_db:
            BillingContracts.consume_recurring_addon_backfill(
                owner_db,
                sales_order_id=sales_order_id,
                account_id=self._uuid(
                    text(event.payload, "account_id"),
                    field="account_id",
                    event=event,
                    consumer=consumer,
                ),
                subscription_id=self._uuid(
                    text(event.payload, "subscription_id"),
                    field="subscription_id",
                    event=event,
                    consumer=consumer,
                ),
                contract_id=self._uuid(
                    text(event.payload, "contract_id"),
                    field="contract_id",
                    event=event,
                    consumer=consumer,
                ),
                current_contract_version_id=self._uuid(
                    text(event.payload, "current_contract_version_id"),
                    field="current_contract_version_id",
                    event=event,
                    consumer=consumer,
                ),
                target_period=target_period,
                terms=terms,
                event_id=event.event_id,
                context=self._context(event, text(event.payload, "subscription_id")),
            )

    def _record_live_addon_purchase(self, db: Session, event: Event) -> None:
        from app.services.billing.contracts import (
            BillingContracts,
            RecurringAddonPurchaseTermSnapshot,
        )

        consumer = "billing.contracts"

        def text(field: str) -> str:
            return require_output_text(
                event.payload,
                field,
                consumer=consumer,
                event_id=event.event_id,
                event_type=_LIVE_ADDON_PURCHASE_OUTPUT,
            )

        subscription_add_on_id = self._uuid(
            text("subscription_add_on_id"),
            field="subscription_add_on_id",
            event=event,
            consumer=consumer,
        )
        self._producer(
            event,
            "financial.addon_purchases",
            source_id=subscription_add_on_id,
            consumer=consumer,
            source_kind="subscription_add_on",
        )
        try:
            purchased_at = datetime.fromisoformat(text("purchased_at"))
        except ValueError as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_LIVE_ADDON_PURCHASE_OUTPUT,
                field="purchased_at",
                reason="invalid_datetime",
            ) from exc
        if purchased_at.tzinfo is None:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_LIVE_ADDON_PURCHASE_OUTPUT,
                field="purchased_at",
                reason="timezone_required",
            )
        billing_cycle_raw = event.payload.get("billing_cycle")
        try:
            billing_cycle = (
                BillingCycle(str(billing_cycle_raw))
                if billing_cycle_raw is not None
                else None
            )
        except ValueError as exc:
            raise invalid_output_payload(
                consumer=consumer,
                event_id=event.event_id,
                event_type=_LIVE_ADDON_PURCHASE_OUTPUT,
                field="billing_cycle",
                reason="unsupported_billing_cycle",
            ) from exc

        term = RecurringAddonPurchaseTermSnapshot(
            account_id=self._uuid(
                text("account_id"),
                field="account_id",
                event=event,
                consumer=consumer,
            ),
            subscription_id=self._uuid(
                text("subscription_id"),
                field="subscription_id",
                event=event,
                consumer=consumer,
            ),
            subscription_add_on_id=subscription_add_on_id,
            add_on_id=self._uuid(
                text("add_on_id"),
                field="add_on_id",
                event=event,
                consumer=consumer,
            ),
            add_on_price_id=self._uuid(
                text("add_on_price_id"),
                field="add_on_price_id",
                event=event,
                consumer=consumer,
            ),
            description=text("description"),
            quantity=self._decimal(
                text("quantity"),
                field="quantity",
                event=event,
                consumer=consumer,
            ),
            unit_price=self._decimal(
                text("unit_price"),
                field="unit_price",
                event=event,
                consumer=consumer,
            ),
            currency=text("currency"),
            purchased_at=purchased_at,
            billing_cycle=billing_cycle,
        )
        with _owner_session(db) as owner_db:
            BillingContracts.consume_recurring_addon_purchase(
                owner_db,
                term=term,
                event_id=event.event_id,
                context=self._context(event, str(term.subscription_id)),
            )

    def _activate_pending_terms(self, db: Session, event: Event) -> None:
        from app.services.billing.contracts import BillingContracts

        consumer = "billing.contracts"

        def text(field: str) -> str:
            return require_output_text(
                event.payload,
                field,
                consumer=consumer,
                event_id=event.event_id,
                event_type=_PENDING_TERMS_EFFECTIVE_TRIGGER,
            )

        contract_id = self._uuid(
            text("entity_id"),
            field="entity_id",
            event=event,
            consumer=consumer,
        )
        with _owner_session(db) as owner_db:
            BillingContracts.consume_pending_terms_effective_due(
                owner_db,
                contract_id=contract_id,
                timer_id=self._uuid(
                    text("timer_id"),
                    field="timer_id",
                    event=event,
                    consumer=consumer,
                ),
                expected_source_version=self._integer(
                    event.payload.get("expected_source_version"),
                    field="expected_source_version",
                    event=event,
                    consumer=consumer,
                ),
                timer_generation=self._integer(
                    event.payload.get("generation"),
                    field="generation",
                    event=event,
                    consumer=consumer,
                ),
                event_id=event.event_id,
                context=self._context(event, str(contract_id)),
            )

    def _schedule_obligations(self, db: Session, event: Event) -> None:
        from app.services.billing.obligations import (
            BillingObligations,
            ScheduleObligationCommand,
        )

        consumer = "billing.obligations"
        sales_order_text = require_output_text(
            event.payload,
            "sales_order_id",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_CONTRACT_OUTPUT,
        )
        sales_order_id = self._uuid(
            sales_order_text,
            field="sales_order_id",
            event=event,
            consumer=consumer,
        )
        change_kind = str(event.payload.get("contract_change_kind") or "sales_funding")
        subscription_id = None
        envelope_source_kind = "sales_order"
        envelope_source_id = sales_order_id
        if change_kind == "recurring_addon_purchase":
            subscription_id = self._uuid(
                require_output_text(
                    event.payload,
                    "subscription_id",
                    consumer=consumer,
                    event_id=event.event_id,
                    event_type=_CONTRACT_OUTPUT,
                ),
                field="subscription_id",
                event=event,
                consumer=consumer,
            )
            envelope_source_kind = "subscription"
            envelope_source_id = subscription_id
        self._producer(
            event,
            "billing.contracts",
            source_id=envelope_source_id,
            consumer=consumer,
            schema_versions=(1, 2),
            source_kind=envelope_source_kind,
        )
        envelope = event.payload["envelope"]
        assert isinstance(envelope, Mapping)
        output_schema_version = int(envelope["schema_version"])
        records = require_output_records(
            event.payload,
            "obligations",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_CONTRACT_OUTPUT,
        )
        commands = tuple(
            ScheduleObligationCommand(
                contract_version_id=self._uuid(
                    require_output_text(
                        record,
                        "contract_version_id",
                        consumer=consumer,
                        event_id=event.event_id,
                        event_type=_CONTRACT_OUTPUT,
                    ),
                    field="obligations.contract_version_id",
                    event=event,
                    consumer=consumer,
                ),
                contract_line_key=self._uuid(
                    require_output_text(
                        record,
                        "contract_line_key",
                        consumer=consumer,
                        event_id=event.event_id,
                        event_type=_CONTRACT_OUTPUT,
                    ),
                    field="obligations.contract_line_key",
                    event=event,
                    consumer=consumer,
                ),
                period_index=self._integer(
                    record.get("period_index", 0),
                    field="obligations.period_index",
                    event=event,
                    consumer=consumer,
                ),
            )
            for record in records
        )
        with _owner_session(db) as owner_db:
            BillingObligations.consume_contract_shadow(
                owner_db,
                sales_order_id=sales_order_id,
                commands=commands,
                event_id=event.event_id,
                output_schema_version=output_schema_version,
                context=self._context(event, sales_order_text),
                contract_change_kind=change_kind,
                envelope_source_kind=envelope_source_kind,
                envelope_source_id=envelope_source_id,
                subscription_id=subscription_id,
            )

    def _record_verification_evidence(self, db: Session, event: Event) -> None:
        # Implemented by ``billing.shadow_verification``; keeping this explicit
        # prevents the terminal owner output from being acknowledged as an
        # unowned log-only delivery.
        from app.services.billing.shadow_verification import BillingShadowVerification

        consumer = "billing.shadow_verification"
        sales_order_text = require_output_text(
            event.payload,
            "sales_order_id",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_OBLIGATION_OUTPUT,
        )
        sales_order_id = self._uuid(
            sales_order_text,
            field="sales_order_id",
            event=event,
            consumer=consumer,
        )
        change_kind = str(event.payload.get("contract_change_kind") or "sales_funding")
        envelope_source_kind = "sales_order"
        envelope_source_id = sales_order_id
        if change_kind == "recurring_addon_purchase":
            subscription_id = self._uuid(
                require_output_text(
                    event.payload,
                    "subscription_id",
                    consumer=consumer,
                    event_id=event.event_id,
                    event_type=_OBLIGATION_OUTPUT,
                ),
                field="subscription_id",
                event=event,
                consumer=consumer,
            )
            envelope_source_kind = "subscription"
            envelope_source_id = subscription_id
        self._producer(
            event,
            "billing.obligations",
            source_id=envelope_source_id,
            consumer=consumer,
            source_kind=envelope_source_kind,
        )
        records = require_output_records(
            event.payload,
            "obligations",
            consumer=consumer,
            event_id=event.event_id,
            event_type=_OBLIGATION_OUTPUT,
        )
        obligation_ids = tuple(
            self._uuid(
                require_output_text(
                    record,
                    "obligation_id",
                    consumer=consumer,
                    event_id=event.event_id,
                    event_type=_OBLIGATION_OUTPUT,
                ),
                field="obligations.obligation_id",
                event=event,
                consumer=consumer,
            )
            for record in records
        )
        with _owner_session(db) as owner_db:
            BillingShadowVerification.consume_terminal_output(
                owner_db,
                sales_order_id=sales_order_id,
                obligation_ids=obligation_ids,
                event_id=event.event_id,
                context=self._context(event, sales_order_text),
            )
