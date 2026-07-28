"""Deliver the Phase 1 shadow billing owner-output chain.

This adapter owns no billing decision. It converts versioned payloads into the
typed commands of ``billing.contracts`` and ``billing.obligations``; each owner
commits its effect with a unique delivery receipt and emits the next output.
All records remain shadow while ADR 0007's cutover gates are open.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
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

HANDLED_EVENT_TYPES = frozenset({EventType.custom})

_FULFILLMENT_OUTPUT = "sales.fulfillment.funding_applied"
_CONTRACT_OUTPUT = "billing.contracts.shadow_recorded"
_OBLIGATION_OUTPUT = "billing.obligations.shadow_scheduled"


class BillingLifecycleProjectionHandler:
    """Route shadow billing outputs to their exact receipted owners."""

    def handle(self, db: Session, event: Event) -> None:
        output = event.payload.get("output")
        if output == _FULFILLMENT_OUTPUT:
            self._record_contracts(db, event)
        elif output == _CONTRACT_OUTPUT:
            self._schedule_obligations(db, event)
        elif output == _OBLIGATION_OUTPUT:
            self._record_verification_evidence(db, event)
        # Every other custom payload belongs to another adapter.

    @staticmethod
    def _context(event: Event, scope: str):
        from app.services.owner_commands import CommandContext

        return CommandContext.system(
            actor=str(event.actor or "billing.lifecycle_projection"),
            scope=scope,
            reason=require_output_text(
                event.payload,
                "output",
                consumer="billing.shadow_pipeline",
                event_id=event.event_id,
                event_type=event.event_type.value,
            ),
            command_id=event.event_id,
            correlation_id=event.event_id,
            causation_id=event.event_id,
            idempotency_key=f"event:{event.event_id}",
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
        expected_fields: tuple[tuple[str, object], ...] = (
            ("schema_version", 1),
            ("producer_owner", expected),
            ("source_kind", "sales_order"),
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
        self._producer(
            event,
            "billing.contracts",
            source_id=sales_order_id,
            consumer=consumer,
        )
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
                net_amount=self._decimal(
                    require_output_text(
                        record,
                        "net_amount",
                        consumer=consumer,
                        event_id=event.event_id,
                        event_type=_CONTRACT_OUTPUT,
                    ),
                    field="obligations.net_amount",
                    event=event,
                    consumer=consumer,
                ),
                tax_amount=self._decimal(
                    require_output_text(
                        record,
                        "tax_amount",
                        consumer=consumer,
                        event_id=event.event_id,
                        event_type=_CONTRACT_OUTPUT,
                    ),
                    field="obligations.tax_amount",
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
                context=self._context(event, sales_order_text),
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
        self._producer(
            event,
            "billing.obligations",
            source_id=sales_order_id,
            consumer=consumer,
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
