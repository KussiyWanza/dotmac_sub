"""Atomic Lead-backed Draft/Sent Quote authoring."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.billing import TaxRate
from app.models.catalog import CatalogOffer, OfferStatus
from app.models.field_material import FieldInventoryItem
from app.models.party import PartyIdentityStatus
from app.models.project import ProjectType
from app.models.sales import Lead, LeadStatus, Quote, QuoteLineItem, QuoteStatus
from app.models.system_user import SystemUser
from app.services.audit_adapter import stage_audit_event
from app.services.common import round_money
from app.services.domain_errors import DomainError
from app.services.events import EventType, emit_event
from app.services.owner_commands import (
    CommandContext,
    OwnerCommandDefinition,
    execute_owner_command,
)
from app.services.sales.selfserve import compute_feasibility

_AUTHOR_QUOTE = OwnerCommandDefinition(
    owner="sales.quote_authoring",
    concern="atomic Lead-backed Draft/Sent Quote authoring",
    name="author_quote",
)

_ELIGIBLE_LEAD_STATUSES = {
    LeadStatus.new.value,
    LeadStatus.contacted.value,
    LeadStatus.qualified.value,
    LeadStatus.proposal.value,
    LeadStatus.negotiation.value,
}
_USABLE_PARTY_STATUSES = {
    PartyIdentityStatus.active.value,
    PartyIdentityStatus.quarantined.value,
}


class QuoteAuthoringError(DomainError):
    """Stable form-safe failure raised by the Quote authoring owner."""


@dataclass(frozen=True, slots=True)
class QuoteInstallLocation:
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    address: str | None = None
    region: str | None = None


@dataclass(frozen=True, slots=True)
class QuoteLineDraft:
    description: str
    quantity: Decimal
    unit_price: Decimal
    discount_percent: Decimal
    sub_offer_id: UUID | None = None
    inventory_item_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AuthorQuoteCommand:
    context: CommandContext
    quote_id: UUID
    actor_system_user_id: UUID
    lead_id: UUID
    status: QuoteStatus
    currency: str
    project_type: ProjectType
    tax_rate_id: UUID | None
    manual_tax_total: Decimal
    expires_at: datetime | None
    is_active: bool
    notes: str | None
    install: QuoteInstallLocation
    lines: tuple[QuoteLineDraft, ...]


@dataclass(frozen=True, slots=True)
class AuthorQuoteOutcome:
    quote_id: UUID
    replayed: bool


def _error(
    suffix: str, message: str, *, field: str | None = None, **details: object
) -> QuoteAuthoringError:
    payload = dict(details)
    if field:
        payload["field"] = field
    return QuoteAuthoringError(
        code=f"sales.quote_authoring.{suffix}", message=message, details=payload
    )


def _fingerprint(command: AuthorQuoteCommand) -> str:
    payload = {
        "actor_system_user_id": str(command.actor_system_user_id),
        "lead_id": str(command.lead_id),
        "status": command.status.value,
        "currency": command.currency,
        "project_type": command.project_type.value,
        "tax_rate_id": str(command.tax_rate_id) if command.tax_rate_id else None,
        "manual_tax_total": str(command.manual_tax_total),
        "expires_at": command.expires_at.isoformat() if command.expires_at else None,
        "is_active": command.is_active,
        "notes": command.notes,
        "install": {
            "latitude": str(command.install.latitude)
            if command.install.latitude is not None
            else None,
            "longitude": str(command.install.longitude)
            if command.install.longitude is not None
            else None,
            "address": command.install.address,
            "region": command.install.region,
        },
        "lines": [
            {
                "description": item.description,
                "quantity": str(item.quantity),
                "unit_price": str(item.unit_price),
                "discount_percent": str(item.discount_percent),
                "sub_offer_id": str(item.sub_offer_id) if item.sub_offer_id else None,
                "inventory_item_id": str(item.inventory_item_id)
                if item.inventory_item_id
                else None,
            }
            for item in command.lines
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _actor(db: Session, command: AuthorQuoteCommand) -> SystemUser:
    actor = db.scalars(
        select(SystemUser)
        .where(SystemUser.id == command.actor_system_user_id)
        .with_for_update()
    ).one_or_none()
    if actor is None or not actor.is_active:
        raise _error(
            "actor_not_eligible",
            "The authenticated staff user cannot create Quotes.",
        )
    return actor


def _lead(db: Session, lead_id: UUID) -> Lead:
    lead = db.scalars(
        select(Lead)
        .where(Lead.id == lead_id)
        .options(selectinload(Lead.party))
        .with_for_update()
    ).one_or_none()
    if lead is None:
        raise _error("lead_not_found", "Select a valid Lead.", field="lead_id")
    if not lead.is_active or lead.status not in _ELIGIBLE_LEAD_STATUSES:
        raise _error(
            "lead_not_eligible",
            "The selected Lead is inactive or no longer eligible for a new Quote.",
            field="lead_id",
        )
    if lead.party_id is None or lead.party is None:
        raise _error(
            "lead_person_required",
            "The selected Lead does not have a usable linked Person.",
            field="lead_id",
        )
    if lead.party.status not in _USABLE_PARTY_STATUSES:
        raise _error(
            "lead_person_ineligible",
            "The Person linked to the selected Lead cannot receive a Quote.",
            field="lead_id",
        )
    return lead


def _validated_lines(
    db: Session, drafts: tuple[QuoteLineDraft, ...]
) -> tuple[tuple[QuoteLineDraft, Decimal], ...]:
    offer_ids = {item.sub_offer_id for item in drafts if item.sub_offer_id is not None}
    inventory_ids = {
        item.inventory_item_id for item in drafts if item.inventory_item_id is not None
    }
    offers = (
        {
            item.id: item
            for item in db.scalars(
                select(CatalogOffer).where(
                    CatalogOffer.id.in_(offer_ids),
                    CatalogOffer.is_active.is_(True),
                    CatalogOffer.status == OfferStatus.active,
                )
            ).all()
        }
        if offer_ids
        else {}
    )
    inventory = (
        {
            item.id: item
            for item in db.scalars(
                select(FieldInventoryItem).where(
                    FieldInventoryItem.id.in_(inventory_ids),
                    FieldInventoryItem.is_active.is_(True),
                )
            ).all()
        }
        if inventory_ids
        else {}
    )

    if set(offers) != offer_ids:
        raise _error(
            "offer_not_active",
            "A selected service offer is missing or inactive.",
            field="line_items",
        )
    if set(inventory) != inventory_ids:
        raise _error(
            "inventory_item_not_active",
            "A selected inventory item is missing or inactive.",
            field="line_items",
        )

    validated: list[tuple[QuoteLineDraft, Decimal]] = []
    for item in drafts:
        description = item.description.strip()
        if not description or len(description) > 255:
            raise _error(
                "line_description_invalid",
                "Every Line Item needs a description of at most 255 characters.",
                field="line_items",
            )
        if not item.quantity.is_finite() or item.quantity <= 0:
            raise _error(
                "line_quantity_invalid",
                "Line Item quantity must be greater than zero.",
                field="line_items",
            )
        if not item.unit_price.is_finite() or item.unit_price < 0:
            raise _error(
                "line_price_invalid",
                "Line Item Unit Price cannot be negative.",
                field="line_items",
            )
        if not item.discount_percent.is_finite() or not Decimal(
            "0"
        ) <= item.discount_percent <= Decimal("100"):
            raise _error(
                "line_discount_invalid",
                "Line Item Discount must be between 0 and 100.",
                field="line_items",
            )
        if item.sub_offer_id is not None and item.inventory_item_id is not None:
            raise _error(
                "line_source_ambiguous",
                "A Line Item cannot reference both an offer and inventory item.",
                field="line_items",
            )
        if item.sub_offer_id is not None:
            offer = offers[item.sub_offer_id]
            if description.casefold() != offer.name.strip().casefold():
                raise _error(
                    "offer_description_mismatch",
                    "The selected service offer no longer matches its description.",
                    field="line_items",
                )
        if item.inventory_item_id is not None:
            stock = inventory[item.inventory_item_id]
            allowed_labels = {stock.name.strip().casefold()}
            if stock.sku:
                allowed_labels.add(f"{stock.name} — {stock.sku}".casefold())
            if description.casefold() not in allowed_labels:
                raise _error(
                    "inventory_description_mismatch",
                    "The selected inventory item no longer matches its description.",
                    field="line_items",
                )
        amount = round_money(
            item.quantity
            * item.unit_price
            * (Decimal("100") - item.discount_percent)
            / Decimal("100")
        )
        validated.append((item, amount))
    return tuple(validated)


def _install_metadata(db: Session, install: QuoteInstallLocation) -> dict[str, object]:
    latitude = install.latitude
    longitude = install.longitude
    if (latitude is None) != (longitude is None):
        raise _error(
            "install_pin_incomplete",
            "Drop a complete pin on the map: latitude and longitude go together.",
            field="install_location",
        )
    if latitude is not None and (
        not latitude.is_finite() or not Decimal("-90") <= latitude <= Decimal("90")
    ):
        raise _error(
            "latitude_invalid",
            "Latitude must be between -90 and 90.",
            field="install_location",
        )
    if longitude is not None and (
        not longitude.is_finite() or not Decimal("-180") <= longitude <= Decimal("180")
    ):
        raise _error(
            "longitude_invalid",
            "Longitude must be between -180 and 180.",
            field="install_location",
        )
    address = (install.address or "").strip() or None
    region = (install.region or "").strip() or None
    if latitude is None and longitude is None and address is None and region is None:
        return {}
    payload: dict[str, object] = {
        "install": {
            "latitude": float(latitude) if latitude is not None else None,
            "longitude": float(longitude) if longitude is not None else None,
            "address": address,
            "region": region,
        }
    }
    if latitude is not None and longitude is not None:
        payload["feasibility"] = compute_feasibility(
            db, float(latitude), float(longitude)
        )
    return payload


def _operation(db: Session, command: AuthorQuoteCommand) -> AuthorQuoteOutcome:
    if command.status not in {QuoteStatus.draft, QuoteStatus.sent}:
        raise _error(
            "initial_status_invalid",
            "A new Quote must be created as Draft or Sent; accept it separately.",
            field="status",
        )
    actor = _actor(db, command)
    fingerprint = _fingerprint(command)
    replay = db.get(Quote, command.quote_id)
    if replay is not None:
        replay_metadata = replay.metadata_ if isinstance(replay.metadata_, dict) else {}
        if replay_metadata.get(
            "authoring_fingerprint"
        ) != fingerprint or replay_metadata.get(
            "authoring_actor_system_user_id"
        ) != str(actor.id):
            raise _error(
                "submission_conflict",
                "This Quote submission was already used with different values.",
            )
        return AuthorQuoteOutcome(quote_id=replay.id, replayed=True)

    lead = _lead(db, command.lead_id)
    currency = command.currency.strip().upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise _error(
            "currency_invalid",
            "Currency must contain exactly three alphabetic characters.",
            field="currency",
        )
    lines = _validated_lines(db, command.lines)
    if command.status == QuoteStatus.sent and not lines:
        raise _error(
            "line_items_required",
            "Add at least one Line Item before sending this Quote.",
            field="line_items",
        )
    if not command.manual_tax_total.is_finite() or command.manual_tax_total < 0:
        raise _error(
            "manual_tax_invalid",
            "Tax Total cannot be negative.",
            field="manual_tax_total",
        )

    tax_rate = None
    if command.tax_rate_id is not None:
        tax_rate = db.scalars(
            select(TaxRate).where(
                TaxRate.id == command.tax_rate_id,
                TaxRate.is_active.is_(True),
            )
        ).one_or_none()
        if tax_rate is None:
            raise _error(
                "tax_rate_not_active",
                "Select an active configured Tax Rate.",
                field="tax_rate_id",
            )
        if not tax_rate.rate.is_finite() or not Decimal(
            "0"
        ) <= tax_rate.rate <= Decimal("100"):
            raise _error(
                "tax_rate_not_active",
                "The configured Tax Rate is not valid for Quote authoring.",
                field="tax_rate_id",
            )

    metadata: dict[str, object] = {
        "source": "admin",
        "quote_name": lead.party.display_name,
        "authoring_key": str(command.quote_id),
        "authoring_fingerprint": fingerprint,
        "authoring_actor_system_user_id": str(actor.id),
    }
    # Compatibility projection for readers that predate the typed Quote
    # column. Fulfillment reads ``Quote.project_type`` as the authority.
    metadata["project_type"] = command.project_type.value
    if tax_rate is not None:
        metadata["tax_rate_id"] = str(tax_rate.id)
    metadata.update(_install_metadata(db, command.install))

    subtotal = round_money(sum((amount for _item, amount in lines), Decimal("0.00")))
    tax_total = (
        round_money(subtotal * Decimal(tax_rate.rate) / Decimal("100"))
        if tax_rate is not None
        else round_money(command.manual_tax_total)
    )
    quote = Quote(
        id=command.quote_id,
        lead_id=lead.id,
        subscriber_id=None,
        owner_person_id=actor.id,
        status=command.status.value,
        project_type=command.project_type.value,
        currency=currency,
        subtotal=subtotal,
        tax_rate=Decimal(tax_rate.rate) if tax_rate is not None else None,
        tax_total=tax_total,
        total=round_money(subtotal + tax_total),
        expires_at=command.expires_at,
        sent_at=(datetime.now(UTC) if command.status == QuoteStatus.sent else None),
        notes=(command.notes or "").strip() or None,
        metadata_=metadata,
        is_active=command.is_active,
    )
    db.add(quote)
    db.flush()
    for draft, amount in lines:
        line_metadata = (
            {"sub_offer_id": str(draft.sub_offer_id)}
            if draft.sub_offer_id is not None
            else None
        )
        db.add(
            QuoteLineItem(
                quote_id=quote.id,
                inventory_item_id=draft.inventory_item_id,
                description=draft.description.strip(),
                quantity=draft.quantity,
                unit_price=draft.unit_price,
                discount_percent=draft.discount_percent,
                amount=amount,
                metadata_=line_metadata,
            )
        )
    emit_event(
        db,
        EventType.quote_created,
        {
            "quote_id": str(quote.id),
            "lead_id": str(lead.id),
            "person_id": str(lead.party_id),
            "status": command.status.value,
            "currency": quote.currency,
            "total": str(quote.total),
        },
        actor=command.context.actor,
    )
    stage_audit_event(
        db,
        action="quote.created",
        entity_type="quote",
        entity_id=str(quote.id),
        actor_id=str(actor.id),
        request_id=str(command.context.command_id),
        metadata={
            "lead_id": str(lead.id),
            "person_id": str(lead.party_id),
            "status": command.status.value,
            "line_count": len(lines),
        },
    )
    db.flush()
    return AuthorQuoteOutcome(quote_id=quote.id, replayed=False)


def author_quote(db: Session, command: AuthorQuoteCommand) -> AuthorQuoteOutcome:
    """Create one Lead-backed Draft/Sent Quote and its lines atomically."""

    return execute_owner_command(
        db,
        definition=_AUTHOR_QUOTE,
        context=command.context,
        operation=lambda: _operation(db, command),
    )
