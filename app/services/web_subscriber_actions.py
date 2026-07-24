"""Service helpers for web/admin subscriber action routes."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from app.models.subscriber import SubscriberCategory
from app.schemas.subscriber import SubscriberUpdate
from app.services import audit as audit_service
from app.services import subscriber as subscriber_service
from app.services import web_customer_actions as web_customer_actions_service
from app.services import web_system_restore_tool as web_system_restore_tool_service
from app.services.audit_helpers import log_audit_event
from app.services.web_subscriber_forms import (
    create_subscriber_with_optional_login,
    resolve_form_customer_ids,
)


def deactivate_subscriber(db: Session, subscriber_id: UUID):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=SubscriberUpdate(is_active=False),
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    return before, after


def _parse_category(value: str | None) -> SubscriberCategory | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    try:
        return SubscriberCategory(raw)
    except ValueError:
        return None


def create_subscriber_from_form(
    db: Session,
    *,
    customer_ref: str | None,
    customer_search: str | None,
    subscriber_type: str | None,
    person_id: str | None,
    business_account_id: str | None,
    subscriber_number: str | None,
    subscriber_category: str | None,
    notes: str | None,
    is_active: str | None,
    create_user: str | None,
    user_username: str | None,
    user_password: str | None,
):
    resolved_type, person_uuid, org_uuid = resolve_form_customer_ids(
        db=db,
        customer_ref=customer_ref,
        customer_search=customer_search,
        subscriber_type=subscriber_type,
        person_id=person_id,
        business_account_id=business_account_id,
    )
    if not person_uuid:
        raise ValueError("person_id is required")
    return create_subscriber_with_optional_login(
        db=db,
        subscriber_type=resolved_type,
        person_uuid=person_uuid,
        organization_uuid=org_uuid,
        subscriber_number=subscriber_number,
        subscriber_category=subscriber_category,
        notes=notes,
        is_active=is_active,
        create_user=create_user,
        user_username=user_username,
        user_password=user_password,
    )


def create_subscriber_from_full_form(
    db: Session,
    *,
    customer_type: str,
    form_data: dict[str, Any],
    contact_columns: dict[str, list[str]],
    subscriber_number: str | None,
    subscriber_category: str | None,
    subscriber_notes: str | None,
    is_active: str | None,
):
    created_type, created_id = web_customer_actions_service.create_customer_from_form(
        db=db,
        customer_type=customer_type,
        form_data=form_data,
        contact_columns=contact_columns,
    )

    if created_type == "person":
        subscriber_type = "person"
        person_uuid = UUID(created_id)
        organization_uuid = None
    else:
        subscriber_type = "business"
        organization_uuid = UUID(created_id)
        person_uuid = organization_uuid

    notes_value = subscriber_notes
    if not notes_value and customer_type == "person":
        notes_value = form_data.get("notes")

    return create_subscriber_with_optional_login(
        db=db,
        subscriber_type=subscriber_type,
        person_uuid=person_uuid,
        organization_uuid=organization_uuid,
        subscriber_number=subscriber_number,
        subscriber_category=subscriber_category,
        notes=notes_value,
        is_active=is_active,
        create_user=None,
        user_username=None,
        user_password=None,
    )


def update_subscriber_from_full_form(
    db: Session,
    *,
    subscriber_id: UUID,
    customer_type: str,
    form_data: dict[str, Any],
    subscriber_number: str | None,
    subscriber_category: str | None,
    subscriber_notes: str | None,
    is_active: str | None,
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    if not before:
        raise ValueError("Subscriber not found")

    if customer_type == "person":
        web_customer_actions_service.update_person_customer(
            db=db,
            customer_id=str(subscriber_id),
            first_name=form_data.get("first_name") or "",
            last_name=form_data.get("last_name") or "",
            display_name=form_data.get("display_name"),
            avatar_url=form_data.get("avatar_url"),
            email=form_data.get("email"),
            email_verified=form_data.get("email_verified"),
            phone=form_data.get("phone"),
            nin=form_data.get("nin"),
            date_of_birth=form_data.get("date_of_birth"),
            gender=form_data.get("gender"),
            preferred_contact_method=form_data.get("preferred_contact_method"),
            locale=form_data.get("locale"),
            timezone_value=form_data.get("timezone"),
            address_line1=form_data.get("address_line1"),
            address_line2=form_data.get("address_line2"),
            city=form_data.get("city"),
            region=form_data.get("region"),
            postal_code=form_data.get("postal_code"),
            country_code=form_data.get("country_code"),
            status=form_data.get("status"),
            is_active=is_active,
            marketing_opt_in=form_data.get("marketing_opt_in"),
            notes=form_data.get("notes"),
            account_start_date=form_data.get("account_start_date"),
            billing_enabled_override=form_data.get("billing_enabled_override"),
            billing_day=form_data.get("billing_day"),
            payment_due_days=form_data.get("payment_due_days"),
            grace_period_days=form_data.get("grace_period_days"),
            min_balance=form_data.get("min_balance"),
            captive_redirect_enabled=form_data.get("captive_redirect_enabled"),
            tax_rate_id=form_data.get("tax_rate_id"),
            withholding_tax_enabled=form_data.get("withholding_tax_enabled"),
            payment_method=form_data.get("payment_method"),
            metadata_json=form_data.get("metadata_json"),
        )
    elif customer_type == "business":
        web_customer_actions_service.update_business_customer(
            db=db,
            customer_id=str(before.id),
            name=form_data.get("name") or "",
            legal_name=form_data.get("legal_name"),
            tax_id=form_data.get("tax_id"),
            domain=form_data.get("domain"),
            website=form_data.get("website"),
            org_notes=form_data.get("org_notes"),
            org_account_start_date=form_data.get("org_account_start_date"),
            billing_enabled_override=form_data.get("billing_enabled_override"),
            billing_day=form_data.get("billing_day"),
            payment_due_days=form_data.get("payment_due_days"),
            grace_period_days=form_data.get("grace_period_days"),
            min_balance=form_data.get("min_balance"),
            captive_redirect_enabled=form_data.get("captive_redirect_enabled"),
            tax_rate_id=form_data.get("tax_rate_id"),
            withholding_tax_enabled=form_data.get("withholding_tax_enabled"),
            payment_method=form_data.get("payment_method"),
        )
    else:
        raise ValueError("customer_type must be person or business")

    payload = SubscriberUpdate(
        subscriber_number=subscriber_number.strip() if subscriber_number else None,
        category=_parse_category(subscriber_category),
        notes=(subscriber_notes or form_data.get("notes") or "").strip() or None,
        is_active=before.is_active if is_active is None else (is_active == "true"),
    )
    updated = subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=payload,
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    return updated, before, after


def build_subscriber_create_form_values(
    *,
    customer_ref: str | None,
    customer_search: str | None,
    subscriber_type: str | None,
    person_id: str | None,
    business_account_id: str | None,
    subscriber_number: str | None,
    subscriber_category: str | None,
    notes: str | None,
    is_active: str | None,
    create_user: str | None,
    user_username: str | None,
) -> dict:
    return {
        "customer_ref": customer_ref or "",
        "customer_search": customer_search or "",
        "subscriber_type": subscriber_type or "",
        "person_id": person_id or "",
        "business_account_id": business_account_id or "",
        "subscriber_number": subscriber_number or "",
        "subscriber_category": subscriber_category or "",
        "notes": notes or "",
        "is_active": is_active == "true",
        "create_user": create_user == "true",
        "user_username": user_username or "",
    }


def update_subscriber_from_form(
    db: Session,
    *,
    subscriber_id: UUID,
    customer_ref: str | None,
    customer_search: str | None,
    subscriber_type: str | None,
    person_id: str | None,
    business_account_id: str | None,
    subscriber_number: str | None,
    subscriber_category: str | None,
    notes: str | None,
    is_active: str | None,
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    resolved_type, _, org_uuid = resolve_form_customer_ids(
        db=db,
        customer_ref=customer_ref,
        customer_search=customer_search,
        subscriber_type=subscriber_type,
        person_id=person_id,
        business_account_id=business_account_id,
    )
    active = before.is_active if is_active is None else (is_active == "true")
    payload = SubscriberUpdate(
        subscriber_number=subscriber_number.strip() if subscriber_number else None,
        category=(
            SubscriberCategory.business
            if resolved_type == "business"
            else _parse_category(subscriber_category)
        ),
        notes=notes.strip() if notes else None,
        is_active=active,
    )
    subscriber = subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(subscriber_id),
        payload=payload,
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=str(subscriber_id))
    return subscriber, before, after


def delete_subscriber(
    db: Session, subscriber_id: UUID, actor_id: str | None = None
) -> None:
    subscriber = subscriber_service.subscribers.get(
        db=db, subscriber_id=str(subscriber_id)
    )
    if subscriber.is_active:
        raise HTTPException(
            status_code=409, detail="Deactivate subscriber before deleting."
        )
    web_system_restore_tool_service.mark_subscriber_deleted(
        db=db,
        subscriber_id=str(subscriber_id),
        actor_id=actor_id,
    )


def bulk_set_subscriber_status(
    db: Session, subscriber_ids: list[str], is_active: bool
) -> tuple[int, int]:
    updated_count = 0
    failed_count = 0
    for subscriber_id in subscriber_ids:
        try:
            subscriber_service.subscribers.update(
                db=db,
                subscriber_id=str(subscriber_id),
                payload=SubscriberUpdate(is_active=is_active),
            )
            updated_count += 1
        except Exception as exc:
            logger.error(
                "Failed to set status for subscriber %s: %s", subscriber_id, exc
            )
            failed_count += 1
            continue
    return updated_count, failed_count


def bulk_delete_inactive_subscribers(
    db: Session,
    subscriber_ids: list[str],
    actor_id: str | None = None,
) -> tuple[int, int, int]:
    deleted_count = 0
    skipped_active = 0
    failed_count = 0
    for subscriber_id in subscriber_ids:
        try:
            subscriber = subscriber_service.subscribers.get(
                db=db, subscriber_id=subscriber_id
            )
            if subscriber.is_active:
                skipped_active += 1
                continue
            web_system_restore_tool_service.mark_subscriber_deleted(
                db=db,
                subscriber_id=str(subscriber_id),
                actor_id=actor_id,
            )
            deleted_count += 1
        except Exception as exc:
            logger.error("Failed to delete subscriber %s: %s", subscriber_id, exc)
            failed_count += 1
            continue
    return deleted_count, skipped_active, failed_count


def toggle_comment_todo(
    db: Session,
    request: Request,
    *,
    subscriber_id: UUID,
    event_id: UUID,
    actor_id: str | None,
) -> bool:
    """Toggle todo completion state for a subscriber comment event.

    Returns:
        True if the todo was toggled, False if the event is not a todo.
    """
    event = audit_service.audit_events.get(db=db, event_id=str(event_id))
    if (
        event.entity_type != "subscriber"
        or str(event.entity_id) != str(subscriber_id)
        or event.action != "comment"
    ):
        raise HTTPException(status_code=404, detail="Comment not found")

    metadata = dict(getattr(event, "metadata_", None) or {})
    if not metadata.get("is_todo"):
        return False

    current_completed = bool(metadata.get("is_completed"))
    metadata["is_completed"] = not current_completed
    event.metadata_ = metadata
    db.add(event)
    db.commit()

    log_audit_event(
        db=db,
        request=request,
        action="comment_todo_toggle",
        entity_type="subscriber",
        entity_id=str(subscriber_id),
        actor_id=actor_id,
        metadata={
            "source_comment_event_id": str(event_id),
            "is_completed": metadata["is_completed"],
        },
    )
    return True
