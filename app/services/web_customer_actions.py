"""Service helpers for web/admin customer action routes."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.auth import ApiKey, MFAMethod, UserCredential
from app.models.auth import Session as AuthSession
from app.models.catalog import AccessCredential, Subscription, SubscriptionStatus
from app.models.collections import DunningCase, DunningCaseStatus
from app.models.enforcement_lock import EnforcementLock
from app.models.notification import (
    NotificationChannel,
    NotificationStatus,
    NotificationTemplate,
)
from app.models.subscriber import (
    AddressType,
    ChannelType,
    ContactMethod,
    Reseller,
    Subscriber,
    SubscriberCategory,
    SubscriberChannel,
    SubscriberStatus,
)
from app.schemas.audit import AuditEventCreate
from app.schemas.notification import NotificationCreate
from app.schemas.subscriber import (
    AddressCreate,
    AddressUpdate,
    SubscriberCreate,
    SubscriberUpdate,
)
from app.services import account_status_commands, customer_portal
from app.services import audit as audit_service
from app.services import catalog as catalog_service
from app.services import notification as notification_service
from app.services import radius as radius_service
from app.services import subscriber as subscriber_service
from app.services import web_customer_lists as web_customer_lists_service
from app.services.account_billing_approval import (
    BILLING_APPROVAL_WRITE_SCOPE,
    ChangeAccountBillingApprovalCommand,
    change_account_billing_approval,
)
from app.services.account_lifecycle import compute_account_status, derive_account_status
from app.services.branding_config import get_brand
from app.services.bulk_actions import (
    BulkSelection,
    membership_scope_token,
    parse_bulk_selection,
)
from app.services.common import coerce_uuid
from app.services.common import parse_date_filter as _parse_date
from app.services.customer_financial_position import get_customer_financial_position
from app.services.customer_identity_normalization import (
    collapse_whitespace,
    customer_name_fingerprint,
    is_placeholder_name,
    normalize_email_identifier,
    normalize_phone_identifier,
)
from app.services.customer_notification_policy import (
    CustomerNotificationPolicyCandidate,
    CustomerNotificationPolicyCohortQuery,
    evaluate_bulk_customer_notification_policy,
    quiet_hours_send_at,
    resolve_notification_category,
)
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations import whatsapp_capability
from app.services.notification_template_conditions import (
    NotificationTemplateConditionError,
    conditions_match_for_subscribers,
    normalize_conditions,
)
from app.services.notification_template_renderer import render_template_text
from app.services.owner_commands import CommandContext
from app.services.radius_access_state import (
    derive_subscriber_access_state,
)
from app.services.whatsapp_notification_templates import (
    build_provider_template_body,
    provider_template_from_template,
    sync_whatsapp_registry_templates,
)

logger = logging.getLogger(__name__)


def resolve_customer_reseller_id(
    db: Session, *, managed_by_reseller: bool, reseller_id: str | None
) -> UUID:
    """Resolve explicit external ownership or the canonical House reseller."""

    if not managed_by_reseller:
        house_id = subscriber_service._default_reseller_id(db)
        if house_id is None:
            raise ValueError("The House reseller is not configured.")
        return UUID(str(house_id))
    try:
        selected_id = UUID(str(reseller_id or "").strip())
    except (TypeError, ValueError, AttributeError):
        raise ValueError("Select the reseller that manages this customer.") from None
    reseller = db.scalars(
        select(Reseller).where(
            Reseller.id == selected_id,
            Reseller.is_active.is_(True),
            Reseller.is_house.is_(False),
        )
    ).one_or_none()
    if reseller is None:
        raise ValueError("Select an active reseller.")
    return reseller.id


WHATSAPP_VARIABLE_CUSTOMER_FIELDS = {
    "first_name",
    "last_name",
    "full_name",
    "email",
    "phone",
    "account_number",
}

_DOUBLE_BRACE_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_SINGLE_BRACE_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{\s*([a-zA-Z0-9_]+)\s*\}(?!\})")
_SUBSCRIPTION_TEMPLATE_VARIABLE_NAMES = frozenset(
    {
        "offer_name",
        "plan_name",
        "pppoe_login",
        "ipv4_address",
        "nas_name",
        "location",
    }
)
_BILLING_TEMPLATE_VARIABLE_NAMES = frozenset(
    {
        "amount",
        "total_amount",
        "balance_due",
        "days_overdue",
        "invoice_number",
        "due_date",
        "payment_link",
        "portal_url",
        "website",
    }
)
_MANUAL_TEMPLATE_VARIABLE_NAMES = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "customer_name",
        "subscriber_name",
        "account_number",
        "subscriber_number",
        "email",
        "phone",
        "status",
        "company_name",
        "support_email",
        *_SUBSCRIPTION_TEMPLATE_VARIABLE_NAMES,
        *_BILLING_TEMPLATE_VARIABLE_NAMES,
    }
)


def _apply_billing_approval_command(
    db: Session,
    *,
    account_id: UUID,
    approved: bool,
    actor_id: str | None,
    reason: str,
):
    """Leave adapter reads, then submit one lifecycle-owned approval command."""
    db_session_adapter.release_read_transaction(db)
    command_id = uuid4()
    context = CommandContext(
        command_id=command_id,
        correlation_id=command_id,
        actor=f"user:{actor_id}" if actor_id else "service:admin_customer_profile",
        scope=BILLING_APPROVAL_WRITE_SCOPE,
        reason=reason,
        idempotency_key=f"billing-approval:{account_id}:{approved}:{command_id}",
    )
    return change_account_billing_approval(
        db,
        ChangeAccountBillingApprovalCommand(
            context=context,
            account_id=account_id,
            approved=approved,
        ),
    )


def parse_json_object(value: str | None, field: str) -> dict | None:
    """Parse an optional JSON object from form input."""
    if not value or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    return parsed


def billing_form_defaults(subscriber: Subscriber | None) -> dict[str, str]:
    """Build string defaults for customer billing form controls."""
    defaults = {
        "billing_enabled_override": "",
        "captive_redirect_enabled": "",
        "billing_day": "",
        "payment_due_days": "",
        "grace_period_days": "",
        "min_balance": "",
        "tax_rate_id": "",
        "withholding_tax_enabled": "false",
        "vat_exempt": "false",
        "payment_method": "",
    }
    if not subscriber:
        return defaults
    defaults.update(
        {
            "billing_enabled_override": (
                "true" if subscriber.billing_enabled else "false"
            )
            if subscriber.billing_enabled is not None
            else "",
            "captive_redirect_enabled": "true"
            if subscriber.captive_redirect_enabled
            else "false",
            "billing_day": (
                str(subscriber.billing_day)
                if subscriber.billing_day is not None
                else ""
            ),
            "payment_due_days": (
                str(subscriber.payment_due_days)
                if subscriber.payment_due_days is not None
                else ""
            ),
            "grace_period_days": (
                str(subscriber.grace_period_days)
                if subscriber.grace_period_days is not None
                else ""
            ),
            "min_balance": (
                str(subscriber.min_balance)
                if subscriber.min_balance is not None
                else ""
            ),
            "tax_rate_id": str(subscriber.tax_rate_id or ""),
            "payment_method": str(subscriber.payment_method or ""),
        }
    )
    return defaults


def resolve_business_customer_id(db: Session, customer_id: str) -> str:
    """Accept only a business subscriber id for business routes."""
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if subscriber.category != SubscriberCategory.business:
        raise HTTPException(status_code=404, detail="Business customer not found")
    return customer_id


def list_active_subscription_ids(db: Session, customer_id: str) -> list[str]:
    """Return active subscription ids attached to a customer."""
    rows = db.execute(
        select(Subscription.id)
        .where(Subscription.subscriber_id == customer_id)
        .where(Subscription.status == SubscriptionStatus.active)
    ).all()
    return [str(row[0]) for row in rows]


def list_suspended_subscription_ids(db: Session, customer_id: str) -> list[str]:
    """Return suspended subscription ids attached to a customer."""
    rows = db.execute(
        select(Subscription.id)
        .where(Subscription.subscriber_id == customer_id)
        .where(Subscription.status == SubscriptionStatus.suspended)
    ).all()
    return [str(row[0]) for row in rows]


def repair_customer_access_state(db: Session, customer_id: str) -> dict[str, Any]:
    """Recompute one customer's active access projection and refresh RADIUS.

    This intentionally does not perform any batch repair. It is only safe when
    the account has active service and no current suspension/dunning signals.
    """
    account_uuid = coerce_uuid(customer_id)
    subscriber = db.get(Subscriber, account_uuid)
    if subscriber is None:
        raise ValueError("Customer not found.")

    active_subscriptions = list(
        db.scalars(
            select(Subscription)
            .where(Subscription.subscriber_id == account_uuid)
            .where(Subscription.status == SubscriptionStatus.active)
        ).all()
    )
    if not active_subscriptions:
        raise ValueError("This customer has no active subscriptions to repair.")

    active_credentials_count = int(
        db.scalar(
            select(func.count())
            .select_from(AccessCredential)
            .where(AccessCredential.subscriber_id == account_uuid)
            .where(AccessCredential.is_active.is_(True))
        )
        or 0
    )
    if active_credentials_count <= 0:
        raise ValueError("This customer has no active access credentials to refresh.")

    active_lock_count = int(
        db.scalar(
            select(func.count())
            .select_from(EnforcementLock)
            .where(EnforcementLock.subscriber_id == account_uuid)
            .where(EnforcementLock.is_active.is_(True))
        )
        or 0
    )
    if active_lock_count > 0:
        raise ValueError("This customer has an active suspension lock.")

    active_dunning_count = int(
        db.scalar(
            select(func.count())
            .select_from(DunningCase)
            .where(DunningCase.account_id == account_uuid)
            .where(
                DunningCase.status.in_(
                    [DunningCaseStatus.open, DunningCaseStatus.paused]
                )
            )
        )
        or 0
    )
    if active_dunning_count > 0:
        raise ValueError("This customer has an active dunning case.")

    if subscriber.lifecycle_override_status is not None:
        raise ValueError("This customer has a manual lifecycle override.")

    before_status = subscriber.status
    derived_status = derive_account_status(db, str(account_uuid))
    if derived_status != SubscriberStatus.active:
        raise ValueError(
            f"This customer derives to {derived_status.value}, not active."
        )

    computed_status = compute_account_status(db, str(account_uuid))
    radius_group_rows_written = 0
    radius_group_rows_deleted = 0
    aggregate = derive_subscriber_access_state(db, account_uuid)
    aggregate_state = aggregate.value if aggregate is not None else None

    reject_rows_removed = radius_service.unblock_external_radius_credentials(
        db, account_uuid
    )

    radius_users_changed = 0
    radius_clients_changed = 0
    external_credentials_synced = 0
    external_nas_synced = 0
    for subscription in active_subscriptions:
        reconcile_result = radius_service.reconcile_subscription_connectivity(
            db, str(subscription.id)
        )
        if not reconcile_result.ok:
            raise RuntimeError(
                "RADIUS connectivity reconciliation failed: "
                f"{reconcile_result.disposition.value}"
            )
        radius_users_changed += reconcile_result.radius_users_changed
        radius_clients_changed += reconcile_result.radius_clients_changed
        external_credentials_synced += reconcile_result.external_credentials_synced
        external_nas_synced += reconcile_result.external_nas_synced

    db.commit()
    db.refresh(subscriber)

    return {
        "subscriber_id": str(account_uuid),
        "status_before": before_status.value,
        "status_after": subscriber.status.value,
        "derived_status": derived_status.value,
        "computed_status": computed_status.value,
        "active_subscriptions": len(active_subscriptions),
        "active_credentials": active_credentials_count,
        "reject_rows_removed": reject_rows_removed,
        "radius_group_rows_written": radius_group_rows_written,
        "radius_group_rows_deleted": radius_group_rows_deleted,
        "radius_users_changed": radius_users_changed,
        "radius_clients_changed": radius_clients_changed,
        "external_credentials_synced": external_credentials_synced,
        "external_nas_synced": external_nas_synced,
        "aggregate_state": aggregate_state,
    }


def save_primary_address_coordinates(
    db: Session,
    *,
    customer_id: str,
    latitude: float,
    longitude: float,
) -> dict[str, object]:
    """Save coordinates to the primary address, creating one from profile data if needed."""
    try:
        parsed_customer_id = coerce_uuid(customer_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid customer id") from exc
    customer = subscriber_service.subscribers.get(
        db=db, subscriber_id=str(parsed_customer_id)
    )
    addresses = subscriber_service.addresses.list(
        db=db,
        subscriber_id=str(parsed_customer_id),
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    primary_address = next(
        (addr for addr in addresses if addr.is_primary),
        addresses[0] if addresses else None,
    )

    created = False
    if primary_address is None:
        if not (customer.address_line1 or "").strip():
            raise HTTPException(
                status_code=400,
                detail="No address exists to geolocate. Add an address first.",
            )
        primary_address = subscriber_service.addresses.create(
            db=db,
            payload=AddressCreate(
                subscriber_id=parsed_customer_id,
                address_line1=customer.address_line1,
                address_line2=customer.address_line2,
                city=customer.city,
                region=customer.region,
                postal_code=customer.postal_code,
                country_code=customer.country_code,
                latitude=latitude,
                longitude=longitude,
                is_primary=True,
            ),
        )
        created = True

    updated = subscriber_service.addresses.update(
        db=db,
        address_id=str(primary_address.id),
        payload=AddressUpdate(latitude=latitude, longitude=longitude),
    )
    return {
        "success": True,
        "created_address": created,
        "address_id": str(updated.id),
        "latitude": updated.latitude,
        "longitude": updated.longitude,
    }


def save_address_coordinates(
    db: Session,
    *,
    address_id: str,
    latitude: float,
    longitude: float,
) -> dict[str, object]:
    """Save coordinates to an existing customer address."""
    try:
        parsed_address_id = coerce_uuid(address_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid address id") from exc
    address = subscriber_service.addresses.update(
        db=db,
        address_id=str(parsed_address_id),
        payload=AddressUpdate(latitude=latitude, longitude=longitude),
    )
    return {
        "success": True,
        "address_id": str(address.id),
        "latitude": address.latitude,
        "longitude": address.longitude,
    }


def _bulk_id_refs(customers: list[Subscriber]) -> list[dict[str, str]]:
    """Shape resolved subscribers as the {id, type} refs the executors expect."""
    return [
        {
            "id": str(customer.id),
            "type": "business" if customer.is_business else "person",
        }
        for customer in customers
    ]


def _status_impact_token(
    resolved: ResolvedCustomerBulkScope, *, target_active: bool
) -> str:
    """Bind confirmation to membership, target state, and observed account state."""
    return membership_scope_token(
        f"{resolved.scope}:customer_status:{'active' if target_active else 'inactive'}",
        [
            f"{customer.id}:{int(bool(customer.is_active))}"
            for customer in resolved.customers
        ],
    )


def _delete_impact(
    db: Session, resolved: ResolvedCustomerBulkScope
) -> tuple[dict[str, object], str]:
    """Return exact deletion eligibility and a token that detects state drift."""
    customer_ids = [customer.id for customer in resolved.customers]
    with_subscriptions = set(
        db.scalars(
            select(Subscription.subscriber_id)
            .where(Subscription.subscriber_id.in_(customer_ids))
            .distinct()
        ).all()
    )
    active_now = sum(1 for customer in resolved.customers if customer.is_active)
    subscription_blocked = sum(
        1 for customer in resolved.customers if customer.id in with_subscriptions
    )
    eligible = sum(
        1
        for customer in resolved.customers
        if not customer.is_active and customer.id not in with_subscriptions
    )
    impact: dict[str, object] = {
        "total": resolved.matched_count,
        "eligible": eligible,
        "active": active_now,
        "with_subscriptions": subscription_blocked,
        "destructive": True,
    }
    token = membership_scope_token(
        f"{resolved.scope}:customer_delete",
        [
            (
                f"{customer.id}:{int(bool(customer.is_active))}:"
                f"{int(customer.id in with_subscriptions)}"
            )
            for customer in resolved.customers
        ],
    )
    return impact, token


def bulk_update_customer_status_from_payload(
    db: Session, payload: dict[str, Any]
) -> dict[str, object]:
    new_status = payload.get("status")
    if new_status not in ("active", "inactive"):
        raise HTTPException(
            status_code=400, detail="status must be 'active' or 'inactive'"
        )
    resolved = resolve_bulk_customer_scope(db, payload)
    if not resolved.customers:
        raise HTTPException(status_code=400, detail="No customers matched this scope")

    # Impact preview: how many rows actually transition vs already sit in the
    # target state. Deactivation is destructive (cuts service), so the owner
    # marks it and the caller must preview then confirm against an unchanged
    # scope before it runs (mirrors the bulk-update contract).
    target_active = new_status == "active"
    confirmation_token = _status_impact_token(resolved, target_active=target_active)
    active_now = sum(1 for customer in resolved.customers if customer.is_active)
    will_change = (resolved.matched_count - active_now) if target_active else active_now
    impact = {
        "total": resolved.matched_count,
        "will_change": will_change,
        "already_in_target_state": resolved.matched_count - will_change,
        "target_status": new_status,
        "destructive": not target_active,
    }
    action_label = "Reactivate customers" if target_active else "Deactivate customers"

    if bool(payload.get("preview_only")):
        return {
            "success": True,
            "preview": True,
            "scope": resolved.scope,
            "matched_count": resolved.matched_count,
            "scope_token": confirmation_token,
            "missing_ids": list(resolved.missing_ids),
            "impact": impact,
        }

    _require_bulk_execution_confirmation(
        payload,
        resolved=resolved,
        action_label=action_label,
        confirmation_token=confirmation_token,
    )
    result = bulk_update_customer_status(
        db=db,
        customer_ids=_bulk_id_refs(resolved.customers),
        is_active=target_active,
    )
    return {
        **result,
        "preview": False,
        "matched_count": resolved.matched_count,
        "scope_token": confirmation_token,
        "missing_ids": list(resolved.missing_ids),
        "impact": impact,
    }


def bulk_delete_customers_from_payload(
    db: Session, payload: dict[str, Any]
) -> dict[str, object]:
    resolved = resolve_bulk_customer_scope(db, payload)
    if not resolved.customers:
        raise HTTPException(status_code=400, detail="No customers matched this scope")

    # Deletion is permanent: bind the preview not only to membership but to
    # each row's active/subscription eligibility so newly eligible rows cannot
    # be deleted under a stale impact statement.
    impact, confirmation_token = _delete_impact(db, resolved)

    if bool(payload.get("preview_only")):
        return {
            "success": True,
            "preview": True,
            "scope": resolved.scope,
            "matched_count": resolved.matched_count,
            "scope_token": confirmation_token,
            "missing_ids": list(resolved.missing_ids),
            "impact": impact,
        }

    _require_bulk_execution_confirmation(
        payload,
        resolved=resolved,
        action_label="Delete customers",
        confirmation_token=confirmation_token,
    )
    result = bulk_delete_customers(
        db=db, customer_ids=_bulk_id_refs(resolved.customers)
    )
    return {
        **result,
        "preview": False,
        "matched_count": resolved.matched_count,
        "scope_token": confirmation_token,
        "missing_ids": list(resolved.missing_ids),
        "impact": impact,
    }


_CUSTOMER_BULK_FILTER_KEYS = (
    "search",
    "status",
    "customer_type",
    "nas_id",
    "pop_site_id",
)


@dataclass(slots=True)
class ResolvedCustomerBulkScope:
    """Canonical customers resolved from one explicit selection request."""

    selection: BulkSelection
    customers: list[Subscriber]
    missing_ids: tuple[str, ...] = ()

    @property
    def scope(self) -> str:
        return self.selection.mode

    @property
    def matched_count(self) -> int:
        return len(self.customers)

    @property
    def scope_token(self) -> str:
        """Fingerprint exact resolved membership, independent of row ordering."""

        return membership_scope_token(
            self.scope, [str(customer.id) for customer in self.customers]
        )


def _parse_customer_bulk_selection(payload: dict[str, Any]) -> BulkSelection:
    try:
        return parse_bulk_selection(
            payload,
            allowed_filter_keys=_CUSTOMER_BULK_FILTER_KEYS,
            filtered_selection_supported=True,
            legacy_id_key="customer_ids",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def resolve_bulk_customer_scope(
    db: Session, payload: dict[str, Any]
) -> ResolvedCustomerBulkScope:
    selection = _parse_customer_bulk_selection(payload)
    if selection.mode == "selected":
        try:
            parsed_ids = [coerce_uuid(item) for item in selection.ids]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid customer id") from exc
        query = (
            web_customer_lists_service.customer_scope_query(
                db,
                search=None,
                status=None,
                customer_type=None,
                nas_id=None,
                pop_site_id=None,
                include_related=True,
            )
            .filter(Subscriber.id.in_(parsed_ids))
            .all()
        )
        ordered = {str(subscriber.id): subscriber for subscriber in query}
        customers = [ordered[item] for item in selection.ids if item in ordered]
        missing_ids = tuple(item for item in selection.ids if item not in ordered)
        return ResolvedCustomerBulkScope(
            selection=selection,
            customers=customers,
            missing_ids=missing_ids,
        )

    try:
        list_query = web_customer_lists_service.build_customer_list_query(
            search=selection.filter_value("search"),
            status=selection.filter_value("status"),
            customer_type=selection.filter_value("customer_type"),
            nas_id=selection.filter_value("nas_id"),
            pop_site_id=selection.filter_value("pop_site_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    customers = web_customer_lists_service.list_customers_for_scope(
        db,
        search=list_query.search,
        status=list_query.filter_value("status"),
        customer_type=list_query.filter_value("customer_type"),
        nas_id=list_query.filter_value("nas_id"),
        pop_site_id=list_query.filter_value("pop_site_id"),
    )
    return ResolvedCustomerBulkScope(selection=selection, customers=customers)


def _require_bulk_execution_confirmation(
    payload: dict[str, Any],
    *,
    resolved: ResolvedCustomerBulkScope,
    action_label: str,
    confirmation_token: str | None = None,
) -> None:
    if not bool(payload.get("confirmed")):
        raise HTTPException(
            status_code=400,
            detail=f"{action_label} confirmation required",
        )
    expected_count = resolved.selection.expected_count
    expected_scope_token = resolved.selection.expected_scope_token
    if expected_count is None or expected_scope_token is None:
        raise HTTPException(
            status_code=400,
            detail=f"Preview {action_label.lower()} before confirming",
        )
    current_token = confirmation_token or resolved.scope_token
    scope_changed = expected_count != resolved.matched_count or not hmac.compare_digest(
        expected_scope_token, current_token
    )
    if scope_changed:
        raise HTTPException(
            status_code=409,
            detail=(
                "The selected customer scope changed after preview, or its impact "
                "changed. "
                "Review the updated impact before confirming again."
            ),
        )


def _coerce_bool_value(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise HTTPException(status_code=400, detail=f"{field_name} must be true or false")


def _coerce_nullable_int(value: Any, field_name: str) -> int | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"{field_name} must be a whole number"
        ) from exc


def _normalize_bulk_updates(payload: dict[str, Any]) -> dict[str, Any]:
    updates = payload.get("updates") or {}
    if not isinstance(updates, dict) or not updates:
        raise HTTPException(status_code=400, detail="updates are required")

    normalized: dict[str, Any] = {}
    if "account_state" in updates:
        account_state = str(updates.get("account_state") or "").strip().lower()
        if account_state not in {"active", "inactive"}:
            raise HTTPException(
                status_code=400,
                detail="account_state must be active or inactive",
            )
        normalized["account_state"] = account_state

    if "preferred_contact_method" in updates:
        raw_method = str(updates.get("preferred_contact_method") or "").strip().lower()
        if raw_method:
            try:
                normalized["preferred_contact_method"] = ContactMethod(raw_method)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="preferred_contact_method is invalid",
                ) from exc
        else:
            normalized["preferred_contact_method"] = None

    if "billing_enabled" in updates:
        normalized["billing_enabled"] = _coerce_bool_value(
            updates.get("billing_enabled"), "billing_enabled"
        )

    for field_name in ("billing_day", "payment_due_days", "grace_period_days"):
        if field_name in updates:
            normalized[field_name] = _coerce_nullable_int(
                updates.get(field_name), field_name
            )

    if "payment_method" in updates:
        normalized["payment_method"] = _normalize_optional(
            str(updates.get("payment_method") or "")
        )

    if "notes" in updates:
        normalized["notes"] = _normalize_optional(str(updates.get("notes") or ""))

    if not normalized:
        raise HTTPException(
            status_code=400, detail="No supported updates were provided"
        )
    return normalized


def bulk_update_customers_from_payload(
    db: Session,
    payload: dict[str, Any],
    *,
    actor_id: str | None = None,
) -> dict[str, object]:
    resolved = resolve_bulk_customer_scope(db, payload)
    if not resolved.customers:
        raise HTTPException(status_code=400, detail="No customers matched this scope")
    updates = _normalize_bulk_updates(payload)
    preview_only = bool(payload.get("preview_only"))
    if preview_only:
        return {
            "success": True,
            "preview": True,
            "scope": resolved.scope,
            "matched_count": resolved.matched_count,
            "scope_token": resolved.scope_token,
            "missing_ids": list(resolved.missing_ids),
            "update_fields": sorted(updates),
        }

    _require_bulk_execution_confirmation(
        payload,
        resolved=resolved,
        action_label="Bulk customer update",
    )
    result = bulk_update_customers(
        db=db,
        customers=resolved.customers,
        updates=updates,
        scope=resolved.scope,
        actor_id=actor_id,
    )
    errors = result["errors"]
    if isinstance(errors, list):
        errors.extend(
            {"id": customer_id, "error": "Customer not found"}
            for customer_id in resolved.missing_ids
        )
    return {
        **result,
        "preview": False,
        "matched_count": resolved.matched_count,
        "scope_token": resolved.scope_token,
        "missing_ids": list(resolved.missing_ids),
    }


def bulk_update_customers(
    db: Session,
    *,
    customers: list[Subscriber],
    updates: dict[str, Any],
    scope: str,
    actor_id: str | None = None,
) -> dict[str, object]:
    updated_count = 0
    updated_ids: list[str] = []
    errors: list[dict[str, str]] = []

    for subscriber in customers:
        try:
            requested_billing_approval = updates.get("billing_enabled")
            if requested_billing_approval is not None:
                _apply_billing_approval_command(
                    db,
                    account_id=subscriber.id,
                    approved=bool(requested_billing_approval),
                    actor_id=actor_id,
                    reason=f"Bulk customer billing approval update ({scope})",
                )

            account_state = updates.get("account_state")
            if account_state and requested_billing_approval is not False:
                is_active = account_state == "active"
                change_customer_account_active_state(
                    db,
                    subscriber,
                    is_active=is_active,
                    source=f"admin:bulk_update:{scope}:{subscriber.id}",
                )

            if "preferred_contact_method" in updates:
                subscriber.preferred_contact_method = updates[
                    "preferred_contact_method"
                ]
            if "billing_day" in updates:
                subscriber.billing_day = updates["billing_day"]
            if "payment_due_days" in updates:
                subscriber.payment_due_days = updates["payment_due_days"]
            if "grace_period_days" in updates:
                subscriber.grace_period_days = updates["grace_period_days"]
            if "payment_method" in updates:
                subscriber.payment_method = updates["payment_method"]
            if "notes" in updates:
                subscriber.notes = updates["notes"]

            db.commit()
            updated_count += 1
            updated_ids.append(str(subscriber.id))
        except Exception as exc:
            db.rollback()
            errors.append({"id": str(subscriber.id), "error": str(exc)})

    return {
        "success": True,
        "scope": scope,
        "updated_count": updated_count,
        "updated_ids": updated_ids,
        "errors": errors,
    }


def _subscriber_channel_address(
    subscriber: Subscriber, channel_type: ChannelType
) -> str | None:
    ordered_channels = sorted(
        subscriber.channels or [],
        key=lambda row: (not bool(row.is_primary), str(row.created_at or "")),
    )
    for channel in ordered_channels:
        if channel.channel_type != channel_type:
            continue
        address = str(channel.address or "").strip()
        if address:
            return address
    return None


def _resolve_notification_recipient(
    subscriber: Subscriber, channel: NotificationChannel
) -> str | None:
    if channel == NotificationChannel.email:
        return normalize_email_identifier(
            subscriber.email
        ) or _subscriber_channel_address(subscriber, ChannelType.email)

    if channel == NotificationChannel.sms:
        return (
            normalize_phone_identifier(subscriber.phone)
            or normalize_phone_identifier(
                _subscriber_channel_address(subscriber, ChannelType.sms)
            )
            or normalize_phone_identifier(
                _subscriber_channel_address(subscriber, ChannelType.phone)
            )
        )

    if channel == NotificationChannel.whatsapp:
        return (
            normalize_phone_identifier(
                _subscriber_channel_address(subscriber, ChannelType.whatsapp)
            )
            or normalize_phone_identifier(subscriber.phone)
            or normalize_phone_identifier(
                _subscriber_channel_address(subscriber, ChannelType.phone)
            )
            or normalize_phone_identifier(
                _subscriber_channel_address(subscriber, ChannelType.sms)
            )
        )
    return None


def _primary_subscription_for_notification(
    subscriber: Subscriber,
) -> Subscription | None:
    subscriptions = list(subscriber.subscriptions or [])
    if not subscriptions:
        return None
    status_rank = {
        SubscriptionStatus.active: 0,
        SubscriptionStatus.pending: 1,
        SubscriptionStatus.suspended: 2,
    }
    return sorted(
        subscriptions,
        key=lambda item: (
            status_rank.get(item.status, 9),
            item.created_at or datetime.min.replace(tzinfo=UTC),
        ),
    )[0]


def _format_money_ngn(value: Decimal | int | float | str | None) -> str:
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    return f"₦{amount:,.2f}"


def _billing_template_variables(db: Session, subscriber: Subscriber) -> dict[str, str]:
    now = datetime.now(UTC)
    position = get_customer_financial_position(
        db,
        subscriber.id,
        now=now,
        include_prepaid_balance=False,
    )
    outstanding = position.open_invoice_balance
    oldest_overdue = position.oldest_due_invoice
    app_url = get_brand()["app_url"].rstrip("/")
    return {
        "amount": _format_money_ngn(outstanding),
        "total_amount": _format_money_ngn(outstanding),
        "balance_due": _format_money_ngn(outstanding),
        "days_overdue": str(position.days_overdue),
        "invoice_number": str(oldest_overdue.invoice_number or "")
        if oldest_overdue
        else "",
        "due_date": oldest_overdue.due_at.strftime("%b %d, %Y")
        if oldest_overdue and oldest_overdue.due_at
        else "",
        "payment_link": f"{app_url}/portal/billing",
        "portal_url": f"{app_url}/portal",
        "website": app_url,
    }


def _notification_template_variables(
    db: Session,
    subscriber: Subscriber,
    *,
    required_variables: set[str] | frozenset[str] | None = None,
) -> dict[str, str]:
    required = (
        _MANUAL_TEMPLATE_VARIABLE_NAMES
        if required_variables is None
        else required_variables
    )
    primary_subscription = (
        _primary_subscription_for_notification(subscriber)
        if required & _SUBSCRIPTION_TEMPLATE_VARIABLE_NAMES
        else None
    )
    nas_name = ""
    pop_site_name = ""
    pppoe_login = ""
    ipv4_address = ""
    offer_name = ""
    if primary_subscription:
        pppoe_login = str(primary_subscription.login or "")
        ipv4_address = str(primary_subscription.ipv4_address or "")
        offer = getattr(primary_subscription, "offer", None)
        offer_name = str(
            (offer.name if offer else None)
            or primary_subscription.service_description
            or "your service"
        )
        if primary_subscription.provisioning_nas_device:
            nas_name = str(primary_subscription.provisioning_nas_device.name or "")
            if primary_subscription.provisioning_nas_device.pop_site:
                pop_site_name = str(
                    primary_subscription.provisioning_nas_device.pop_site.name or ""
                )
    customer_name = str(
        subscriber.company_name
        or subscriber.display_name
        or subscriber.full_name
        or "Valued Customer"
    ).strip()
    variables = {
        "first_name": str(subscriber.first_name or ""),
        "last_name": str(subscriber.last_name or ""),
        "full_name": str(subscriber.full_name or "").strip(),
        "customer_name": customer_name,
        "subscriber_name": customer_name,
        "account_number": str(subscriber.account_number or ""),
        "subscriber_number": str(subscriber.subscriber_number or ""),
        "email": str(subscriber.email or ""),
        "phone": str(subscriber.phone or ""),
        "status": subscriber.status.value if subscriber.status else "",
        "offer_name": offer_name or "your service",
        "plan_name": offer_name or "your service",
        "pppoe_login": pppoe_login,
        "ipv4_address": ipv4_address,
        "nas_name": nas_name,
        "location": pop_site_name,
        "company_name": "",
        "support_email": "",
    }
    if required & {"company_name", "support_email"}:
        brand = get_brand()
        variables["company_name"] = brand["legal_name"]
        variables["support_email"] = brand["support_email"]
    if required & _BILLING_TEMPLATE_VARIABLE_NAMES:
        variables.update(_billing_template_variables(db, subscriber))
    return variables


def _render_manual_template_text(text: str | None, variables: dict[str, str]) -> str:
    if not text:
        return ""

    def _replace_double(match: re.Match[str]) -> str:
        key = match.group(1)
        return variables[key] if key in variables else match.group(0)

    return render_template_text(
        _DOUBLE_BRACE_PLACEHOLDER_RE.sub(_replace_double, text),
        variables,
    )


def _unresolved_template_variables(text: str | None) -> list[str]:
    if not text:
        return []
    names = {
        *(_DOUBLE_BRACE_PLACEHOLDER_RE.findall(text or "")),
        *(_SINGLE_BRACE_PLACEHOLDER_RE.findall(text or "")),
    }
    return sorted(names)


def _parse_whatsapp_registry_template_id(template_id: str) -> tuple[str, str] | None:
    if not template_id.startswith("whatsapp:"):
        return None
    parts = template_id.split(":", 2)
    if len(parts) != 3 or not parts[1].strip():
        return None
    return parts[1].strip(), parts[2].strip() or "en"


def _whatsapp_registry_template(db: Session, template_id: str) -> dict[str, str] | None:
    parsed = _parse_whatsapp_registry_template_id(template_id)
    if not parsed:
        return None
    name, language = parsed
    config = whatsapp_capability.active_config(db)
    for item in config.get("templates") or []:
        item_name = str(item.get("name") or "").strip()
        item_language = str(item.get("language") or "").strip() or "en"
        if item_name == name and item_language == language:
            return {"name": item_name, "language": item_language}
    return None


def _notification_template_for_whatsapp(
    db: Session, template_id: str
) -> NotificationTemplate | None:
    registry_template = _whatsapp_registry_template(db, template_id)
    if registry_template:
        for template in (
            db.query(NotificationTemplate)
            .filter(NotificationTemplate.channel == NotificationChannel.whatsapp)
            .all()
        ):
            provider_template = provider_template_from_template(template)
            if provider_template and (
                provider_template["name"],
                provider_template.get("language") or "en",
            ) == (
                registry_template["name"],
                registry_template["language"],
            ):
                return template
        return None
    try:
        template_uuid = coerce_uuid(template_id)
    except Exception:
        return None
    return db.get(NotificationTemplate, template_uuid)


def whatsapp_template_details(
    db: Session, *, name: str, language: str | None
) -> dict[str, Any]:
    return whatsapp_capability.fetch_template_details(
        db,
        template_name=name.strip(),
        language=(language or "").strip() or None,
    )


def _resolve_whatsapp_variable(
    spec: object,
    customer_values: dict[str, str],
) -> str:
    if isinstance(spec, dict):
        source = str(spec.get("source") or "").strip()
        custom_value = str(spec.get("custom_value") or "")
    else:
        source = "custom"
        custom_value = "" if spec is None else str(spec)

    if source == "custom":
        return custom_value
    if source not in WHATSAPP_VARIABLE_CUSTOMER_FIELDS:
        return ""
    return str(customer_values.get(source) or "")


_BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT = 10
_BULK_MESSAGE_RENDER_SAMPLE_LIMIT = 3


def _mask_notification_recipient(
    recipient: str,
    channel: NotificationChannel,
) -> str:
    value = recipient.strip()
    if channel == NotificationChannel.email and "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:1] or '*'}***@{domain}"
    digits = "".join(character for character in value if character.isdigit())
    if digits:
        return f"***{digits[-4:]}"
    return "***"


def _bulk_message_recipient_item(
    subscriber: Subscriber,
    *,
    channel: NotificationChannel,
    recipient: str | None,
    disposition: str,
    reason_code: str | None = None,
    reason: str | None = None,
) -> dict[str, str]:
    item = {
        "id": str(subscriber.id),
        "name": _subscriber_display_name(subscriber),
        "account_number": str(
            subscriber.account_number or subscriber.subscriber_number or ""
        ),
        "recipient": (
            _mask_notification_recipient(recipient, channel) if recipient else ""
        ),
        "disposition": disposition,
    }
    if reason_code:
        item["reason_code"] = reason_code
    if reason:
        item["reason"] = reason
    return item


def _render_bulk_customer_message(
    db: Session,
    *,
    subscriber: Subscriber,
    template: NotificationTemplate,
    provider_template: dict[str, object] | None,
    payload_variables: dict[object, object],
    required_variables: set[str],
) -> tuple[str | None, str]:
    variables = _notification_template_variables(
        db,
        subscriber,
        required_variables=required_variables,
    )
    if provider_template:
        resolved_variables = {
            str(key): _resolve_whatsapp_variable(value, variables)
            for key, value in payload_variables.items()
        }
        return None, build_provider_template_body(
            name=str(provider_template["name"]),
            language=str(provider_template.get("language") or "en"),
            variables=resolved_variables,
        )
    return (
        _render_manual_template_text(
            template.subject or "Service Update",
            variables,
        ),
        _render_manual_template_text(template.body, variables),
    )


def _bulk_message_impact_token(
    *,
    scope_token: str,
    template: NotificationTemplate,
    channel: NotificationChannel,
    template_variables: dict[object, object],
    impact_rows: list[dict[str, object]],
) -> str:
    payload = {
        "scope_token": scope_token,
        "template": {
            "id": str(template.id),
            "updated_at": str(template.updated_at or ""),
            "subject": template.subject or "",
            "body": template.body or "",
            "conditions": template.conditions or {},
        },
        "channel": channel.value,
        "template_variables": template_variables,
        "recipients": sorted(
            impact_rows,
            key=lambda row: (
                str(row.get("subscriber_id") or ""),
                str(row.get("recipient") or ""),
                str(row.get("disposition") or ""),
            ),
        ),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def queue_bulk_message_from_payload(
    db: Session, payload: dict[str, Any]
) -> dict[str, object]:
    template_id = str(payload.get("template_id") or "").strip()
    channel_value = str(payload.get("channel") or "").strip().lower()
    if not template_id or not channel_value:
        raise HTTPException(
            status_code=400, detail="template_id and channel are required"
        )

    try:
        channel = NotificationChannel(channel_value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Unsupported notification channel"
        ) from exc

    sync_whatsapp_registry_templates(db)
    template = None
    if channel == NotificationChannel.whatsapp:
        template = _notification_template_for_whatsapp(db, template_id)
    else:
        template = db.get(NotificationTemplate, coerce_uuid(template_id))
    if not template or not template.is_active:
        raise HTTPException(status_code=404, detail="Template not found")
    if template.channel != channel:
        raise HTTPException(
            status_code=400,
            detail="Template channel does not match selected channel",
        )

    resolved = resolve_bulk_customer_scope(db, payload)
    customers = resolved.customers
    if not customers:
        raise HTTPException(status_code=400, detail="No customers matched this scope")
    resolved_scope_token = resolved.scope_token

    preview_only = bool(payload.get("preview_only"))
    if not preview_only:
        _require_bulk_execution_confirmation(
            payload,
            resolved=resolved,
            action_label="Bulk message",
        )
    provider_template = provider_template_from_template(template)
    payload_variables = payload.get("template_variables") or {}
    if not isinstance(payload_variables, dict):
        raise HTTPException(
            status_code=400, detail="template_variables must be an object"
        )
    required_variables: set[str] = set()
    if provider_template:
        for value in payload_variables.values():
            if isinstance(value, dict):
                source = str(value.get("source") or "").strip()
                if source in WHATSAPP_VARIABLE_CUSTOMER_FIELDS:
                    required_variables.add(source)
    else:
        required_variables = {
            *_unresolved_template_variables(template.subject or "Service Update"),
            *_unresolved_template_variables(template.body),
        }
        unsupported = sorted(required_variables - _MANUAL_TEMPLATE_VARIABLE_NAMES)
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{channel.value.upper()} template has unsupported or "
                    "unavailable variable(s): "
                    + ", ".join("{" + name + "}" for name in unsupported)
                ),
            )
    try:
        normalized_conditions = normalize_conditions(template.conditions)
    except NotificationTemplateConditionError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Template conditions are invalid: {exc}",
        ) from exc

    created_count = 0
    notification_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    recipient_summary: list[dict[str, str]] = []
    impact_rows: list[dict[str, object]] = []
    for customer_id in resolved.missing_ids:
        item = {
            "id": customer_id,
            "name": customer_id,
            "reason": "Customer not found",
        }
        if len(skipped) < _BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT:
            skipped.append(item)
        impact_rows.append(
            {
                "subscriber_id": customer_id,
                "recipient": None,
                "disposition": "skipped",
                "reason_code": "customer_not_found",
            }
        )
    suppressed: list[dict[str, str]] = []
    suppression_counts: dict[str, int] = {}
    queued_count = 0
    suppressed_count = 0
    skipped_count = len(resolved.missing_ids)
    category = resolve_notification_category("service_bulk_message")
    quiet_send_at = quiet_hours_send_at(db)
    addressed_customers: list[tuple[Subscriber, str]] = []
    for subscriber in customers:
        recipient = _resolve_notification_recipient(subscriber, channel)
        if not recipient:
            if len(skipped) < _BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT:
                skipped.append(
                    {
                        "id": str(subscriber.id),
                        "name": subscriber.company_name
                        or subscriber.display_name
                        or subscriber.full_name,
                        "reason": f"Missing {channel.value} recipient",
                    }
                )
            skipped_count += 1
            if len(recipient_summary) < _BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT:
                recipient_summary.append(
                    _bulk_message_recipient_item(
                        subscriber,
                        channel=channel,
                        recipient=None,
                        disposition="skipped",
                        reason_code="missing_recipient",
                        reason=f"Missing {channel.value} recipient",
                    )
                )
            impact_rows.append(
                {
                    "subscriber_id": str(subscriber.id),
                    "recipient": None,
                    "disposition": "skipped",
                    "reason_code": "missing_recipient",
                }
            )
            continue
        addressed_customers.append((subscriber, recipient))

    policy_decisions = evaluate_bulk_customer_notification_policy(
        db,
        CustomerNotificationPolicyCohortQuery(
            candidates=tuple(
                CustomerNotificationPolicyCandidate(
                    subscriber_id=subscriber.id,
                    recipient=recipient,
                )
                for subscriber, recipient in addressed_customers
            ),
            channel=channel,
            category=category,
            event_type="service_bulk_message",
            evaluated_at=datetime.now(UTC),
        ),
    )
    condition_matches = conditions_match_for_subscribers(
        db,
        subscriber_ids=tuple(
            subscriber.id for subscriber, _recipient in addressed_customers
        ),
        conditions=normalized_conditions,
    )
    planned: list[tuple[Subscriber, str, bool, str | None, str | None, str | None]] = []
    for subscriber, recipient in addressed_customers:
        decision = policy_decisions.decision_for(
            subscriber_id=subscriber.id,
            recipient=recipient,
        )
        condition_matched = condition_matches.get(subscriber.id, True)
        condition_reason_code: str | None = None
        condition_reason: str | None = None
        condition_last_error: str | None = None
        if not condition_matched:
            if _template_has_open_ticket_exclusion(normalized_conditions):
                condition_reason_code = "open_ticket"
                condition_reason = "Customer has an open ticket"
                condition_last_error = "Suppressed by open ticket template condition"
            else:
                condition_reason_code = "template_conditions"
                condition_reason = "Suppressed by template conditions"
                condition_last_error = condition_reason

        created_count += 1
        reason_code = decision.reason_code
        reason = decision.reason
        if decision.allowed and not condition_matched:
            reason_code = condition_reason_code
            reason = condition_reason
        disposition = "suppressed" if reason_code else "queued"
        if reason_code:
            suppression_counts[reason_code] = suppression_counts.get(reason_code, 0) + 1
            if len(suppressed) < _BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT:
                suppressed.append(
                    _bulk_message_suppression_item(
                        subscriber,
                        reason_code=reason_code,
                        reason=reason or "Suppressed by notification policy",
                    )
                )
            suppressed_count += 1
        else:
            queued_count += 1
        if len(recipient_summary) < _BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT:
            recipient_summary.append(
                _bulk_message_recipient_item(
                    subscriber,
                    channel=channel,
                    recipient=recipient,
                    disposition=disposition,
                    reason_code=reason_code,
                    reason=reason,
                )
            )
        impact_rows.append(
            {
                "subscriber_id": str(subscriber.id),
                "recipient": recipient,
                "disposition": disposition,
                "reason_code": reason_code,
            }
        )
        planned.append(
            (
                subscriber,
                recipient,
                not bool(reason_code),
                reason_code,
                reason,
                condition_last_error,
            )
        )

    impact_token = _bulk_message_impact_token(
        scope_token=resolved_scope_token,
        template=template,
        channel=channel,
        template_variables=payload_variables,
        impact_rows=impact_rows,
    )

    if preview_only:
        render_sample_count = 0
        for subscriber, _recipient, allowed, *_rest in planned:
            if not allowed:
                continue
            _render_bulk_customer_message(
                db,
                subscriber=subscriber,
                template=template,
                provider_template=provider_template,
                payload_variables=payload_variables,
                required_variables=required_variables,
            )
            render_sample_count += 1
            if render_sample_count >= _BULK_MESSAGE_RENDER_SAMPLE_LIMIT:
                break
        return {
            "success": True,
            "preview": True,
            "scope": resolved.scope,
            "matched_count": len(customers),
            "scope_token": resolved_scope_token,
            "impact_token": impact_token,
            "missing_ids": list(resolved.missing_ids),
            "created_count": created_count,
            "queued_count": queued_count,
            "suppressed_count": suppressed_count,
            "suppression_counts": suppression_counts,
            "skipped_count": skipped_count,
            "suppressed": suppressed,
            "skipped": skipped,
            "recipient_summary": recipient_summary,
            "recipient_summary_limit": _BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT,
            "render_sample_count": render_sample_count,
            "notification_ids": [],
        }

    expected_impact_token = str(payload.get("expected_impact_token") or "")
    if not expected_impact_token:
        raise HTTPException(
            status_code=400,
            detail="Preview the bulk message impact before confirming",
        )
    if not hmac.compare_digest(expected_impact_token, impact_token):
        raise HTTPException(
            status_code=409,
            detail=(
                "The recipients, template, or suppression impact changed after "
                "preview. Review the updated impact before confirming again."
            ),
        )

    queued_count = 0
    suppressed_count = 0
    suppressed = []
    suppression_counts = {}
    for subscriber, recipient, allowed, reason_code, reason, condition_error in planned:
        subject, body = _render_bulk_customer_message(
            db,
            subscriber=subscriber,
            template=template,
            provider_template=provider_template,
            payload_variables=payload_variables,
            required_variables=required_variables,
        )
        notification = notification_service.notifications.queue_customer_notification(
            db,
            NotificationCreate(
                template_id=template.id,
                subscriber_id=subscriber.id,
                channel=channel,
                event_type="service_bulk_message",
                category=category,
                recipient=recipient,
                subject=subject if channel == NotificationChannel.email else None,
                body=body,
                status=(
                    NotificationStatus.queued
                    if allowed
                    else NotificationStatus.canceled
                ),
                send_at=quiet_send_at if allowed else None,
                last_error=condition_error or reason,
            ),
        )
        if notification.id:
            notification_ids.append(str(notification.id))
        if notification.status == NotificationStatus.queued:
            queued_count += 1
        else:
            suppressed_count += 1
            resolved_reason_code = reason_code or "notification_policy"
            suppression_counts[resolved_reason_code] = (
                suppression_counts.get(resolved_reason_code, 0) + 1
            )
            if len(suppressed) < _BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT:
                suppressed.append(
                    _bulk_message_suppression_item(
                        subscriber,
                        reason_code=resolved_reason_code,
                        reason=reason or "Suppressed by notification policy",
                    )
                )
    db.commit()

    return {
        "success": True,
        "preview": preview_only,
        "scope": resolved.scope,
        "matched_count": len(customers),
        "scope_token": resolved_scope_token,
        "impact_token": impact_token,
        "missing_ids": list(resolved.missing_ids),
        "created_count": created_count,
        "queued_count": queued_count,
        "suppressed_count": suppressed_count,
        "suppression_counts": suppression_counts,
        "skipped_count": skipped_count,
        "suppressed": suppressed,
        "skipped": skipped,
        "recipient_summary": recipient_summary,
        "recipient_summary_limit": _BULK_MESSAGE_PREVIEW_SAMPLE_LIMIT,
        "notification_ids": notification_ids,
    }


def _bulk_message_suppression_item(
    subscriber: Subscriber, *, reason_code: str, reason: str
) -> dict[str, str]:
    return {
        "id": str(subscriber.id),
        "name": _subscriber_display_name(subscriber),
        "reason_code": reason_code,
        "reason": reason,
    }


def _subscriber_display_name(subscriber: Subscriber) -> str:
    return (
        subscriber.company_name
        or subscriber.display_name
        or subscriber.full_name
        or subscriber.email
        or subscriber.phone
        or str(subscriber.id)
    )


def _template_has_open_ticket_exclusion(conditions: Any) -> bool:
    try:
        normalized = normalize_conditions(conditions)
    except NotificationTemplateConditionError:
        return False
    for group_name in ("all", "any"):
        for condition in normalized.get(group_name, []):
            field = condition.get("field")
            operator = condition.get("operator")
            value = condition.get("value")
            if (
                field == "customer_has_open_ticket"
                and operator == "="
                and value is False
            ):
                return True
            if field == "open_ticket_count" and operator in {"=", "<="}:
                try:
                    if Decimal(str(value)) <= 0:
                        return True
                except (InvalidOperation, TypeError, ValueError):
                    continue
    return False


def _business_identity_from_contacts(
    company_name: str,
    contact_rows: list[dict[str, Any]],
) -> dict[str, str | None]:
    primary_contact = next(
        (row for row in contact_rows if row.get("is_primary")),
        contact_rows[0] if contact_rows else None,
    )
    email = _normalize_optional((primary_contact or {}).get("email"))
    return {
        "first_name": _normalize_optional((primary_contact or {}).get("first_name"))
        or company_name,
        "last_name": _normalize_optional((primary_contact or {}).get("last_name"))
        or "Business",
        "email": email or f"business-{uuid4().hex}@placeholder.local",
        "phone": _normalize_optional((primary_contact or {}).get("phone")),
    }


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _require_text(value: str | None, label: str, *, max_length: int) -> str:
    """Validate a required free-text field: trim, reject blank/whitespace-only,
    and enforce a max length so we surface a clean message instead of relying on
    a raw DB length error."""
    normalized = _normalize_optional(value)
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > max_length:
        raise ValueError(f"{label} must be {max_length} characters or fewer")
    return normalized


def _optional_int(value: str | None) -> int | None:
    normalized = _normalize_optional(value)
    if normalized is None:
        return None
    return int(normalized)


def _optional_decimal(value: str | None) -> Decimal | None:
    normalized = _normalize_optional(value)
    if normalized is None:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError("min_balance must be a valid decimal value.") from exc


def _billing_override_payload(
    *,
    billing_enabled_override: str | None,
    billing_day: str | None,
    payment_due_days: str | None,
    grace_period_days: str | None,
    min_balance: str | None,
    captive_redirect_enabled: str | None,
    tax_rate_id: str | None,
    payment_method: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "billing_day": _optional_int(billing_day),
        "payment_due_days": _optional_int(payment_due_days),
        "grace_period_days": _optional_int(grace_period_days),
        "min_balance": _optional_decimal(min_balance),
        "tax_rate_id": _normalize_optional(tax_rate_id),
        "payment_method": _normalize_optional(payment_method),
        "captive_redirect_enabled": captive_redirect_enabled == "true",
    }
    normalized_enabled = _normalize_optional(billing_enabled_override)
    if normalized_enabled == "true":
        payload["billing_enabled"] = True
    elif normalized_enabled == "false":
        payload["billing_enabled"] = False
    return payload


def _suspend_customer_subscriptions(db: Session, customer_id: str) -> int:
    """Suspend active/pending subscriptions for a customer via enforcement locks."""
    from app.models.enforcement_lock import EnforcementReason
    from app.services.account_lifecycle import suspend_subscription

    subscriptions = (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == coerce_uuid(customer_id))
        .filter(
            Subscription.status.in_(
                [SubscriptionStatus.active, SubscriptionStatus.pending]
            )
        )
        .all()
    )
    suspended_count = 0
    for subscription in subscriptions:
        try:
            suspend_subscription(
                db,
                str(subscription.id),
                reason=EnforcementReason.admin,
                source=f"admin:deactivate_customer:{customer_id}",
            )
            suspended_count += 1
        except ValueError as e:
            logger.info("Skipped suspending subscription %s: %s", subscription.id, e)
    return suspended_count


def change_customer_account_active_state(
    db: Session,
    subscriber: Subscriber,
    *,
    is_active: bool,
    source: str,
    actor_id: str | None = None,
) -> None:
    action = (
        account_status_commands.AccountStatusAction.activate
        if is_active
        else account_status_commands.AccountStatusAction.suspend
    )
    preview = account_status_commands.preview_account_status_change(
        db,
        account_status_commands.PreviewAccountStatusRequest(
            account_id=subscriber.id,
            action=action,
        ),
    )
    if not preview.allowed:
        return
    account_id = subscriber.id
    command_id = uuid4()
    db_session_adapter.release_read_transaction(db)
    account_status_commands.confirm_account_status_change(
        db,
        account_status_commands.ConfirmAccountStatusCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{actor_id}"
                if actor_id
                else "system:admin_customer_action",
                scope=account_status_commands.ACCOUNT_STATUS_WRITE_SCOPE,
                reason=(
                    "Administrative account activation"
                    if is_active
                    else "Administrative account suspension"
                ),
                idempotency_key=(
                    "admin-account-status:"
                    + hashlib.sha256(
                        f"{source}:{preview.fingerprint}".encode()
                    ).hexdigest()
                ),
            ),
            account_id=account_id,
            action=action,
            expected_preview_fingerprint=preview.fingerprint,
        ),
    )


def unsuspend_customer_account(
    db: Session,
    customer_id: str,
    *,
    preview_fingerprint: str,
    actor_id: str | None,
) -> account_status_commands.AccountStatusOutcome:
    """Confirm the reviewed, provenance-scoped account unsuspension."""
    account_id = coerce_uuid(customer_id)
    command_id = uuid4()
    db_session_adapter.release_read_transaction(db)
    return account_status_commands.confirm_account_status_change(
        db,
        account_status_commands.ConfirmAccountStatusCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=(
                    f"user:{actor_id}"
                    if actor_id
                    else "system:admin_customer_unsuspend"
                ),
                scope=account_status_commands.ACCOUNT_STATUS_WRITE_SCOPE,
                reason="Administrative account unsuspension",
                idempotency_key=(
                    "admin-unsuspend:"
                    + hashlib.sha256(
                        f"{account_id}:{preview_fingerprint}".encode()
                    ).hexdigest()
                ),
            ),
            account_id=account_id,
            action=account_status_commands.AccountStatusAction.unsuspend,
            expected_preview_fingerprint=preview_fingerprint,
        ),
    )


def _create_subscriber(db: Session, payload: dict[str, Any]) -> Subscriber:
    data = dict(payload)
    if not data.get("email"):
        data["email"] = f"customer-{uuid4().hex}@placeholder.local"
    if not data.get("first_name"):
        data["first_name"] = "Customer"
    if not data.get("last_name"):
        data["last_name"] = "User"
    return cast(
        Subscriber,
        subscriber_service.subscribers.create(db=db, payload=SubscriberCreate(**data)),
    )


def _create_subscriber_channels_from_rows(
    db: Session,
    account_id: str,
    contact_rows: list[dict],
) -> None:
    from app.services.customer_identity_normalization import normalize_channel_address
    from app.services.customer_identity_resolution import (
        rebuild_identity_index_for_subscriber,
    )

    subscriber = db.get(Subscriber, account_id)
    if not subscriber:
        return
    for row in contact_rows:
        email = normalize_channel_address("email", row.get("email")) or ""
        phone = normalize_channel_address("phone", row.get("phone")) or ""
        is_primary = bool(row.get("is_primary"))
        if email:
            existing_email_channels = (
                db.query(SubscriberChannel)
                .filter(SubscriberChannel.subscriber_id == subscriber.id)
                .filter(SubscriberChannel.channel_type == ChannelType.email)
                .all()
            )
            exists = next(
                (
                    channel
                    for channel in existing_email_channels
                    if normalize_channel_address("email", channel.address) == email
                ),
                None,
            )
            if not exists:
                db.add(
                    SubscriberChannel(
                        subscriber_id=subscriber.id,
                        channel_type=ChannelType.email,
                        address=email,
                        label=row.get("role") or row.get("title"),
                        is_primary=is_primary,
                    )
                )
        if phone:
            existing_phone_channels = (
                db.query(SubscriberChannel)
                .filter(SubscriberChannel.subscriber_id == subscriber.id)
                .filter(SubscriberChannel.channel_type == ChannelType.phone)
                .all()
            )
            exists = next(
                (
                    channel
                    for channel in existing_phone_channels
                    if normalize_channel_address("phone", channel.address) == phone
                ),
                None,
            )
            if not exists:
                db.add(
                    SubscriberChannel(
                        subscriber_id=subscriber.id,
                        channel_type=ChannelType.phone,
                        address=phone,
                        label=row.get("role") or row.get("title"),
                        is_primary=is_primary,
                    )
                )
    db.flush()
    rebuild_identity_index_for_subscriber(db, subscriber.id)


def parse_contact_rows(contact_columns: dict[str, list[str]]) -> list[dict[str, Any]]:
    contact_first_name = contact_columns.get("first_name", [])
    contact_last_name = contact_columns.get("last_name", [])
    contact_title = contact_columns.get("title", [])
    contact_role = contact_columns.get("role", [])
    contact_email = contact_columns.get("email", [])
    contact_phone = contact_columns.get("phone", [])
    contact_is_primary = contact_columns.get("is_primary", [])

    fields = [
        contact_first_name,
        contact_last_name,
        contact_title,
        contact_role,
        contact_email,
        contact_phone,
        contact_is_primary,
    ]
    max_len = max((len(field) for field in fields), default=0)
    rows: list[dict[str, Any]] = []
    for idx in range(max_len):
        first = (
            contact_first_name[idx].strip()
            if idx < len(contact_first_name) and contact_first_name[idx]
            else ""
        )
        last = (
            contact_last_name[idx].strip()
            if idx < len(contact_last_name) and contact_last_name[idx]
            else ""
        )
        title_value = (
            contact_title[idx].strip()
            if idx < len(contact_title) and contact_title[idx]
            else None
        )
        email_value = (
            contact_email[idx].strip()
            if idx < len(contact_email) and contact_email[idx]
            else None
        )
        phone_value = (
            contact_phone[idx].strip()
            if idx < len(contact_phone) and contact_phone[idx]
            else None
        )
        is_primary_value = (
            contact_is_primary[idx].strip().lower() == "true"
            if idx < len(contact_is_primary) and contact_is_primary[idx]
            else False
        )
        if not any(
            [first, last, title_value, email_value, phone_value, is_primary_value]
        ):
            continue
        if not first or not last:
            raise ValueError("Contact first and last name are required.")
        role_value = (
            contact_role[idx].strip()
            if idx < len(contact_role) and contact_role[idx]
            else "primary"
        )
        rows.append(
            {
                "first_name": first,
                "last_name": last,
                "title": title_value,
                "role": role_value,
                "email": email_value,
                "phone": phone_value,
                "is_primary": is_primary_value,
            }
        )
    return rows


def build_error_contact_rows(
    contact_columns: dict[str, list[str]],
) -> list[dict[str, Any]]:
    contact_first_name = contact_columns.get("first_name", [])
    contact_last_name = contact_columns.get("last_name", [])
    contact_title = contact_columns.get("title", [])
    contact_role = contact_columns.get("role", [])
    contact_email = contact_columns.get("email", [])
    contact_phone = contact_columns.get("phone", [])
    contact_is_primary = contact_columns.get("is_primary", [])
    rows: list[dict[str, Any]] = []
    for idx in range(
        max(
            len(contact_first_name),
            len(contact_last_name),
            len(contact_title),
            len(contact_role),
            len(contact_email),
            len(contact_phone),
            len(contact_is_primary),
        )
    ):
        rows.append(
            {
                "first_name": contact_first_name[idx]
                if idx < len(contact_first_name)
                else "",
                "last_name": contact_last_name[idx]
                if idx < len(contact_last_name)
                else "",
                "title": contact_title[idx] if idx < len(contact_title) else "",
                "role": contact_role[idx] if idx < len(contact_role) else "primary",
                "email": contact_email[idx] if idx < len(contact_email) else "",
                "phone": contact_phone[idx] if idx < len(contact_phone) else "",
                "is_primary": (
                    contact_is_primary[idx].strip().lower() == "true"
                    if idx < len(contact_is_primary) and contact_is_primary[idx]
                    else False
                ),
            }
        )
    return rows


def create_customer_from_wizard(db: Session, data: dict[str, Any]) -> tuple[str, str]:
    customer_type = data.get("customer_type", "person")
    if customer_type == "person":
        existing_metadata = data.get("metadata")
        if isinstance(existing_metadata, dict):
            ingest_metadata = existing_metadata
        else:
            ingest_metadata = {}
        if not ingest_metadata.get("ingest"):
            ingest_metadata["ingest"] = {
                "source": "admin/customers/wizard",
                "received_at": datetime.now(UTC).isoformat(),
                "raw": dict(data),
                "cleaning_version": "v1",
            }
        email = (data.get("email") or "").strip()
        if not email:
            raise ValueError("email is required")
        # Email is contact info, not an identity — duplicates are valid
        # (customers under one reseller often share a contact address).
        person = _create_subscriber(
            db=db,
            payload={
                "first_name": (data.get("first_name") or "").strip(),
                "last_name": (data.get("last_name") or "").strip(),
                "display_name": (data.get("display_name") or "").strip() or None,
                "email": email,
                "phone": (data.get("phone") or "").strip() or None,
                "date_of_birth": data.get("date_of_birth") or None,
                "gender": data.get("gender", "unknown"),
                "billing_mode": data.get("billing_mode", "prepaid"),
                "address_line1": (data.get("address_line1") or "").strip() or None,
                "address_line2": (data.get("address_line2") or "").strip() or None,
                "city": (data.get("city") or "").strip() or None,
                "region": (data.get("region") or "").strip() or None,
                "postal_code": (data.get("postal_code") or "").strip() or None,
                "country_code": (data.get("country_code") or "").strip() or None,
                "is_active": data.get("is_active", True),
                "status": data.get("status", "active"),
                "notes": (data.get("notes") or "").strip() or None,
                "metadata_": ingest_metadata,
            },
        )
        return "person", str(person.id)

    if customer_type == "business":
        company_name = (data.get("name") or "").strip()
        if not company_name:
            raise ValueError("Business name is required")
        contacts = [
            item
            for item in data.get("contacts", [])
            if (item.get("first_name") or "").strip()
            or (item.get("last_name") or "").strip()
            or (item.get("email") or "").strip()
            or (item.get("phone") or "").strip()
        ]
        identity = _business_identity_from_contacts(company_name, contacts)
        subscriber = _create_subscriber(
            db=db,
            payload={
                "first_name": identity["first_name"],
                "last_name": identity["last_name"],
                "display_name": company_name,
                "company_name": company_name,
                "legal_name": (data.get("legal_name") or "").strip() or None,
                "tax_id": (data.get("tax_id") or "").strip() or None,
                "domain": (data.get("domain") or "").strip() or None,
                "website": (data.get("website") or "").strip() or None,
                "email": identity["email"],
                "phone": identity["phone"],
                "billing_mode": data.get("billing_mode", "prepaid"),
                "address_line1": (data.get("address_line1") or "").strip() or None,
                "address_line2": (data.get("address_line2") or "").strip() or None,
                "city": (data.get("city") or "").strip() or None,
                "region": (data.get("region") or "").strip() or None,
                "postal_code": (data.get("postal_code") or "").strip() or None,
                "country_code": (data.get("country_code") or "").strip() or None,
                "is_active": True,
                "status": "active",
                "notes": (data.get("notes") or "").strip() or None,
                "category": SubscriberCategory.business.value,
            },
        )
        if contacts:
            _create_subscriber_channels_from_rows(db, str(subscriber.id), contacts)
        return "business", str(subscriber.id)

    raise ValueError("Invalid customer type")


def create_customer_from_form(
    db: Session,
    *,
    customer_type: str,
    form_data: dict[str, Any],
    contact_columns: dict[str, list[str]],
) -> tuple[str, str]:
    contact_rows = parse_contact_rows(contact_columns)
    if customer_type not in {"person", "business"}:
        raise ValueError("customer_type must be person or business")
    if customer_type == "person":
        normalized_email = _normalize_optional(form_data.get("email"))
        if not normalized_email:
            raise ValueError("email is required")
        first_name = _require_text(
            form_data.get("first_name"), "First name", max_length=80
        )
        last_name = _require_text(
            form_data.get("last_name"), "Last name", max_length=80
        )
        reseller_id = resolve_customer_reseller_id(
            db,
            managed_by_reseller=bool(form_data.get("managed_by_reseller")),
            reseller_id=str(form_data.get("reseller_id") or ""),
        )
        # Email is contact info, not an identity — duplicates are valid.
        customer = _create_subscriber(
            db=db,
            payload={
                "first_name": first_name,
                "last_name": last_name,
                "display_name": _normalize_optional(form_data.get("display_name")),
                "avatar_url": _normalize_optional(form_data.get("avatar_url")),
                "email": normalized_email,
                "email_verified": form_data.get("email_verified") == "true",
                "phone": _normalize_optional(form_data.get("phone")),
                "nin": form_data.get("nin") or None,
                "date_of_birth": form_data.get("date_of_birth") or None,
                "gender": form_data.get("gender") or "unknown",
                "preferred_contact_method": form_data.get("preferred_contact_method")
                or None,
                "locale": _normalize_optional(form_data.get("locale")),
                "timezone": _normalize_optional(form_data.get("timezone")),
                "address_line1": _normalize_optional(form_data.get("address_line1")),
                "address_line2": _normalize_optional(form_data.get("address_line2")),
                "city": _normalize_optional(form_data.get("city")),
                "region": _normalize_optional(form_data.get("region")),
                "postal_code": _normalize_optional(form_data.get("postal_code")),
                "country_code": _normalize_optional(form_data.get("country_code")),
                "pop_site_id": _normalize_optional(form_data.get("pop_site_id")),
                "status": form_data.get("status") or "active",
                "is_active": form_data.get("is_active") == "true",
                "marketing_opt_in": form_data.get("marketing_opt_in") == "true",
                "category": SubscriberCategory.residential.value,
                "reseller_id": reseller_id,
                "captive_redirect_enabled": form_data.get("captive_redirect_enabled")
                == "true",
                "account_start_date": _parse_date(form_data.get("account_start_date")),
                "notes": _normalize_optional(form_data.get("notes")),
                "metadata_": form_data.get("metadata_json"),
            },
        )
        if contact_rows:
            _create_subscriber_channels_from_rows(db, str(customer.id), contact_rows)
        return "person", str(customer.id)

    company_name = _require_text(form_data.get("name"), "Business name", max_length=120)
    reseller_id = resolve_customer_reseller_id(
        db,
        managed_by_reseller=bool(form_data.get("managed_by_reseller")),
        reseller_id=str(form_data.get("reseller_id") or ""),
    )
    identity = _business_identity_from_contacts(company_name, contact_rows)
    business = _create_subscriber(
        db=db,
        payload={
            "first_name": identity["first_name"],
            "last_name": identity["last_name"],
            "display_name": company_name,
            "company_name": company_name,
            "legal_name": _normalize_optional(form_data.get("legal_name")),
            "tax_id": _normalize_optional(form_data.get("tax_id")),
            "domain": _normalize_optional(form_data.get("domain")),
            "website": _normalize_optional(form_data.get("website")),
            "email": identity["email"],
            "phone": identity["phone"],
            "is_active": True,
            "category": SubscriberCategory.business.value,
            "reseller_id": reseller_id,
            "notes": _normalize_optional(form_data.get("org_notes")),
            "account_start_date": _parse_date(form_data.get("org_account_start_date")),
        },
    )
    if contact_rows:
        _create_subscriber_channels_from_rows(db, str(business.id), contact_rows)
    return "business", str(business.id)


def create_impersonation_session(
    db: Session,
    request: Request,
    customer_type: str,
    customer_id: str,
    account_id: str,
    subscription_id: str | None,
    auth: dict,
) -> str:
    subscribers = []
    if customer_type == "person":
        subscriber = db.get(Subscriber, customer_id)
        subscribers = [subscriber] if subscriber else []
    else:
        subscriber = db.get(Subscriber, customer_id)
        subscribers = [subscriber] if subscriber else []

    accounts = [sub for sub in subscribers if sub]
    account_lookup = {str(acc.id): acc for acc in accounts}
    selected_account = account_lookup.get(account_id)
    if not selected_account:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    selected_subscription_id = None
    if subscription_id:
        subscription = catalog_service.subscriptions.get(
            db=db, subscription_id=subscription_id
        )
        if str(getattr(subscription, "subscriber_id", "")) != str(selected_account.id):
            raise HTTPException(status_code=404, detail="Subscription not found")
        selected_subscription_id = subscription.id
    else:
        active_subs = catalog_service.subscriptions.list(
            db=db,
            subscriber_id=str(selected_account.id),
            offer_id=None,
            status="active",
            order_by="created_at",
            order_dir="desc",
            limit=1,
            offset=0,
        )
        if active_subs:
            selected_subscription_id = active_subs[0].id
        else:
            any_subs = catalog_service.subscriptions.list(
                db=db,
                subscriber_id=str(selected_account.id),
                offer_id=None,
                status=None,
                order_by="created_at",
                order_dir="desc",
                limit=1,
                offset=0,
            )
            if any_subs:
                selected_subscription_id = any_subs[0].id

    session_token = customer_portal.create_customer_session(
        username=f"impersonate:{customer_type}:{customer_id}:{selected_account.id}",
        account_id=selected_account.id,
        subscriber_id=selected_account.id,
        subscription_id=selected_subscription_id,
        is_impersonation=True,
        return_to=(
            f"/admin/customers/business/{selected_account.id}"
            if selected_account.category == SubscriberCategory.business
            else f"/admin/customers/person/{selected_account.id}"
        ),
    )

    actor_id_value = None
    if isinstance(auth, dict):
        actor_id_value = (
            str(auth.get("subscriber_id") or auth.get("person_id") or "") or None
        )

    audit_payload = AuditEventCreate(
        actor_type=AuditActorType.user,
        actor_id=actor_id_value,
        action="impersonate",
        entity_type="subscriber_account",
        entity_id=str(selected_account.id),
        status_code=303,
        is_success=True,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        metadata_={
            "customer_type": customer_type,
            "customer_id": customer_id,
            "subscription_id": str(selected_subscription_id)
            if selected_subscription_id
            else None,
        },
    )
    audit_service.audit_events.create(db=db, payload=audit_payload)
    return session_token


def update_person_customer(
    db: Session,
    customer_id: str,
    *,
    first_name: str,
    last_name: str,
    display_name: str | None,
    avatar_url: str | None,
    email: str | None,
    email_verified: str | None,
    phone: str | None,
    nin: str | None,
    date_of_birth: str | None,
    gender: str | None,
    preferred_contact_method: str | None,
    locale: str | None,
    timezone_value: str | None,
    address_line1: str | None,
    address_line2: str | None,
    city: str | None,
    region: str | None,
    postal_code: str | None,
    country_code: str | None,
    marketing_opt_in: str | None,
    notes: str | None,
    account_start_date: str | None,
    billing_enabled_override: str | None,
    billing_day: str | None,
    payment_due_days: str | None,
    grace_period_days: str | None,
    min_balance: str | None,
    captive_redirect_enabled: str | None,
    tax_rate_id: str | None,
    withholding_tax_enabled: str | None,
    payment_method: str | None,
    metadata_json: dict | None,
    managed_by_reseller: bool | None = None,
    reseller_id: str | None = None,
    vat_exempt: str | None = None,
    actor_id: str | None = None,
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    resolved_reseller_id = (
        before.reseller_id
        if managed_by_reseller is None
        else resolve_customer_reseller_id(
            db,
            managed_by_reseller=managed_by_reseller,
            reseller_id=reseller_id,
        )
    )
    # Email is contact info, not an identity — duplicates across customers are
    # valid, so editing one to match another's address is allowed.
    normalized_email = _normalize_optional(email)
    billing_payload = _billing_override_payload(
        billing_enabled_override=billing_enabled_override,
        billing_day=billing_day,
        payment_due_days=payment_due_days,
        grace_period_days=grace_period_days,
        min_balance=min_balance,
        captive_redirect_enabled=captive_redirect_enabled,
        tax_rate_id=tax_rate_id,
        payment_method=payment_method,
    )
    requested_billing_approval = billing_payload.pop("billing_enabled", None)
    if requested_billing_approval is True:
        _apply_billing_approval_command(
            db,
            account_id=before.id,
            approved=True,
            actor_id=actor_id,
            reason="Administrator approved account billing and service",
        )
    data: dict[str, Any] = {
        "first_name": _require_text(first_name, "First name", max_length=80),
        "last_name": _require_text(last_name, "Last name", max_length=80),
        "display_name": _normalize_optional(display_name),
        "avatar_url": _normalize_optional(avatar_url),
        "email": normalized_email,
        "email_verified": email_verified == "true",
        "phone": phone or None,
        "nin": nin or None,
        "date_of_birth": date_of_birth or None,
        "gender": gender or None,
        "preferred_contact_method": preferred_contact_method or None,
        "locale": _normalize_optional(locale),
        "timezone": _normalize_optional(timezone_value),
        "address_line1": _normalize_optional(address_line1),
        "address_line2": _normalize_optional(address_line2),
        "city": _normalize_optional(city),
        "region": _normalize_optional(region),
        "postal_code": _normalize_optional(postal_code),
        "country_code": _normalize_optional(country_code),
        "marketing_opt_in": marketing_opt_in == "true",
        "notes": _normalize_optional(notes),
        "reseller_id": resolved_reseller_id,
    }
    if metadata_json is not None:
        metadata_payload = dict(metadata_json)
        metadata_payload["subscriber_category"] = before.category.value
        data["metadata_"] = metadata_payload
    if bool((before.metadata_ or {}).get("nin_verified")) and data["nin"] != before.nin:
        data["nin"] = before.nin
    data.update(billing_payload)
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=customer_id,
        payload=SubscriberUpdate.model_validate(data),
    )
    from app.services import customer_tax_policies

    # Capture the command inputs while the read transaction is still open, then
    # release it before entering the WHT-policy owner boundary, which requires a
    # transaction-free session at entry. Reading before.id after the release
    # would re-expire the row and reopen a transaction.
    wht_account_id = before.id
    wht_actor = str(actor_id or f"customer:{before.id}")
    wht_enabled = withholding_tax_enabled == "true"
    vat_exempt_enabled = vat_exempt == "true"
    db_session_adapter.release_read_transaction(db)
    customer_tax_policies.set_customer_withholding_tax_policy(
        db,
        customer_tax_policies.SetCustomerWithholdingTaxPolicyCommand(
            account_id=wht_account_id,
            withholding_tax_enabled=wht_enabled,
            updated_by=wht_actor,
        ),
        context=CommandContext.system(
            actor=wht_actor,
            scope=customer_tax_policies.WRITE_SCOPE,
            reason="Administrator updated customer withholding-tax eligibility",
            idempotency_key=f"customer-wht-policy:{wht_account_id}:{wht_enabled}",
        ),
    )
    db_session_adapter.release_read_transaction(db)
    customer_tax_policies.set_customer_vat_exemption_policy(
        db,
        customer_tax_policies.SetCustomerVatExemptionPolicyCommand(
            account_id=wht_account_id,
            vat_exempt=vat_exempt_enabled,
            updated_by=wht_actor,
        ),
        context=CommandContext.system(
            actor=wht_actor,
            scope=customer_tax_policies.WRITE_SCOPE,
            reason="Administrator updated customer VAT exemption",
            idempotency_key=(
                f"customer-vat-exemption:{wht_account_id}:{vat_exempt_enabled}"
            ),
        ),
    )
    if requested_billing_approval is False:
        _apply_billing_approval_command(
            db,
            account_id=before.id,
            approved=False,
            actor_id=actor_id,
            reason="Administrator revoked account billing and service approval",
        )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if account_start_date:
        subscriber = db.get(Subscriber, customer_id)
        if subscriber:
            parsed_date = _parse_date(account_start_date)
            if parsed_date:
                subscriber.account_start_date = parsed_date
                db.commit()
    return before, after


def update_business_customer(
    db: Session,
    customer_id: str,
    *,
    name: str,
    legal_name: str | None,
    tax_id: str | None,
    domain: str | None,
    website: str | None,
    org_notes: str | None,
    org_account_start_date: str | None,
    billing_enabled_override: str | None,
    billing_day: str | None,
    payment_due_days: str | None,
    grace_period_days: str | None,
    min_balance: str | None,
    captive_redirect_enabled: str | None,
    tax_rate_id: str | None,
    withholding_tax_enabled: str | None,
    payment_method: str | None,
    managed_by_reseller: bool | None = None,
    reseller_id: str | None = None,
    vat_exempt: str | None = None,
    actor_id: str | None = None,
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    resolved_reseller_id = (
        before.reseller_id
        if managed_by_reseller is None
        else resolve_customer_reseller_id(
            db,
            managed_by_reseller=managed_by_reseller,
            reseller_id=reseller_id,
        )
    )
    company_name = _require_text(name, "Business name", max_length=120)
    billing_payload = _billing_override_payload(
        billing_enabled_override=billing_enabled_override,
        billing_day=billing_day,
        payment_due_days=payment_due_days,
        grace_period_days=grace_period_days,
        min_balance=min_balance,
        captive_redirect_enabled=captive_redirect_enabled,
        tax_rate_id=tax_rate_id,
        payment_method=payment_method,
    )
    requested_billing_approval = billing_payload.pop("billing_enabled", None)
    if requested_billing_approval is True:
        _apply_billing_approval_command(
            db,
            account_id=before.id,
            approved=True,
            actor_id=actor_id,
            reason="Administrator approved account billing and service",
        )
    payload = SubscriberUpdate.model_validate(
        {
            "company_name": company_name,
            "display_name": company_name,
            "legal_name": _normalize_optional(legal_name),
            "tax_id": _normalize_optional(tax_id),
            "domain": _normalize_optional(domain),
            "website": _normalize_optional(website),
            "notes": _normalize_optional(org_notes),
            "category": SubscriberCategory.business.value,
            "reseller_id": resolved_reseller_id,
            **billing_payload,
        }
    )
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=customer_id,
        payload=payload,
    )
    from app.services import customer_tax_policies

    # Capture the command inputs while the read transaction is still open, then
    # release it before entering the WHT-policy owner boundary, which requires a
    # transaction-free session at entry. Reading before.id after the release
    # would re-expire the row and reopen a transaction.
    wht_account_id = before.id
    wht_actor = str(actor_id or f"customer:{before.id}")
    wht_enabled = withholding_tax_enabled == "true"
    vat_exempt_enabled = vat_exempt == "true"
    db_session_adapter.release_read_transaction(db)
    customer_tax_policies.set_customer_withholding_tax_policy(
        db,
        customer_tax_policies.SetCustomerWithholdingTaxPolicyCommand(
            account_id=wht_account_id,
            withholding_tax_enabled=wht_enabled,
            updated_by=wht_actor,
        ),
        context=CommandContext.system(
            actor=wht_actor,
            scope=customer_tax_policies.WRITE_SCOPE,
            reason="Administrator updated customer withholding-tax eligibility",
            idempotency_key=f"customer-wht-policy:{wht_account_id}:{wht_enabled}",
        ),
    )
    db_session_adapter.release_read_transaction(db)
    customer_tax_policies.set_customer_vat_exemption_policy(
        db,
        customer_tax_policies.SetCustomerVatExemptionPolicyCommand(
            account_id=wht_account_id,
            vat_exempt=vat_exempt_enabled,
            updated_by=wht_actor,
        ),
        context=CommandContext.system(
            actor=wht_actor,
            scope=customer_tax_policies.WRITE_SCOPE,
            reason="Administrator updated customer VAT exemption",
            idempotency_key=(
                f"customer-vat-exemption:{wht_account_id}:{vat_exempt_enabled}"
            ),
        ),
    )
    if requested_billing_approval is False:
        _apply_billing_approval_command(
            db,
            account_id=before.id,
            approved=False,
            actor_id=actor_id,
            reason="Administrator revoked account billing and service approval",
        )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if org_account_start_date:
        subscriber = db.get(Subscriber, customer_id)
        if subscriber:
            parsed_date = _parse_date(org_account_start_date)
            if parsed_date:
                subscriber.account_start_date = parsed_date
                db.commit()
    return before, after


def convert_person_to_business_customer(
    db: Session,
    customer_id: str,
    *,
    company_name: str,
    legal_name: str | None,
    tax_id: str | None,
    domain: str | None,
    website: str | None,
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if before.category == SubscriberCategory.business:
        raise ValueError("Customer is already a business customer.")

    normalized_company_name = _require_text(
        company_name, "Business name", max_length=160
    )
    db.expunge(before)
    payload = SubscriberUpdate.model_validate(
        {
            "category": SubscriberCategory.business.value,
            "company_name": normalized_company_name,
            "display_name": normalized_company_name,
            "legal_name": _normalize_optional(legal_name),
            "tax_id": _normalize_optional(tax_id),
            "domain": _normalize_optional(domain),
            "website": _normalize_optional(website),
        }
    )
    subscriber_service.subscribers.update(
        db=db,
        subscriber_id=customer_id,
        payload=payload,
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    return before, after


def deactivate_person_customer(
    db: Session, customer_id: str, *, actor_id: str | None = None
):
    before = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    change_customer_account_active_state(
        db,
        before,
        is_active=False,
        source=f"admin:deactivate_customer:{customer_id}",
        actor_id=actor_id,
    )
    after = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    return before, after


def deactivate_business_customer(
    db: Session, customer_id: str, *, actor_id: str | None = None
) -> None:
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    change_customer_account_active_state(
        db,
        subscriber,
        is_active=False,
        source=f"admin:deactivate_business:{customer_id}",
        actor_id=actor_id,
    )


def delete_person_customer(db: Session, customer_id: str) -> None:
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if subscriber.is_active:
        raise HTTPException(
            status_code=409, detail="Deactivate customer before deleting."
        )

    db.query(UserCredential).filter(
        UserCredential.subscriber_id == subscriber.id
    ).delete(synchronize_session=False)
    db.query(MFAMethod).filter(MFAMethod.subscriber_id == subscriber.id).delete(
        synchronize_session=False
    )
    db.query(AuthSession).filter(AuthSession.subscriber_id == subscriber.id).delete(
        synchronize_session=False
    )
    db.query(ApiKey).filter(ApiKey.subscriber_id == subscriber.id).delete(
        synchronize_session=False
    )
    db.commit()
    subscriber_service.subscribers.delete(db=db, subscriber_id=customer_id)


def delete_business_customer(db: Session, customer_id: str) -> None:
    subscriber = subscriber_service.subscribers.get(db=db, subscriber_id=customer_id)
    if (
        db.query(Subscription)
        .filter(Subscription.subscriber_id == subscriber.id)
        .count()
    ):
        raise HTTPException(
            status_code=409,
            detail="Delete subscriptions before deleting business customer.",
        )
    delete_person_customer(db, customer_id)


def bulk_update_customer_status(
    db: Session,
    customer_ids: list[dict[str, str]],
    is_active: bool,
) -> dict[str, Any]:
    updated_count = 0
    errors: list[dict[str, str]] = []
    for item in customer_ids:
        customer_id = item.get("id")
        customer_type = item.get("type")
        try:
            if customer_type in {"person", "subscriber"}:
                subscriber = db.get(Subscriber, customer_id)
                if not subscriber:
                    errors.append(
                        {
                            "id": str(customer_id),
                            "type": str(customer_type),
                            "error": "Subscriber not found",
                        }
                    )
                    continue
                change_customer_account_active_state(
                    db,
                    subscriber,
                    is_active=is_active,
                    source=(
                        f"admin:bulk_activate:{customer_id}"
                        if is_active
                        else f"admin:bulk_deactivate:{customer_id}"
                    ),
                )

            elif customer_type == "business":
                subscriber = db.get(Subscriber, customer_id)
                if not subscriber:
                    errors.append(
                        {
                            "id": str(customer_id),
                            "type": str(customer_type),
                            "error": "Business customer not found",
                        }
                    )
                    continue
                change_customer_account_active_state(
                    db,
                    subscriber,
                    is_active=is_active,
                    source=(
                        f"admin:bulk_activate:{customer_id}"
                        if is_active
                        else f"admin:bulk_deactivate:{customer_id}"
                    ),
                )

            updated_count += 1
        except Exception as exc:
            errors.append(
                {"id": str(customer_id), "type": str(customer_type), "error": str(exc)}
            )
    db.commit()
    return {
        "success": True,
        "updated_count": updated_count,
        "errors": errors,
    }


def bulk_delete_customers(
    db: Session,
    customer_ids: list[dict[str, str]],
) -> dict[str, Any]:
    deleted_count = 0
    skipped: list[dict[str, str]] = []
    for item in customer_ids:
        customer_id = item.get("id")
        customer_type = item.get("type")
        try:
            if customer_type in {"person", "subscriber"}:
                subscriber = db.get(Subscriber, customer_id)
                if not subscriber:
                    skipped.append(
                        {
                            "id": str(customer_id),
                            "type": str(customer_type),
                            "reason": "Subscriber not found",
                        }
                    )
                    continue
                if subscriber.is_active:
                    skipped.append(
                        {
                            "id": str(customer_id),
                            "type": str(customer_type),
                            "reason": "Customer is still active",
                        }
                    )
                    continue
                if (
                    db.query(Subscription)
                    .filter(Subscription.subscriber_id == subscriber.id)
                    .count()
                ):
                    skipped.append(
                        {
                            "id": str(customer_id),
                            "type": str(customer_type),
                            "reason": "Has associated subscriptions",
                        }
                    )
                    continue
                db.query(UserCredential).filter(
                    UserCredential.subscriber_id == subscriber.id
                ).delete(synchronize_session=False)
                db.query(MFAMethod).filter(
                    MFAMethod.subscriber_id == subscriber.id
                ).delete(synchronize_session=False)
                db.query(AuthSession).filter(
                    AuthSession.subscriber_id == subscriber.id
                ).delete(synchronize_session=False)
                db.query(ApiKey).filter(ApiKey.subscriber_id == subscriber.id).delete(
                    synchronize_session=False
                )
                subscriber_service.subscribers.delete(
                    db=db, subscriber_id=str(customer_id)
                )
                deleted_count += 1
            elif customer_type == "business":
                subscriber = db.get(Subscriber, customer_id)
                if not subscriber:
                    skipped.append(
                        {
                            "id": str(customer_id),
                            "type": str(customer_type),
                            "reason": "Business customer not found",
                        }
                    )
                    continue
                if (
                    db.query(Subscription)
                    .filter(Subscription.subscriber_id == subscriber.id)
                    .count()
                ):
                    skipped.append(
                        {
                            "id": str(customer_id),
                            "type": str(customer_type),
                            "reason": "Has associated subscriptions",
                        }
                    )
                    continue
                delete_person_customer(db, str(customer_id))
                deleted_count += 1
        except Exception as exc:
            skipped.append(
                {"id": str(customer_id), "type": str(customer_type), "reason": str(exc)}
            )
    return {
        "success": True,
        "deleted_count": deleted_count,
        "skipped": skipped,
    }


def export_customers_csv(
    db: Session,
    *,
    ids: str,
    search: str | None,
    customer_type: str | None,
) -> tuple[str, str]:
    customers: list[dict[str, str]] = []
    if ids == "all":
        if customer_type != "business":
            people_stmt = select(Subscriber).where(
                func.lower(
                    func.coalesce(
                        Subscriber.metadata_["subscriber_category"].as_string(), ""
                    )
                )
                != SubscriberCategory.business.value
            )
            if search:
                people_stmt = people_stmt.where(Subscriber.email.ilike(f"%{search}%"))
            people = db.scalars(
                people_stmt.order_by(Subscriber.created_at.desc())
            ).all()
            for person in people:
                customers.append(
                    {
                        "id": str(person.id),
                        "type": "person",
                        "name": f"{person.first_name} {person.last_name}",
                        "email": person.email,
                        "phone": person.phone or "",
                        "is_active": "Active" if person.is_active else "Inactive",
                        "created_at": person.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        if person.created_at
                        else "",
                    }
                )
        if customer_type != "person":
            orgs_stmt = select(Subscriber).where(
                func.lower(
                    func.coalesce(
                        Subscriber.metadata_["subscriber_category"].as_string(), ""
                    )
                )
                == SubscriberCategory.business.value
            )
            if search:
                orgs_stmt = orgs_stmt.where(
                    Subscriber.company_name.ilike(f"%{search}%")
                )
            orgs = db.scalars(orgs_stmt.order_by(Subscriber.company_name.asc())).all()
            for org in orgs:
                customers.append(
                    {
                        "id": str(org.id),
                        "type": "business",
                        "name": org.company_name or org.display_name or org.full_name,
                        "email": org.email,
                        "phone": org.phone or "",
                        "is_active": "Active" if org.is_active else "Inactive",
                        "created_at": org.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        if org.created_at
                        else "",
                    }
                )
    else:
        for item in ids.split(","):
            if ":" not in item:
                continue
            ctype, cid = item.split(":", 1)
            try:
                if ctype == "person":
                    person = subscriber_service.subscribers.get(
                        db=db, subscriber_id=cid
                    )
                    customers.append(
                        {
                            "id": str(person.id),
                            "type": "person",
                            "name": f"{person.first_name} {person.last_name}",
                            "email": person.email,
                            "phone": person.phone or "",
                            "is_active": "Active" if person.is_active else "Inactive",
                            "created_at": person.created_at.strftime(
                                "%Y-%m-%d %H:%M:%S"
                            )
                            if person.created_at
                            else "",
                        }
                    )
                elif ctype == "business":
                    org = subscriber_service.subscribers.get(db=db, subscriber_id=cid)
                    customers.append(
                        {
                            "id": str(org.id),
                            "type": "business",
                            "name": org.company_name
                            or org.display_name
                            or org.full_name,
                            "email": org.email,
                            "phone": org.phone or "",
                            "is_active": "Active" if org.is_active else "Inactive",
                            "created_at": org.created_at.strftime("%Y-%m-%d %H:%M:%S")
                            if org.created_at
                            else "",
                        }
                    )
            except Exception:
                logger.debug(
                    "Skipping organization %s during customer export",
                    getattr(org, "id", None),
                    exc_info=True,
                )
                continue
    output = io.StringIO()
    fieldnames = ["id", "type", "name", "email", "phone", "is_active", "created_at"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for customer in customers:
        writer.writerow(customer)
    content = output.getvalue()
    output.close()
    filename = f"customers_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return content, filename


def _status_from_legacy(
    value: str | None, is_active: bool | None = None
) -> SubscriberStatus | None:
    if is_active is not None and not is_active:
        return SubscriberStatus.suspended
    if not value:
        return None
    normalized = str(value).strip().lower()
    mapping = {
        "active": SubscriberStatus.active,
        "customer": SubscriberStatus.active,
        "subscriber": SubscriberStatus.active,
        "lead": SubscriberStatus.active,
        "contact": SubscriberStatus.active,
        "inactive": SubscriberStatus.suspended,
        "blocked": SubscriberStatus.suspended,
        "suspended": SubscriberStatus.suspended,
        "delinquent": SubscriberStatus.delinquent,
        "canceled": SubscriberStatus.canceled,
    }
    return mapping.get(normalized)


def convert_contact_to_subscriber(
    db: Session,
    *,
    person_id: UUID,
    account_status: str | None,
) -> tuple[Subscriber, bool]:
    person = db.get(Subscriber, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    person.is_active = True
    person.status = (
        _status_from_legacy(account_status, is_active=True) or SubscriberStatus.active
    )
    db.commit()
    db.refresh(person)
    return person, not bool(person.email)


def create_customer_address(
    db: Session,
    *,
    subscriber_id: str,
    address_type: str,
    label: str | None,
    address_line1: str,
    address_line2: str | None,
    city: str | None,
    region: str | None,
    postal_code: str | None,
    country_code: str | None,
    is_primary: str | None,
) -> None:
    addr_type_map = {
        "service": AddressType.service,
        "billing": AddressType.billing,
        "mailing": AddressType.mailing,
    }
    payload = AddressCreate(
        subscriber_id=UUID(subscriber_id),
        address_type=addr_type_map.get(address_type, AddressType.service),
        label=label or None,
        address_line1=address_line1,
        address_line2=address_line2 or None,
        city=city or None,
        region=region or None,
        postal_code=postal_code or None,
        country_code=country_code or None,
        is_primary=is_primary == "true",
    )
    subscriber_service.addresses.create(db=db, payload=payload)


def delete_customer_address(db: Session, *, address_id: str) -> None:
    subscriber_service.addresses.delete(db=db, address_id=address_id)


def create_customer_contact(
    db: Session,
    *,
    account_id: str,
    first_name: str,
    last_name: str,
    role: str,
    title: str | None,
    email: str | None,
    phone: str | None,
    is_primary: str | None,
) -> None:
    row = {
        "first_name": first_name,
        "last_name": last_name,
        "title": title or None,
        "role": role,
        "email": email or "",
        "phone": phone or "",
        "is_primary": is_primary == "true",
    }
    _create_subscriber_channels_from_rows(db, str(UUID(account_id)), [row])
    # _create_subscriber_channels_from_rows only flushes; persist the new
    # contact (delete_customer_contact already commits its own change).
    db.commit()


def delete_customer_contact(db: Session, *, contact_id: str) -> None:
    channel = db.get(SubscriberChannel, contact_id)
    if channel:
        db.delete(channel)
        db.commit()


def update_customer_profile(
    db: Session,
    *,
    subscriber_id: str,
    first_name: str,
    last_name: str,
    email: str,
    display_name: str | None = None,
    phone: str | None = None,
    nin: str | None = None,
    date_of_birth: str | None = None,
    gender: str | None = None,
    preferred_contact_method: str | None = None,
    address_line1: str | None = None,
    address_line2: str | None = None,
    city: str | None = None,
    region: str | None = None,
    postal_code: str | None = None,
    country_code: str | None = None,
    billing_notifications: bool,
    sms_updates: bool,
    push_notifications: bool = True,
    service_notifications: bool = True,
    account_notifications: bool = True,
    usage_notifications: bool = True,
    general_notifications: bool = True,
    locale: str | None = None,
) -> Subscriber | None:
    """Update a customer's profile fields."""
    subscriber = db.get(Subscriber, subscriber_id)
    if not subscriber:
        return None
    metadata = dict(subscriber.metadata_ or {})
    metadata["billing_notifications"] = bool(billing_notifications)
    metadata["sms_updates"] = bool(sms_updates)
    metadata["push_notifications"] = bool(push_notifications)
    metadata["service_notifications"] = bool(service_notifications)
    metadata["account_notifications"] = bool(account_notifications)
    metadata["usage_notifications"] = bool(usage_notifications)
    metadata["general_notifications"] = bool(general_notifications)

    new_email = email.strip()
    # A changed email address must be re-verified: reset the flag and dispatch a
    # fresh verification link so the verified state can never lag the address.
    email_changed = new_email.lower() != (subscriber.email or "").strip().lower()

    display = (display_name or "").strip()
    nin_locked = bool((subscriber.metadata_ or {}).get("nin_verified"))
    update_fields: dict[str, Any] = {
        "first_name": first_name.strip(),
        "last_name": last_name.strip(),
        "display_name": display or None,
        "email": new_email,
        "phone": phone.strip() if phone else None,
        "locale": (locale or "").strip() or None,
        "metadata_": metadata,
    }
    if not nin_locked:
        update_fields["nin"] = (nin or "").strip() or None

    # Date of birth: blank clears it; a malformed value is ignored (keep prior).
    dob = (date_of_birth or "").strip()
    if not dob:
        update_fields["date_of_birth"] = None
    else:
        try:
            update_fields["date_of_birth"] = date.fromisoformat(dob)
        except ValueError:
            pass

    # gender always carries a value from the form (defaults to "unknown");
    # contact method is optional ("" → no preference). Pydantic coerces the
    # string to the enum on the SubscriberUpdate model.
    gender_value = (gender or "").strip()
    if gender_value:
        update_fields["gender"] = gender_value
    contact_value = (preferred_contact_method or "").strip()
    update_fields["preferred_contact_method"] = contact_value or None

    # Contact address: each blank field clears it; country is stored uppercase.
    update_fields["address_line1"] = (address_line1 or "").strip() or None
    update_fields["address_line2"] = (address_line2 or "").strip() or None
    update_fields["city"] = (city or "").strip() or None
    update_fields["region"] = (region or "").strip() or None
    update_fields["postal_code"] = (postal_code or "").strip() or None
    update_fields["country_code"] = (country_code or "").strip().upper() or None

    # Set in the constructor so exclude_unset keeps it (post-init assignment
    # would be dropped by model_dump(exclude_unset=True)).
    if email_changed:
        update_fields["email_verified"] = False
    updated = subscriber_service.subscribers.update(
        db=db,
        subscriber_id=subscriber_id,
        payload=SubscriberUpdate(**update_fields),
    )

    if email_changed and updated is not None and new_email:
        try:
            from app.services import auth_flow

            auth_flow.send_email_verification(db, str(subscriber_id))
        except Exception:
            logger.warning(
                "verification email after profile email change failed for %s",
                subscriber_id,
                exc_info=True,
            )

    # Back-fill service-location coordinates from the typed address (best-effort;
    # skips when a pin already exists so it never overwrites an approved pin).
    if updated is not None:
        from app.services import customer_location_requests as location_service

        location_service.geocode_service_address(db, updated)
        db.commit()
    return updated


def approve_subscriber_name_correction(
    db: Session,
    *,
    subscriber_id: str,
    first_name: str,
    last_name: str,
    display_name: str | None = None,
    expected_current_fingerprint: str,
    actor_id: str | None = None,
    reason: str | None = None,
    manifest_digest: str | None = None,
    commit: bool = True,
) -> Subscriber:
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber not found")
    if subscriber.party_id is not None:
        raise HTTPException(
            status_code=409,
            detail="Party-bound subscriber rows are not eligible for legacy name correction.",
        )

    current_fingerprint = customer_name_fingerprint(
        first_name=subscriber.first_name,
        last_name=subscriber.last_name,
        display_name=subscriber.display_name,
        party_id=subscriber.party_id,
    )
    if current_fingerprint != expected_current_fingerprint:
        raise HTTPException(
            status_code=409,
            detail="Subscriber name fingerprint mismatch; reload the row and retry.",
        )

    replacement_display = collapse_whitespace(display_name)
    replacement_first = collapse_whitespace(first_name) or ""
    replacement_last = collapse_whitespace(last_name) or ""
    if (
        not replacement_first
        or not replacement_last
        or is_placeholder_name(replacement_first)
        or is_placeholder_name(replacement_last)
        or (
            replacement_display is not None and is_placeholder_name(replacement_display)
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="Customer name is missing or invalid.",
        )

    try:
        payload = SubscriberUpdate(
            first_name=replacement_first,
            last_name=replacement_last,
            display_name=replacement_display,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="Customer name is missing or invalid.",
        ) from exc
    subscriber.first_name = payload.first_name or subscriber.first_name
    subscriber.last_name = payload.last_name or subscriber.last_name
    subscriber.display_name = payload.display_name
    audit_payload = AuditEventCreate(
        actor_type=AuditActorType.user if actor_id else AuditActorType.service,
        actor_id=actor_id or "customer.profile_commands",
        action="subscriber_name_correction_applied",
        entity_type="subscriber",
        entity_id=str(subscriber.id),
        status_code=200,
        is_success=True,
        metadata_={
            "source": "customer.profile_commands",
            "expected_current_fingerprint": expected_current_fingerprint,
            "manifest_digest": manifest_digest,
            "current_name": {
                "first_name": subscriber.first_name,
                "last_name": subscriber.last_name,
                "display_name": subscriber.display_name,
            },
            "replacement_name": {
                "first_name": replacement_first,
                "last_name": replacement_last,
                "display_name": replacement_display,
            },
            "reason": reason,
        },
    )
    if commit:
        audit_service.audit_events.create(db=db, payload=audit_payload)
    else:
        audit_service.audit_events.stage(db=db, payload=audit_payload)
    if commit:
        db.commit()
        db.refresh(subscriber)
    return subscriber
