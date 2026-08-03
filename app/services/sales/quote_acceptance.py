"""Atomic, idempotent Lead-to-service conversion owned by Quote acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, TypeAlias
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.audit import AuditActorType
from app.models.party import Party, PartyContactPoint, PartyContactPointType, PartyType
from app.models.project import ProjectTask, ProjectTemplateTask
from app.models.sales import Quote, QuoteStatus
from app.models.subscriber import SubscriberCategory
from app.models.work_order import WorkOrder
from app.schemas.subscriber import SubscriberCreate
from app.services import projects, sales_fulfillment, sales_orders
from app.services.audit_adapter import stage_audit_event
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sales import account_conversion
from app.services.sales import lifecycle as lead_lifecycle
from app.services.work_order_commands import (
    AutomatedProjectTaskWorkOrderCommand,
    work_order_commands,
)

_ACCEPT_QUOTE = OwnerCommandDefinition(
    owner="sales.quote_acceptance",
    concern="atomic accepted-Quote sales conversion",
    name="accept_quote",
)


class QuoteAcceptanceError(DomainError):
    """Stable failure raised by the quote-acceptance coordinator."""


AcceptedQuoteMutation: TypeAlias = Literal[
    "quote_fields",
    "quote_deactivation",
    "line_item_create",
    "line_item_update",
    "line_item_delete",
]


@dataclass(frozen=True)
class AcceptQuoteCommand:
    context: CommandContext
    quote_id: UUID
    deposit: QuoteAcceptanceDeposit | None = None


@dataclass(frozen=True)
class QuoteAcceptanceDeposit:
    reference: str
    amount: Decimal
    provider: str | None = None


@dataclass(frozen=True)
class QuoteAcceptanceDepositEvidence:
    reference: str
    amount: Decimal
    provider: str | None


@dataclass(frozen=True)
class QuoteAcceptanceOutcome:
    quote_id: UUID
    lead_id: UUID
    subscriber_id: UUID
    sales_order_id: UUID
    project_id: UUID
    project_template_id: UUID
    project_task_ids: tuple[UUID, ...]
    work_order_ids: tuple[UUID, ...]
    deposit: QuoteAcceptanceDepositEvidence | None
    replayed: bool


def _error(suffix: str, message: str, **details: object) -> QuoteAcceptanceError:
    return QuoteAcceptanceError(
        code=f"sales.quote_acceptance.{suffix}",
        message=message,
        details=details,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def assert_quote_mutable(
    quote: Quote,
    *,
    mutation: AcceptedQuoteMutation,
) -> None:
    """Reject changes that would corrupt an accepted commercial snapshot.

    Acceptance copies the Quote and its lines into a SalesOrder. From that
    transition onward, the accepted Quote is immutable evidence of what the
    business approved; later commercial changes require a new Quote.
    """

    if quote.status != QuoteStatus.accepted.value:
        return
    raise _error(
        "accepted_quote_immutable",
        "An accepted Quote and its line items cannot be changed; create a new "
        "Quote for revised commercial terms",
        quote_id=str(quote.id),
        attempted_mutation=mutation,
    )


def _assert_quote_not_expired(
    quote: Quote,
    *,
    acceptance_attempted_at: datetime,
) -> None:
    expires_at = quote.expires_at
    if expires_at is None:
        return
    expires_at_utc = (
        expires_at.replace(tzinfo=UTC)
        if expires_at.tzinfo is None
        else expires_at.astimezone(UTC)
    )
    attempted_at_utc = (
        acceptance_attempted_at.replace(tzinfo=UTC)
        if acceptance_attempted_at.tzinfo is None
        else acceptance_attempted_at.astimezone(UTC)
    )
    if expires_at_utc > attempted_at_utc:
        return
    raise _error(
        "quote_expired",
        "This Quote has expired and cannot be accepted; create a new Quote with "
        "current commercial terms",
        quote_id=str(quote.id),
        expires_at=expires_at_utc.isoformat(),
        acceptance_attempted_at=attempted_at_utc.isoformat(),
    )


def _locked_quote(db: Session, quote_id: UUID) -> Quote:
    quote = db.scalars(
        select(Quote)
        .where(Quote.id == quote_id)
        .options(selectinload(Quote.lead), selectinload(Quote.line_items))
        .with_for_update()
    ).one_or_none()
    if quote is None or not quote.is_active:
        raise _error("quote_not_found", "Quote not found")
    if quote.lead is None or quote.lead_id is None:
        raise _error("lead_required", "Quote acceptance requires an exact Lead")
    if quote.lead.party_id is None:
        raise _error(
            "lead_party_required",
            "Quote acceptance requires a reviewed Party-bound Lead",
        )
    return quote


def _primary_contact(
    db: Session, *, party_id: UUID, channel_type: PartyContactPointType
) -> str | None:
    rows = db.scalars(
        select(PartyContactPoint)
        .where(
            PartyContactPoint.party_id == party_id,
            PartyContactPoint.channel_type == channel_type.value,
            PartyContactPoint.is_active.is_(True),
        )
        .order_by(
            PartyContactPoint.is_primary.desc(),
            PartyContactPoint.created_at.asc(),
            PartyContactPoint.id.asc(),
        )
    ).all()
    return rows[0].normalized_value if rows else None


def _account_from_lead_party(db: Session, quote: Quote) -> SubscriberCreate:
    lead = quote.lead
    assert lead is not None and lead.party_id is not None
    party = db.scalars(
        select(Party).where(Party.id == lead.party_id).with_for_update()
    ).one_or_none()
    if party is None:
        raise _error("party_not_found", "Lead Party was not found")
    name_parts = party.display_name.strip().split(maxsplit=1)
    is_organization = party.party_type == PartyType.organization.value
    if len(name_parts) != 2 and not is_organization:
        raise _error(
            "account_profile_incomplete",
            "Lead Party needs a first and last name before Quote acceptance",
            missing_field="party.display_name",
        )
    email = _primary_contact(
        db, party_id=party.id, channel_type=PartyContactPointType.email
    )
    if email is None:
        raise _error(
            "account_profile_incomplete",
            "Lead Party needs an email contact before Quote acceptance",
            missing_field="party.email",
        )
    phone = _primary_contact(
        db, party_id=party.id, channel_type=PartyContactPointType.phone
    )
    try:
        return SubscriberCreate(
            first_name=name_parts[0][:80],
            last_name=(name_parts[1] if len(name_parts) == 2 else "Account")[:80],
            display_name=party.display_name[:120],
            company_name=party.display_name[:160] if is_organization else None,
            category=(
                SubscriberCategory.business
                if is_organization
                else SubscriberCategory.residential
            ),
            email=email,
            phone=phone[:40] if phone else None,
            address_line1=(lead.address or "").strip()[:120] or None,
            region=(lead.region or "").strip()[:80] or None,
        )
    except ValidationError as exc:
        raise _error(
            "account_profile_invalid",
            "Lead Party account data is not valid for conversion",
        ) from exc


def _convert_account(db: Session, quote: Quote, actor: str) -> UUID:
    lead = quote.lead
    assert lead is not None and lead.party_id is not None
    existing_target = lead.subscriber_id or quote.subscriber_id
    result = account_conversion.stage_lead_account_conversion(
        db,
        lead_id=lead.id,
        party_id=lead.party_id,
        actor_id=actor,
        subscriber_id=existing_target,
        new_account=(None if existing_target else _account_from_lead_party(db, quote)),
    )
    if quote.subscriber_id not in (None, result.subscriber_id):
        raise _error(
            "quote_account_conflict",
            "Quote account does not match the converted Lead account",
        )
    quote.subscriber_id = result.subscriber_id
    return result.subscriber_id


def _stage_deposit_evidence(
    quote: Quote,
    deposit: QuoteAcceptanceDeposit | None,
) -> QuoteAcceptanceDepositEvidence | None:
    if deposit is None:
        return None
    reference = deposit.reference.strip()
    provider = (deposit.provider or "").strip() or None
    if not reference or not deposit.amount.is_finite() or deposit.amount < 0:
        raise _error(
            "deposit_evidence_invalid",
            "Quote acceptance deposit evidence is incomplete or invalid",
        )
    normalized = QuoteAcceptanceDepositEvidence(
        reference=reference,
        amount=round_money(deposit.amount),
        provider=provider,
    )
    evidence = {
        "reference": normalized.reference,
        "amount": str(normalized.amount),
        "provider": normalized.provider,
        "paid": True,
    }
    metadata = dict(quote.metadata_ or {})
    existing = metadata.get("deposit")
    if existing == evidence:
        return normalized
    if existing != evidence and (
        existing is not None or quote.status == QuoteStatus.accepted.value
    ):
        raise _error(
            "deposit_evidence_conflict",
            "Quote already carries different deposit evidence",
            quote_id=str(quote.id),
            submitted_reference=normalized.reference,
            submitted_amount=str(normalized.amount),
            submitted_provider=normalized.provider,
        )
    metadata["deposit"] = evidence
    quote.metadata_ = metadata
    return normalized


def _automated_work_orders(
    db: Session,
    *,
    project_id: UUID,
    subscriber_id: UUID,
    address: str | None,
) -> tuple[UUID, ...]:
    configured = db.execute(
        select(ProjectTask, ProjectTemplateTask)
        .join(
            ProjectTemplateTask,
            ProjectTemplateTask.id == ProjectTask.template_task_id,
        )
        .where(
            ProjectTask.project_id == project_id,
            ProjectTask.is_active.is_(True),
        )
        .order_by(ProjectTemplateTask.sort_order.asc(), ProjectTask.id.asc())
    ).all()
    ids: list[UUID] = []
    for task, template_task in configured:
        automation = projects.resolve_project_task_work_order_automation(
            task,
            template_task,
        )
        if not automation.auto_create:
            continue
        row = work_order_commands.stage_automated_project_task_work_order(
            db,
            AutomatedProjectTaskWorkOrderCommand(
                project_id=project_id,
                project_task_id=task.id,
                subscriber_id=subscriber_id,
                title=task.title,
                description=task.description,
                priority=task.priority,
                address=(address or "")[:255] or None,
                requires_as_built_evidence=automation.requires_as_built_evidence,
                idempotency_key=f"quote-acceptance:project-task:{task.id}",
            ),
        )
        ids.append(row.id)
    return tuple(ids)


def _stage_accept_quote(
    db: Session, command: AcceptQuoteCommand
) -> QuoteAcceptanceOutcome:
    """Stage one accepted-Quote conversion in the caller's transaction.

    ``accept_quote`` invokes this private participant inside its sole owner
    transaction. Quote authoring cannot invoke the conversion pipeline.
    """

    quote = _locked_quote(db, command.quote_id)
    was_accepted = quote.status == QuoteStatus.accepted.value
    if not was_accepted and quote.status not in {
        QuoteStatus.draft.value,
        QuoteStatus.sent.value,
    }:
        raise _error(
            "invalid_transition",
            "Only Draft or Sent Quotes can transition to Accepted",
            current_status=quote.status,
        )
    if not was_accepted:
        _assert_quote_not_expired(
            quote,
            acceptance_attempted_at=_utc_now(),
        )
    if not quote.line_items:
        raise _error(
            "line_items_required",
            "Add at least one line item before accepting this Quote",
        )
    deposit_evidence = _stage_deposit_evidence(quote, command.deposit)

    subscriber_id = _convert_account(db, quote, command.context.actor)
    lead = quote.lead
    assert lead is not None
    lead_lifecycle.stage_quote_acceptance(db, lead=lead)

    order = sales_orders.sales_orders._stage_from_quote_acceptance(
        db, quote=quote, subscriber_id=subscriber_id
    )
    scope = sales_fulfillment.ensure_implementation_scope(
        db,
        sales_order_id=order.id,
        actor_id=command.context.actor,
        commit=False,
    )
    project_tasks = tuple(
        db.scalars(
            select(ProjectTask.id)
            .where(
                ProjectTask.project_id == scope.project.id,
                ProjectTask.is_active.is_(True),
            )
            .order_by(ProjectTask.created_at.asc(), ProjectTask.id.asc())
        ).all()
    )
    _automated_work_orders(
        db,
        project_id=scope.project.id,
        subscriber_id=subscriber_id,
        address=scope.project.customer_address,
    )
    work_order_ids = tuple(
        db.scalars(
            select(WorkOrder.id)
            .where(
                WorkOrder.project_id == scope.project.id,
                WorkOrder.is_active.is_(True),
            )
            .order_by(WorkOrder.created_at.asc(), WorkOrder.id.asc())
        ).all()
    )
    project_template_id = scope.project.project_template_id
    if project_template_id is None:
        raise _error(
            "project_template_required",
            "Accepted Quote Project is missing its configured Project Template",
        )

    if not was_accepted:
        quote.status = QuoteStatus.accepted.value
        emit_event(
            db,
            EventType.quote_accepted,
            {
                "quote_id": str(quote.id),
                "lead_id": str(lead.id),
                "subscriber_id": str(subscriber_id),
                "sales_order_id": str(order.id),
                "project_id": str(scope.project.id),
                "project_template_id": str(project_template_id),
                "total": str(quote.total or 0),
                "currency": quote.currency,
            },
            actor=command.context.actor,
            subscriber_id=subscriber_id,
        )
        stage_audit_event(
            db,
            action="quote.accepted",
            entity_type="quote",
            entity_id=str(quote.id),
            actor_type=AuditActorType.system,
            actor_id=command.context.actor,
            request_id=str(command.context.command_id),
            metadata={
                "lead_id": str(lead.id),
                "subscriber_id": str(subscriber_id),
                "sales_order_id": str(order.id),
                "project_id": str(scope.project.id),
                "project_template_id": str(project_template_id),
                "project_task_count": len(project_tasks),
                "work_order_count": len(work_order_ids),
            },
        )
    db.flush()
    return QuoteAcceptanceOutcome(
        quote_id=quote.id,
        lead_id=lead.id,
        subscriber_id=subscriber_id,
        sales_order_id=order.id,
        project_id=scope.project.id,
        project_template_id=project_template_id,
        project_task_ids=project_tasks,
        work_order_ids=work_order_ids,
        deposit=deposit_evidence,
        replayed=was_accepted,
    )


def accept_quote(db: Session, command: AcceptQuoteCommand) -> QuoteAcceptanceOutcome:
    """Accept one Quote and commit every conversion consequence exactly once."""

    def operation() -> QuoteAcceptanceOutcome:
        try:
            return _stage_accept_quote(db, command)
        except QuoteAcceptanceError:
            raise
        except account_conversion.LeadAccountConversionError as exc:
            raise _error(
                "participant_rejected",
                "The exact Lead/Party account conversion was rejected",
                participant_code=exc.code,
            ) from exc
        except ValueError as exc:
            raise _error(
                "participant_rejected",
                "A sales-conversion participant rejected Quote acceptance",
                participant_code=str(getattr(exc, "code", "participant_rejected")),
            ) from exc

    return execute_owner_command(
        db,
        definition=_ACCEPT_QUOTE,
        context=command.context,
        operation=operation,
    )
