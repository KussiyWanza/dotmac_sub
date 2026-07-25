"""Service helpers for subscriber create/edit web forms."""

import logging
import re
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.auth import AuthProvider
from app.models.domain_settings import SettingDomain
from app.models.subscriber import Subscriber, SubscriberCategory
from app.schemas.auth import UserCredentialCreate
from app.schemas.subscriber import SubscriberUpdate
from app.services import auth as auth_service
from app.services import settings_spec
from app.services import subscriber as subscriber_service
from app.services.auth_flow import hash_password

logger = logging.getLogger(__name__)


def parse_customer_ref(value: str | None) -> tuple[str, UUID]:
    if not value:
        raise ValueError("customer_ref is required")
    if ":" not in value:
        raise ValueError("customer_ref must be selected from the list")
    ref_type, ref_id = value.split(":", 1)
    if ref_type not in ("person", "business"):
        raise ValueError("customer_ref must be person or business")
    return ref_type, UUID(ref_id)


def resolve_customer_ref(
    db: Session,
    customer_ref: str | None,
    customer_search: str | None,
) -> tuple[str, UUID] | None:
    if customer_ref:
        try:
            return parse_customer_ref(customer_ref)
        except ValueError:
            # Some clients can submit raw UUIDs from typeahead items.
            # Accept those by resolving against subscriber ids directly.
            raw_ref = customer_ref.strip()
            try:
                raw_uuid = UUID(raw_ref)
            except (TypeError, ValueError):
                raw_uuid = None
            if raw_uuid:
                subscriber = db.get(Subscriber, raw_uuid)
                if subscriber:
                    return (
                        "business"
                        if subscriber.category == SubscriberCategory.business
                        else "person",
                        raw_uuid,
                    )
    search_term = (customer_search or "").strip()
    if not search_term:
        return None
    from app.services import customer_search as customer_search_service

    candidate_terms: list[str] = [search_term]

    # Support labels like: "John Doe (john@example.com)" by searching the inner token too.
    paren_match = re.search(r"\(([^()]+)\)\s*$", search_term)
    if paren_match:
        inner = paren_match.group(1).strip()
        outer = search_term[: paren_match.start()].strip()
        if inner:
            candidate_terms.append(inner)
        if outer:
            candidate_terms.append(outer)

    # Add conservative token fallback for full-name labels.
    parts = [part for part in re.split(r"\s+", search_term) if part]
    if len(parts) >= 2:
        candidate_terms.append(f"{parts[0]} {parts[-1]}")
    candidate_terms.extend(parts[:2])

    seen: set[str] = set()
    for term in candidate_terms:
        normalized = term.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        matches = customer_search_service.search(db=db, query=term, limit=2)
        if len(matches) == 1:
            return parse_customer_ref(matches[0].get("ref"))

    # If every strategy failed, keep message actionable for user flow.
    raise ValueError("No customer matches that search")


def resolve_subscriber_for_org(db: Session, org_id: UUID) -> UUID | None:
    subscriber = db.get(Subscriber, org_id)
    if subscriber and subscriber.category == SubscriberCategory.business:
        return cast(UUID | None, subscriber.id)
    return None


def resolve_form_customer_ids(
    db: Session,
    customer_ref: str | None,
    customer_search: str | None,
    subscriber_type: str | None,
    person_id: str | None,
    business_account_id: str | None,
) -> tuple[str, UUID | None, UUID | None]:
    resolved_ref = resolve_customer_ref(db, customer_ref, customer_search)
    if resolved_ref:
        resolved_type, ref_id = resolved_ref
        person_uuid = ref_id if resolved_type == "person" else None
        org_uuid = ref_id if resolved_type == "business" else None
        if resolved_type == "business":
            person_uuid = resolve_subscriber_for_org(db, org_uuid) if org_uuid else None
            if not person_uuid:
                raise ValueError("business subscriber is required")
        if not person_uuid:
            raise ValueError("person_id is required")
        return resolved_type, person_uuid, org_uuid

    if subscriber_type not in ("person", "business"):
        raise ValueError("customer_ref is required")

    person_uuid = UUID(person_id) if person_id else None
    org_uuid = UUID(business_account_id) if business_account_id else None
    if subscriber_type == "person" and not person_uuid:
        raise ValueError("person_id is required for person subscribers")
    if subscriber_type == "business" and not org_uuid:
        raise ValueError("business subscriber id is required for business subscribers")
    if subscriber_type == "business":
        person_uuid = resolve_subscriber_for_org(db, org_uuid) if org_uuid else None
    if not person_uuid:
        raise ValueError("person_id is required")
    return subscriber_type, person_uuid, org_uuid


def create_subscriber_with_optional_login(
    db: Session,
    subscriber_type: str,
    person_uuid: UUID,
    organization_uuid: UUID | None,
    subscriber_number: str | None,
    subscriber_category: str | None,
    notes: str | None,
    is_active: str | None,
    create_user: str | None,
    user_username: str | None,
    user_password: str | None,
):
    subscriber = db.get(Subscriber, person_uuid)
    if not subscriber:
        raise ValueError("Subscriber not found")

    if create_user == "true":
        if subscriber_type != "person":
            raise ValueError(
                "Customer portal logins can only be created for person subscribers."
            )
        if not user_username or not user_password:
            raise ValueError("Username and password are required to create a login.")
        existing = auth_service.user_credentials.list(
            db=db,
            subscriber_id=str(person_uuid),
            provider=AuthProvider.local.value,
            is_active=True,
            order_by="created_at",
            order_dir="desc",
            limit=1,
            offset=0,
        )
        if existing:
            raise ValueError("Customer already has a portal login.")

    subscriber_number_value = subscriber_number.strip() if subscriber_number else None
    if subscriber_number_value:
        prefix = settings_spec.resolve_value(
            db,
            SettingDomain.subscriber,
            "subscriber_number_prefix",
        )
        configured_prefix = prefix if isinstance(prefix, str) else "SUB-"
        if configured_prefix and not subscriber_number_value.startswith(
            configured_prefix
        ):
            # Keep manual numbers only when they respect configured numbering format.
            subscriber_number_value = None

    payload = SubscriberUpdate(
        subscriber_number=subscriber_number_value,
        category=(
            SubscriberCategory.business.value
            if subscriber_type == "business"
            else subscriber_category.strip().lower()
            if subscriber_category
            else None
        ),
        notes=notes.strip() if notes else None,
    )
    subscriber = subscriber_service.subscribers.update(
        db=db,
        subscriber_id=str(person_uuid),
        payload=payload,
    )

    if create_user == "true":
        if user_username is None or user_password is None:
            raise ValueError("Username and password are required to create a login.")
        credential = UserCredentialCreate(
            subscriber_id=person_uuid,
            provider=AuthProvider.local,
            username=user_username.strip(),
            password_hash=hash_password(user_password),
        )
        auth_service.user_credentials.create(db=db, payload=credential)
    return subscriber


def load_subscriber_form_options(db: Session, limit: int = 500):
    people = subscriber_service.subscribers.list(
        db=db,
        business_account_id=None,
        subscriber_type="person",
        order_by="created_at",
        order_dir="asc",
        limit=limit,
        offset=0,
    )
    businesses = subscriber_service.subscribers.list(
        db=db,
        business_account_id=None,
        subscriber_type="business",
        order_by="created_at",
        order_dir="asc",
        limit=limit,
        offset=0,
    )
    return people, businesses


def resolve_new_form_prefill(
    db: Session,
    *,
    subscriber_id: str | None,
    business_account_id: str | None,
) -> tuple[str, str]:
    """Resolve customer prefill tokens for subscriber new form."""
    prefill_ref = ""
    prefill_label = ""
    if subscriber_id:
        try:
            subscriber = subscriber_service.subscribers.get(
                db=db, subscriber_id=subscriber_id
            )
            prefill_ref = f"person:{subscriber.id}"
            prefill_label = f"{subscriber.first_name} {subscriber.last_name}".strip()
            if subscriber.email:
                prefill_label = f"{prefill_label} ({subscriber.email})"
            return prefill_ref, prefill_label
        except Exception:
            return "", ""
    if business_account_id:
        try:
            business_subscriber = subscriber_service.subscribers.get(
                db=db,
                subscriber_id=business_account_id,
            )
            if business_subscriber.category != SubscriberCategory.business:
                return "", ""
            prefill_ref = f"business:{business_subscriber.id}"
            prefill_label = (
                business_subscriber.company_name
                or business_subscriber.display_name
                or business_subscriber.full_name
            )
            if business_subscriber.domain:
                prefill_label = f"{prefill_label} ({business_subscriber.domain})"
            return prefill_ref, prefill_label
        except Exception:
            return "", ""
    return "", ""


def build_subscriber_update_form_values(
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
) -> dict:
    """Build field values for subscriber edit form error re-render."""
    return {
        "customer_ref": customer_ref or "",
        "customer_search": customer_search or "",
        "subscriber_type": subscriber_type or "",
        "person_id": person_id or "",
        "business_account_id": business_account_id or "",
        "subscriber_number": subscriber_number or "",
        "subscriber_category": subscriber_category or "",
        "notes": notes or "",
        "is_active": True if is_active is None else (is_active == "true"),
    }
