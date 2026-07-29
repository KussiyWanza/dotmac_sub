"""`billing.contracts` — versioned customer billing terms (ADR 0007 Phase 1).

This owner turns an accepted commercial commitment, or a later authorized
service change, into one immutable :class:`BillingContractVersion` and its
lines. It is the single place that decides what a customer contracted to pay,
replacing billing mode, cadence, and price duplicated across account,
subscription, and catalog rows.

Phase 1 is expand-and-shadow. While the registry declares this owner's
migration state as ``shadowing``, every row it writes is
``BillingRecordAuthority.shadow`` and must produce no financial effect. The
authority flag is not a feature toggle: it is read from the executable manifest
in ``app/services/sot_relationships.py``, so promoting rows to authoritative
requires the registry change that records the passed cutover gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.billing_contract import (
    AccountingTreatment,
    BillingContract,
    BillingContractLine,
    BillingContractSourceKind,
    BillingContractVersion,
    BillingContractVersionStatus,
    BillingRecordAuthority,
    CadenceAlignment,
    ChargeComponent,
    CollectionTiming,
    EndOfMonthRule,
    IntervalUnit,
    ProrationPolicy,
    RateBasis,
)
from app.models.catalog import BillingCycle, BillingMode
from app.models.durable_timer import DurableTimer, TimerStatus
from app.models.sales import SalesOrderLine
from app.services.billing.cadence import (
    BillingCadence,
    Interval,
    period_containing,
    service_period,
)
from app.services.domain_errors import DomainError
from app.services.events.owner_outputs import (
    OwnerOutputEnvelope,
    consume_owner_output,
    stage_owner_output,
)
from app.services.events.types import EventType
from app.services.locking import lock_for_update
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sot_manifest import AuthorityMigrationState

OWNER = "billing.contracts"

_RECORD_VERSION_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="versioned billing contract terms",
    name="record_billing_contract_version",
)
_SUPERSEDE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="billing contract version supersession",
    name="supersede_billing_contract_version",
)
_CONSUME_SALES_FUNDING_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="versioned billing contract terms",
    name="consume_sales_funding_contracts",
)
_CONSUME_ADDON_BACKFILL_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="versioned billing contract terms",
    name="consume_recurring_addon_contract_backfill",
)
_CONSUME_ADDON_PURCHASE_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="versioned billing contract terms",
    name="consume_recurring_addon_purchase",
)
_ACTIVATE_PENDING_TERMS_COMMAND = OwnerCommandDefinition(
    owner=OWNER,
    concern="billing contract version supersession",
    name="consume_pending_terms_effective_due",
)

PENDING_TERMS_EFFECTIVE_TRIGGER = "billing.contracts.pending_terms_effective_due"


class BillingContractError(DomainError):
    """Fail-closed billing-contract error."""


def _error(suffix: str, message: str, **details: object) -> BillingContractError:
    return BillingContractError(
        code=f"{OWNER}.{suffix}", message=message, details=dict(details)
    )


def _aware_utc(value: datetime | None) -> datetime | None:
    """Restore UTC tzinfo on instants read back from persistence.

    SQLite drops timezone metadata in tests; production PostgreSQL preserves
    the UTC offset the owner wrote.
    """

    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def permitted_authority() -> BillingRecordAuthority:
    """Return the authority this owner may write, from the manifest state.

    Reading the declared migration state keeps the shadow/cutover boundary in
    the executable registry rather than in an ad-hoc readiness flag.
    """

    from app.services.sot_relationships import service_relationship

    contract = service_relationship(OWNER).contract
    if contract is None:  # pragma: no cover - manifest guarantees the contract
        raise _error(
            "command_contract_violation",
            "Billing contract owner has no typed manifest contract.",
        )
    if contract.migration.state in {
        AuthorityMigrationState.CUT_OVER,
        AuthorityMigrationState.COMPLETE,
    }:
        return BillingRecordAuthority.authoritative
    return BillingRecordAuthority.shadow


@dataclass(frozen=True)
class ContractLineInput:
    """One charge component of a contracted version."""

    charge_component: ChargeComponent
    description: str
    unit_price: Decimal
    currency: str
    accounting_treatment: AccountingTreatment
    quantity: Decimal = Decimal("1")
    component_key: str = ""
    is_finite: bool = False
    tax_treatment_code: str | None = None
    contract_line_key: UUID | None = None


@dataclass(frozen=True)
class RecordContractVersionCommand:
    """Typed request to record one immutable set of contracted terms."""

    account_id: UUID
    subscription_id: UUID
    source_kind: BillingContractSourceKind
    source_id: UUID
    starts_at: datetime
    contracted_price: Decimal
    currency: str
    cadence: BillingCadence
    lines: tuple[ContractLineInput, ...]
    source_version: int = 1
    ends_at: datetime | None = None
    payment_terms_days: int = 0
    tax_treatment_code: str | None = None
    tax_inclusive: bool = False
    discount_code: str | None = None
    discount_amount: Decimal | None = None


@dataclass(frozen=True)
class ContractVersionResult:
    """Outcome of recording a version, including replay detection."""

    contract_id: UUID
    version_id: UUID
    version: int
    authority: BillingRecordAuthority
    line_ids: tuple[UUID, ...]
    replayed: bool


@dataclass(frozen=True)
class SalesFundingContractSnapshot:
    """Exact legacy sale/subscription terms carried by the fulfilment output."""

    sales_order_line_id: UUID
    account_id: UUID
    subscription_id: UUID
    starts_at: datetime
    description: str
    quantity: Decimal
    unit_price: Decimal
    currency: str
    billing_cycle: BillingCycle
    billing_mode: BillingMode


@dataclass(frozen=True)
class RecurringAddonContractTermSnapshot:
    """Exact recurring add-on terms delivered by the migration producer."""

    subscription_add_on_id: UUID
    add_on_id: UUID
    add_on_price_id: UUID
    description: str
    quantity: Decimal
    unit_price: Decimal
    currency: str
    source_started_at: datetime | None
    source_ends_at: datetime | None


@dataclass(frozen=True)
class RecurringAddonPurchaseTermSnapshot:
    """Exact recurring term accepted by the live add-on purchase owner."""

    account_id: UUID
    subscription_id: UUID
    subscription_add_on_id: UUID
    add_on_id: UUID
    add_on_price_id: UUID
    description: str
    quantity: Decimal
    unit_price: Decimal
    currency: str
    purchased_at: datetime
    billing_cycle: BillingCycle | None


@dataclass(frozen=True)
class PendingContractTermsResult:
    """The next-boundary draft and exact timer generation it requires."""

    contract_id: UUID
    draft_version_id: UUID
    draft_version: int
    effective_at: datetime
    timer_id: UUID
    timer_generation: int


_CYCLE_INTERVAL: dict[BillingCycle, tuple[IntervalUnit, int]] = {
    BillingCycle.daily: (IntervalUnit.day, 1),
    BillingCycle.weekly: (IntervalUnit.week, 1),
    BillingCycle.monthly: (IntervalUnit.month, 1),
    BillingCycle.quarterly: (IntervalUnit.month, 3),
    BillingCycle.annual: (IntervalUnit.year, 1),
}


def _sales_funding_command(
    snapshot: SalesFundingContractSnapshot,
) -> RecordContractVersionCommand:
    interval_unit, interval_count = _CYCLE_INTERVAL[snapshot.billing_cycle]
    currency = snapshot.currency.strip().upper()
    prepaid = snapshot.billing_mode is BillingMode.prepaid
    cadence = BillingCadence(
        rate_basis=RateBasis.fixed_per_service_period,
        rate_unit=interval_unit,
        rate_quantity=Decimal("1"),
        service_interval_unit=interval_unit,
        service_interval_count=interval_count,
        invoice_interval_unit=interval_unit,
        invoice_interval_count=interval_count,
        collection_timing=(
            CollectionTiming.advance if prepaid else CollectionTiming.arrears
        ),
        alignment=CadenceAlignment.contract_anniversary,
        timezone_name="Africa/Lagos",
        end_of_month_rule=EndOfMonthRule.clamp_to_month_end,
        proration_policy=ProrationPolicy.none,
    )
    return RecordContractVersionCommand(
        account_id=snapshot.account_id,
        subscription_id=snapshot.subscription_id,
        source_kind=BillingContractSourceKind.sales_order_line,
        source_id=snapshot.sales_order_line_id,
        starts_at=snapshot.starts_at,
        contracted_price=snapshot.unit_price,
        currency=currency,
        cadence=cadence,
        lines=(
            ContractLineInput(
                charge_component=ChargeComponent.recurring_service,
                component_key=str(snapshot.sales_order_line_id),
                description=snapshot.description,
                quantity=snapshot.quantity,
                unit_price=snapshot.unit_price,
                currency=currency,
                accounting_treatment=(
                    AccountingTreatment.prepaid_consumption
                    if prepaid
                    else AccountingTreatment.receivable
                ),
            ),
        ),
    )


def _validate(command: RecordContractVersionCommand) -> None:
    if command.starts_at.tzinfo is None:
        raise _error(
            "invalid_contract_terms",
            "Contract start must be timezone-aware.",
        )
    if command.ends_at is not None and command.ends_at <= command.starts_at:
        raise _error(
            "invalid_contract_terms",
            "Contract version must end after it starts.",
        )
    if command.contracted_price < 0:
        raise _error(
            "invalid_contract_terms",
            "Contracted price cannot be negative.",
        )
    if len(command.currency) != 3:
        raise _error(
            "invalid_contract_terms",
            "Currency must be a three-letter code.",
            currency=command.currency,
        )
    if not command.lines:
        raise _error(
            "invalid_contract_terms",
            "A contract version requires at least one line.",
        )
    for line in command.lines:
        if line.currency != command.currency:
            raise _error(
                "mixed_currency_contract",
                "Every contract line must use the contract currency.",
                line_currency=line.currency,
                contract_currency=command.currency,
            )
        if line.unit_price < 0 or line.quantity <= 0:
            raise _error(
                "invalid_contract_terms",
                "Line unit price must be non-negative and quantity positive.",
            )
    seen = {(line.charge_component, line.component_key) for line in command.lines}
    if len(seen) != len(command.lines):
        raise _error(
            "duplicate_contract_line",
            "Two lines share the same charge component and component key.",
        )


class BillingContracts:
    """Public command owner for versioned billing contract terms."""

    @staticmethod
    def record_version(
        db: Session,
        command: RecordContractVersionCommand,
        *,
        context: CommandContext,
    ) -> ContractVersionResult:
        """Record one immutable contract version in one owner transaction."""

        return execute_owner_command(
            db,
            definition=_RECORD_VERSION_COMMAND,
            context=context,
            operation=lambda: BillingContracts._record_version(
                db, command=command, context=context
            ),
        )

    @staticmethod
    def consume_sales_funding(
        db: Session,
        *,
        sales_order_id: UUID,
        snapshots: tuple[SalesFundingContractSnapshot, ...],
        event_id: UUID,
        context: CommandContext,
    ) -> tuple[ContractVersionResult, ...] | None:
        """Receipt fulfilment terms and emit the next shadow-pipeline output."""

        def _effect() -> tuple[ContractVersionResult, ...]:
            subscription_ids = [snapshot.subscription_id for snapshot in snapshots]
            if len(subscription_ids) != len(set(subscription_ids)):
                raise _error(
                    "duplicate_subscription_output",
                    "One fulfilment output repeats a subscription contract.",
                    sales_order_id=str(sales_order_id),
                )
            results = tuple(
                BillingContracts._record_version(
                    db,
                    command=_sales_funding_command(snapshot),
                    context=context,
                )
                for snapshot in snapshots
            )
            BillingContracts._stage_shadow_recorded_output(
                db,
                sales_order_id=sales_order_id,
                results=results,
                context=context,
                change_kind="sales_funding",
            )
            return results

        return execute_owner_command(
            db,
            definition=_CONSUME_SALES_FUNDING_COMMAND,
            context=context,
            operation=lambda: consume_owner_output(
                db,
                consumer=OWNER,
                event_id=event_id,
                event_type="sales.fulfillment.funding_applied",
                producer_owner="sales.fulfillment",
                context=context,
                operation=_effect,
            )[0],
        )

    @staticmethod
    def consume_recurring_addon_backfill(
        db: Session,
        *,
        sales_order_id: UUID,
        account_id: UUID,
        subscription_id: UUID,
        contract_id: UUID,
        current_contract_version_id: UUID,
        target_period: Interval,
        terms: tuple[RecurringAddonContractTermSnapshot, ...],
        event_id: UUID,
        context: CommandContext,
    ) -> PendingContractTermsResult | None:
        """Receipt one exact snapshot into the shared boundary draft and timer."""

        def _effect() -> PendingContractTermsResult:
            contract = db.execute(
                select(BillingContract)
                .where(
                    BillingContract.id == contract_id,
                    BillingContract.subscription_id == subscription_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if contract is None:
                raise _error(
                    "contract_not_found",
                    "The add-on snapshot references no billing contract.",
                    contract_id=str(contract_id),
                )
            if contract.account_id != account_id:
                raise _error(
                    "contract_account_mismatch",
                    "The add-on snapshot account differs from the contract account.",
                    contract_account_id=str(contract.account_id),
                    snapshot_account_id=str(account_id),
                )
            current = BillingContracts._current_effective(db, contract_id=contract.id)
            if current is None or current.id != current_contract_version_id:
                raise _error(
                    "stale_addon_snapshot",
                    "The current contract version changed after add-on capture.",
                    expected_contract_version_id=str(current_contract_version_id),
                    actual_contract_version_id=(
                        str(current.id) if current is not None else None
                    ),
                )
            cadence = BillingContracts.cadence_of(current)
            expected_period = service_period(
                cadence=cadence,
                contract_start=target_period.starts_at,
                index=0,
            )
            if expected_period != target_period:
                raise _error(
                    "invalid_addon_period",
                    "The add-on snapshot is not one complete contract service period.",
                    target_period_start=target_period.starts_at.isoformat(),
                    target_period_end=target_period.ends_at.isoformat(),
                )

            source_anchor_exists = db.execute(
                select(BillingContractVersion.id)
                .join(
                    SalesOrderLine,
                    BillingContractVersion.source_id == SalesOrderLine.id,
                )
                .where(
                    BillingContractVersion.contract_id == contract.id,
                    BillingContractVersion.source_kind
                    == BillingContractSourceKind.sales_order_line,
                    SalesOrderLine.sales_order_id == sales_order_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if source_anchor_exists is None:
                raise _error(
                    "sales_order_anchor_mismatch",
                    "The add-on snapshot is not anchored to this contract's sale.",
                    sales_order_id=str(sales_order_id),
                    contract_id=str(contract.id),
                )

            drafts = list(
                db.execute(
                    select(BillingContractVersion)
                    .where(
                        BillingContractVersion.contract_id == contract.id,
                        BillingContractVersion.status
                        == BillingContractVersionStatus.draft,
                    )
                    .with_for_update()
                ).scalars()
            )
            if len(drafts) > 1:
                raise _error(
                    "ambiguous_pending_contract_terms",
                    "The contract has multiple pending term versions.",
                    contract_id=str(contract.id),
                )
            if drafts:
                draft = drafts[0]
                if (
                    _aware_utc(draft.starts_at) != target_period.starts_at
                    or draft.supersedes_id != current.id
                ):
                    raise _error(
                        "stale_pending_contract_terms",
                        "Pending terms no longer match the backfill boundary.",
                        draft_version_id=str(draft.id),
                    )
            else:
                draft = BillingContracts._create_pending_version(
                    db,
                    contract=contract,
                    current=current,
                    effective_at=target_period.starts_at,
                    context=context,
                    source_kind=BillingContractSourceKind.migration_backfill,
                    reason="Pending recurring add-on migration snapshot",
                )

            for line in list(
                db.execute(
                    select(BillingContractLine).where(
                        BillingContractLine.contract_version_id == draft.id,
                        BillingContractLine.charge_component == ChargeComponent.addon,
                        BillingContractLine.is_finite.is_(False),
                    )
                ).scalars()
            ):
                db.delete(line)
            db.flush()
            treatment = (
                AccountingTreatment.prepaid_consumption
                if current.collection_timing is CollectionTiming.advance
                else AccountingTreatment.receivable
            )
            for term in terms:
                db.add(
                    BillingContractLine(
                        contract_version_id=draft.id,
                        contract_line_key=BillingContracts._inherited_line_key(
                            db,
                            contract_id=contract.id,
                            charge_component=ChargeComponent.addon,
                            component_key=str(term.subscription_add_on_id),
                        ),
                        charge_component=ChargeComponent.addon,
                        component_key=str(term.subscription_add_on_id),
                        description=term.description,
                        quantity=term.quantity,
                        unit_price=term.unit_price,
                        currency=term.currency,
                        accounting_treatment=treatment,
                        is_finite=False,
                        tax_treatment_code=current.tax_treatment_code,
                    )
                )
            db.flush()

            from app.services.runtime_durable_timers import (
                ScheduleTimerCommand,
                schedule_timer,
            )

            timer = schedule_timer(
                db,
                ScheduleTimerCommand(
                    owner=OWNER,
                    entity_kind="billing_contract",
                    entity_id=contract.id,
                    purpose="pending_terms_effective",
                    due_at=target_period.starts_at,
                    expected_source_version=draft.version,
                    output_event_type=PENDING_TERMS_EFFECTIVE_TRIGGER,
                ),
                context=context,
            )
            return PendingContractTermsResult(
                contract_id=contract.id,
                draft_version_id=draft.id,
                draft_version=draft.version,
                effective_at=target_period.starts_at,
                timer_id=timer.id,
                timer_generation=timer.generation,
            )

        return execute_owner_command(
            db,
            definition=_CONSUME_ADDON_BACKFILL_COMMAND,
            context=context,
            operation=lambda: consume_owner_output(
                db,
                consumer=OWNER,
                event_id=event_id,
                event_type="billing.addon_contract_backfill.captured",
                producer_owner="billing.addon_contract_backfill",
                context=context,
                operation=_effect,
            )[0],
        )

    @staticmethod
    def consume_recurring_addon_purchase(
        db: Session,
        *,
        term: RecurringAddonPurchaseTermSnapshot,
        event_id: UUID,
        context: CommandContext,
    ) -> PendingContractTermsResult | None:
        """Receipt one live purchase into a next-boundary draft and timer."""

        def _effect() -> PendingContractTermsResult | None:
            if term.purchased_at.tzinfo is None:
                raise _error(
                    "invalid_addon_purchase_time",
                    "Recurring add-on purchase time must be timezone-aware.",
                )
            if term.quantity <= 0 or term.unit_price < 0:
                raise _error(
                    "invalid_addon_terms",
                    "Recurring add-on quantity and price must be valid.",
                    subscription_add_on_id=str(term.subscription_add_on_id),
                )

            contract = db.execute(
                select(BillingContract)
                .where(
                    BillingContract.subscription_id == term.subscription_id,
                    BillingContract.account_id == term.account_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if contract is None:
                raise _error(
                    "contract_not_found",
                    "The recurring add-on purchase has no billing contract.",
                    subscription_id=str(term.subscription_id),
                )
            current = BillingContracts._current_effective(db, contract_id=contract.id)
            if current is None:
                raise _error(
                    "contract_version_not_found",
                    "The recurring add-on purchase has no effective contract version.",
                    contract_id=str(contract.id),
                )

            cadence = BillingContracts.cadence_of(current)
            if term.billing_cycle is not None:
                addon_interval = _CYCLE_INTERVAL[term.billing_cycle]
                contract_interval = (
                    cadence.service_interval_unit,
                    cadence.service_interval_count,
                )
                if addon_interval != contract_interval:
                    raise _error(
                        "unsupported_addon_cadence",
                        "The add-on cadence differs from the contract service cadence.",
                        addon_billing_cycle=term.billing_cycle.value,
                        contract_interval_unit=cadence.service_interval_unit.value,
                        contract_interval_count=cadence.service_interval_count,
                    )
            currency = term.currency.strip().upper()
            if currency != current.currency:
                raise _error(
                    "mixed_currency_contract",
                    "The recurring add-on currency differs from the contract.",
                    addon_currency=currency,
                    contract_currency=current.currency,
                )
            treatment = (
                AccountingTreatment.prepaid_consumption
                if current.collection_timing is CollectionTiming.advance
                else AccountingTreatment.receivable
            )
            already_effective = db.execute(
                select(BillingContractLine).where(
                    BillingContractLine.contract_version_id == current.id,
                    BillingContractLine.charge_component == ChargeComponent.addon,
                    BillingContractLine.component_key
                    == str(term.subscription_add_on_id),
                )
            ).scalar_one_or_none()
            if already_effective is not None:
                matches = (
                    already_effective.description == term.description
                    and already_effective.quantity == term.quantity
                    and already_effective.unit_price == term.unit_price
                    and already_effective.currency == currency
                    and already_effective.accounting_treatment is treatment
                    and not already_effective.is_finite
                )
                if not matches:
                    raise _error(
                        "duplicate_addon_term_conflict",
                        "The effective version has different terms for this add-on.",
                        subscription_add_on_id=str(term.subscription_add_on_id),
                    )
                # A delayed delivery after a backfill boundary is already
                # satisfied by the exact immutable term. The receipt is still
                # committed, but no later draft or timer is invented.
                return None

            current_start = _aware_utc(current.starts_at)
            assert current_start is not None
            try:
                _period_index, current_period = period_containing(
                    cadence=cadence,
                    contract_start=current_start,
                    moment=term.purchased_at,
                )
            except DomainError as exc:
                raise _error(
                    "invalid_addon_purchase_time",
                    "The add-on purchase is outside the effective contract.",
                    purchased_at=term.purchased_at.isoformat(),
                ) from exc
            effective_at = current_period.ends_at

            drafts = list(
                db.execute(
                    select(BillingContractVersion)
                    .where(
                        BillingContractVersion.contract_id == contract.id,
                        BillingContractVersion.status
                        == BillingContractVersionStatus.draft,
                    )
                    .with_for_update()
                ).scalars()
            )
            if len(drafts) > 1:
                raise _error(
                    "ambiguous_pending_contract_terms",
                    "The contract has multiple pending term versions.",
                    contract_id=str(contract.id),
                )
            if drafts:
                draft = drafts[0]
                draft_start = _aware_utc(draft.starts_at)
                if draft_start != effective_at or draft.supersedes_id != current.id:
                    raise _error(
                        "stale_pending_contract_terms",
                        "Pending terms no longer match the current contract boundary.",
                        draft_version_id=str(draft.id),
                    )
            else:
                draft = BillingContracts._create_pending_version(
                    db,
                    contract=contract,
                    current=current,
                    effective_at=effective_at,
                    context=context,
                )
            # A live owner transition is stronger provenance than a temporary
            # migration snapshot. Draft terms are intentionally mutable until
            # their boundary; effective/historical versions remain immutable.
            draft.source_kind = BillingContractSourceKind.plan_change
            draft.source_id = contract.subscription_id
            draft.reason = "Pending live recurring add-on terms"

            existing_line = db.execute(
                select(BillingContractLine).where(
                    BillingContractLine.contract_version_id == draft.id,
                    BillingContractLine.charge_component == ChargeComponent.addon,
                    BillingContractLine.component_key
                    == str(term.subscription_add_on_id),
                )
            ).scalar_one_or_none()
            if existing_line is not None:
                matches = (
                    existing_line.description == term.description
                    and existing_line.quantity == term.quantity
                    and existing_line.unit_price == term.unit_price
                    and existing_line.currency == currency
                    and existing_line.accounting_treatment is treatment
                )
                if not matches:
                    raise _error(
                        "duplicate_addon_term_conflict",
                        "The pending version already has different terms for this add-on.",
                        subscription_add_on_id=str(term.subscription_add_on_id),
                    )
            else:
                db.add(
                    BillingContractLine(
                        contract_version_id=draft.id,
                        contract_line_key=BillingContracts._inherited_line_key(
                            db,
                            contract_id=contract.id,
                            charge_component=ChargeComponent.addon,
                            component_key=str(term.subscription_add_on_id),
                        ),
                        charge_component=ChargeComponent.addon,
                        component_key=str(term.subscription_add_on_id),
                        description=term.description,
                        quantity=term.quantity,
                        unit_price=term.unit_price,
                        currency=currency,
                        accounting_treatment=treatment,
                        is_finite=False,
                        tax_treatment_code=current.tax_treatment_code,
                    )
                )
                db.flush()

            from app.services.runtime_durable_timers import (
                ScheduleTimerCommand,
                schedule_timer,
            )

            timer = schedule_timer(
                db,
                ScheduleTimerCommand(
                    owner=OWNER,
                    entity_kind="billing_contract",
                    entity_id=contract.id,
                    purpose="pending_terms_effective",
                    due_at=effective_at,
                    expected_source_version=draft.version,
                    output_event_type=PENDING_TERMS_EFFECTIVE_TRIGGER,
                ),
                context=context,
            )
            return PendingContractTermsResult(
                contract_id=contract.id,
                draft_version_id=draft.id,
                draft_version=draft.version,
                effective_at=effective_at,
                timer_id=timer.id,
                timer_generation=timer.generation,
            )

        return execute_owner_command(
            db,
            definition=_CONSUME_ADDON_PURCHASE_COMMAND,
            context=context,
            operation=lambda: consume_owner_output(
                db,
                consumer=OWNER,
                event_id=event_id,
                event_type="billing.contract_terms.recurring_addon_added",
                producer_owner="financial.addon_purchases",
                context=context,
                operation=_effect,
            )[0],
        )

    @staticmethod
    def consume_pending_terms_effective_due(
        db: Session,
        *,
        contract_id: UUID,
        timer_id: UUID,
        expected_source_version: int,
        timer_generation: int,
        event_id: UUID,
        context: CommandContext,
    ) -> ContractVersionResult | None:
        """Receipt one exact due timer and activate its shadow draft."""

        def _effect() -> ContractVersionResult:
            timer = db.execute(
                select(DurableTimer)
                .where(DurableTimer.id == timer_id)
                .with_for_update()
            ).scalar_one_or_none()
            if (
                timer is None
                or timer.owner != OWNER
                or timer.entity_kind != "billing_contract"
                or timer.entity_id != contract_id
                or timer.purpose != "pending_terms_effective"
                or timer.generation != timer_generation
                or timer.expected_source_version != expected_source_version
                or timer.output_event_type != PENDING_TERMS_EFFECTIVE_TRIGGER
                or timer.status is not TimerStatus.fired
            ):
                raise _error(
                    "invalid_pending_terms_timer",
                    "The fired timer does not identify the pending contract terms.",
                    timer_id=str(timer_id),
                )

            contract = db.execute(
                select(BillingContract)
                .where(BillingContract.id == contract_id)
                .with_for_update()
            ).scalar_one_or_none()
            if contract is None:
                raise _error(
                    "contract_not_found",
                    "The pending-terms timer references no contract.",
                    contract_id=str(contract_id),
                )
            draft = db.execute(
                select(BillingContractVersion)
                .where(
                    BillingContractVersion.contract_id == contract.id,
                    BillingContractVersion.version == expected_source_version,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if draft is None or draft.status is not BillingContractVersionStatus.draft:
                raise _error(
                    "pending_contract_version_not_found",
                    "The timer's pending contract version is absent or no longer draft.",
                    contract_id=str(contract.id),
                    expected_source_version=expected_source_version,
                )
            current = BillingContracts._current_effective(db, contract_id=contract.id)
            if current is None or draft.supersedes_id != current.id:
                raise _error(
                    "stale_pending_contract_terms",
                    "The pending version no longer supersedes the current contract.",
                    draft_version_id=str(draft.id),
                )
            draft_start = _aware_utc(draft.starts_at)
            timer_due = _aware_utc(timer.due_at)
            if draft_start is None or timer_due != draft_start:
                raise _error(
                    "invalid_pending_terms_timer",
                    "The timer due time differs from the draft boundary.",
                    timer_id=str(timer.id),
                )

            current.ends_at = draft.starts_at
            current.status = BillingContractVersionStatus.superseded
            current.superseded_at = draft.starts_at
            db.flush()
            draft.status = BillingContractVersionStatus.effective
            db.flush()

            line_ids = tuple(
                db.execute(
                    select(BillingContractLine.id).where(
                        BillingContractLine.contract_version_id == draft.id
                    )
                )
                .scalars()
                .all()
            )
            result = ContractVersionResult(
                contract_id=contract.id,
                version_id=draft.id,
                version=draft.version,
                authority=draft.authority,
                line_ids=line_ids,
                replayed=False,
            )
            is_live_change = draft.source_kind is BillingContractSourceKind.plan_change
            BillingContracts._stage_shadow_recorded_output(
                db,
                sales_order_id=BillingContracts._sales_order_anchor(
                    db, contract_id=contract.id
                ),
                results=(result,),
                context=context,
                change_kind=(
                    "recurring_addon_purchase"
                    if is_live_change
                    else "recurring_addon_backfill"
                ),
                envelope_source_kind=(
                    "subscription" if is_live_change else "sales_order"
                ),
                envelope_source_id=(
                    contract.subscription_id if is_live_change else None
                ),
                subscription_id=(contract.subscription_id if is_live_change else None),
            )
            return result

        return execute_owner_command(
            db,
            definition=_ACTIVATE_PENDING_TERMS_COMMAND,
            context=context,
            operation=lambda: consume_owner_output(
                db,
                consumer=OWNER,
                event_id=event_id,
                event_type=PENDING_TERMS_EFFECTIVE_TRIGGER,
                producer_owner="runtime.durable_timers",
                context=context,
                operation=_effect,
            )[0],
        )

    @staticmethod
    def _stage_shadow_recorded_output(
        db: Session,
        *,
        sales_order_id: UUID,
        results: tuple[ContractVersionResult, ...],
        context: CommandContext,
        change_kind: str,
        envelope_source_kind: str = "sales_order",
        envelope_source_id: UUID | None = None,
        subscription_id: UUID | None = None,
    ) -> UUID:
        obligation_inputs: list[dict[str, object]] = []
        for result in results:
            lines = db.execute(
                select(BillingContractLine).where(
                    BillingContractLine.id.in_(result.line_ids),
                    BillingContractLine.is_finite.is_(False),
                )
            ).scalars()
            for line in lines:
                obligation_inputs.append(
                    {
                        "contract_version_id": str(result.version_id),
                        "contract_line_key": str(line.contract_line_key),
                        "period_index": 0,
                    }
                )
        return stage_owner_output(
            db,
            OwnerOutputEnvelope(
                event_type=EventType.custom,
                producer_owner=OWNER,
                source_kind=envelope_source_kind,
                source_id=envelope_source_id or sales_order_id,
                schema_version=2,
            ),
            {
                "output": "billing.contracts.shadow_recorded",
                "sales_order_id": str(sales_order_id),
                "contract_change_kind": change_kind,
                **(
                    {"subscription_id": str(subscription_id)}
                    if subscription_id is not None
                    else {}
                ),
                "contracts": [
                    {
                        "contract_id": str(result.contract_id),
                        "contract_version_id": str(result.version_id),
                        "authority": result.authority.value,
                    }
                    for result in results
                ],
                "obligations": obligation_inputs,
            },
            context=context,
        )

    @staticmethod
    def _create_pending_version(
        db: Session,
        *,
        contract: BillingContract,
        current: BillingContractVersion,
        effective_at: datetime,
        context: CommandContext,
        source_kind: BillingContractSourceKind = BillingContractSourceKind.plan_change,
        reason: str = "Pending recurring add-on terms",
    ) -> BillingContractVersion:
        """Clone current terms into one mutable, non-effective boundary draft."""

        current_start = _aware_utc(current.starts_at)
        if current_start is None or effective_at <= current_start:
            raise _error(
                "invalid_pending_contract_boundary",
                "Pending terms must begin after the current version.",
                current_version_id=str(current.id),
                effective_at=effective_at.isoformat(),
            )
        next_version = (
            db.execute(
                select(BillingContractVersion.version)
                .where(BillingContractVersion.contract_id == contract.id)
                .order_by(BillingContractVersion.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            or 0
        ) + 1
        draft = BillingContractVersion(
            contract_id=contract.id,
            version=next_version,
            status=BillingContractVersionStatus.draft,
            authority=current.authority,
            account_id=current.account_id,
            subscription_id=current.subscription_id,
            source_kind=source_kind,
            source_id=current.subscription_id,
            source_version=current.source_version + 1,
            starts_at=effective_at,
            ends_at=None,
            contracted_price=current.contracted_price,
            currency=current.currency,
            rate_basis=current.rate_basis,
            rate_unit=current.rate_unit,
            rate_quantity=current.rate_quantity,
            service_interval_unit=current.service_interval_unit,
            service_interval_count=current.service_interval_count,
            invoice_interval_unit=current.invoice_interval_unit,
            invoice_interval_count=current.invoice_interval_count,
            collection_timing=current.collection_timing,
            alignment=current.alignment,
            anchor_day=current.anchor_day,
            end_of_month_rule=current.end_of_month_rule,
            timezone_name=current.timezone_name,
            proration_policy=current.proration_policy,
            payment_terms_days=current.payment_terms_days,
            tax_treatment_code=current.tax_treatment_code,
            tax_inclusive=current.tax_inclusive,
            discount_code=current.discount_code,
            discount_amount=current.discount_amount,
            supersedes_id=current.id,
            actor=context.actor,
            reason=reason,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
            idempotency_key=(f"pending:{contract.id}:{effective_at.isoformat()}"),
        )
        db.add(draft)
        db.flush()
        current_lines = tuple(
            db.execute(
                select(BillingContractLine).where(
                    BillingContractLine.contract_version_id == current.id
                )
            )
            .scalars()
            .all()
        )
        for line in current_lines:
            db.add(
                BillingContractLine(
                    contract_version_id=draft.id,
                    contract_line_key=line.contract_line_key,
                    charge_component=line.charge_component,
                    component_key=line.component_key,
                    description=line.description,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    currency=line.currency,
                    accounting_treatment=line.accounting_treatment,
                    is_finite=line.is_finite,
                    tax_treatment_code=line.tax_treatment_code,
                )
            )
        db.flush()
        return draft

    @staticmethod
    def _sales_order_anchor(db: Session, *, contract_id: UUID) -> UUID:
        """Resolve the immutable sale that opened this structural contract."""

        sales_order_id = db.execute(
            select(SalesOrderLine.sales_order_id)
            .join(
                BillingContractVersion,
                BillingContractVersion.source_id == SalesOrderLine.id,
            )
            .where(
                BillingContractVersion.contract_id == contract_id,
                BillingContractVersion.source_kind
                == BillingContractSourceKind.sales_order_line,
            )
            .order_by(BillingContractVersion.version.asc())
            .limit(1)
        ).scalar_one_or_none()
        if sales_order_id is None:
            raise _error(
                "sales_order_anchor_mismatch",
                "The billing contract has no structural sales-order anchor.",
                contract_id=str(contract_id),
            )
        return sales_order_id

    @staticmethod
    def _record_version(
        db: Session,
        *,
        command: RecordContractVersionCommand,
        context: CommandContext,
    ) -> ContractVersionResult:
        _validate(command)
        if not context.idempotency_key:
            raise _error(
                "missing_idempotency_key",
                "Recording contract terms requires a business idempotency key.",
            )

        authority = permitted_authority()
        contract = BillingContracts._ensure_contract(
            db,
            account_id=command.account_id,
            subscription_id=command.subscription_id,
            authority=authority,
        )

        existing = db.execute(
            select(BillingContractVersion).where(
                BillingContractVersion.contract_id == contract.id,
                BillingContractVersion.idempotency_key == context.idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Named apart from the list built for a fresh version below: this
            # one is already the finished tuple read back off the replay.
            replayed_line_ids = tuple(
                db.execute(
                    select(BillingContractLine.id).where(
                        BillingContractLine.contract_version_id == existing.id
                    )
                )
                .scalars()
                .all()
            )
            return ContractVersionResult(
                contract_id=contract.id,
                version_id=existing.id,
                version=existing.version,
                authority=existing.authority,
                line_ids=replayed_line_ids,
                replayed=True,
            )

        current = BillingContracts._current_effective(db, contract_id=contract.id)
        current_starts_at = (
            _aware_utc(current.starts_at) if current is not None else None
        )
        if current_starts_at is not None and command.starts_at <= current_starts_at:
            # Equal starts would close the previous version into a zero-length
            # interval, which its own check constraint forbids.
            raise _error(
                "out_of_order_contract_version",
                "A new version must start after the current effective one.",
                current_starts_at=current_starts_at.isoformat(),
                requested_starts_at=command.starts_at.isoformat(),
            )

        next_version = (
            db.execute(
                select(BillingContractVersion.version)
                .where(BillingContractVersion.contract_id == contract.id)
                .order_by(BillingContractVersion.version.desc())
                .limit(1)
            ).scalar_one_or_none()
            or 0
        ) + 1

        if current is not None:
            # Close the previous version *before* inserting the new one. Both
            # rows would otherwise satisfy the "one open-ended effective
            # version per contract" partial unique index at the same instant.
            # The intervals stay contiguous and half-open: no gap, no overlap.
            current.ends_at = command.starts_at
            current.status = BillingContractVersionStatus.superseded
            current.superseded_at = command.starts_at
            db.flush()

        cadence = command.cadence
        version = BillingContractVersion(
            contract_id=contract.id,
            version=next_version,
            status=BillingContractVersionStatus.effective,
            authority=authority,
            account_id=command.account_id,
            subscription_id=command.subscription_id,
            source_kind=command.source_kind,
            source_id=command.source_id,
            source_version=command.source_version,
            starts_at=command.starts_at,
            ends_at=command.ends_at,
            contracted_price=command.contracted_price,
            currency=command.currency,
            rate_basis=cadence.rate_basis,
            rate_unit=cadence.rate_unit,
            rate_quantity=cadence.rate_quantity,
            service_interval_unit=cadence.service_interval_unit,
            service_interval_count=cadence.service_interval_count,
            invoice_interval_unit=cadence.invoice_interval_unit,
            invoice_interval_count=cadence.invoice_interval_count,
            collection_timing=cadence.collection_timing,
            alignment=cadence.alignment,
            anchor_day=cadence.anchor_day,
            end_of_month_rule=cadence.end_of_month_rule,
            timezone_name=cadence.timezone_name,
            proration_policy=cadence.proration_policy,
            payment_terms_days=command.payment_terms_days,
            tax_treatment_code=command.tax_treatment_code,
            tax_inclusive=command.tax_inclusive,
            discount_code=command.discount_code,
            discount_amount=command.discount_amount,
            supersedes_id=current.id if current is not None else None,
            actor=context.actor,
            reason=context.reason,
            command_id=context.command_id,
            correlation_id=context.correlation_id,
            idempotency_key=context.idempotency_key,
        )
        db.add(version)
        db.flush()

        line_ids: list[UUID] = []
        for line in command.lines:
            record = BillingContractLine(
                contract_version_id=version.id,
                contract_line_key=line.contract_line_key
                or BillingContracts._inherited_line_key(
                    db,
                    contract_id=contract.id,
                    charge_component=line.charge_component,
                    component_key=line.component_key,
                ),
                charge_component=line.charge_component,
                component_key=line.component_key,
                description=line.description,
                quantity=line.quantity,
                unit_price=line.unit_price,
                currency=line.currency,
                accounting_treatment=line.accounting_treatment,
                is_finite=line.is_finite,
                tax_treatment_code=line.tax_treatment_code,
            )
            db.add(record)
            db.flush()
            line_ids.append(record.id)

        return ContractVersionResult(
            contract_id=contract.id,
            version_id=version.id,
            version=version.version,
            authority=authority,
            line_ids=tuple(line_ids),
            replayed=False,
        )

    @staticmethod
    def _ensure_contract(
        db: Session,
        *,
        account_id: UUID,
        subscription_id: UUID,
        authority: BillingRecordAuthority,
    ) -> BillingContract:
        contract = db.execute(
            select(BillingContract)
            .where(BillingContract.subscription_id == subscription_id)
            .with_for_update()
        ).scalar_one_or_none()
        if contract is not None:
            if contract.account_id != account_id:
                raise _error(
                    "contract_account_mismatch",
                    "Subscription already has a contract under another account.",
                    contract_account_id=str(contract.account_id),
                    requested_account_id=str(account_id),
                )
            return contract

        contract = BillingContract(
            account_id=account_id,
            subscription_id=subscription_id,
            authority=authority,
        )
        db.add(contract)
        db.flush()
        # Re-take the row lock now that it exists, so a concurrent command
        # serialises behind this one rather than racing the version insert.
        lock_for_update(db, BillingContract, contract.id)
        return contract

    @staticmethod
    def _inherited_line_key(
        db: Session,
        *,
        contract_id: UUID,
        charge_component: ChargeComponent,
        component_key: str,
    ) -> UUID:
        """Reuse the lineage key so obligations survive a supersession."""

        existing = db.execute(
            select(BillingContractLine.contract_line_key)
            .join(
                BillingContractVersion,
                BillingContractLine.contract_version_id == BillingContractVersion.id,
            )
            .where(
                BillingContractVersion.contract_id == contract_id,
                BillingContractLine.charge_component == charge_component,
                BillingContractLine.component_key == component_key,
            )
            .order_by(BillingContractVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        return existing if existing is not None else uuid4()

    @staticmethod
    def _current_effective(
        db: Session, *, contract_id: UUID
    ) -> BillingContractVersion | None:
        return db.execute(
            select(BillingContractVersion)
            .where(
                BillingContractVersion.contract_id == contract_id,
                BillingContractVersion.status == BillingContractVersionStatus.effective,
                BillingContractVersion.ends_at.is_(None),
            )
            .with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def effective_version_at(
        db: Session, *, subscription_id: UUID, moment: datetime
    ) -> BillingContractVersion | None:
        """Return the one version effective for ``subscription_id`` at ``moment``.

        Read-only resolver. ADR 0007 invariant 1: at most one version is
        effective for a contract line at an instant, so this returns at most
        one row.
        """

        if moment.tzinfo is None:
            raise _error(
                "invalid_contract_terms",
                "Effective-version lookup requires a timezone-aware instant.",
            )
        return db.execute(
            select(BillingContractVersion)
            .join(
                BillingContract,
                BillingContractVersion.contract_id == BillingContract.id,
            )
            .where(
                BillingContract.subscription_id == subscription_id,
                BillingContractVersion.status.in_(
                    (
                        BillingContractVersionStatus.effective,
                        BillingContractVersionStatus.superseded,
                    )
                ),
                BillingContractVersion.starts_at <= moment,
                (BillingContractVersion.ends_at.is_(None))
                | (BillingContractVersion.ends_at > moment),
            )
            .order_by(BillingContractVersion.version.desc())
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def cadence_of(version: BillingContractVersion) -> BillingCadence:
        """Rebuild the typed cadence value object from a stored version."""

        return BillingCadence(
            rate_basis=RateBasis(version.rate_basis),
            rate_unit=IntervalUnit(version.rate_unit),
            rate_quantity=version.rate_quantity,
            service_interval_unit=IntervalUnit(version.service_interval_unit),
            service_interval_count=version.service_interval_count,
            invoice_interval_unit=IntervalUnit(version.invoice_interval_unit),
            invoice_interval_count=version.invoice_interval_count,
            collection_timing=CollectionTiming(version.collection_timing),
            alignment=CadenceAlignment(version.alignment),
            timezone_name=version.timezone_name,
            end_of_month_rule=EndOfMonthRule(version.end_of_month_rule),
            proration_policy=ProrationPolicy(version.proration_policy),
            anchor_day=version.anchor_day,
        )

    @staticmethod
    def cancel_version(
        db: Session,
        *,
        version_id: UUID,
        context: CommandContext,
    ) -> UUID:
        """Cancel a version the owner issued in error.

        A correction originates only from the owner of the wrong fact
        (ADR 0007 invariant 20); history is closed, never edited away.
        """

        return execute_owner_command(
            db,
            definition=_SUPERSEDE_COMMAND,
            context=context,
            operation=lambda: BillingContracts._cancel_version(
                db, version_id=version_id, context=context
            ),
        )

    @staticmethod
    def _cancel_version(
        db: Session, *, version_id: UUID, context: CommandContext
    ) -> UUID:
        version = lock_for_update(db, BillingContractVersion, version_id)
        if version is None:
            raise _error(
                "contract_version_not_found",
                "Contract version does not exist.",
                version_id=str(version_id),
            )
        if version.status is BillingContractVersionStatus.canceled:
            return version.id
        version.status = BillingContractVersionStatus.canceled
        version.superseded_at = version.superseded_at or version.starts_at
        version.reason = context.reason
        db.flush()
        return version.id
