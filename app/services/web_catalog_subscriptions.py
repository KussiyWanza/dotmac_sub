"""Service helpers for admin catalog subscription web routes."""

from __future__ import annotations

import ipaddress
import json
import logging
from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload
from starlette.datastructures import FormData

from app.models.audit import AuditActorType
from app.models.billing import InvoiceStatus, TaxRate
from app.models.catalog import (
    AccessCredential,
    AddOn,
    AddOnPrice,
    AddOnType,
    BillingCycle,
    BillingMode,
    ContractTerm,
    NasDevice,
    OfferPrice,
    OfferStatus,
    PriceType,
    RadiusProfile,
    Subscription,
    SubscriptionAddOn,
    SubscriptionStatus,
    billing_cycle_suffix,
)
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.enforcement_lock import EnforcementLock, EnforcementReason
from app.models.event_store import EventStore
from app.models.network import (
    IPAssignment,
    IpBlock,
    IpPool,
    IPv4Address,
    IPVersion,
    SubscriberAdditionalRoute,
)
from app.models.notification import Notification, NotificationTemplate
from app.models.radius import (
    RadiusClient,
    RadiusServer,
    RadiusSyncJob,
    RadiusSyncRun,
    RadiusUser,
)
from app.models.radius_error import RadiusAuthError
from app.models.subscriber import Address, ChannelType, Subscriber
from app.schemas.catalog import (
    SubscriptionCreate,
    SubscriptionTechnicalUpdate,
    SubscriptionUpdate,
)
from app.schemas.network import IPAssignmentCreate, IPAssignmentUpdate
from app.schemas.subscriber import SubscriberAccountCreate
from app.services import auth_flow as auth_flow_service
from app.services import catalog as catalog_service
from app.services import email as email_service
from app.services import network as network_service
from app.services import radius as radius_service
from app.services import radius_reject as radius_reject_service
from app.services import settings_spec
from app.services import sms as sms_service
from app.services import subscriber as subscriber_service
from app.services.audit_adapter import record_audit_event
from app.services.audit_helpers import (
    build_changes_metadata,
)
from app.services.billing_adapter import (
    InvoiceIntent,
    InvoiceLineIntent,
    billing_adapter,
)
from app.services.billing_settings import resolve_payment_due_days
from app.services.credential_crypto import decrypt_credential
from app.services.ip_assignment_lifecycle import (
    IPv4ServedProjectionDecision,
    RepairServiceIPv4AssignmentCommand,
    RepairServiceIPv4ProjectionCommand,
    preview_service_ipv4_assignment_repair,
    preview_service_ipv4_projection_repair,
    repair_service_ipv4_assignment,
    repair_service_ipv4_projection,
)
from app.services.network.radius_sessions import (
    latest_open_accounting_session_for_subscription,
)
from app.services.owner_commands import CommandContext
from app.timezone import APP_TIMEZONE_NAME, format_in_app_timezone

logger = logging.getLogger(__name__)

PUBLIC_IP_ADDON_PREFIXES = (24, 29, 30, 32)
MAX_ROUTE_RANGE_OPTIONS_PER_BLOCK = 512
PUBLIC_IP_PRIORITY_PREFIXES = ("160.", "102.")
POOL_IPV4_SELECTOR_PREFIX = "pool:"
UNSPECIFIED_IPV4 = ipaddress.IPv4Address(0)


def _format_offer_price_summary(
    amount: object | None, cycle: BillingCycle | None = None
) -> str:
    value = _coerce_setting_decimal(amount)
    if value is None:
        return ""
    return f"₦{value:,.0f}{billing_cycle_suffix(cycle)}"


def _offer_option(offer: object) -> dict[str, str]:
    offer_id = str(getattr(offer, "id", "") or "")
    name = str(getattr(offer, "name", "") or "")
    prices = getattr(offer, "prices", None) or []
    amount = getattr(prices[0], "amount", None) if prices else None
    cycle = getattr(prices[0], "billing_cycle", None) if prices else None
    price_summary = _format_offer_price_summary(amount, cycle)
    label = name
    if price_summary:
        label = f"{name} - {price_summary}"
    return {
        "id": offer_id,
        "name": name,
        "price_summary": price_summary,
        "label": label,
    }


def _offer_options(
    db: Session, offers: list[object], *, include_prices: bool = True
) -> list[dict[str, str]]:
    offer_ids = [
        getattr(offer, "id", None) for offer in offers if getattr(offer, "id", None)
    ]
    first_price_by_offer_id: dict[str, tuple[object, BillingCycle | None]] = {}
    if include_prices and offer_ids:
        price_rows = (
            db.query(OfferPrice.offer_id, OfferPrice.amount, OfferPrice.billing_cycle)
            .filter(OfferPrice.offer_id.in_(offer_ids))
            .filter(OfferPrice.is_active.is_(True))
            .filter(OfferPrice.price_type == PriceType.recurring)
            .order_by(OfferPrice.created_at.asc())
            .all()
        )
        for offer_id, amount, cycle in price_rows:
            first_price_by_offer_id.setdefault(str(offer_id), (amount, cycle))

    options: list[dict[str, str]] = []
    for offer in offers:
        offer_id = str(getattr(offer, "id", "") or "")
        name = str(getattr(offer, "name", "") or "")
        amount, cycle = first_price_by_offer_id.get(offer_id, (None, None))
        price_summary = _format_offer_price_summary(amount, cycle)
        label = f"{name} - {price_summary}" if price_summary else name
        options.append(
            {
                "id": offer_id,
                "name": name,
                "price_summary": price_summary,
                "label": label,
                "price": str(amount) if amount is not None else "",
            }
        )
    return options


def active_offer_options(db: Session) -> list[dict[str, str]]:
    """Return active offers shaped for plan-change dropdowns."""
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status=OfferStatus.active.value,
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=500,
        offset=0,
    )
    return _offer_options(db, list(offers))


def _coerce_setting_int(value: object | None) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _coerce_setting_decimal(value: object | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None


def _format_commercial_value(key: str, value: object | None) -> str:
    if value is None:
        return "Not set"
    if key == "billing_enabled":
        return "Approved" if bool(value) else "Administratively disabled"
    if key == "billing_day":
        return f"Day {value}"
    if key in {"payment_due_days", "grace_period_days"}:
        return f"{value} day(s)"
    if key == "min_balance":
        try:
            return f"NGN {Decimal(str(value)):,.2f}"
        except Exception:
            return str(value)
    return str(value)


def _enum_raw_value(value: object | None) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip()


def _billing_global_defaults(db: Session) -> dict[str, object | None]:
    keys = {"billing_day", "minimum_balance"}
    rows = (
        db.query(DomainSetting)
        .filter(DomainSetting.domain == SettingDomain.billing)
        .filter(DomainSetting.key.in_(keys))
        .all()
    )
    raw = {
        row.key: row.value_json if row.value_json is not None else row.value_text
        for row in rows
    }
    return {
        "billing_day": _coerce_setting_int(raw.get("billing_day")),
        "payment_due_days": resolve_payment_due_days(db),
        "min_balance": _coerce_setting_decimal(raw.get("minimum_balance")),
    }


def _tax_rate_label(rate: TaxRate | None) -> str:
    if not rate:
        return "Not set"
    percentage = (Decimal(rate.rate) * Decimal("100")).quantize(Decimal("0.01"))
    return f"{rate.name} ({percentage}%)"


def _subscription_commercial_policy(
    db: Session, subscription: Subscription
) -> dict[str, object]:
    subscriber = subscription.subscriber or db.get(
        Subscriber, subscription.subscriber_id
    )
    global_defaults = _billing_global_defaults(db)

    rows = [
        {
            "key": "billing_mode",
            "label": "Billing Mode",
            "effective": _format_commercial_value(
                "billing_mode",
                subscription.billing_mode.value.replace("_", " ").title()
                if subscription.billing_mode
                else None,
            ),
            "source": "Subscription",
            "global": "Not set",
            "override": _format_commercial_value(
                "billing_mode",
                subscription.billing_mode.value.replace("_", " ").title()
                if subscription.billing_mode
                else None,
            ),
        },
        {
            "key": "contract_term",
            "label": "Contract Term",
            "effective": _format_commercial_value(
                "contract_term",
                subscription.contract_term.value.replace("_", " ").title()
                if subscription.contract_term
                else None,
            ),
            "source": "Subscription",
            "global": "Not set",
            "override": _format_commercial_value(
                "contract_term",
                subscription.contract_term.value.replace("_", " ").title()
                if subscription.contract_term
                else None,
            ),
        },
    ]

    subscriber_fields = [
        ("payment_method", "Payment Method", False),
        ("billing_enabled", "Billing and Service Approval", False),
        ("billing_day", "Billing Day", True),
        ("payment_due_days", "Payment Due", True),
        ("grace_period_days", "Grace Period", False),
        ("min_balance", "Minimum Balance", True),
    ]
    for key, label, uses_global in subscriber_fields:
        raw = getattr(subscriber, key, None) if subscriber else None
        effective = (
            raw
            if raw is not None
            else (global_defaults.get(key) if uses_global else None)
        )
        source = (
            "Customer override"
            if raw is not None
            else ("Global default" if uses_global else "Not set")
        )
        rows.append(
            {
                "key": key,
                "label": label,
                "effective": _format_commercial_value(key, effective),
                "source": source,
                "global": _format_commercial_value(key, global_defaults.get(key))
                if uses_global
                else "Not set",
                "override": _format_commercial_value(key, raw),
            }
        )

    tax_source = "Not set"
    tax_global = "Not set"
    tax_override = "Not set"
    tax_effective = "Not set"
    tax_rate_id = None
    if subscription.service_address_id:
        address = db.get(Address, subscription.service_address_id)
        if address and address.tax_rate_id:
            tax_rate_id = address.tax_rate_id
            tax_source = "Service address"
    if tax_rate_id is None and subscriber and subscriber.tax_rate_id:
        tax_rate_id = subscriber.tax_rate_id
        tax_source = "Customer override"
        tax_override = _tax_rate_label(db.get(TaxRate, subscriber.tax_rate_id))

    effective_tax = db.get(TaxRate, tax_rate_id) if tax_rate_id else None
    if effective_tax:
        tax_effective = _tax_rate_label(effective_tax)
    if tax_source == "Service address" and subscriber and subscriber.tax_rate_id:
        tax_override = _tax_rate_label(db.get(TaxRate, subscriber.tax_rate_id))

    rows.append(
        {
            "key": "tax_rate",
            "label": "Tax Rate",
            "effective": tax_effective,
            "source": tax_source,
            "global": tax_global,
            "override": tax_override,
        }
    )

    return {
        "rows": rows,
        "effective_tax_source": tax_source,
    }


def _subscription_policy_subject(
    db: Session,
    subscription_data: dict[str, object],
    *,
    default_billing_mode: str,
) -> Subscription | None:
    subscription_id = str(subscription_data.get("id") or "").strip()
    if subscription_id:
        try:
            existing = db.get(Subscription, UUID(subscription_id))
        except (TypeError, ValueError):
            existing = None
        if existing:
            return existing

    subscriber_id = str(subscription_data.get("subscriber_id") or "").strip()
    if not subscriber_id:
        return None

    billing_mode_value = (
        _enum_raw_value(subscription_data.get("billing_mode")) or default_billing_mode
    )
    contract_term_value = (
        _enum_raw_value(subscription_data.get("contract_term"))
        or ContractTerm.month_to_month.value
    )
    service_address_id = str(subscription_data.get("service_address_id") or "").strip()
    return Subscription(
        subscriber_id=UUID(subscriber_id),
        billing_mode=BillingMode(billing_mode_value),
        contract_term=ContractTerm(contract_term_value),
        service_address_id=UUID(service_address_id) if service_address_id else None,
    )


def _form_str(form: FormData, key: str, default: str = "") -> str:
    value = form.get(key, default)
    return value if isinstance(value, str) else default


def _normalize_discount_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw == "percent":
        return "percentage"
    return raw


def default_subscription_form(account_id: str, subscriber_id: str) -> dict[str, object]:
    """Return default values for subscription create form."""
    return {
        "account_id": account_id,
        "subscriber_id": subscriber_id,
        "offer_id": "",
        "status": SubscriptionStatus.pending.value,
        "requested_status": SubscriptionStatus.pending.value,
        "billing_mode": "",
        "contract_term": ContractTerm.month_to_month.value,
        "start_at": "",
        "end_at": "",
        "next_billing_at": "",
        "canceled_at": "",
        "cancel_reason": "",
        "router_id": "",
        "service_description": "",
        "quantity": "",
        "unit": "",
        "unit_price": "",
        "discount": False,
        "discount_value": "",
        "discount_type": "",
        "service_status_raw": "",
        "login": "",
        "ipv4_address": "",
        "ipv6_address": "",
        "mac_address": "",
        "provisioning_nas_device_id": "",
        "radius_profile_id": "",
        "service_password": "",  # nosec
        "ipv4_method": "permanent_static",
        "ipv4_block_ids": [],
        "ipv4_addresses": [],
        "ip_addon_id": "",
        "ip_addon_quantity": "1",
        "additional_route_cidrs": [""],
        "additional_route_metrics": [""],
    }


def parse_subscription_form(
    form: FormData, *, subscription_id: str | None = None
) -> dict[str, object]:
    """Parse subscription form payload from request form."""
    ipv4_block_ids = [
        str(value).strip()
        for value in form.getlist("ipv4_block_ids")
        if str(value).strip()
    ][:1]
    ipv4_addresses = [
        str(value).strip()
        for value in form.getlist("ipv4_addresses")
        if str(value).strip()
    ][:1]
    additional_route_cidrs = [
        str(value).strip() for value in form.getlist("additional_route_cidrs")
    ]
    additional_route_metrics = [
        str(value).strip() for value in form.getlist("additional_route_metrics")
    ]
    data = {
        "account_id": _form_str(form, "account_id").strip(),
        "subscriber_id": _form_str(form, "subscriber_id").strip(),
        "offer_id": _form_str(form, "offer_id").strip(),
        "status": _form_str(form, "status").strip(),
        "billing_mode": _form_str(form, "billing_mode").strip(),
        "contract_term": _form_str(form, "contract_term").strip(),
        "start_at": _form_str(form, "start_at").strip(),
        "end_at": _form_str(form, "end_at").strip(),
        "next_billing_at": _form_str(form, "next_billing_at").strip(),
        "canceled_at": _form_str(form, "canceled_at").strip(),
        "cancel_reason": _form_str(form, "cancel_reason").strip(),
        "router_id": _form_str(form, "router_id").strip(),
        "service_description": _form_str(form, "service_description").strip(),
        "quantity": _form_str(form, "quantity").strip(),
        "unit": _form_str(form, "unit").strip(),
        "unit_price": _form_str(form, "unit_price").strip(),
        "discount": form.get("discount") == "true",
        "discount_value": _form_str(form, "discount_value").strip(),
        "discount_type": _normalize_discount_type(
            _form_str(form, "discount_type").strip()
        ),
        "service_status_raw": _form_str(form, "service_status_raw").strip(),
        "login": _form_str(form, "login").strip(),
        "ipv4_address": _form_str(form, "ipv4_address").strip(),
        "ipv6_address": _form_str(form, "ipv6_address").strip(),
        "mac_address": _form_str(form, "mac_address").strip(),
        "provisioning_nas_device_id": _form_str(
            form, "provisioning_nas_device_id"
        ).strip(),
        "radius_profile_id": _form_str(form, "radius_profile_id").strip(),
        "service_password": _form_str(form, "service_password").strip(),
        "ipv4_method": _form_str(form, "ipv4_method", "permanent_static")
        .strip()
        .lower()
        or "permanent_static",
        "ipv4_block_ids": ipv4_block_ids,
        "ipv4_addresses": ipv4_addresses,
        "ip_addon_id": _form_str(form, "ip_addon_id").strip(),
        "ip_addon_quantity": _form_str(form, "ip_addon_quantity", "1").strip() or "1",
        "additional_route_cidrs": additional_route_cidrs,
        "additional_route_metrics": additional_route_metrics,
    }
    if subscription_id:
        data["id"] = subscription_id
    else:
        requested_status = str(data["status"] or SubscriptionStatus.pending.value)
        if form.get("activate_immediately") == "1":
            # Backward compatibility for submissions from the previous form.
            requested_status = SubscriptionStatus.active.value
        # Creation establishes technical and commercial intent only. Lifecycle
        # facts are owned by subscription_lifecycle_commands after the record
        # and its provisioning inputs have been staged.
        data.update(
            {
                "status": SubscriptionStatus.pending.value,
                "requested_status": requested_status,
                "start_at": "",
                "end_at": "",
                "next_billing_at": "",
                "canceled_at": "",
                "cancel_reason": "",
            }
        )
    return data


def resolve_account_id(db: Session, subscription: dict[str, object]) -> str | None:
    """Resolve account from subscriber id when account is omitted."""
    account_id = str(subscription.get("account_id") or "")
    subscriber_id = str(subscription.get("subscriber_id") or "")
    if account_id:
        return None
    if not subscriber_id:
        return "Account or subscriber is required."
    try:
        subscriber_uuid = UUID(subscriber_id)
    except ValueError:
        return "Subscriber is invalid."

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=str(subscriber_uuid),
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=1,
        offset=0,
    )
    if accounts:
        subscription["account_id"] = str(accounts[0].id)
        return None
    try:
        account = subscriber_service.accounts.create(
            db=db,
            payload=SubscriberAccountCreate(subscriber_id=subscriber_uuid),
        )
    except Exception as exc:
        return exc.detail if hasattr(exc, "detail") else str(exc)
    subscription["account_id"] = str(account.id)
    return None


def validate_subscription_form(
    subscription: dict[str, object], *, for_create: bool
) -> str | None:
    """Validate required subscription form fields."""
    if for_create:
        if not subscription.get("account_id") and not subscription.get("subscriber_id"):
            return "Account or subscriber is required."
    else:
        if not subscription.get("account_id"):
            return "Account is required."
    if not subscription.get("offer_id"):
        return "Offer is required."
    if for_create and str(subscription.get("requested_status") or "") not in {
        SubscriptionStatus.pending.value,
        SubscriptionStatus.active.value,
        SubscriptionStatus.suspended.value,
        SubscriptionStatus.disabled.value,
        SubscriptionStatus.canceled.value,
    }:
        return "Select a valid lifecycle state."
    return None


def _valid_assigned_ipv4(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return None
    if parsed.version != 4 or parsed == UNSPECIFIED_IPV4:
        return None
    return str(parsed)


def build_payload_data(subscription: dict[str, object]) -> dict[str, object]:
    """Build Subscription create/update payload dict."""
    ipv4_method = str(subscription.get("ipv4_method") or "").strip().lower()
    if ipv4_method in {"permanent_static", "dynamic"}:
        subscription["service_status_raw"] = ipv4_method
    payload_data = {
        "account_id": subscription["account_id"],
        "offer_id": subscription["offer_id"],
        "discount": subscription["discount"],
    }
    optional_fields = [
        "status",
        "billing_mode",
        "contract_term",
        "start_at",
        "end_at",
        "next_billing_at",
        "canceled_at",
        "cancel_reason",
        "router_id",
        "service_description",
        "quantity",
        "unit",
        "unit_price",
        "discount_value",
        "discount_type",
        "service_status_raw",
        "login",
        "ipv4_address",
        "ipv6_address",
        "mac_address",
        "provisioning_nas_device_id",
        "radius_profile_id",
    ]
    for field in optional_fields:
        value = subscription.get(field)
        if field == "ipv4_address":
            value = _valid_assigned_ipv4(value)
        if value:
            payload_data[field] = value
    return payload_data


def _subscriber_seq_from_number(subscriber_number: str | None) -> int:
    text = str(subscriber_number or "").strip()
    if "-" in text:
        text = text.rsplit("-", 1)[-1]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return 0
    return int(digits)


def _generated_service_login(subscriber: Subscriber) -> str:
    seq = _subscriber_seq_from_number(subscriber.subscriber_number)
    return f"1{seq:07d}"


def _generated_service_password(subscriber: Subscriber) -> str:
    if subscriber.subscriber_number:
        return str(subscriber.subscriber_number)
    return str(subscriber.id)


def _pppoe_auto_generate_enabled(db: Session) -> bool:  # noqa: ARG001
    """PPPoE auto-generation is always enabled — credentials are mandatory."""
    return True


def _pool_allows_network_broadcast(pool: IpPool | None) -> bool:
    notes = str(getattr(pool, "notes", "") or "")
    for raw_line in notes.splitlines():
        line = raw_line.strip().lower()
        if line == "[allow_network_broadcast:true]":
            return True
    return False


def _iter_block_ipv4_hosts(block: IpBlock) -> list[str]:
    try:
        network = ipaddress.ip_network(str(block.cidr), strict=False)
    except ValueError:
        return []
    if network.version != 4:
        return []
    if (
        _pool_allows_network_broadcast(getattr(block, "pool", None))
        or network.prefixlen >= 31
    ):
        return [str(ip) for ip in network]
    return [str(ip) for ip in network.hosts()]


def _available_ipv4_strings_for_block(db: Session, *, block: IpBlock) -> list[str]:
    address_rows = (
        db.query(IPv4Address, IPAssignment)
        .outerjoin(
            IPAssignment,
            and_(
                IPAssignment.ipv4_address_id == IPv4Address.id,
                IPAssignment.is_active.is_(True),
            ),
        )
        .filter(IPv4Address.pool_id == block.pool_id)
        .all()
    )
    address_state: dict[str, tuple[IPv4Address, IPAssignment | None]] = {
        str(address.address): (address, assignment)
        for address, assignment in address_rows
    }
    available: list[str] = []
    for ip_text in _iter_block_ipv4_hosts(block):
        row = address_state.get(ip_text)
        if not row:
            available.append(ip_text)
            continue
        address, assignment = row
        if assignment is None and not bool(address.is_reserved):
            available.append(ip_text)
    return available


def _ipv4_address_state_by_pool(
    db: Session,
) -> dict[str, dict[str, tuple[bool, bool]]]:
    address_rows = (
        db.query(
            IPv4Address.pool_id,
            IPv4Address.address,
            IPv4Address.is_reserved,
            IPAssignment.id,
        )
        .outerjoin(
            IPAssignment,
            and_(
                IPAssignment.ipv4_address_id == IPv4Address.id,
                IPAssignment.is_active.is_(True),
            ),
        )
        .all()
    )
    by_pool: dict[str, dict[str, tuple[bool, bool]]] = {}
    for pool_id, address, is_reserved, assignment_id in address_rows:
        pool_id = str(pool_id or "")
        if not pool_id:
            continue
        by_pool.setdefault(pool_id, {})[str(address)] = (
            bool(is_reserved),
            assignment_id is not None,
        )
    return by_pool


def _available_ipv4_strings_for_network_state(
    *,
    network: ipaddress.IPv4Network,
    pool: IpPool | None,
    address_state: dict[str, tuple[bool, bool]],
) -> list[str]:
    available: list[str] = []
    for ip_text in _iter_service_ipv4_hosts(network, pool=pool):
        row = address_state.get(ip_text)
        if not row:
            available.append(ip_text)
            continue
        is_reserved, is_assigned = row
        if not is_assigned and not is_reserved:
            available.append(ip_text)
    return available


def _iter_service_ipv4_hosts(
    network: ipaddress.IPv4Network,
    *,
    pool: IpPool | None,
) -> list[str]:
    if _pool_allows_network_broadcast(pool) or network.prefixlen >= 31:
        return [str(ip) for ip in network]
    return [str(ip) for ip in network.hosts()]


def _available_ipv4_strings_for_network(
    db: Session,
    *,
    network: ipaddress.IPv4Network,
    pool_id: object,
    pool: IpPool | None,
    address_state: dict[str, tuple[IPv4Address, IPAssignment | None]] | None = None,
) -> list[str]:
    host_ips = _iter_service_ipv4_hosts(network, pool=pool)
    pool_key = str(pool_id)
    if address_state is None:
        address_state = {}
        for offset in range(0, len(host_ips), 1000):
            chunk = host_ips[offset : offset + 1000]
            address_rows = (
                db.query(IPv4Address, IPAssignment)
                .outerjoin(
                    IPAssignment,
                    and_(
                        IPAssignment.ipv4_address_id == IPv4Address.id,
                        IPAssignment.is_active.is_(True),
                    ),
                )
                .filter(IPv4Address.pool_id == pool_id)
                .filter(IPv4Address.address.in_(chunk))
                .all()
            )
            address_state.update(
                {
                    str(address.address): (address, assignment)
                    for address, assignment in address_rows
                }
            )
    available: list[str] = []
    for ip_text in host_ips:
        row = address_state.get(ip_text)
        if not row:
            available.append(ip_text)
            continue
        address, assignment = row
        if str(address.pool_id or "") != pool_key:
            continue
        if assignment is None and not bool(address.is_reserved):
            available.append(ip_text)
    return available


def _pool_ipv4_selector(pool_id: object, cidr: object) -> str:
    return f"{POOL_IPV4_SELECTOR_PREFIX}{pool_id}:{cidr}"


def _service_ipv4_selector_for_range(
    *,
    pool_id: object,
    cidr: object,
    block_id: object | None = None,
) -> str:
    block_text = str(block_id or "").strip()
    if block_text:
        return block_text
    return _pool_ipv4_selector(pool_id, cidr)


def _service_ipv4_ranges_for_ipam(
    db: Session,
) -> list[tuple[ipaddress.IPv4Network, str, str, str]]:
    ranges: list[tuple[ipaddress.IPv4Network, str, str, str]] = []
    pools = (
        db.query(IpPool)
        .options(selectinload(IpPool.blocks))
        .filter(IpPool.ip_version == IPVersion.ipv4)
        .filter(IpPool.is_active.is_(True))
        .order_by(IpPool.name.asc())
        .all()
    )
    for pool in pools:
        active_blocks = [block for block in pool.blocks if block.is_active]
        if active_blocks:
            for block in active_blocks:
                try:
                    network = ipaddress.ip_network(str(block.cidr), strict=False)
                except ValueError:
                    continue
                if network.version == 4:
                    ranges.append(
                        (
                            cast(ipaddress.IPv4Network, network),
                            str(pool.name or "Pool"),
                            str(pool.id),
                            str(block.id),
                        )
                    )
            continue
        try:
            network = ipaddress.ip_network(str(pool.cidr), strict=False)
        except ValueError:
            continue
        if network.version == 4:
            ranges.append(
                (
                    cast(ipaddress.IPv4Network, network),
                    str(pool.name or "Pool"),
                    str(pool.id),
                    "",
                )
            )
    return sorted(ranges, key=lambda item: _network_priority_key(item[0]))


def _service_ipv4_block_options(
    db: Session, *, include_available_ips: bool = False
) -> list[dict[str, object]]:
    options: list[dict[str, object]] = []
    address_state_by_pool = _ipv4_address_state_by_pool(db)
    for network, pool_name, pool_id, block_id in _service_ipv4_ranges_for_ipam(db):
        pool = db.get(IpPool, pool_id)
        if not pool or not pool.is_active or pool.ip_version != IPVersion.ipv4:
            continue
        selector = _service_ipv4_selector_for_range(
            pool_id=pool_id,
            cidr=network,
            block_id=block_id,
        )
        option: dict[str, object] = {
            "id": selector,
            "pool_id": str(pool_id),
            "block_id": str(block_id or ""),
            "pool_name": pool_name,
            "name": pool_name,
            "cidr": str(network),
            "display": f"{pool_name} - {network}",
        }
        if include_available_ips:
            available_ips = _available_ipv4_strings_for_network_state(
                network=network,
                pool=pool,
                address_state=address_state_by_pool.get(str(pool_id), {}),
            )
            option["available_count"] = len(available_ips)
            option["available_ips"] = available_ips
            option["display"] = f"{pool_name} - {network} ({len(available_ips)} free)"
        options.append(option)
    return options


def _service_ipv4_block_option_summaries(db: Session) -> list[dict[str, object]]:
    options: list[dict[str, object]] = []
    for network, pool_name, pool_id, block_id in _service_ipv4_ranges_for_ipam(db):
        selector = _service_ipv4_selector_for_range(
            pool_id=pool_id,
            cidr=network,
            block_id=block_id,
        )
        options.append(
            {
                "id": selector,
                "pool_id": str(pool_id),
                "block_id": str(block_id or ""),
                "pool_name": pool_name,
                "name": pool_name,
                "cidr": str(network),
                "display": f"{pool_name} - {network}",
            }
        )
    return options


def available_ipv4_options_for_selector(
    db: Session,
    *,
    selector: str,
    current_ip: str | None = None,
) -> list[str]:
    selector = str(selector or "").strip()
    if not selector:
        return []
    if selector.startswith(POOL_IPV4_SELECTOR_PREFIX):
        resolved_pool, resolved_network = _resolve_pool_ipv4_selector(db, selector)
    else:
        try:
            block_uuid = UUID(selector)
        except ValueError as exc:
            raise ValueError("Invalid IPv4 block selected.") from exc
        block = db.get(IpBlock, block_uuid)
        if not block or not block.is_active:
            raise ValueError("Selected IPv4 block is not active.")
        block_pool = db.get(IpPool, block.pool_id)
        if (
            not block_pool
            or not block_pool.is_active
            or block_pool.ip_version != IPVersion.ipv4
        ):
            raise ValueError("Selected IPv4 block is not active.")
        try:
            parsed_network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError as exc:
            raise ValueError("Selected IPv4 block is invalid.") from exc
        if parsed_network.version != 4:
            raise ValueError("Selected IPv4 block is invalid.")
        resolved_pool = block_pool
        resolved_network = cast(ipaddress.IPv4Network, parsed_network)

    available = _available_ipv4_strings_for_network(
        db,
        network=resolved_network,
        pool_id=resolved_pool.id,
        pool=resolved_pool,
    )
    current = str(current_ip or "").strip()
    if current:
        try:
            current_addr = ipaddress.ip_address(current)
        except ValueError:
            current_addr = None
        if (
            current_addr
            and current_addr.version == 4
            and current_addr in resolved_network
        ):
            if current not in available:
                available.insert(0, current)
    return available


def _service_ipv4_selector_for_address(
    db: Session,
    *,
    address: IPv4Address,
) -> str:
    ip_text = str(address.address or "").strip()
    if not ip_text or not address.pool_id:
        return ""
    try:
        ip_obj = ipaddress.ip_address(ip_text)
    except ValueError:
        return ""
    pool = db.get(IpPool, address.pool_id)
    if not pool:
        return ""
    blocks = (
        db.query(IpBlock)
        .filter(IpBlock.pool_id == address.pool_id)
        .filter(IpBlock.is_active.is_(True))
        .order_by(IpBlock.created_at.asc())
        .all()
    )
    for block in blocks:
        try:
            if ip_obj in ipaddress.ip_network(str(block.cidr), strict=False):
                return str(block.id)
        except ValueError:
            continue
    try:
        pool_network = ipaddress.ip_network(str(pool.cidr), strict=False)
    except ValueError:
        return ""
    if pool_network.version != 4 or ip_obj not in pool_network:
        return ""
    return _pool_ipv4_selector(address.pool_id, pool_network)


def _subscription_ipv4_form_rows(
    db: Session,
    *,
    subscription_obj: Subscription,
) -> tuple[list[str], list[str]]:
    # Query by subscriber_id since IP assignments link to subscribers, not subscriptions
    assignments = (
        db.query(IPAssignment)
        .filter(IPAssignment.subscriber_id == subscription_obj.subscriber_id)
        .filter(IPAssignment.ip_version == IPVersion.ipv4)
        .filter(IPAssignment.is_active.is_(True))
        .order_by(IPAssignment.created_at.asc())
        .all()
    )
    if not assignments:
        return (
            [],
            [subscription_obj.ipv4_address] if subscription_obj.ipv4_address else [],
        )

    selectors: list[str] = []
    addresses: list[str] = []
    for assignment in assignments:
        address = getattr(assignment, "ipv4_address", None)
        if not isinstance(address, IPv4Address):
            continue
        ip_text = str(getattr(address, "address", "") or "").strip()
        if not ip_text:
            continue
        addresses.append(ip_text)
        selectors.append(_service_ipv4_selector_for_address(db, address=address))
    return selectors, addresses


def _validate_unique_selected_ipv4s(selected_ips: list[str] | None) -> None:
    seen: set[str] = set()
    for raw_ip in selected_ips or []:
        ip_text = str(raw_ip or "").strip()
        if not ip_text:
            continue
        try:
            parsed = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError(f"IPv4 address {ip_text} is invalid.") from exc
        if parsed.version != 4 or parsed == UNSPECIFIED_IPV4:
            raise ValueError(f"IPv4 address {ip_text} is not assignable.")
        if ip_text in seen:
            raise ValueError(f"IPv4 address {ip_text} was selected more than once.")
        seen.add(ip_text)


def _nas_device_label(device: NasDevice | None) -> str:
    if not device:
        return ""
    label = str(device.name or "")
    if device.management_ip:
        return f"{label} ({device.management_ip})"
    if device.ip_address:
        return f"{label} ({device.ip_address})"
    if device.nas_ip:
        return f"{label} ({device.nas_ip})"
    return label


def apply_generated_service_credentials(
    db: Session, subscription: dict[str, object]
) -> None:
    subscriber_id = str(
        subscription.get("subscriber_id") or subscription.get("account_id") or ""
    )
    if not subscriber_id:
        return
    try:
        subscriber = subscriber_service.subscribers.get(
            db=db, subscriber_id=subscriber_id
        )
    except Exception:
        logger.warning(
            "Subscriber lookup failed during credential generation for %s",
            subscriber_id,
            exc_info=True,
        )
        return
    if not str(subscription.get("login") or "").strip():
        if subscription.get("id"):
            subscription["login"] = _generated_service_login(subscriber)
        elif not _pppoe_auto_generate_enabled(db):
            subscription["login"] = _generated_service_login(subscriber)
    if _pppoe_auto_generate_enabled(db):
        return
    if (
        not subscription.get("id")
        and not str(subscription.get("service_password") or "").strip()
    ):
        subscription["service_password"] = _generated_service_password(subscriber)


def _upsert_access_credential(
    db: Session,
    *,
    subscriber_id: UUID,
    username: str,
    plain_password: str | None = None,
    radius_profile_id: str | None = None,
) -> None:
    normalized_username = str(username or "").strip()
    same_username_query = db.query(AccessCredential).filter(
        AccessCredential.subscriber_id == subscriber_id,
        AccessCredential.username == normalized_username,
    )
    credential = (
        same_username_query.filter(AccessCredential.is_active.is_(True))
        .order_by(AccessCredential.created_at.desc())
        .first()
    )
    if credential is None:
        credential = same_username_query.order_by(
            AccessCredential.created_at.desc()
        ).first()
    if credential is None:
        credential = (
            db.query(AccessCredential)
            .filter(AccessCredential.subscriber_id == subscriber_id)
            .filter(AccessCredential.is_active.is_(True))
            .order_by(AccessCredential.created_at.desc())
            .first()
        )
    conflicting_username = db.query(AccessCredential).filter(
        AccessCredential.username == normalized_username
    )
    if credential is not None:
        conflicting_username = conflicting_username.filter(
            AccessCredential.id != credential.id
        )
    if normalized_username and conflicting_username.first() is not None:
        raise ValueError(
            f"Service login {normalized_username} is already used by another "
            "customer or credential."
        )
    secret_hash = (
        auth_flow_service.hash_service_secret(plain_password)
        if plain_password
        else None
    )
    radius_profile_uuid: UUID | None = None
    if radius_profile_id:
        try:
            radius_profile_uuid = UUID(str(radius_profile_id))
        except ValueError:
            radius_profile_uuid = None
    if credential:
        credential.username = normalized_username
        if secret_hash:
            credential.secret_hash = secret_hash
        credential.is_active = True
        if radius_profile_uuid:
            credential.radius_profile_id = radius_profile_uuid
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError(
                f"Service login {normalized_username} is already used by another "
                "customer or credential."
            ) from exc
        return
    if not secret_hash:
        return
    db.add(
        AccessCredential(
            subscriber_id=subscriber_id,
            username=normalized_username,
            secret_hash=secret_hash,
            is_active=True,
            radius_profile_id=radius_profile_uuid,
        )
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(
            f"Service login {normalized_username} is already used by another "
            "customer or credential."
        ) from exc


def _current_access_credential(
    db: Session, subscriber_id: str | UUID | None
) -> AccessCredential | None:
    if not subscriber_id:
        return None
    try:
        subscriber_uuid = UUID(str(subscriber_id))
    except ValueError:
        return None
    return (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber_uuid)
        .order_by(AccessCredential.created_at.desc())
        .first()
    )


def _current_service_password(
    db: Session, subscriber_id: str | UUID | None
) -> str | None:
    credential = _current_access_credential(db, subscriber_id)
    if not credential or not credential.secret_hash:
        return None
    try:
        return decrypt_credential(credential.secret_hash)
    except Exception:
        logger.warning(
            "Failed to decrypt service credential for subscriber %s",
            subscriber_id,
            exc_info=True,
        )
        return None


def _credential_contact_targets(subscriber: Subscriber) -> dict[str, list[str]]:
    emails: list[str] = []
    phones: list[str] = []

    def _push_unique(targets: list[str], value: str | None) -> None:
        text = str(value or "").strip()
        if text and text not in targets:
            targets.append(text)

    _push_unique(emails, subscriber.email)
    _push_unique(phones, subscriber.phone)
    for channel in getattr(subscriber, "channels", []) or []:
        channel_type = getattr(channel, "channel_type", None)
        if channel_type == ChannelType.email:
            _push_unique(emails, getattr(channel, "address", None))
        elif channel_type in {ChannelType.phone, ChannelType.sms}:
            _push_unique(phones, getattr(channel, "address", None))

    return {"email": emails, "sms": phones}


def send_subscription_credentials(
    db: Session,
    *,
    subscription_id: str,
) -> dict[str, object]:
    subscription = catalog_service.subscriptions.get(
        db=db, subscription_id=subscription_id
    )
    subscriber = db.get(Subscriber, subscription.subscriber_id)
    if not subscriber:
        raise ValueError("Subscriber not found for this subscription.")

    credential = _current_access_credential(db, subscription.subscriber_id)
    if not credential or not credential.username:
        raise ValueError("No service credential is stored for this subscriber.")

    password = _current_service_password(db, subscription.subscriber_id)
    if not password:
        raise ValueError("Current service password is not available for delivery.")

    targets = _credential_contact_targets(subscriber)
    if not targets["email"] and not targets["sms"]:
        raise ValueError("Subscriber has no email or SMS contact targets.")

    subject = "Your Internet service credentials"
    body_text = (
        f"Hello {subscriber.full_name},\n\n"
        f"Your service login is: {credential.username}\n"
        f"Your service password is: {password}\n\n"
        "Please keep these details secure."
    )
    from html import escape

    from app.services.brand_profiles import resolve_brand
    from app.services.email_template import wrap_email_html

    body_html = wrap_email_html(
        (
            f"<p>Hello {escape(subscriber.full_name or '')},</p>"
            f"<p>Your service login is: <strong>{escape(credential.username or '')}</strong><br>"
            f"Your service password is: <strong>{escape(password)}</strong></p>"
            "<p>Please keep these details secure.</p>"
        ),
        subject=subject,
        brand=resolve_brand(db, subscriber_id=subscriber.id).to_dict(),
    )

    email_sent = 0
    sms_sent = 0
    for email in targets["email"]:
        email_service.send_email(
            db=db,
            to_email=email,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            activity="subscription_welcome",
        )
        email_sent += 1

    sms_body = (
        f"Service login: {credential.username} | Password: {password}. Keep it secure."
    )
    for phone in targets["sms"]:
        if sms_service.send_sms(db, phone, sms_body, track=True):
            sms_sent += 1

    return {
        "email_sent": email_sent,
        "sms_sent": sms_sent,
        "email_targets": targets["email"],
        "sms_targets": targets["sms"],
    }


def _reconcile_active_subscription_after_credential_sync(
    db: Session, subscription_id: str | None
) -> None:
    if not subscription_id:
        return
    try:
        subscription = catalog_service.subscriptions.get(
            db=db, subscription_id=str(subscription_id)
        )
    except Exception:
        logger.warning(
            "Subscription lookup failed during RADIUS reconcile for %s",
            subscription_id,
            exc_info=True,
        )
        return
    if subscription.status != SubscriptionStatus.active:
        return
    try:
        from app.services.radius import reconcile_subscription_connectivity

        reconcile_subscription_connectivity(
            db, str(subscription.id)
        ).require_projected()
    except Exception:
        logger.warning(
            "RADIUS reconcile failed during subscription credential sync for %s",
            subscription.id,
            exc_info=True,
        )


def _refresh_subscription_radius_session(
    db: Session,
    subscription_id: str | None,
    *,
    reason: str,
) -> None:
    """Push updated RADIUS replies and force a fresh Access-Accept."""
    if not subscription_id:
        return
    _reconcile_active_subscription_after_credential_sync(db, subscription_id)
    try:
        subscription = catalog_service.subscriptions.get(
            db=db, subscription_id=str(subscription_id)
        )
    except Exception:
        logger.warning(
            "Subscription lookup failed during session refresh for %s",
            subscription_id,
            exc_info=True,
        )
        return
    if subscription.status != SubscriptionStatus.active:
        return
    try:
        from app.services.enforcement import disconnect_subscription_sessions

        disconnect_subscription_sessions(db, str(subscription.id), reason=reason)
    except Exception:
        logger.warning(
            "Session disconnect failed during subscription refresh for %s",
            subscription.id,
            exc_info=True,
        )


def _resolve_ipv4_for_block(
    db: Session,
    *,
    block: IpBlock,
    requested_ip: str | None = None,
) -> IPv4Address | None:
    try:
        network = ipaddress.ip_network(str(block.cidr), strict=False)
    except ValueError:
        return None
    if network.version != 4:
        return None
    pool = db.get(IpPool, block.pool_id)
    available_ips = _available_ipv4_strings_for_network(
        db,
        network=cast(ipaddress.IPv4Network, network),
        pool_id=block.pool_id,
        pool=pool,
    )
    if not available_ips:
        return None
    selected_ip = str(requested_ip or "").strip() or available_ips[0]
    if selected_ip not in available_ips:
        raise ValueError(
            f"Selected IPv4 address {selected_ip} is not available in block {block.cidr}."
        )
    address = db.query(IPv4Address).filter(IPv4Address.address == selected_ip).first()
    if address:
        return address
    address = IPv4Address(address=selected_ip, pool_id=block.pool_id, is_reserved=False)
    db.add(address)
    db.commit()
    db.refresh(address)
    return address


def _resolve_pool_ipv4_selector(
    db: Session,
    selector: str,
) -> tuple[IpPool, ipaddress.IPv4Network]:
    payload = selector[len(POOL_IPV4_SELECTOR_PREFIX) :]
    pool_id_text, separator, cidr_text = payload.partition(":")
    if not separator or not pool_id_text or not cidr_text:
        raise ValueError("Invalid IPv4 block selected.")
    try:
        pool_uuid = UUID(pool_id_text)
    except ValueError as exc:
        raise ValueError("Invalid IPv4 block selected.") from exc
    pool = db.get(IpPool, pool_uuid)
    if not pool or not pool.is_active or pool.ip_version != IPVersion.ipv4:
        raise ValueError("Selected IPv4 block is not active.")
    try:
        selected_network = ipaddress.ip_network(cidr_text, strict=False)
        pool_network = ipaddress.ip_network(str(pool.cidr), strict=False)
    except ValueError as exc:
        raise ValueError("Invalid IPv4 block selected.") from exc
    if (
        selected_network.version != 4
        or pool_network.version != 4
        or not selected_network.subnet_of(pool_network)
    ):
        raise ValueError("Selected IPv4 block is not part of its IPAM pool.")
    return pool, cast(ipaddress.IPv4Network, selected_network)


def available_ipv4_addresses_for_selector(
    db: Session,
    *,
    selector: str,
    query: str = "",
    limit: int = 1000,
) -> dict[str, object]:
    """Return available IPv4 addresses for one subscription-form block selector."""
    selector = str(selector or "").strip()
    if not selector:
        raise ValueError("IPv4 block is required.")
    if limit < 1:
        limit = 1

    block: IpBlock | None = None
    if selector.startswith(POOL_IPV4_SELECTOR_PREFIX):
        pool, network = _resolve_pool_ipv4_selector(db, selector)
    else:
        try:
            block_uuid = UUID(selector)
        except ValueError as exc:
            raise ValueError("Invalid IPv4 block selected.") from exc
        block = db.get(IpBlock, block_uuid)
        if not block or not block.is_active:
            raise ValueError("Selected IPv4 block is not active.")
        block_pool = cast(IpPool | None, db.get(IpPool, block.pool_id))
        if (
            not block_pool
            or not block_pool.is_active
            or block_pool.ip_version != IPVersion.ipv4
        ):
            raise ValueError("Selected IPv4 block is not active.")
        pool = block_pool
        try:
            parsed_network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError as exc:
            raise ValueError("Invalid IPv4 block selected.") from exc
        if parsed_network.version != 4:
            raise ValueError("Selected IPv4 block is not IPv4.")
        network = cast(ipaddress.IPv4Network, parsed_network)

    available_ips = _available_ipv4_strings_for_network(
        db,
        network=network,
        pool_id=pool.id,
        pool=pool,
    )
    needle = str(query or "").strip()
    if needle:
        available_ips = [ip for ip in available_ips if ip.startswith(needle)]
    limited_ips = available_ips[:limit]
    return {
        "selector": selector,
        "pool_id": str(pool.id),
        "block_id": str(block.id) if block else "",
        "cidr": str(network),
        "available_count": len(available_ips),
        "addresses": limited_ips,
        "has_more": len(available_ips) > len(limited_ips),
    }


def _resolve_ipv4_for_selector(
    db: Session,
    *,
    selector: str,
    requested_ip: str | None = None,
) -> tuple[IPv4Address | None, IpBlock | None, str]:
    selector = str(selector or "").strip()
    if selector.startswith(POOL_IPV4_SELECTOR_PREFIX):
        pool, network = _resolve_pool_ipv4_selector(db, selector)
        available_ips = _available_ipv4_strings_for_network(
            db,
            network=network,
            pool_id=pool.id,
            pool=pool,
        )
        label = str(network)
        if not available_ips:
            return None, None, label
        selected_ip = str(requested_ip or "").strip() or available_ips[0]
        if selected_ip not in available_ips:
            raise ValueError(
                f"Selected IPv4 address {selected_ip} is not available in block {label}."
            )
        address = (
            db.query(IPv4Address).filter(IPv4Address.address == selected_ip).first()
        )
        if address:
            return address, None, label
        address = IPv4Address(address=selected_ip, pool_id=pool.id, is_reserved=False)
        db.add(address)
        db.commit()
        db.refresh(address)
        return address, None, label

    try:
        block_uuid = UUID(selector)
    except ValueError as exc:
        raise ValueError("Invalid IPv4 block selected.") from exc
    block = db.get(IpBlock, block_uuid)
    if not block or not block.is_active:
        raise ValueError("Selected IPv4 block is not active.")
    address = _resolve_ipv4_for_block(
        db,
        block=block,
        requested_ip=requested_ip,
    )
    return address, block, str(block.cidr)


def _pool_id_for_ipv4_selector(db: Session, selector: str) -> object | None:
    selector = str(selector or "").strip()
    if selector.startswith(POOL_IPV4_SELECTOR_PREFIX):
        pool, _network = _resolve_pool_ipv4_selector(db, selector)
        return pool.id
    try:
        block_uuid = UUID(selector)
    except ValueError as exc:
        raise ValueError("Invalid IPv4 block selected.") from exc
    block = db.get(IpBlock, block_uuid)
    if not block or not block.is_active:
        raise ValueError("Selected IPv4 block is not active.")
    return block.pool_id


def _append_block_usage_note(
    db: Session,
    *,
    block: IpBlock,
    subscriber: Subscriber,
    allocated_ip: str,
) -> None:
    display_name = (
        subscriber.display_name
        or f"{subscriber.first_name or ''} {subscriber.last_name or ''}".strip()
        or subscriber.subscriber_number
        or str(subscriber.id)
    )
    entry = (
        f"{format_in_app_timezone(datetime.now(UTC), '%Y-%m-%d %H:%M')} "
        f"{APP_TIMEZONE_NAME}: allocated {allocated_ip} to {display_name}"
    )
    existing = str(block.notes or "").strip()
    block.notes = f"{existing}\n{entry}".strip() if existing else entry
    db.commit()


def _allocate_ipv4_assignments_for_subscription(
    db: Session,
    *,
    subscription_obj: Subscription,
    block_ids: list[str],
    selected_ips: list[str] | None = None,
) -> list[str]:
    from app.services.ip_assignment_lifecycle import (
        mark_primary_ipv4_assignment,
    )

    if not block_ids:
        return []
    _validate_unique_selected_ipv4s(selected_ips)
    subscriber = db.get(Subscriber, subscription_obj.subscriber_id)
    if not subscriber:
        return []
    allocated: list[str] = []
    for index, selector in enumerate(block_ids):
        requested_ip = ""
        if selected_ips and index < len(selected_ips):
            requested_ip = str(selected_ips[index] or "").strip()
        address = None
        if requested_ip:
            selector_pool_id = _pool_id_for_ipv4_selector(db, str(selector))
            existing_address = (
                db.query(IPv4Address)
                .filter(IPv4Address.address == requested_ip)
                .first()
            )
            existing_assignment = (
                getattr(existing_address, "assignment", None)
                if existing_address
                else None
            )
            if (
                existing_address
                and existing_assignment
                and existing_assignment.subscriber_id == subscription_obj.subscriber_id
                and existing_assignment.is_active
                and existing_address.pool_id == selector_pool_id
            ):
                address = existing_address
                block = None
                block_label = requested_ip
        if address is None:
            address, block, block_label = _resolve_ipv4_for_selector(
                db,
                selector=str(selector),
                requested_ip=requested_ip or None,
            )
        if not address:
            raise ValueError(f"No available IPv4 address in block {block_label}.")
        assignment_payload = {
            "account_id": subscription_obj.subscriber_id,
            "subscription_id": subscription_obj.id,
            "ip_version": IPVersion.ipv4,
            "ipv4_address_id": address.id,
            "is_active": True,
        }
        existing_assignment = getattr(address, "assignment", None)
        if existing_assignment:
            network_service.ip_assignments.update(
                db=db,
                assignment_id=str(existing_assignment.id),
                payload=IPAssignmentUpdate.model_validate(assignment_payload),
            )
        else:
            network_service.ip_assignments.create(
                db=db,
                payload=IPAssignmentCreate.model_validate(assignment_payload),
            )
        # The caller serves allocated[0] as subscriptions.ipv4_address, so the
        # first allocation is the served address and the rest are additional
        # holdings. This form is an adapter, so it asks the assignment owner to
        # move the marker rather than setting the flag itself.
        if index == 0:
            mark_primary_ipv4_assignment(
                db,
                subscription_id=subscription_obj.id,
                ipv4_address_id=address.id,
            )

        allocated_ip = str(address.address)
        allocated.append(allocated_ip)
        if block is not None:
            _append_block_usage_note(
                db,
                block=block,
                subscriber=subscriber,
                allocated_ip=allocated_ip,
            )
    return allocated


def _sync_ipv4_assignments_for_subscription(
    db: Session,
    *,
    subscription_obj: Subscription,
    block_ids: list[str] | None,
    selected_ips: list[str] | None = None,
    assignment_submitted: bool = False,
) -> list[str]:
    if not assignment_submitted:
        return []
    if not block_ids:
        raise ValueError("Select a valid IPv4 block before changing service IPv4.")
    desired_ips = _allocate_ipv4_assignments_for_subscription(
        db,
        subscription_obj=subscription_obj,
        block_ids=block_ids,
        selected_ips=selected_ips,
    )
    desired_set = {ip for ip in desired_ips if ip}
    active_assignments = (
        db.query(IPAssignment)
        .filter(
            or_(
                IPAssignment.subscription_id == subscription_obj.id,
                and_(
                    IPAssignment.subscription_id.is_(None),
                    IPAssignment.subscriber_id == subscription_obj.subscriber_id,
                ),
            )
        )
        .filter(IPAssignment.ip_version == IPVersion.ipv4)
        .filter(IPAssignment.is_active.is_(True))
        .all()
    )
    for assignment in active_assignments:
        address = getattr(assignment, "ipv4_address", None)
        ip_text = str(getattr(address, "address", "") or "").strip()
        if ip_text and ip_text in desired_set:
            continue
        network_service.ip_assignments.delete(db, str(assignment.id))
    return desired_ips


def active_service_ipv4_address(db: Session, subscription_id: str) -> str | None:
    """Return the one exact active service IPv4, or fail on ledger ambiguity."""

    rows = list(
        db.execute(
            select(IPAssignment, IPv4Address.address)
            .join(IPv4Address, IPAssignment.ipv4_address_id == IPv4Address.id)
            .where(
                IPAssignment.subscription_id == UUID(subscription_id),
                IPAssignment.ip_version == IPVersion.ipv4,
                IPAssignment.is_active.is_(True),
            )
            .order_by(IPAssignment.id)
        ).all()
    )
    if len(rows) > 1:
        raise ValueError(
            "This service has multiple active IPv4 assignments. Review IPAM "
            "before replacing its address."
        )
    return str(rows[0][1]) if rows else None


def replace_subscription_ipv4_with_owner(
    db: Session,
    *,
    subscription_id: str,
    selector: str,
    requested_ip: str,
    actor_id: str | None,
) -> str:
    """Replace one active service IPv4 without entering a billing owner.

    The admin action is an adapter around the fingerprinted assignment and
    served-projection owners. It never updates SubscriptionAddOn, billing
    contracts, adjustments, invoices, cadence, account, offer, or billing mode.
    """

    actor = str(actor_id or "").strip()
    if not actor:
        raise ValueError("Authenticated actor required for IPv4 replacement.")
    normalized_selector = str(selector or "").strip()
    normalized_ip = str(requested_ip or "").strip()
    if not normalized_selector or not normalized_ip:
        raise ValueError("Select an IPv4 block and address to replace the service IP.")

    subscription_uuid = UUID(subscription_id)
    subscription = db.get(Subscription, subscription_uuid)
    if subscription is None:
        raise ValueError("Subscription not found.")
    if subscription.status is not SubscriptionStatus.active:
        raise ValueError(
            "Service IPv4 replacement currently requires an active subscription."
        )

    active_assignments = list(
        db.scalars(
            select(IPAssignment)
            .where(
                IPAssignment.subscription_id == subscription_uuid,
                IPAssignment.ip_version == IPVersion.ipv4,
                IPAssignment.is_active.is_(True),
            )
            .order_by(IPAssignment.id)
        ).all()
    )
    if len(active_assignments) > 1:
        raise ValueError(
            "This service has multiple active IPv4 assignments. Review IPAM "
            "before replacing its address."
        )
    current_assignment = active_assignments[0] if active_assignments else None
    current_address = (
        str(current_assignment.ipv4_address.address)
        if current_assignment is not None and current_assignment.ipv4_address
        else None
    )
    if current_address == normalized_ip:
        if current_assignment is None:
            raise ValueError("The current IPv4 assignment could not be resolved.")
        current_projection = preview_service_ipv4_projection_repair(
            db,
            subscription_id=subscription_uuid,
            assignment_id=current_assignment.id,
        )
        if current_projection.decision is IPv4ServedProjectionDecision.noop:
            return normalized_ip
        if current_projection.applicable:
            raise ValueError(
                "This IPv4 is already assigned, but its served-IP projection is "
                "stale. Use Reconcile served IPv4 on the subscription detail page."
            )
        raise ValueError(
            "This IPv4 is already assigned, but its served-IP projection cannot "
            "be reconciled yet "
            f"({current_projection.decision.value}). Review the subscription detail."
        )

    target, _, _ = _resolve_ipv4_for_selector(
        db,
        selector=normalized_selector,
        requested_ip=normalized_ip,
    )
    if target is None:
        raise ValueError("The selected IPv4 address is no longer available.")

    if current_assignment is not None:
        current_projection = preview_service_ipv4_projection_repair(
            db,
            subscription_id=subscription_uuid,
            assignment_id=current_assignment.id,
        )
        if current_projection.decision not in {
            IPv4ServedProjectionDecision.noop,
            IPv4ServedProjectionDecision.ready,
        }:
            raise ValueError(
                "The current IPAM, served-IP, or RADIUS evidence is not aligned "
                f"({current_projection.decision.value}). Repair the drift before "
                "replacing the address."
            )

    assignment_preview = preview_service_ipv4_assignment_repair(
        db,
        subscription_id=subscription_uuid,
        desired_address_id=target.id,
        deactivate_assignment_ids=(
            (current_assignment.id,) if current_assignment is not None else ()
        ),
    )
    if not assignment_preview.applicable:
        raise ValueError(
            "The selected IPv4 replacement is not safe to apply "
            f"({assignment_preview.decision.value})."
        )
    target_id = target.id
    assignment_fingerprint = assignment_preview.fingerprint
    deactivate_assignment_ids = assignment_preview.deactivate_assignment_ids
    command_id = uuid4()
    correlation_id = command_id
    db.commit()

    assignment_outcome = repair_service_ipv4_assignment(
        db,
        RepairServiceIPv4AssignmentCommand(
            context=CommandContext(
                command_id=command_id,
                correlation_id=correlation_id,
                actor=actor,
                scope="catalog:subscription:ipv4:replace",
                reason=(
                    f"Administrator replaced the exact service IPv4 with "
                    f"{normalized_ip}; commercial IP entitlement unchanged"
                ),
                idempotency_key=f"admin-ipv4-assignment:{command_id}",
            ),
            subscription_id=subscription_uuid,
            desired_address_id=target_id,
            deactivate_assignment_ids=deactivate_assignment_ids,
            preview_fingerprint=assignment_fingerprint,
        ),
    )
    if assignment_outcome.desired_assignment_id is None:
        raise ValueError("IPv4 assignment owner did not return a desired assignment.")

    projection_preview = preview_service_ipv4_projection_repair(
        db,
        subscription_id=subscription_uuid,
        assignment_id=assignment_outcome.desired_assignment_id,
    )
    if projection_preview.decision is IPv4ServedProjectionDecision.noop:
        db.commit()
        return normalized_ip
    if not projection_preview.applicable:
        decision = projection_preview.decision.value
        db.rollback()
        raise ValueError(
            "IPAM was updated, but the served-IP projection requires operator "
            f"review ({decision})."
        )
    projection_fingerprint = projection_preview.fingerprint
    desired_assignment_id = assignment_outcome.desired_assignment_id
    db.commit()

    projection_command_id = uuid4()
    repair_service_ipv4_projection(
        db,
        RepairServiceIPv4ProjectionCommand(
            context=CommandContext(
                command_id=projection_command_id,
                correlation_id=correlation_id,
                causation_id=command_id,
                actor=actor,
                scope="catalog:subscription:ipv4:replace",
                reason=(
                    f"Project the exact service IPv4 replacement to {normalized_ip}; "
                    "commercial IP entitlement unchanged"
                ),
                idempotency_key=f"admin-ipv4-projection:{command_id}",
            ),
            subscription_id=subscription_uuid,
            assignment_id=desired_assignment_id,
            preview_fingerprint=projection_fingerprint,
        ),
    )
    return normalized_ip


def _additional_route_form_rows(
    db: Session,
    *,
    subscription_id,
) -> tuple[list[str], list[str]]:
    if not subscription_id:
        return [""], [""]
    subscription = db.get(Subscription, subscription_id)
    query = db.query(SubscriberAdditionalRoute).filter(
        SubscriberAdditionalRoute.subscription_id == subscription_id
    )
    routes = query.filter(SubscriberAdditionalRoute.is_active.is_(True)).all()
    if (
        not routes
        and subscription is not None
        and _legacy_route_scope_is_unambiguous(db, subscription)
    ):
        routes = (
            db.query(SubscriberAdditionalRoute)
            .filter(
                SubscriberAdditionalRoute.subscriber_id == subscription.subscriber_id,
                SubscriberAdditionalRoute.subscription_id.is_(None),
                SubscriberAdditionalRoute.is_active.is_(True),
            )
            .all()
        )
    routes.sort(key=lambda route: str(route.cidr or ""))
    if not routes:
        return [""], [""]
    return (
        [str(route.cidr or "") for route in routes],
        [str(route.metric or 1) for route in routes],
    )


def _legacy_route_scope_is_unambiguous(
    db: Session,
    subscription_obj: Subscription,
) -> bool:
    count = (
        db.query(func.count(Subscription.id))
        .filter(
            Subscription.subscriber_id == subscription_obj.subscriber_id,
            Subscription.status.in_(
                (SubscriptionStatus.pending, SubscriptionStatus.active)
            ),
        )
        .scalar()
        or 0
    )
    return count == 1


def normalize_additional_routes(
    cidrs: list[str] | None,
    metrics: list[str] | None = None,
) -> list[tuple[str, int, int]]:
    """Validate additional routed blocks from the admin form.

    Returns normalized (cidr, prefix_length, metric) tuples and rejects duplicate
    routes before any DB writes happen.
    """
    routes: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for index, raw_cidr in enumerate(cidrs or []):
        text = str(raw_cidr or "").strip()
        if not text:
            continue
        if "/" not in text:
            text = f"{text}/32"
        try:
            # strict=False intentionally snaps a host address to its network
            # (e.g. 203.0.113.9/29 -> 203.0.113.8/29); the snapped value is what
            # gets stored, billed and validated, so there is no stored-vs-billed
            # mismatch — callers should display the normalized CIDR back.
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid additional routed IP block: {raw_cidr}") from exc
        if network.version != 4:
            raise ValueError("Additional routed IP blocks must be IPv4 CIDRs.")
        if (
            network.prefixlen == 0
            or network.is_multicast
            or network.is_loopback
            or network.is_link_local
            or network.is_reserved
            or network.is_unspecified
        ):
            raise ValueError(
                f"Additional routed IP block {network} is not a routable range "
                "(default-route, multicast, loopback, link-local, or reserved space)."
            )
        cidr = str(network)
        if cidr in seen:
            raise ValueError(f"Duplicate additional routed IP block: {cidr}")
        seen.add(cidr)

        metric_raw = str((metrics or [])[index] if index < len(metrics or []) else "")
        metric_text = metric_raw.strip()
        if not metric_text:
            metric = 1
        else:
            try:
                metric = int(metric_text)
            except ValueError as exc:
                raise ValueError(f"Invalid route metric for {cidr}.") from exc
            if metric < 1:
                raise ValueError(f"Route metric for {cidr} must be 1 or higher.")
        routes.append((cidr, int(network.prefixlen), metric))
    # Reject overlapping routes within a single submission (exact-string dedupe
    # above does not catch a nested block, e.g. /30 inside a /28).
    for outer in range(len(routes)):
        net_a = ipaddress.ip_network(routes[outer][0], strict=False)
        for inner in range(outer + 1, len(routes)):
            net_b = ipaddress.ip_network(routes[inner][0], strict=False)
            if net_a.overlaps(net_b):
                raise ValueError(
                    f"Additional routed IP blocks {net_a} and {net_b} overlap."
                )
    return routes


def validate_additional_route_billing(
    db: Session,
    *,
    cidrs: list[str] | None,
    metrics: list[str] | None = None,
) -> None:
    """Validate that every desired routed block has priced billing coverage."""
    desired = normalize_additional_routes(cidrs, metrics)
    _priced_public_ip_addons_for_routes(db, desired)


def _validate_additional_routes_available(
    db: Session,
    *,
    subscription_obj: Subscription,
    desired_cidrs: list[str],
) -> None:
    if not desired_cidrs or not subscription_obj.subscriber_id:
        return

    desired_networks: list[ipaddress.IPv4Network] = []
    for cidr in desired_cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        if network.version == 4:
            desired_networks.append(cast(ipaddress.IPv4Network, network))
    for desired in desired_networks:
        if _subnet_starts_at_zero_address(desired):
            raise ValueError(
                f"Additional routed IP block {desired} cannot start at .0."
            )

    legacy_scope = _legacy_route_scope_is_unambiguous(db, subscription_obj)
    assigned_networks = _active_route_networks(
        db,
        current_subscriber_id=(
            subscription_obj.subscriber_id if legacy_scope else None
        ),
        current_subscription_id=subscription_obj.id,
    )
    current_subscriber_routes: list[ipaddress.IPv4Network] = []
    current_routes = (
        db.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscription_id == subscription_obj.id)
        .filter(SubscriberAdditionalRoute.is_active.is_(True))
        .all()
    )
    if not current_routes and legacy_scope:
        current_routes = (
            db.query(SubscriberAdditionalRoute)
            .filter(
                SubscriberAdditionalRoute.subscriber_id
                == subscription_obj.subscriber_id,
                SubscriberAdditionalRoute.subscription_id.is_(None),
                SubscriberAdditionalRoute.is_active.is_(True),
            )
            .all()
        )
    for route in current_routes:
        try:
            route_network = ipaddress.ip_network(str(route.cidr), strict=False)
        except ValueError:
            continue
        if route_network.version == 4:
            current_subscriber_routes.append(cast(ipaddress.IPv4Network, route_network))

    for desired in desired_networks:
        for assigned in assigned_networks:
            if desired.overlaps(assigned):
                raise ValueError(
                    f"Additional routed IP block {desired} is already assigned."
                )
        if _additional_route_has_ipam_conflict(
            db,
            desired,
            current_subscriber_routes=current_subscriber_routes,
        ):
            raise ValueError(
                f"Additional routed IP block {desired} contains an IP already "
                "reserved or assigned in IPAM."
            )


def _additional_route_has_ipam_conflict(
    db: Session,
    desired: ipaddress.IPv4Network,
    *,
    current_subscriber_routes: list[ipaddress.IPv4Network],
) -> bool:
    addresses = [str(ip) for ip in desired]
    for offset in range(0, len(addresses), 1000):
        chunk = addresses[offset : offset + 1000]
        rows = (
            db.query(IPv4Address, IPAssignment)
            .outerjoin(
                IPAssignment,
                and_(
                    IPAssignment.ipv4_address_id == IPv4Address.id,
                    IPAssignment.is_active.is_(True),
                ),
            )
            .filter(IPv4Address.address.in_(chunk))
            .all()
        )
        for address, assignment in rows:
            if assignment is not None:
                return True
            if address.ont_unit_id is not None:
                return True
            allocation_type = str(address.allocation_type or "").strip().lower()
            if allocation_type == "management":
                return True
            if not bool(address.is_reserved) and not allocation_type:
                continue
            try:
                ip_obj = ipaddress.ip_address(str(address.address))
            except ValueError:
                return True
            if any(
                route == desired and ip_obj in route
                for route in current_subscriber_routes
            ):
                continue
            return True
    return False


def _public_ip_addon_prefix(db: Session, add_on_id: str | None) -> int | None:
    selected = str(add_on_id or "").strip()
    if not selected:
        return None
    try:
        selected_uuid = UUID(selected)
    except ValueError as exc:
        raise ValueError("Selected additional IP add-on is invalid.") from exc
    add_on = db.get(AddOn, selected_uuid)
    if not add_on or not add_on.is_active:
        raise ValueError("Selected additional IP add-on is not available.")
    if add_on.ip_prefix_length is None:
        return None
    return int(add_on.ip_prefix_length)


def _validate_additional_routes_match_addon(
    db: Session,
    *,
    desired: list[tuple[str, int, int]],
    add_on_id: str | None = None,
    quantity: str | int | None = None,
) -> None:
    if not desired:
        return
    expected_prefix = _public_ip_addon_prefix(db, add_on_id)
    if expected_prefix is None:
        return
    for cidr, prefix_length, _metric in desired:
        if prefix_length != expected_prefix:
            raise ValueError(
                f"Additional routed IP block {cidr} must match the selected "
                f"/{expected_prefix} add-on."
            )
    try:
        qty = int(str(quantity or "1").strip() or "1")
    except ValueError as exc:
        raise ValueError("Additional IP add-on quantity must be a number.") from exc
    if len(desired) > qty:
        raise ValueError(
            "Additional routed IP block count cannot exceed the selected add-on "
            "quantity."
        )


def _priced_public_ip_addon_by_prefix(db: Session, prefix_length: int) -> AddOn | None:
    matches = (
        db.query(AddOn)
        .join(AddOnPrice, AddOnPrice.add_on_id == AddOn.id)
        .filter(AddOn.is_active.is_(True))
        .filter(AddOn.ip_is_public.is_(True))
        .filter(AddOn.ip_prefix_length == prefix_length)
        .filter(AddOnPrice.is_active.is_(True))
        .filter(AddOnPrice.price_type == PriceType.recurring)
        .order_by(AddOn.name.asc(), AddOnPrice.created_at.asc())
        .all()
    )
    # An add-on can have more than one active recurring price row, so dedupe by
    # add-on before judging ambiguity.
    unique: list[AddOn] = []
    seen_ids: set = set()
    for add_on in matches:
        if add_on.id not in seen_ids:
            seen_ids.add(add_on.id)
            unique.append(add_on)
    if len(unique) > 1:
        # The route→add-on tie is by prefix length, not a FK; two priced add-ons
        # of the same prefix mean the route silently maps to whichever sorts
        # first and may be billed the wrong price. Surface it.
        logger.warning(
            "ambiguous_public_ip_addon_prefix: %d priced add-ons for /%d (%s) — "
            "using '%s'; prefix→add-on mapping is not unique",
            len(unique),
            prefix_length,
            ", ".join(a.name for a in unique),
            unique[0].name,
        )
    return unique[0] if unique else None


def _priced_public_ip_addons_for_routes(
    db: Session,
    desired: list[tuple[str, int, int]],
    *,
    require: bool = True,
) -> dict[int, AddOn]:
    addons: dict[int, AddOn] = {}
    for cidr, prefix_length, _metric in desired:
        if prefix_length in addons:
            continue
        add_on = _priced_public_ip_addon_by_prefix(db, prefix_length)
        if add_on is None:
            if not require:
                # Grandfathered/existing route with no priced add-on: resolve
                # what exists, skip what doesn't (the new-route requirement is
                # enforced earlier in sync_additional_routes_for_subscription).
                continue
            raise ValueError(
                f"Additional routed IP block {cidr} requires an active "
                f"/{prefix_length} public IP add-on with a recurring price."
            )
        addons[prefix_length] = add_on
    return addons


def _public_ip_addon_has_recurring_price(db: Session, add_on_id: str | None) -> bool:
    if not add_on_id:
        return False
    try:
        selected_uuid = UUID(str(add_on_id))
    except ValueError:
        return False
    return (
        db.query(AddOnPrice.id)
        .filter(AddOnPrice.add_on_id == selected_uuid)
        .filter(AddOnPrice.is_active.is_(True))
        .filter(AddOnPrice.price_type == PriceType.recurring)
        .first()
        is not None
    )


def sync_additional_routes_for_subscription(
    db: Session,
    *,
    subscription_obj: Subscription,
    cidrs: list[str] | None,
    metrics: list[str] | None = None,
    add_on_id: str | None = None,
    quantity: str | int | None = None,
    commit: bool = True,
) -> list[str]:
    """Upsert active subscriber additional routes and deactivate removed ones."""
    if not subscription_obj.subscriber_id:
        return []
    from app.services.enforcement import (
        network_identity_signature,
        reauth_subscription_on_identity_change,
    )

    before_sig = network_identity_signature(db, subscription_obj)
    desired = normalize_additional_routes(cidrs, metrics)
    _validate_additional_routes_match_addon(
        db,
        desired=desired,
        add_on_id=add_on_id,
        quantity=quantity,
    )
    desired_map = {
        cidr: (prefix_length, metric) for cidr, prefix_length, metric in desired
    }
    _validate_additional_routes_available(
        db,
        subscription_obj=subscription_obj,
        desired_cidrs=list(desired_map),
    )
    existing = (
        db.query(SubscriberAdditionalRoute)
        .filter(SubscriberAdditionalRoute.subscription_id == subscription_obj.id)
        .all()
    )
    if not existing and _legacy_route_scope_is_unambiguous(db, subscription_obj):
        existing = (
            db.query(SubscriberAdditionalRoute)
            .filter(
                SubscriberAdditionalRoute.subscriber_id
                == subscription_obj.subscriber_id,
                SubscriberAdditionalRoute.subscription_id.is_(None),
            )
            .all()
        )
        for route in existing:
            route.subscription_id = subscription_obj.id
    existing_by_cidr = {str(route.cidr): route for route in existing}
    # Routed blocks are provisioning state. A recurring public-IP charge is
    # created only when an explicit public-IP billing add-on is selected.
    # Existing routes are otherwise preserved/grandfathered.
    if str(add_on_id or "").strip():
        new_desired = [
            (cidr, prefix_length, metric)
            for cidr, prefix_length, metric in desired
            if cidr not in existing_by_cidr
        ]
        if new_desired and not _public_ip_addon_has_recurring_price(db, add_on_id):
            _cidr, prefix_length, _metric = new_desired[0]
            raise ValueError(
                f"Additional routed IP block {_cidr} requires an active "
                f"/{prefix_length} public IP add-on with a recurring price."
            )

    for cidr, (prefix_length, metric) in desired_map.items():
        existing_route = existing_by_cidr.get(cidr)
        if existing_route is None:
            db.add(
                SubscriberAdditionalRoute(
                    subscriber_id=subscription_obj.subscriber_id,
                    subscription_id=subscription_obj.id,
                    cidr=cidr,
                    prefix_length=prefix_length,
                    metric=metric,
                    is_active=True,
                    source="admin_subscription_form",
                )
            )
            continue
        existing_route.prefix_length = prefix_length
        existing_route.metric = metric
        existing_route.is_active = True
        if not existing_route.source:
            existing_route.source = "admin_subscription_form"

    for route in existing:
        if str(route.cidr) not in desired_map and route.is_active:
            route.is_active = False

    if commit:
        db.commit()
    else:
        db.flush()

    # Routes changed -> reconcile RADIUS and kick live sessions so the BNG
    # re-learns the Framed-Routes. No-op if the route set is unchanged or the
    # subscription isn't active.
    if commit:
        reauth_subscription_on_identity_change(
            db,
            str(subscription_obj.id),
            before=before_sig,
            reason="additional_routes_change",
        )
    return list(desired_map)


def sync_public_ip_addons_for_routes(
    db: Session,
    *,
    subscription_obj: Subscription,
    routes: list[tuple[str, int, int]],
) -> dict[int, str]:
    """Sync public-IP billing add-ons to the active routed blocks."""
    now = datetime.now(UTC)
    desired_counts: dict[int, int] = {}
    for _cidr, prefix_length, _metric in routes:
        desired_counts[prefix_length] = desired_counts.get(prefix_length, 0) + 1
    desired_addons = _priced_public_ip_addons_for_routes(db, routes, require=False)
    active_rows = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription_obj.id)
        .filter(AddOn.ip_is_public.is_(True))
        .filter(SubscriptionAddOn.end_at.is_(None))
        .all()
    )
    rows_by_addon_id = {
        str(sub_addon.add_on_id): sub_addon for sub_addon, _ in active_rows
    }
    desired_addon_ids = {str(add_on.id) for add_on in desired_addons.values()}

    for sub_addon, add_on in active_rows:
        if str(sub_addon.add_on_id) not in desired_addon_ids:
            sub_addon.end_at = now

    synced: dict[int, str] = {}
    for prefix_length, add_on in desired_addons.items():
        qty = desired_counts[prefix_length]
        row = rows_by_addon_id.get(str(add_on.id))
        if row is None:
            row = SubscriptionAddOn(
                subscription_id=subscription_obj.id,
                add_on_id=add_on.id,
                quantity=qty,
                start_at=now,
            )
            db.add(row)
        else:
            row.quantity = qty
        synced[prefix_length] = str(add_on.id)

    db.commit()
    return synced


def _public_ip_addon_options(db: Session) -> list[AddOn]:
    options: list[AddOn] = (
        db.query(AddOn)
        .filter(AddOn.is_active.is_(True))
        .filter(
            or_(
                AddOn.ip_is_public.is_(True),
                AddOn.addon_type.in_([AddOnType.extra_ip, AddOnType.static_ip]),
                AddOn.name.ilike("%ip%"),
                AddOn.name.ilike("%ipv4%"),
            )
        )
        .order_by(AddOn.name.asc())
        .all()
    )
    prefixes: set[int] = set()
    blocks = (
        db.query(IpBlock)
        .join(IpPool, IpPool.id == IpBlock.pool_id)
        .filter(IpPool.ip_version == IPVersion.ipv4)
        .filter(IpPool.is_active.is_(True))
        .filter(IpBlock.is_active.is_(True))
        .all()
    )
    for block in blocks:
        try:
            network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError:
            continue
        if network.version == 4:
            prefixes.add(int(network.prefixlen))
            for prefix in PUBLIC_IP_ADDON_PREFIXES:
                if network.prefixlen <= prefix:
                    prefixes.add(prefix)

    existing_prefixes = {
        int(add_on.ip_prefix_length)
        for add_on in options
        if add_on.ip_prefix_length is not None
    }
    existing_names = {str(add_on.name or "").strip().lower() for add_on in options}

    for prefix in sorted(prefixes):
        if prefix in existing_prefixes or f"/{prefix} ip" in existing_names:
            continue
        add_on = AddOn(
            name=f"/{prefix} IP",
            addon_type=AddOnType.static_ip if prefix == 32 else AddOnType.extra_ip,
            description=f"Public IPv4 /{prefix} block.",
            is_active=True,
            ip_is_public=True,
            ip_prefix_length=prefix,
        )
        db.add(add_on)
        options.append(add_on)
    if options:
        db.commit()
        for add_on in options:
            if add_on in db:
                db.refresh(add_on)
    return sorted(options, key=lambda add_on: str(add_on.name or ""))


def _network_priority_key(network: ipaddress.IPv4Network) -> tuple[int, int]:
    network_text = str(network.network_address)
    priority = 0 if network_text.startswith(PUBLIC_IP_PRIORITY_PREFIXES) else 1
    return priority, int(network.network_address)


def _active_route_networks(
    db: Session,
    *,
    current_subscriber_id: object | None = None,
    current_subscription_id: object | None = None,
) -> list[ipaddress.IPv4Network]:
    query = db.query(SubscriberAdditionalRoute).filter(
        SubscriberAdditionalRoute.is_active.is_(True)
    )
    if current_subscriber_id:
        query = query.filter(
            SubscriberAdditionalRoute.subscriber_id != current_subscriber_id
        )
    if current_subscription_id:
        query = query.filter(
            or_(
                SubscriberAdditionalRoute.subscription_id.is_(None),
                SubscriberAdditionalRoute.subscription_id != current_subscription_id,
            )
        )
    networks: list[ipaddress.IPv4Network] = []
    for route in query.all():
        try:
            network = ipaddress.ip_network(str(route.cidr), strict=False)
        except ValueError:
            continue
        if network.version == 4:
            networks.append(network)
    return networks


def _used_ipam_address_ints(db: Session) -> list[int]:
    """Return IPv4 addresses already unavailable in IPAM host inventory."""
    rows = (
        db.query(IPv4Address, IPAssignment)
        .outerjoin(
            IPAssignment,
            and_(
                IPAssignment.ipv4_address_id == IPv4Address.id,
                IPAssignment.is_active.is_(True),
            ),
        )
        .all()
    )
    used: set[int] = set()
    for address, assignment in rows:
        if (
            assignment is None
            and not bool(address.is_reserved)
            and not str(address.allocation_type or "").strip()
            and address.ont_unit_id is None
        ):
            continue
        try:
            used.add(int(ipaddress.ip_address(str(address.address))))
        except ValueError:
            continue
    return sorted(used)


def _subnet_contains_used_ipam_address(
    subnet: ipaddress.IPv4Network,
    used_ip_ints: list[int],
) -> bool:
    if not used_ip_ints:
        return False
    first = int(subnet.network_address)
    last = int(subnet.broadcast_address)
    index = bisect_left(used_ip_ints, first)
    return index < len(used_ip_ints) and used_ip_ints[index] <= last


def _subnet_starts_at_zero_address(subnet: ipaddress.IPv4Network) -> bool:
    if subnet.prefixlen <= 24:
        return False
    return int(str(subnet.network_address).rsplit(".", 1)[-1]) == 0


def _route_parent_networks_for_ipam(
    db: Session,
) -> list[tuple[ipaddress.IPv4Network, str, str, str]]:
    """Return active IPv4 IPAM ranges collapsed to /24 parent options."""
    ranges: dict[str, tuple[ipaddress.IPv4Network, str, str, str]] = {}

    pools = (
        db.query(IpPool)
        .filter(IpPool.ip_version == IPVersion.ipv4)
        .filter(IpPool.is_active.is_(True))
        .order_by(IpPool.name.asc())
        .all()
    )
    for pool in pools:
        try:
            network = ipaddress.ip_network(str(pool.cidr), strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue
        network = cast(ipaddress.IPv4Network, network)
        parent_networks = (
            [network] if network.prefixlen >= 24 else network.subnets(new_prefix=24)
        )
        for parent in parent_networks:
            # Keep the real owned network as the parent. A sub-/24 block must NOT
            # be widened to its enclosing /24 — that would offer routed children
            # outside the org's actual allocation (i.e. third-party address space).
            key = str(parent)
            ranges.setdefault(
                key,
                (
                    parent,
                    str(getattr(pool, "name", "Pool") or "Pool"),
                    str(pool.id),
                    "",
                ),
            )

    blocks = (
        db.query(IpBlock)
        .join(IpPool, IpPool.id == IpBlock.pool_id)
        .filter(IpPool.ip_version == IPVersion.ipv4)
        .filter(IpPool.is_active.is_(True))
        .filter(IpBlock.is_active.is_(True))
        .order_by(IpPool.name.asc(), IpBlock.cidr.asc())
        .all()
    )
    for block in blocks:
        try:
            network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue
        network = cast(ipaddress.IPv4Network, network)
        parent_networks = (
            [network] if network.prefixlen >= 24 else network.subnets(new_prefix=24)
        )
        for parent in parent_networks:
            # Keep the real owned network as the parent. A sub-/24 block must NOT
            # be widened to its enclosing /24 — that would offer routed children
            # outside the org's actual allocation (i.e. third-party address space).
            key = str(parent)
            ranges[key] = (
                parent,
                str(getattr(block.pool, "name", "Pool") or "Pool"),
                str(block.pool_id),
                str(block.id),
            )

    return sorted(ranges.values(), key=lambda item: _network_priority_key(item[0]))


def _route_range_options_for_ipam(
    db: Session,
    *,
    current_subscriber_id: object | None = None,
) -> list[dict[str, object]]:
    assigned_networks = _active_route_networks(
        db, current_subscriber_id=current_subscriber_id
    )
    used_ip_ints = _used_ipam_address_ints(db)
    options: list[dict[str, object]] = []
    for network, pool_name, pool_id, block_id in _route_parent_networks_for_ipam(db):
        children_by_prefix: dict[str, list[dict[str, object]]] = {}
        for prefix in PUBLIC_IP_ADDON_PREFIXES:
            if network.prefixlen > prefix:
                continue
            subnet_count = 2 ** (prefix - network.prefixlen)
            if subnet_count > MAX_ROUTE_RANGE_OPTIONS_PER_BLOCK:
                continue
            children: list[dict[str, object]] = []
            for subnet in network.subnets(new_prefix=prefix):
                if _subnet_starts_at_zero_address(subnet):
                    continue
                if any(subnet.overlaps(assigned) for assigned in assigned_networks):
                    continue
                if _subnet_contains_used_ipam_address(subnet, used_ip_ints):
                    continue
                children.append(
                    {
                        "id": str(subnet),
                        "cidr": str(subnet),
                        "prefix": prefix,
                        "parent_cidr": str(network),
                        "pool_name": pool_name,
                        "display": str(subnet),
                    }
                )
            children_by_prefix[str(prefix)] = children
        options.append(
            {
                "id": str(network),
                "cidr": str(network),
                "prefix": network.prefixlen,
                "pool_id": pool_id,
                "block_id": block_id,
                "pool_name": pool_name,
                "display": f"{pool_name} - {network}",
                "children_by_prefix": children_by_prefix,
            }
        )
    return options


def _route_range_parent_options_for_ipam(db: Session) -> list[dict[str, object]]:
    return [
        {
            "id": str(network),
            "cidr": str(network),
            "prefix": network.prefixlen,
            "pool_id": pool_id,
            "block_id": block_id,
            "pool_name": pool_name,
            "display": f"{pool_name} - {network}",
        }
        for network, pool_name, pool_id, block_id in _route_parent_networks_for_ipam(db)
    ]


def route_child_options_for_parent(
    db: Session,
    *,
    parent_cidr: str,
    prefix: int,
    current_subscriber_id: object | None = None,
) -> list[dict[str, object]]:
    try:
        network = ipaddress.ip_network(str(parent_cidr), strict=False)
    except ValueError as exc:
        raise ValueError("Selected route parent is invalid.") from exc
    if network.version != 4:
        raise ValueError("Selected route parent is invalid.")
    network = cast(ipaddress.IPv4Network, network)
    if prefix not in PUBLIC_IP_ADDON_PREFIXES or network.prefixlen > prefix:
        return []
    subnet_count = 2 ** (prefix - network.prefixlen)
    if subnet_count > MAX_ROUTE_RANGE_OPTIONS_PER_BLOCK:
        return []

    parent_lookup = {
        str(parent): (pool_name, pool_id, block_id)
        for parent, pool_name, pool_id, block_id in _route_parent_networks_for_ipam(db)
    }
    parent_meta = parent_lookup.get(str(network))
    if parent_meta is None:
        raise ValueError("Selected route parent is not active.")
    pool_name, _pool_id, _block_id = parent_meta

    assigned_networks = _active_route_networks(
        db, current_subscriber_id=current_subscriber_id
    )
    used_ip_ints = _used_ipam_address_ints(db)
    children: list[dict[str, object]] = []
    for subnet in network.subnets(new_prefix=prefix):
        if _subnet_starts_at_zero_address(subnet):
            continue
        if any(subnet.overlaps(assigned) for assigned in assigned_networks):
            continue
        if _subnet_contains_used_ipam_address(subnet, used_ip_ints):
            continue
        children.append(
            {
                "id": str(subnet),
                "cidr": str(subnet),
                "prefix": prefix,
                "parent_cidr": str(network),
                "pool_name": pool_name,
                "display": str(subnet),
            }
        )
    return children


def initial_route_rows_for_form(
    db: Session,
    *,
    cidrs: list[str],
    metrics: list[str],
) -> list[dict[str, str]]:
    parents = _route_range_parent_options_for_ipam(db)
    parsed_parents: list[tuple[ipaddress.IPv4Network, dict[str, object]]] = []
    for parent in parents:
        try:
            network = ipaddress.ip_network(str(parent["cidr"]), strict=False)
        except ValueError:
            continue
        if network.version == 4:
            parsed_parents.append((cast(ipaddress.IPv4Network, network), parent))

    rows: list[dict[str, str]] = []
    for index, raw_cidr in enumerate(cidrs):
        cidr = str(raw_cidr or "").strip()
        metric = str(metrics[index] if index < len(metrics) else "1").strip() or "1"
        parent_id = ""
        parent_query = ""
        child_id = cidr
        if cidr:
            try:
                child_network = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                child_network = None
            if child_network and child_network.version == 4:
                for parent_network, parent in parsed_parents:
                    if cast(ipaddress.IPv4Network, child_network).subnet_of(
                        parent_network
                    ):
                        parent_id = str(parent["id"])
                        parent_query = str(parent["display"])
                        break
        rows.append(
            {
                "parentId": parent_id,
                "parentQuery": parent_query,
                "childId": child_id,
                "cidr": cidr,
                "metric": metric,
            }
        )
    return rows


def _route_range_options_for_blocks(blocks: list[IpBlock]) -> list[dict[str, object]]:
    """Compatibility wrapper for tests/older callers."""
    options: list[dict[str, object]] = []
    for block in blocks:
        try:
            network = ipaddress.ip_network(str(block.cidr), strict=False)
        except ValueError:
            continue
        if network.version != 4:
            continue
        pool_name = getattr(block.pool, "name", "Pool")
        children_by_prefix: dict[str, list[dict[str, object]]] = {}
        for prefix in PUBLIC_IP_ADDON_PREFIXES:
            if network.prefixlen > prefix:
                continue
            subnet_count = 2 ** (prefix - network.prefixlen)
            if subnet_count > MAX_ROUTE_RANGE_OPTIONS_PER_BLOCK:
                continue
            children_by_prefix[str(prefix)] = [
                {
                    "id": str(subnet),
                    "cidr": str(subnet),
                    "prefix": prefix,
                    "parent_cidr": str(network),
                    "pool_name": pool_name,
                    "display": str(subnet),
                }
                for subnet in network.subnets(new_prefix=prefix)
            ]
        options.append(
            {
                "id": str(network),
                "cidr": str(network),
                "prefix": network.prefixlen,
                "parent_cidr": str(network),
                "pool_name": pool_name,
                "display": f"{pool_name} - {network}",
                "children_by_prefix": children_by_prefix,
            }
        )
    return sorted(
        options,
        key=lambda item: _network_priority_key(
            cast(
                ipaddress.IPv4Network,
                ipaddress.ip_network(str(item["cidr"]), strict=False),
            )
        ),
    )


def _subscription_public_ip_addon_form_row(
    db: Session,
    *,
    subscription_id,
) -> tuple[str, str]:
    if not subscription_id:
        return "", "1"
    row = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription_id)
        .filter(AddOn.ip_is_public.is_(True))
        .filter(SubscriptionAddOn.end_at.is_(None))
        .order_by(SubscriptionAddOn.start_at.desc().nullslast())
        .first()
    )
    if row is None:
        return "", "1"
    sub_addon, _add_on = row
    return str(sub_addon.add_on_id), str(sub_addon.quantity or 1)


def sync_public_ip_addon_for_subscription(
    db: Session,
    *,
    subscription_obj: Subscription,
    add_on_id: str | None,
    quantity: str | int | None,
) -> str | None:
    """Sync the active public-IP billing add-on from the admin form."""
    selected = str(add_on_id or "").strip()
    try:
        qty = int(str(quantity or "1").strip() or "1")
    except ValueError as exc:
        raise ValueError("Additional IP add-on quantity must be a number.") from exc
    if qty < 1:
        raise ValueError("Additional IP add-on quantity must be 1 or higher.")

    now = datetime.now(UTC)
    active_rows = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription_obj.id)
        .filter(AddOn.ip_is_public.is_(True))
        .filter(SubscriptionAddOn.end_at.is_(None))
        .all()
    )

    if not selected:
        for sub_addon, _add_on in active_rows:
            sub_addon.end_at = now
        db.commit()
        return None

    try:
        selected_uuid = UUID(selected)
    except ValueError as exc:
        raise ValueError("Selected additional IP add-on is invalid.") from exc
    add_on = db.get(AddOn, selected_uuid)
    if not add_on or not add_on.is_active:
        raise ValueError("Selected additional IP add-on is not available.")
    addon_type = getattr(add_on.addon_type, "value", add_on.addon_type)
    name = str(add_on.name or "").lower()
    if (
        not add_on.ip_is_public
        and addon_type not in {AddOnType.extra_ip.value, AddOnType.static_ip.value}
        and "ip" not in name
        and "ipv4" not in name
    ):
        raise ValueError("Selected additional IP add-on is not available.")

    matched = None
    for sub_addon, _add_on in active_rows:
        if str(sub_addon.add_on_id) == selected:
            matched = sub_addon
            sub_addon.quantity = qty
        else:
            sub_addon.end_at = now
    if matched is None:
        db.add(
            SubscriptionAddOn(
                subscription_id=subscription_obj.id,
                add_on_id=selected_uuid,
                quantity=qty,
                start_at=now,
            )
        )
    db.commit()
    return selected


def validate_public_ip_addon_selection(
    db: Session,
    *,
    add_on_id: str | None,
    quantity: str | int | None,
) -> None:
    selected = str(add_on_id or "").strip()
    try:
        qty = int(str(quantity or "1").strip() or "1")
    except ValueError as exc:
        raise ValueError("Additional IP add-on quantity must be a number.") from exc
    if qty < 1:
        raise ValueError("Additional IP add-on quantity must be 1 or higher.")
    if not selected:
        return
    try:
        selected_uuid = UUID(selected)
    except ValueError as exc:
        raise ValueError("Selected additional IP add-on is invalid.") from exc
    add_on = db.get(AddOn, selected_uuid)
    if not add_on or not add_on.is_active:
        raise ValueError("Selected additional IP add-on is not available.")
    addon_type = getattr(add_on.addon_type, "value", add_on.addon_type)
    name = str(add_on.name or "").lower()
    if (
        not add_on.ip_is_public
        and addon_type not in {AddOnType.extra_ip.value, AddOnType.static_ip.value}
        and "ip" not in name
        and "ipv4" not in name
    ):
        raise ValueError("Selected additional IP add-on is not available.")


def ensure_ipv4_blocks_allocatable(
    db: Session,
    block_ids: list[str],
    selected_ips: list[str] | None = None,
) -> None:
    """Validate selected IPv4 blocks before subscription creation."""
    _validate_unique_selected_ipv4s(selected_ips)
    if selected_ips and not block_ids:
        raise ValueError("Select an IPv4 block before entering an IPv4 address.")
    for index, selector in enumerate(block_ids):
        requested_ip = ""
        if selected_ips and index < len(selected_ips):
            requested_ip = str(selected_ips[index] or "").strip()
        address, _block, block_label = _resolve_ipv4_for_selector(
            db,
            selector=str(selector),
            requested_ip=requested_ip or None,
        )
        if not address:
            raise ValueError(f"No available IPv4 address in block {block_label}.")


def apply_create_quick_options(
    payload_data: dict[str, object], form: FormData
) -> tuple[bool, bool, bool]:
    """Return create follow-up flags without bypassing lifecycle ownership."""
    activate_immediately = (
        str(form.get("status") or "").strip().lower() == SubscriptionStatus.active.value
        or form.get("activate_immediately") == "1"
    )
    generate_invoice = form.get("generate_invoice") == "1"
    send_welcome_email = form.get("send_welcome_email") == "1"
    if activate_immediately:
        payload_data["status"] = SubscriptionStatus.pending.value
        for field in (
            "start_at",
            "end_at",
            "next_billing_at",
            "canceled_at",
            "cancel_reason",
        ):
            payload_data.pop(field, None)
    return activate_immediately, generate_invoice, send_welcome_email


def create_subscription(db: Session, payload_data: dict[str, object]) -> Subscription:
    """Create subscription."""
    return catalog_service.subscriptions.create(
        db=db, payload=SubscriptionCreate.model_validate(payload_data)
    )


def update_subscription(
    db: Session, subscription_id: str, payload_data: dict[str, object]
) -> Subscription:
    """Update subscription."""
    technical = SubscriptionTechnicalUpdate.model_validate(payload_data)
    return catalog_service.subscriptions.update(
        db=db,
        subscription_id=subscription_id,
        payload=SubscriptionUpdate.model_validate(
            technical.model_dump(exclude_unset=True)
        ),
    )


def create_invoice_for_subscription(db: Session, created: Subscription) -> None:
    """Generate initial invoice for subscription."""
    if not created.subscriber_id:
        return
    offer = catalog_service.offers.get(db=db, offer_id=str(created.offer_id))
    line_amount = Decimal("0.00")
    line_description = "Subscription"
    if offer:
        line_description = offer.name
        if offer.prices:
            line_amount = offer.prices[0].amount or Decimal("0.00")

    billing_adapter.create_invoice_with_lines(
        db,
        InvoiceIntent(
            account_id=created.subscriber_id,
            status=InvoiceStatus.issued,
            total=line_amount,
            issued_at=datetime.now(UTC),
        ),
        [
            InvoiceLineIntent(
                description=line_description,
                quantity=Decimal("1"),
                unit_price=line_amount,
            )
        ],
    )


def send_welcome_email_for_subscription(db: Session, created: Subscription) -> None:
    """Send welcome email when subscriber has email."""
    if not created.subscriber_id:
        return
    subscriber = db.get(Subscriber, created.subscriber_id)
    email_addr = subscriber.email if subscriber else None
    if not email_addr:
        return
    from app.services.email_template import wrap_email_html

    subject = "Welcome to your new subscription"
    body_text = "Welcome! Your subscription is now set up."
    body_html = wrap_email_html(f"<p>{body_text}</p>", subject=subject)
    email_service.send_email(
        db=db,
        to_email=email_addr,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        activity="subscription_welcome",
    )


def error_message(exc: Exception) -> str:
    """Normalize exception details for UI errors."""
    return exc.detail if hasattr(exc, "detail") else str(exc)


def edit_form_data(db: Session, subscription_obj: Subscription) -> dict[str, object]:
    """Convert persisted subscription to form dict."""
    ipv4_block_ids, ipv4_addresses = _subscription_ipv4_form_rows(
        db,
        subscription_obj=subscription_obj,
    )
    additional_route_cidrs, additional_route_metrics = _additional_route_form_rows(
        db,
        subscription_id=subscription_obj.id,
    )
    ip_addon_id, ip_addon_quantity = _subscription_public_ip_addon_form_row(
        db,
        subscription_id=subscription_obj.id,
    )
    return {
        "id": str(subscription_obj.id),
        "account_id": str(subscription_obj.subscriber_id),
        "subscriber_id": str(subscription_obj.subscriber_id),
        "offer_id": str(subscription_obj.offer_id),
        "status": subscription_obj.status.value if subscription_obj.status else "",
        "billing_mode": subscription_obj.billing_mode.value
        if subscription_obj.billing_mode
        else "",
        "contract_term": subscription_obj.contract_term.value
        if subscription_obj.contract_term
        else "",
        "start_at": subscription_obj.start_at.strftime("%Y-%m-%dT%H:%M")
        if subscription_obj.start_at
        else "",
        "end_at": subscription_obj.end_at.strftime("%Y-%m-%dT%H:%M")
        if subscription_obj.end_at
        else "",
        "next_billing_at": subscription_obj.next_billing_at.strftime("%Y-%m-%dT%H:%M")
        if subscription_obj.next_billing_at
        else "",
        "canceled_at": subscription_obj.canceled_at.strftime("%Y-%m-%dT%H:%M")
        if subscription_obj.canceled_at
        else "",
        "cancel_reason": subscription_obj.cancel_reason or "",
        "router_id": subscription_obj.router_id or "",
        "service_description": subscription_obj.service_description or "",
        "quantity": subscription_obj.quantity or "",
        "unit": subscription_obj.unit or "",
        "unit_price": subscription_obj.unit_price or "",
        "discount": subscription_obj.discount,
        "discount_value": subscription_obj.discount_value or "",
        "discount_type": _normalize_discount_type(
            subscription_obj.discount_type.value
            if subscription_obj.discount_type
            else ""
        ),
        "service_status_raw": subscription_obj.service_status_raw or "",
        "login": subscription_obj.login or "",
        "ipv4_address": subscription_obj.ipv4_address or "",
        "ipv6_address": subscription_obj.ipv6_address or "",
        "mac_address": subscription_obj.mac_address or "",
        "provisioning_nas_device_id": str(subscription_obj.provisioning_nas_device_id)
        if subscription_obj.provisioning_nas_device_id
        else "",
        "radius_profile_id": str(subscription_obj.radius_profile_id)
        if subscription_obj.radius_profile_id
        else "",
        "service_password": "",  # nosec
        "ipv4_method": (
            "permanent_static"
            if (subscription_obj.service_status_raw or "").strip().lower()
            == "permanent_static"
            else "dynamic"
        ),
        "ipv4_block_ids": ipv4_block_ids,
        "ipv4_addresses": ipv4_addresses,
        "ip_addon_id": ip_addon_id,
        "ip_addon_quantity": ip_addon_quantity,
        "additional_route_cidrs": additional_route_cidrs,
        "additional_route_metrics": additional_route_metrics,
    }


def _password_sync_evidence(credential: AccessCredential | None) -> dict[str, str]:
    if not credential or not credential.secret_hash:
        return {
            "label": "No synced password",
            "attribute": "none",
            "detail": "Credential is missing a service secret.",
        }
    password_row = radius_service._external_password_row(
        credential,
        default_attribute="Cleartext-Password",
        default_op=":=",
    )
    if not password_row:
        return {
            "label": "Unsyncable password",
            "attribute": "none",
            "detail": "Stored secret cannot be pushed to external RADIUS as usable auth material.",
        }
    attribute = password_row[0]
    if attribute == "Cleartext-Password":
        detail = "External RADIUS can authenticate with the subscriber secret."
    elif attribute == "Crypt-Password":
        detail = "External RADIUS uses a legacy crypt-compatible password row."
    else:
        detail = f"External RADIUS sync uses {attribute}."
    return {
        "label": attribute,
        "attribute": attribute,
        "detail": detail,
    }


def _subscription_ip_pool(db: Session, subscription: Subscription) -> IpPool | None:
    """Return the IpPool that the subscription's current IPv4 address belongs to.

    Surfaced on the detail page so operators can jump from a sub's IP back to
    its source pool without having to reason about radius_pool tag chains.
    """
    address = (subscription.ipv4_address or "").strip()
    if not address:
        return None
    try:
        row = (
            db.query(IPv4Address)
            .options(joinedload(IPv4Address.pool))
            .filter(IPv4Address.address == address)
            .first()
        )
    except Exception:
        return None
    return row.pool if row else None


def _ip_assignment_mode(db: Session, subscription: Subscription) -> tuple[str, str]:
    mode = str(subscription.service_status_raw or "").strip().lower()
    if mode == "dynamic":
        return ("Dynamic pool", "IP is assigned from RADIUS/DHCP pool at session time.")
    if mode == "permanent_static":
        return (
            "Static assignment",
            "Subscription is configured with a fixed IPv4 assignment.",
        )
    if subscription.ipv4_address:
        return (
            "Static assignment",
            "Subscription stores a fixed IPv4 address directly.",
        )
    # Query IP assignments by subscriber (devices link to subscribers, not subscriptions)
    has_active_ip = (
        db.query(IPAssignment)
        .filter(IPAssignment.subscriber_id == subscription.subscriber_id)
        .filter(IPAssignment.is_active.is_(True))
        .first()
    ) is not None
    if has_active_ip:
        return (
            "Assigned IP",
            "Subscriber has active IP assignments linked in inventory.",
        )
    return (
        "Unspecified",
        "No explicit IP assignment mode is recorded on the subscription.",
    )


def _humanize_label(value: object | None) -> str:
    raw = str(getattr(value, "value", value) or "").strip()
    if not raw:
        return "Not set"
    return raw.replace(".", " ").replace("_", " ").title()


def _subscription_domain_events(
    db: Session, subscription: Subscription
) -> list[dict[str, object]]:
    rows = (
        db.query(EventStore)
        .filter(
            or_(
                EventStore.subscription_id == subscription.id,
                EventStore.account_id == subscription.subscriber_id,
            )
        )
        .order_by(EventStore.created_at.desc())
        .limit(8)
        .all()
    )
    events: list[dict[str, object]] = []
    for row in rows:
        payload = row.payload or {}
        detail_parts: list[str] = []
        if payload.get("from_status") and payload.get("to_status"):
            detail_parts.append(f"{payload['from_status']} -> {payload['to_status']}")
        if payload.get("reason"):
            detail_parts.append(str(payload["reason"]))
        if payload.get("offer_name"):
            detail_parts.append(str(payload["offer_name"]))
        failed_handlers = [
            str(item.get("handler") or "").strip()
            for item in (row.failed_handlers or [])
            if str(item.get("handler") or "").strip()
        ]
        events.append(
            {
                "event_type": row.event_type,
                "label": _humanize_label(row.event_type),
                "status": _humanize_label(row.status),
                "created_at": row.created_at,
                "processed_at": row.processed_at,
                "detail": " · ".join(detail_parts)
                if detail_parts
                else "System event persisted.",
                "failed_handlers": failed_handlers,
                "failed_handler_text": ", ".join(failed_handlers),
            }
        )
    return events


def _subscription_notifications(
    db: Session, subscription: Subscription
) -> dict[str, object]:
    subscriber = subscription.subscriber or db.get(
        Subscriber, subscription.subscriber_id
    )
    targets = (
        _credential_contact_targets(subscriber)
        if subscriber
        else {"email": [], "sms": []}
    )
    recipients = list(dict.fromkeys([*targets["email"], *targets["sms"]]))
    if not recipients:
        return {"targets": targets, "items": []}

    template_codes = {
        "subscription_created",
        "subscription_activated",
        "subscription_suspended",
        "subscription_canceled",
        "invoice_created",
        "invoice_sent",
        "invoice_overdue",
        "payment_received",
        "payment_failed",
        "provisioning_completed",
        "provisioning_failed",
    }
    rows = (
        db.query(Notification, NotificationTemplate)
        .outerjoin(
            NotificationTemplate, Notification.template_id == NotificationTemplate.id
        )
        .filter(Notification.is_active.is_(True))
        .filter(Notification.recipient.in_(recipients))
        .filter(
            or_(
                NotificationTemplate.code.in_(template_codes),
                Notification.template_id.is_(None),
            )
        )
        .order_by(Notification.created_at.desc())
        .limit(8)
        .all()
    )
    items = [
        {
            "channel": _humanize_label(notification.channel),
            "status": _humanize_label(notification.status),
            "recipient": notification.recipient,
            "subject": notification.subject or "No subject",
            "template_code": template.code if template else "",
            "created_at": notification.created_at,
            "sent_at": notification.sent_at,
            "last_error": notification.last_error or "",
        }
        for notification, template in rows
    ]
    return {"targets": targets, "items": items}


def _subscription_radius_sync_evidence(
    db: Session,
    subscription: Subscription,
    credential: AccessCredential | None,
) -> dict[str, object]:
    radius_user = None
    if credential:
        radius_user = (
            db.query(RadiusUser)
            .filter(
                or_(
                    RadiusUser.access_credential_id == credential.id,
                    RadiusUser.subscription_id == subscription.id,
                )
            )
            .order_by(RadiusUser.last_sync_at.desc(), RadiusUser.created_at.desc())
            .first()
        )

    nas_clients: list[RadiusClient] = []
    last_sync_run: RadiusSyncRun | None = None
    if subscription.provisioning_nas_device_id:
        nas_clients = (
            db.query(RadiusClient)
            .join(RadiusServer, RadiusServer.id == RadiusClient.server_id)
            .filter(
                RadiusClient.nas_device_id == subscription.provisioning_nas_device_id
            )
            .filter(RadiusClient.is_active.is_(True))
            .filter(RadiusServer.is_active.is_(True))
            .order_by(RadiusServer.name.asc())
            .all()
        )
        last_sync_run = (
            db.query(RadiusSyncRun)
            .join(RadiusSyncJob, RadiusSyncJob.id == RadiusSyncRun.job_id)
            .filter(RadiusSyncJob.is_active.is_(True))
            .filter(RadiusSyncJob.sync_nas_clients.is_(True))
            .order_by(RadiusSyncRun.finished_at.desc(), RadiusSyncRun.started_at.desc())
            .first()
        )

    external_jobs = (
        db.query(RadiusSyncJob)
        .filter(RadiusSyncJob.is_active.is_(True))
        .filter(RadiusSyncJob.connector_config_id.isnot(None))
        .filter(
            or_(
                RadiusSyncJob.sync_users.is_(True),
                RadiusSyncJob.sync_nas_clients.is_(True),
            )
        )
        .all()
    )
    external_job_ids = [job.id for job in external_jobs]
    external_run = None
    if external_job_ids:
        external_run = (
            db.query(RadiusSyncRun)
            .filter(RadiusSyncRun.job_id.in_(external_job_ids))
            .order_by(RadiusSyncRun.finished_at.desc(), RadiusSyncRun.started_at.desc())
            .first()
        )
    external_details = (
        external_run.details
        if external_run and isinstance(external_run.details, dict)
        else {}
    )

    return {
        "internal_user": radius_user,
        "nas_clients": nas_clients,
        "nas_client_count": len(nas_clients),
        "last_sync_run": last_sync_run,
        "external_job_count": len(external_jobs),
        "external_run": external_run,
        "external_users_synced": int(
            external_details.get("external_users_synced") or 0
        ),
        "external_nas_synced": int(external_details.get("external_nas_synced") or 0),
        "external_credentials_scanned": int(
            external_details.get("credentials_scanned") or 0
        ),
        "external_nas_devices_synced": int(
            external_details.get("nas_devices_synced") or 0
        ),
        "external_error": str(external_details.get("error") or ""),
    }


def _subscription_vacation_hold(
    db: Session, subscription: Subscription
) -> dict[str, object] | None:
    """Get active vacation hold (customer_hold) info for a subscription.

    Returns None if no active vacation hold exists.
    """
    lock = (
        db.query(EnforcementLock)
        .filter(EnforcementLock.subscription_id == subscription.id)
        .filter(EnforcementLock.reason == EnforcementReason.customer_hold)
        .filter(EnforcementLock.is_active.is_(True))
        .first()
    )
    if not lock:
        return None
    return {
        "lock_id": str(lock.id),
        "created_at": lock.created_at,
        "resume_at": lock.resume_at,
        "notes": lock.notes,
        "source": lock.source,
    }


def _subscription_enforcement_state(
    db: Session, subscription: Subscription
) -> dict[str, object]:
    runtime_state = radius_reject_service._load_runtime_state(db)
    runtime_entry = runtime_state.get("subscriptions", {}).get(str(subscription.id), {})
    last_event = (
        db.query(EventStore)
        .filter(EventStore.subscription_id == subscription.id)
        .filter(
            EventStore.event_type.in_(
                [
                    "subscription.suspended",
                    "subscription.resumed",
                    "subscription.activated",
                    "invoice.overdue",
                    "payment.received",
                ]
            )
        )
        .order_by(EventStore.created_at.desc())
        .first()
    )
    if runtime_entry:
        label = "Reject IP active"
        detail = f"Traffic is currently pinned to {runtime_entry.get('reject_ipv4') or 'a reject IP'}."
    elif subscription.status == SubscriptionStatus.active:
        label = "Restored"
        detail = "No reject-IP runtime state is active for this subscription."
    else:
        label = _humanize_label(subscription.status)
        detail = "No reject-IP runtime state has been recorded."
    return {
        "label": label,
        "detail": detail,
        "runtime_entry": runtime_entry,
        "last_event": last_event,
    }


def _subscription_external_radius_rows(
    db: Session,
    credential: AccessCredential | None,
) -> list[dict[str, object]]:
    if not credential or not credential.username:
        return []
    return radius_service.read_external_radius_rows_for_username(
        db, credential.username
    )


_RADIUS_CONNECTED_FRESH_SECONDS = 15 * 60


def _subscription_connection_status(
    db: Session,
    subscription: Subscription,
    credential: AccessCredential | None,
) -> dict[str, object]:
    if subscription.status != SubscriptionStatus.active:
        return {
            "state": "inactive",
            "label": "Not connected",
            "detail": "Service is not active",
            "session": None,
            "last_seen_at": None,
            "framed_ip_address": None,
            "identifier": None,
        }

    session = latest_open_accounting_session_for_subscription(
        db,
        subscription.id,
        access_credential_id=credential.id if credential else None,
    )
    if not session:
        return {
            "state": "offline",
            "label": "Not connected",
            "detail": "No open RADIUS accounting session",
            "session": None,
            "last_seen_at": None,
            "framed_ip_address": None,
            "identifier": None,
        }

    last_seen_at = session.last_update_at or session.session_start or session.created_at
    last_seen_utc = last_seen_at
    if last_seen_utc and last_seen_utc.tzinfo is None:
        last_seen_utc = last_seen_utc.replace(tzinfo=UTC)
    is_fresh = bool(
        last_seen_utc
        and last_seen_utc
        >= datetime.now(UTC) - timedelta(seconds=_RADIUS_CONNECTED_FRESH_SECONDS)
    )
    return {
        "state": "connected" if is_fresh else "stale",
        "label": "Connected" if is_fresh else "Last seen",
        "detail": "Open RADIUS accounting session"
        if is_fresh
        else "Open session has stale accounting updates",
        "session": session,
        "last_seen_at": last_seen_at,
        "framed_ip_address": session.framed_ip_address,
        "identifier": session.framed_ip_address or session.session_id,
    }


def _active_public_ip_addons_for_subscription(
    db: Session,
    subscription_id: object,
) -> dict[int, SubscriptionAddOn]:
    rows = (
        db.query(SubscriptionAddOn, AddOn)
        .join(AddOn, AddOn.id == SubscriptionAddOn.add_on_id)
        .filter(SubscriptionAddOn.subscription_id == subscription_id)
        .filter(SubscriptionAddOn.end_at.is_(None))
        .filter(AddOn.ip_is_public.is_(True))
        .all()
    )
    result: dict[int, SubscriptionAddOn] = {}
    for sub_addon, add_on in rows:
        if add_on.ip_prefix_length is None:
            continue
        result[int(add_on.ip_prefix_length)] = sub_addon
    return result


def _additional_route_rows_for_subscription(
    db: Session,
    *,
    subscription: Subscription,
    routes: list[SubscriberAdditionalRoute],
) -> list[dict[str, object]]:
    active_addons = _active_public_ip_addons_for_subscription(db, subscription.id)
    counts_by_prefix: dict[int, int] = {}
    for route in routes:
        counts_by_prefix[int(route.prefix_length)] = (
            counts_by_prefix.get(int(route.prefix_length), 0) + 1
        )
    rows: list[dict[str, object]] = []
    for route in routes:
        prefix = int(route.prefix_length)
        sub_addon = active_addons.get(prefix)
        qty = int(getattr(sub_addon, "quantity", 0) or 0) if sub_addon else 0
        expected_qty = counts_by_prefix.get(prefix, 0)
        billing_ok = bool(sub_addon and qty >= expected_qty)
        rows.append(
            {
                "cidr": route.cidr,
                "prefix_length": prefix,
                "type_label": "Additional IP" if prefix == 32 else "Routed IP Block",
                "billing_ok": billing_ok,
                "billing_label": f"Billed /{prefix} IP x{qty}"
                if billing_ok
                else "Billing add-on missing",
            }
        )
    return rows


def subscription_detail_context(
    db: Session, subscription: Subscription
) -> dict[str, object]:
    from app.services.connection_type_provisioning import build_radius_reply_attributes

    active_additional_routes = []
    if subscription.subscriber_id is not None:
        active_additional_routes = (
            db.query(SubscriberAdditionalRoute)
            .filter(
                SubscriberAdditionalRoute.subscriber_id == subscription.subscriber_id
            )
            .filter(SubscriberAdditionalRoute.is_active.is_(True))
            .order_by(SubscriberAdditionalRoute.cidr.asc())
            .all()
        )
    active_additional_route_rows = _additional_route_rows_for_subscription(
        db,
        subscription=subscription,
        routes=active_additional_routes,
    )
    credential = _current_access_credential(db, subscription.subscriber_id)
    password_sync = _password_sync_evidence(credential)
    commercial_policy = _subscription_commercial_policy(db, subscription)
    reply_attributes = build_radius_reply_attributes(
        db,
        subscription,
        profile=subscription.radius_profile,
        nas_device=subscription.provisioning_nas_device,
    )
    connection_status = _subscription_connection_status(db, subscription, credential)
    recent_auth_errors = (
        db.query(RadiusAuthError)
        .filter(
            (RadiusAuthError.subscription_id == subscription.id)
            | (RadiusAuthError.subscriber_id == subscription.subscriber_id)
        )
        .order_by(RadiusAuthError.occurred_at.desc())
        .limit(3)
        .all()
    )
    lifecycle_events = sorted(
        subscription.lifecycle_events or [],
        key=lambda event: event.created_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    ip_assignment_mode, ip_assignment_detail = _ip_assignment_mode(db, subscription)
    ip_pool = _subscription_ip_pool(db, subscription)
    has_service_orders = bool(subscription.service_orders)
    domain_events = _subscription_domain_events(db, subscription)
    notification_evidence = _subscription_notifications(db, subscription)
    radius_sync_evidence = _subscription_radius_sync_evidence(
        db, subscription, credential
    )
    enforcement_state = _subscription_enforcement_state(db, subscription)
    external_radius_rows = _subscription_external_radius_rows(db, credential)
    vacation_hold = _subscription_vacation_hold(db, subscription)
    return {
        "access_credential": credential,
        "password_sync": password_sync,
        "radius_reply_attributes": reply_attributes,
        "connection_status": connection_status,
        "active_session": connection_status.get("session"),
        "recent_auth_errors": recent_auth_errors,
        "lifecycle_events": lifecycle_events,
        "ip_assignment_mode": ip_assignment_mode,
        "ip_assignment_detail": ip_assignment_detail,
        "ip_pool": ip_pool,
        "active_additional_routes": active_additional_routes,
        "active_additional_route_rows": active_additional_route_rows,
        "direct_active_workflow": subscription.status == SubscriptionStatus.active
        and not has_service_orders,
        "commercial_policy": commercial_policy,
        "domain_events": domain_events,
        "notification_evidence": notification_evidence,
        "radius_sync_evidence": radius_sync_evidence,
        "enforcement_state": enforcement_state,
        "external_radius_rows": external_radius_rows,
        "vacation_hold": vacation_hold,
    }


def _resolve_subscriber_label(db: Session, subscriber_id: str) -> str:
    """Look up a human-readable label for a subscriber."""
    if not subscriber_id:
        return ""
    try:
        subscriber = subscriber_service.subscribers.get(
            db=db, subscriber_id=subscriber_id
        )
        label = str(getattr(subscriber, "name", "") or "").strip() or "Subscriber"
        if subscriber.subscriber_number:
            label = f"{label} ({subscriber.subscriber_number})"
        return str(label)
    except Exception:
        logger.warning(
            "Failed to resolve subscriber label for %s",
            subscriber_id,
            exc_info=True,
        )
        return ""


def subscription_form_context(
    db: Session,
    subscription: dict[str, object],
    error: str | None = None,
) -> dict[str, object]:
    """Build context dict for the subscription create/edit form template.

    Returns all reference data (accounts, offers, NAS devices, RADIUS profiles,
    enum value lists, settings) needed by the form.
    """
    default_billing_mode = (
        settings_spec.resolve_value(db, SettingDomain.catalog, "default_billing_mode")
        or BillingMode.prepaid.value
    )
    if not subscription.get("subscriber_id") and subscription.get("account_id"):
        subscription["subscriber_id"] = subscription.get("account_id")
    if not subscription.get("billing_mode"):
        subscription["billing_mode"] = default_billing_mode

    accounts = subscriber_service.accounts.list(
        db=db,
        subscriber_id=None,
        reseller_id=None,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status=OfferStatus.active.value,
        is_active=True,
        order_by="created_at",
        order_dir="desc",
        limit=500,
        offset=0,
    )

    nas_stmt = (
        select(NasDevice).where(NasDevice.is_active.is_(True)).order_by(NasDevice.name)
    )
    nas_devices = db.scalars(nas_stmt).all()
    ipv4_pools = (
        db.query(IpPool)
        .filter(IpPool.ip_version == IPVersion.ipv4)
        .filter(IpPool.is_active.is_(True))
        .order_by(IpPool.name.asc())
        .all()
    )
    block_options = _service_ipv4_block_option_summaries(db)
    rp_stmt = (
        select(RadiusProfile)
        .where(RadiusProfile.is_active.is_(True))
        .order_by(RadiusProfile.name)
    )
    radius_profiles = db.scalars(rp_stmt).all()

    subscriber_id = (
        subscription.get("subscriber_id") if isinstance(subscription, dict) else None
    )
    route_range_options = _route_range_parent_options_for_ipam(db)
    subscriber_label = _resolve_subscriber_label(db, str(subscriber_id or ""))
    current_password = _current_service_password(db, str(subscriber_id or ""))
    current_credential = _current_access_credential(db, str(subscriber_id or ""))
    credential_targets = None
    if subscriber_id:
        try:
            subscriber = subscriber_service.subscribers.get(
                db=db, subscriber_id=str(subscriber_id)
            )
        except Exception:
            logger.warning(
                "Subscriber lookup failed for form context: %s",
                subscriber_id,
                exc_info=True,
            )
            subscriber = None
        if subscriber:
            credential_targets = _credential_contact_targets(subscriber)
    selected_router_label = ""
    provisioning_nas_device_id = str(
        subscription.get("provisioning_nas_device_id") or ""
    ).strip()

    # Auto-populate NAS from subscriber's POP site if not already set
    if not provisioning_nas_device_id and subscriber_id:
        try:
            sub_obj = subscriber_service.subscribers.get(
                db=db, subscriber_id=str(subscriber_id)
            )
            if sub_obj and getattr(sub_obj, "pop_site_id", None):
                pop_nas = (
                    db.query(NasDevice)
                    .filter(NasDevice.pop_site_id == sub_obj.pop_site_id)
                    .filter(NasDevice.is_active.is_(True))
                    .order_by(NasDevice.name)
                    .first()
                )
                if pop_nas:
                    provisioning_nas_device_id = str(pop_nas.id)
                    subscription["provisioning_nas_device_id"] = (
                        provisioning_nas_device_id
                    )
                    logger.debug(
                        "Auto-selected NAS %s from subscriber POP site %s",
                        pop_nas.name,
                        sub_obj.pop_site_id,
                    )
        except Exception:
            logger.debug(
                "POP-based NAS auto-select failed for subscriber %s",
                subscriber_id,
                exc_info=True,
            )

    if provisioning_nas_device_id:
        try:
            selected_router_label = _nas_device_label(
                catalog_service.nas_devices.get(db, provisioning_nas_device_id)
            )
        except Exception:
            logger.warning(
                "NAS device lookup failed for %s",
                provisioning_nas_device_id,
                exc_info=True,
            )
            selected_router_label = ""
    apply_generated_service_credentials(db, subscription)
    if not subscription.get("ipv4_method"):
        subscription["ipv4_method"] = "permanent_static"
    if not isinstance(subscription.get("ipv4_block_ids"), list):
        subscription["ipv4_block_ids"] = []
    if not isinstance(subscription.get("ipv4_addresses"), list):
        subscription["ipv4_addresses"] = []
    if not subscription.get("ip_addon_quantity"):
        subscription["ip_addon_quantity"] = "1"
    if not isinstance(subscription.get("additional_route_cidrs"), list):
        subscription["additional_route_cidrs"] = [""]
    if not isinstance(subscription.get("additional_route_metrics"), list):
        subscription["additional_route_metrics"] = [""]
    additional_route_cidrs = cast(
        list[object],
        subscription.get("additional_route_cidrs", []),
    )
    additional_route_metrics = cast(
        list[object],
        subscription.get("additional_route_metrics", []),
    )
    initial_route_rows = initial_route_rows_for_form(
        db,
        cidrs=[str(value or "") for value in additional_route_cidrs],
        metrics=[str(value or "") for value in additional_route_metrics],
    )

    ip_addon_options = _public_ip_addon_options(db)
    ip_addon_options_json = json.dumps(
        [
            {
                "id": str(add_on.id),
                "name": str(add_on.name or ""),
                "prefix": int(add_on.ip_prefix_length)
                if add_on.ip_prefix_length is not None
                else None,
            }
            for add_on in ip_addon_options
        ]
    )

    context: dict[str, object] = {
        "subscription": subscription,
        "accounts": accounts,
        "offers": offers,
        "offer_options": _offer_options(db, list(offers), include_prices=False),
        "nas_devices": nas_devices,
        "router_devices": nas_devices,
        "ipv4_pools": ipv4_pools,
        "ipv4_blocks": block_options,
        "ipv4_blocks_json": json.dumps(block_options),
        "route_range_options": route_range_options,
        "route_range_options_json": json.dumps(route_range_options),
        "initial_route_rows_json": json.dumps(initial_route_rows),
        "ip_addon_options": ip_addon_options,
        "ip_addon_options_json": ip_addon_options_json,
        "radius_profiles": radius_profiles,
        "subscription_statuses": [item.value for item in SubscriptionStatus],
        "billing_modes": [item.value for item in BillingMode],
        "contract_terms": [item.value for item in ContractTerm],
        "action_url": "/admin/catalog/subscriptions",
        "subscriber_label": subscriber_label,
        "selected_router_label": selected_router_label,
        "billing_mode_help_text": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_help_text"
        )
        or "Overrides tariff default.",
        "billing_mode_prepaid_notice": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_prepaid_notice"
        )
        or "Balance enforcement applies.",
        "billing_mode_postpaid_notice": settings_spec.resolve_value(
            db, SettingDomain.catalog, "billing_mode_postpaid_notice"
        )
        or "This subscription follows dunning steps.",
        "current_service_login": getattr(current_credential, "username", "")
        if current_credential
        else "",
        "current_service_password": current_password or "",
        "credential_targets": credential_targets or {"email": [], "sms": []},
        "commercial_policy": {"rows": []},
    }
    if subscriber_id:
        policy_subject = _subscription_policy_subject(
            db,
            subscription,
            default_billing_mode=str(default_billing_mode),
        )
        if policy_subject is not None:
            context["commercial_policy"] = _subscription_commercial_policy(
                db,
                policy_subject,
            )
    if error:
        context["error"] = error
    return context


def subscriptions_list_page_data(
    db: Session,
    *,
    status: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, object]:
    """Build page data for the subscriptions list route."""
    offset = (page - 1) * per_page
    subscriptions = catalog_service.subscriptions.list(
        db=db,
        subscriber_id=None,
        offer_id=None,
        status=status if status else None,
        order_by="created_at",
        order_dir="desc",
        limit=per_page,
        offset=offset,
    )

    count_stmt = select(func.count(Subscription.id))
    if status:
        count_stmt = count_stmt.where(Subscription.status == status)
    total: int = db.scalar(count_stmt) or 0
    total_pages = (total + per_page - 1) // per_page if total else 1

    # Load active offers for bulk plan change modal
    offers = catalog_service.offers.list(
        db=db,
        service_type=None,
        access_type=None,
        status="active",
        is_active=True,
        order_by="name",
        order_dir="asc",
        limit=200,
        offset=0,
    )

    return {
        "subscriptions": subscriptions,
        "offers": offers,
        "offer_options": _offer_options(db, list(offers), include_prices=False),
        "status": status,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


def bulk_update_status(
    db: Session,
    subscription_ids_csv: str,
    target_status: SubscriptionStatus,
    allowed_from: list[SubscriptionStatus],
    request: object,
    actor_id: str | None,
) -> dict[str, Any]:
    """Compatibility adapter for canonical lifecycle command execution."""
    from app.services.subscription_lifecycle import SubscriptionCommandKind
    from app.services.subscription_lifecycle_commands import (
        execute_subscription_command_batch,
    )

    action_labels = {
        SubscriptionStatus.active: "activate",
        SubscriptionStatus.suspended: "suspend",
        SubscriptionStatus.canceled: "cancel",
    }
    action = action_labels.get(target_status)
    if action is None:
        raise ValueError(f"Unsupported bulk lifecycle status {target_status.value}")

    command_kind_by_status: dict[SubscriptionStatus, SubscriptionCommandKind] = {}
    for status in allowed_from:
        if target_status == SubscriptionStatus.active:
            if status == SubscriptionStatus.pending:
                command_kind_by_status[status] = SubscriptionCommandKind.activate
            elif status in {
                SubscriptionStatus.blocked,
                SubscriptionStatus.suspended,
                SubscriptionStatus.stopped,
                SubscriptionStatus.disabled,
            }:
                command_kind_by_status[status] = SubscriptionCommandKind.restore
        elif target_status == SubscriptionStatus.suspended:
            command_kind_by_status[status] = SubscriptionCommandKind.suspend
        elif target_status == SubscriptionStatus.canceled:
            command_kind_by_status[status] = SubscriptionCommandKind.cancel

    result = execute_subscription_command_batch(
        db,
        subscription_ids_csv.split(","),
        command_kind_by_status=command_kind_by_status,
        source=f"admin:catalog_bulk:{action}:{actor_id or 'system'}",
        idempotency_key=_bulk_lifecycle_operation_key(request, action=action),
        actor_id=actor_id,
        actor_type=AuditActorType.user if actor_id else AuditActorType.system,
        reason=f"Bulk {action} requested from the admin catalog",
    )
    return result.as_dict()


def bulk_change_plan(
    db: Session,
    subscription_ids_csv: str,
    target_offer_id: str,
    request: object,
    actor_id: str | None,
    effective_timing: str = "instant",
    include_suspended: bool = False,
    preview_fingerprint: str | None = None,
    preview_effective_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Compatibility adapter for canonical plan-change command execution.

    Only changes active subscriptions by default; when ``include_suspended`` is
    true, suspended subscriptions are eligible too. Returns ``{changed,
    skipped_ids, failed_ids}`` so callers can surface partial success.

    ``effective_timing`` selects when the change lands:

    - ``instant`` (default): accept one owner-previewed change and execute it
      through the subscription lifecycle command owner.
    - ``next_cycle``: record an approved scheduled change effective at each
      subscription's next billing date; the applier task swaps the offer at the
      boundary with no immediate financial transaction. This is the only bulk
      mode until a batch owner can preview each subscription separately.
    """
    from app.models.catalog import CatalogOffer
    from app.services.prepaid_plan_changes import resolve_prepaid_plan_change
    from app.services.subscription_lifecycle import (
        SubscriptionCommandKind,
        SubscriptionEffectiveTiming,
    )
    from app.services.subscription_lifecycle_commands import (
        execute_subscription_command_batch,
    )

    target_offer = db.get(CatalogOffer, target_offer_id)
    if not target_offer:
        raise ValueError("Target offer not found")

    if effective_timing not in ("instant", "next_cycle"):
        raise ValueError("Invalid effective_timing")
    subscription_ids = [
        value.strip() for value in subscription_ids_csv.split(",") if value.strip()
    ]
    if effective_timing == "instant":
        if len(subscription_ids) != 1:
            raise ValueError(
                "Immediate plan changes require one preview per subscription"
            )
        if not (preview_fingerprint or "").strip():
            raise ValueError("Preview the immediate plan change before confirming it")
        if not (idempotency_key or "").strip():
            raise ValueError("Plan-change idempotency key is required")

        subscription = catalog_service.subscriptions.get(db, subscription_ids[0])
        decision = resolve_prepaid_plan_change(
            db,
            subscription,
            target_offer_id,
            effective_at=preview_effective_at,
        )
        if decision.fingerprint != preview_fingerprint:
            raise ValueError("Financial state changed after preview; preview again")
    allowed_from = {SubscriptionStatus.active}
    if include_suspended:
        allowed_from.add(SubscriptionStatus.suspended)
    timing = (
        SubscriptionEffectiveTiming.immediate
        if effective_timing == "instant"
        else SubscriptionEffectiveTiming.next_cycle
    )
    result = execute_subscription_command_batch(
        db,
        subscription_ids,
        command_kind_by_status=dict.fromkeys(
            allowed_from, SubscriptionCommandKind.change_plan
        ),
        source=f"admin:catalog_bulk:change_plan:{actor_id or 'system'}",
        idempotency_key=(
            str(idempotency_key)
            if effective_timing == "instant"
            else _bulk_lifecycle_operation_key(request, action="change_plan")
        ),
        actor_id=actor_id,
        actor_type=AuditActorType.user if actor_id else AuditActorType.system,
        reason="Bulk plan change requested from the admin catalog",
        target_offer_id=target_offer_id,
        effective_timing=timing,
        effective_at=(preview_effective_at if effective_timing == "instant" else None),
    )
    return result.as_dict()


def _bulk_lifecycle_operation_key(request: object, *, action: str) -> str:
    headers = getattr(request, "headers", None)
    supplied = headers.get("Idempotency-Key") if headers is not None else None
    state = getattr(request, "state", None)
    request_id = getattr(state, "request_id", None) if state is not None else None
    root = str(supplied or request_id or uuid4())
    return f"admin-catalog-bulk:{action}:{root}"


def force_subscription_reauth(
    db: Session,
    subscription_id: str,
    request: object,
    actor_id: str | None,
) -> dict[str, object]:
    """Refresh RADIUS state and disconnect active PPPoE sessions for one sub."""
    subscription = catalog_service.subscriptions.get(db, subscription_id)
    subscriber = subscription.subscriber or db.get(
        Subscriber, subscription.subscriber_id
    )
    nas_device = subscription.provisioning_nas_device
    nas_ip = None
    if nas_device is not None:
        nas_ip = (
            getattr(nas_device, "nas_ip", None)
            or getattr(nas_device, "management_ip", None)
            or getattr(nas_device, "ip_address", None)
        )
    metadata = {
        "reason": "manual_force_reauth",
        "subscription_id": str(subscription.id),
        "subscriber_id": str(subscription.subscriber_id),
        "customer_name": (
            getattr(subscriber, "display_name", None) if subscriber else None
        ),
        "customer_account_number": (
            getattr(subscriber, "account_number", None) if subscriber else None
        ),
        "nas_device_id": str(subscription.provisioning_nas_device_id)
        if subscription.provisioning_nas_device_id
        else None,
        "nas_name": getattr(nas_device, "name", None) if nas_device else None,
        "nas_ip": nas_ip,
        "login": subscription.login,
    }
    request_id = getattr(getattr(request, "state", None), "request_id", None)
    try:
        from app.services.radius import reconcile_subscription_connectivity

        reconcile_subscription_connectivity(
            db, str(subscription.id)
        ).require_projected()
        metadata["radius_reconciled"] = True
    except Exception:
        logger.warning(
            "RADIUS reconcile failed before manual force reauth for %s",
            subscription.id,
            exc_info=True,
        )
        metadata["radius_reconciled"] = False

    from app.services.enforcement import disconnect_subscription_sessions

    kicked_count = disconnect_subscription_sessions(
        db, str(subscription.id), reason="manual_force_reauth"
    )
    metadata["sessions_disconnected"] = kicked_count
    record_audit_event(
        db,
        action="force_reauth",
        entity_type="subscription",
        entity_id=str(subscription.id),
        actor_id=actor_id,
        metadata=metadata,
        request_id=request_id,
    )
    return {
        "subscription_id": str(subscription.id),
        "sessions_disconnected": kicked_count,
        "metadata": metadata,
    }


def create_subscription_with_audit(
    db: Session,
    payload_data: dict[str, object],
    form: FormData,
    request: object,
    actor_id: str | None,
) -> Subscription:
    """Create subscription with quick-options, invoice, welcome email, and audit.

    Returns the created subscription ORM object.
    """
    activate_immediately, generate_invoice, send_welcome_email = (
        apply_create_quick_options(payload_data, form)
    )
    created = create_subscription(db, payload_data)

    subscriber = db.get(Subscriber, created.subscriber_id)
    if subscriber:
        pppoe_auto_generate = _pppoe_auto_generate_enabled(db)
        existing_credential = _current_access_credential(db, created.subscriber_id)
        if not existing_credential and (
            activate_immediately
            or str(getattr(created, "status", "") or "").strip().lower() == "active"
        ):
            try:
                from app.services.pppoe_credentials import (
                    auto_generate_pppoe_credential,
                )

                auto_generate_pppoe_credential(
                    db,
                    str(created.subscriber_id),
                    radius_profile_id=str(created.radius_profile_id)
                    if created.radius_profile_id
                    else None,
                )
                existing_credential = _current_access_credential(
                    db, created.subscriber_id
                )
            except Exception:
                logger.warning(
                    "PPPoE credential auto-generation failed during web subscription create for %s",
                    created.id,
                    exc_info=True,
                )
        generated_login = _generated_service_login(subscriber)
        generated_password = _generated_service_password(subscriber)
        explicit_login = str(form.get("login") or "").strip()
        selected_login = (
            explicit_login
            or str(getattr(existing_credential, "username", "") or "").strip()
            or str(created.login or "").strip()
            or ("" if pppoe_auto_generate else generated_login)
        )
        explicit_password = str(form.get("service_password") or "").strip()
        selected_password = explicit_password or (
            None
            if pppoe_auto_generate
            or (
                existing_credential
                and getattr(existing_credential, "secret_hash", None)
            )
            else generated_password
        )
        update_payload: dict[str, object] = {
            "service_status_raw": "permanent_static"
            if str(form.get("ipv4_method") or "").strip().lower() == "permanent_static"
            else "dynamic",
        }
        if selected_login:
            update_payload["login"] = selected_login
        created = update_subscription(db, str(created.id), update_payload)

    selected_block_ids = [
        str(value).strip()
        for value in form.getlist("ipv4_block_ids")
        if str(value).strip()
    ]
    selected_ips = [
        str(value).strip()
        for value in form.getlist("ipv4_addresses")
        if str(value).strip()
    ]
    additional_route_cidrs = [
        str(value).strip() for value in form.getlist("additional_route_cidrs")
    ]
    additional_route_metrics = [
        str(value).strip() for value in form.getlist("additional_route_metrics")
    ]
    selected_ip_addon_id = str(form.get("ip_addon_id") or "").strip()
    selected_ip_addon_quantity = str(form.get("ip_addon_quantity") or "1")
    desired_additional_routes = normalize_additional_routes(
        additional_route_cidrs,
        additional_route_metrics,
    )
    allocated_ips = _allocate_ipv4_assignments_for_subscription(
        db,
        subscription_obj=created,
        block_ids=selected_block_ids,
        selected_ips=selected_ips,
    )
    if allocated_ips:
        created = update_subscription(
            db,
            str(created.id),
            {
                "ipv4_address": allocated_ips[0],
            },
        )
    additional_routes = sync_additional_routes_for_subscription(
        db,
        subscription_obj=created,
        cidrs=additional_route_cidrs,
        metrics=additional_route_metrics,
        add_on_id=selected_ip_addon_id,
        quantity=selected_ip_addon_quantity,
    )
    route_fields_supplied = bool(additional_route_cidrs)
    if desired_additional_routes or route_fields_supplied:
        public_ip_addon_id = sync_public_ip_addon_for_subscription(
            db,
            subscription_obj=created,
            add_on_id=selected_ip_addon_id if desired_additional_routes else "",
            quantity=selected_ip_addon_quantity,
        )
    else:
        public_ip_addon_id = sync_public_ip_addon_for_subscription(
            db,
            subscription_obj=created,
            add_on_id=selected_ip_addon_id,
            quantity=selected_ip_addon_quantity,
        )

    if subscriber:
        try:
            _upsert_access_credential(
                db,
                subscriber_id=created.subscriber_id,
                username=selected_login,
                plain_password=selected_password,
                radius_profile_id=str(created.radius_profile_id)
                if created.radius_profile_id
                else None,
            )
        except ValueError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.warning(
                "Access credential sync failed during subscription create for %s",
                created.id,
                exc_info=True,
            )
            raise ValueError(
                "Unable to update the service credential. Please check the service "
                "login and try again."
            ) from exc
        else:
            _reconcile_active_subscription_after_credential_sync(db, str(created.id))
    elif additional_routes:
        _reconcile_active_subscription_after_credential_sync(db, str(created.id))

    record_audit_event(
        db,
        action="create",
        entity_type="subscription",
        entity_id=str(created.id),
        actor_id=actor_id,
        metadata={
            "offer_id": str(created.offer_id),
            "account_id": str(created.subscriber_id),
            "generated_login": created.login,
            "service_password_changed": bool(
                str(form.get("service_password") or "").strip()
            ),
            "ipv4_method": str(form.get("ipv4_method") or "permanent_static"),
            "allocated_ipv4_count": len(allocated_ips),
            "allocated_ipv4_addresses": allocated_ips,
            "public_ip_addon_id": public_ip_addon_id,
            "additional_route_count": len(additional_routes),
            "additional_routes": additional_routes,
        },
    )

    if generate_invoice and created.subscriber_id:
        create_invoice_for_subscription(db, created)

    if send_welcome_email and created.subscriber_id:
        send_welcome_email_for_subscription(db, created)

    return created


def update_subscription_with_audit(
    db: Session,
    subscription_id: str,
    payload_data: dict[str, object],
    service_password: str | None,
    ipv4_block_ids: list[str] | None,
    ipv4_addresses: list[str] | None,
    request: object,
    actor_id: str | None,
    additional_route_cidrs: list[str] | None = None,
    additional_route_metrics: list[str] | None = None,
    ip_addon_id: str | None = None,
    ip_addon_quantity: str | int | None = None,
    ipv4_assignment_submitted: bool = False,
) -> object:
    """Update subscription, compute diff, and log audit.

    Returns the updated subscription ORM object.
    """
    before = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
    manage_ipv4_assignment = ipv4_assignment_submitted or bool(
        ipv4_block_ids or ipv4_addresses
    )
    if manage_ipv4_assignment:
        raise ValueError(
            "Use Replace service IPv4 so IPAM and RADIUS change without "
            "entering subscription or billing updates."
        )
    # Generic edits own technical metadata only; lifecycle and commercial facts
    # change through subscription_lifecycle_commands.
    payload_data = dict(payload_data)
    for protected_field in (
        "subscriber_id",
        "account_id",
        "offer_id",
        "billing_mode",
        "status",
        "start_at",
        "end_at",
        "next_billing_at",
        "canceled_at",
        "cancel_reason",
    ):
        payload_data.pop(protected_field, None)
    update_subscription(db, subscription_id, payload_data)
    after = catalog_service.subscriptions.get(db=db, subscription_id=subscription_id)
    allocated_ips: list[str] = []
    additional_routes: list[str] = []
    desired_additional_routes: list[tuple[str, int, int]] = []
    additional_routes_supplied = additional_route_cidrs is not None
    if additional_route_cidrs is not None:
        desired_additional_routes = normalize_additional_routes(
            additional_route_cidrs,
            additional_route_metrics,
        )
        additional_routes = sync_additional_routes_for_subscription(
            db,
            subscription_obj=after,
            cidrs=additional_route_cidrs,
            metrics=additional_route_metrics,
            add_on_id=ip_addon_id,
            quantity=ip_addon_quantity,
        )
    public_ip_addon_id: str | None = None
    public_ip_addon_supplied = ip_addon_id is not None
    if additional_routes_supplied:
        public_ip_addon_id = sync_public_ip_addon_for_subscription(
            db,
            subscription_obj=after,
            add_on_id=ip_addon_id if desired_additional_routes else "",
            quantity=ip_addon_quantity,
        )
    elif ip_addon_id is not None:
        public_ip_addon_id = sync_public_ip_addon_for_subscription(
            db,
            subscription_obj=after,
            add_on_id=ip_addon_id,
            quantity=ip_addon_quantity,
        )
    entered_password = str(service_password or "").strip()
    try:
        _upsert_access_credential(
            db,
            subscriber_id=after.subscriber_id,
            username=str(after.login or ""),
            plain_password=entered_password or None,
            radius_profile_id=str(after.radius_profile_id)
            if after.radius_profile_id
            else None,
        )
    except ValueError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.warning(
            "Access credential sync failed during subscription update for %s",
            subscription_id,
            exc_info=True,
        )
        raise ValueError(
            "Unable to update the service credential. Please check the service "
            "login and try again."
        ) from exc
    else:
        _reconcile_active_subscription_after_credential_sync(db, subscription_id)
    if additional_routes_supplied or public_ip_addon_supplied:
        _refresh_subscription_radius_session(
            db,
            subscription_id,
            reason="additional_ip_update",
        )
    metadata_payload = build_changes_metadata(before, after) or {}
    metadata_payload["service_password_changed"] = bool(entered_password)
    if allocated_ips:
        metadata_payload["allocated_ipv4_count"] = len(allocated_ips)
        metadata_payload["allocated_ipv4_addresses"] = allocated_ips
    metadata_payload["public_ip_addon_id"] = public_ip_addon_id
    metadata_payload["additional_route_count"] = len(additional_routes)
    metadata_payload["additional_routes"] = additional_routes

    record_audit_event(
        db,
        action="update",
        entity_type="subscription",
        entity_id=str(subscription_id),
        actor_id=actor_id,
        metadata=metadata_payload,
    )

    return after
