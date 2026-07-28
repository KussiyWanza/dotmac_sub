from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas.crm_provisioning import CRMSubscriberProvisionRequest
from app.services import crm_api
from app.services.crm_subscriber_provisioning import (
    CRM_PROVISIONING_SCOPE,
    ProvisionCRMSubscriberCommand,
    provision_crm_subscriber,
)
from app.services.domain_errors import DomainError
from app.services.owner_commands import CommandContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/crm", tags=["crm-api"])

CRM_MAX_PER_PAGE = 500


def _error(
    status_code: int, message: str, errors: dict[str, list[str]] | None = None
) -> NoReturn:
    detail: dict[str, Any] = {"message": message}
    if errors:
        detail["errors"] = errors
    raise HTTPException(status_code=status_code, detail=detail)


CRM_INTEGRATION_PERMISSION = "integration:crm"


def require_crm_service_auth(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
    db: Session = Depends(get_db),
) -> None:
    """Require a scoped, rotatable Sub API key from the CRM caller."""
    if isinstance(x_api_key, str) and x_api_key:
        from app.services.auth_dependencies import _api_key_principal, has_permission

        auth = _api_key_principal(db, x_api_key, request)
        if auth is not None and has_permission(auth, db, CRM_INTEGRATION_PERMISSION):
            return
        _error(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or insufficiently scoped API key.",
        )
    _error(status.HTTP_401_UNAUTHORIZED, "CRM API requires a scoped X-Api-Key.")


def _envelope(data: Any, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"data": data}
    if meta is not None:
        payload["meta"] = meta
    return payload


def _query_value(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if value not in ("", None) else None


def _pagination(
    request: Request, *, max_per_page: int = CRM_MAX_PER_PAGE
) -> tuple[int, int, dict[str, Any]]:
    errors: dict[str, list[str]] = {}

    def parse_int(
        name: str,
        default: int,
        *,
        min_value: int,
        max_value: int,
        aliases: tuple[str, ...] = (),
    ) -> int:
        source_name = name
        raw = _query_value(request, name)
        if raw is None:
            for alias in aliases:
                raw = _query_value(request, alias)
                if raw is not None:
                    source_name = alias
                    break
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            errors.setdefault(source_name, []).append("Must be an integer.")
            return default
        if value < min_value:
            errors.setdefault(source_name, []).append(
                f"Must be greater than or equal to {min_value}."
            )
        if value > max_value:
            errors.setdefault(source_name, []).append(
                f"Must be less than or equal to {max_value}."
            )
        return value

    page = parse_int("page", 1, min_value=1, max_value=1_000_000)
    per_page = parse_int(
        "per_page",
        100,
        min_value=1,
        max_value=max_per_page,
        aliases=("limit",),
    )
    if errors:
        _error(status.HTTP_400_BAD_REQUEST, "Invalid query parameters.", errors)
    return page, per_page, {"page": page, "per_page": per_page}


def _finish_read_response(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    bind = db.get_bind()
    if (
        bind.dialect.name == "postgresql"
        and db.in_transaction()
        and not db.in_nested_transaction()
    ):
        db.rollback()
    return payload


def _include_values(request: Request, allowed: set[str]) -> set[str]:
    raw = _query_value(request, "include")
    if raw is None:
        return set()
    values = {item.strip() for item in raw.split(",") if item.strip()}
    unknown = values - allowed
    if unknown:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {
                "include": [
                    f"Unsupported include value(s): {', '.join(sorted(unknown))}."
                ]
            },
        )
    return values


def _parse_date_filter(request: Request, name: str) -> datetime | None:
    raw = _query_value(request, name)
    if raw is None:
        return None
    try:
        if "T" in raw:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            boundary = time.max if name == "date_to" else time.min
            value = datetime.combine(datetime.fromisoformat(raw).date(), boundary)
    except ValueError:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {name: ["Must be an ISO 8601 date or datetime."]},
        )
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_customer_filter(request: Request) -> Any:
    raw = _query_value(request, "customer_id")
    if raw is None:
        return None
    parsed = crm_api.coerce_subscriber_id(raw)
    if parsed is None:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {"customer_id": ["Must be a valid subscriber UUID."]},
        )
    return parsed


def _subscriber_or_404(db: Session, subscriber_id: str):
    subscriber = crm_api.get_subscriber_or_none(db, subscriber_id)
    if subscriber is None:
        _error(status.HTTP_404_NOT_FOUND, "Subscriber not found.")
    return subscriber


@router.get("/ping", dependencies=[Depends(require_crm_service_auth)])
def ping() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/subscribers", dependencies=[Depends(require_crm_service_auth)])
def list_subscribers(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    includes = _include_values(
        request, {"services", "billing", "session_state", "last_seen"}
    )
    subscribers, total = crm_api.list_subscribers(db, page=page, per_page=per_page)
    subscriber_ids = [item.id for item in subscribers]
    services = (
        crm_api.services_by_subscriber(db, subscriber_ids)
        if "services" in includes
        else {}
    )
    billing = (
        crm_api.billing_by_subscriber(db, subscribers) if "billing" in includes else {}
    )
    sessions = crm_api.latest_session_by_subscriber(db, subscriber_ids)
    metadata = crm_api.subscriber_payload_metadata(db, subscribers)
    data = []
    for subscriber in subscribers:
        kwargs: dict[str, Any] = {}
        if "services" in includes:
            kwargs["services"] = services.get(subscriber.id, [])
        if "billing" in includes:
            kwargs["billing"] = billing.get(subscriber.id)
        data.append(
            crm_api.subscriber_payload(
                db,
                subscriber,
                session=sessions.get(subscriber.id),
                include_session_state="session_state" in includes,
                metadata=metadata.get(subscriber.id),
                **kwargs,
            )
        )
    return _finish_read_response(db, _envelope(data, {**meta, "total": total}))


@router.post(
    "/subscribers",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_crm_service_auth)],
)
def provision_subscriber(
    request: Request,
    payload: CRMSubscriberProvisionRequest,
    response: Response,
    idempotency_key: str = Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=120,
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Explicitly provision a canonical Sub customer for one CRM identity."""

    auth = getattr(request.state, "auth", {})
    principal_id = str(auth.get("principal_id") or "").strip()
    if not principal_id:
        _error(
            status.HTTP_401_UNAUTHORIZED,
            "CRM API key principal is unavailable.",
        )

    command_id = uuid.uuid4()
    command = ProvisionCRMSubscriberCommand(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=f"api_key:{principal_id}",
            scope=CRM_PROVISIONING_SCOPE,
            reason="CRM sales customer provisioning",
            idempotency_key=idempotency_key,
        ),
        payload=payload,
    )
    try:
        result = provision_crm_subscriber(db, command)
    except DomainError as exc:
        conflict_codes = {
            "customer.crm_subscriber_provisioning.ambiguous_identity",
            "customer.crm_subscriber_provisioning.active_caller_transaction",
            "customer.crm_subscriber_provisioning.idempotency_conflict",
            "customer.crm_subscriber_provisioning.identity_conflict",
        }
        client_error_codes = {
            "customer.crm_subscriber_provisioning.invalid_command",
            "customer.crm_subscriber_provisioning.missing_idempotency_key",
        }
        if exc.code in conflict_codes:
            error_status = status.HTTP_409_CONFLICT
        elif exc.code in client_error_codes:
            error_status = status.HTTP_400_BAD_REQUEST
        else:
            logger.error(
                "crm_subscriber_provisioning_contract_failure",
                extra={"error_code": exc.code},
            )
            error_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        _error(
            error_status,
            exc.message,
            {str(exc.details.get("field") or "command"): [exc.code]},
        )
    if result.replayed or result.outcome == "reused":
        response.status_code = status.HTTP_200_OK
    return _envelope(
        {
            "id": str(result.subscriber_id),
            "subscriber_id": result.subscriber_number or str(result.subscriber_id),
            "subscriber_number": result.subscriber_number,
            "account_number": result.account_number,
            "outcome": result.outcome,
            "replayed": result.replayed,
            "command_id": str(result.command_id),
            "correlation_id": str(result.correlation_id),
        }
    )


@router.get("/subscribers/search", dependencies=[Depends(require_crm_service_auth)])
def search_subscribers(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    q = _query_value(request, "q")
    if not q:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {"q": ["Search query is required."]},
        )
    assert q is not None
    page, per_page, meta = _pagination(request)
    subscribers, total = crm_api.search_subscribers(db, q, page=page, per_page=per_page)
    sessions = crm_api.latest_session_by_subscriber(
        db, [item.id for item in subscribers]
    )
    metadata = crm_api.subscriber_payload_metadata(db, subscribers)
    data = [
        crm_api.subscriber_payload(
            db,
            subscriber,
            session=sessions.get(subscriber.id),
            metadata=metadata.get(subscriber.id),
        )
        for subscriber in subscribers
    ]
    return _finish_read_response(db, _envelope(data, {**meta, "total": total}))


@router.get("/subscribers/online", dependencies=[Depends(require_crm_service_auth)])
def online_subscribers(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    # Online state is inferred from open, fresh RADIUS accounting sessions.
    # It is not an authoritative real-time device poll; subscribers whose NAS
    # has not sent interim accounting inside ONLINE_FRESH_SECONDS are excluded.
    page, per_page, meta = _pagination(request)
    data, total = crm_api.online_subscribers(db, page=page, per_page=per_page)
    return _finish_read_response(db, _envelope(data, {**meta, "total": total}))


@router.get(
    "/subscribers/{subscriber_id}", dependencies=[Depends(require_crm_service_auth)]
)
def subscriber_detail(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    session = crm_api.latest_session_by_subscriber(db, [subscriber.id]).get(
        subscriber.id
    )
    return _envelope(
        crm_api.subscriber_payload(
            db,
            subscriber,
            services=crm_api.subscriber_services(db, subscriber.id),
            billing=crm_api.billing_summary(db, subscriber),
            session=session,
            include_session_state=True,
        )
    )


@router.get(
    "/subscribers/{subscriber_id}/services",
    dependencies=[Depends(require_crm_service_auth)],
)
def subscriber_services(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.subscriber_services(db, subscriber.id))


@router.get(
    "/subscribers/{subscriber_id}/billing",
    dependencies=[Depends(require_crm_service_auth)],
)
def subscriber_billing(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.billing_summary(db, subscriber))


@router.get(
    "/subscribers/{subscriber_id}/sessions",
    dependencies=[Depends(require_crm_service_auth)],
)
def subscriber_sessions(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.session_rows(db, subscriber.id))


@router.get(
    "/subscribers/{subscriber_id}/statistics",
    dependencies=[Depends(require_crm_service_auth)],
)
def subscriber_statistics(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.session_rows(db, subscriber.id))


@router.patch(
    "/subscribers/{subscriber_id}/status",
    dependencies=[Depends(require_crm_service_auth)],
)
def update_subscriber_status(
    subscriber_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    x_crm_actor: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    requested_status = str(payload.get("status") or "").strip()
    reason = str(payload.get("reason") or "").strip() or None
    source = str(payload.get("source") or "").strip() or None
    errors: dict[str, list[str]] = {}
    if requested_status != "disabled":
        errors["status"] = ["Only 'disabled' is supported."]
    if not reason:
        errors["reason"] = ["Reason is required."]
    if source != "crm":
        errors["source"] = ["Source must be 'crm'."]
    if errors:
        _error(status.HTTP_400_BAD_REQUEST, "Invalid request body.", errors)

    return _envelope(
        crm_api.update_subscriber_status_from_crm(
            db,
            subscriber,
            requested_status=requested_status,
            actor=x_crm_actor,
            source=source,
            reason=reason,
        )
    )


@router.get("/locations", dependencies=[Depends(require_crm_service_auth)])
def locations(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    data, total = crm_api.locations(db, page=page, per_page=per_page)
    return _finish_read_response(db, _envelope(data, {**meta, "total": total}))


@router.get("/billing-risk-source", dependencies=[Depends(require_crm_service_auth)])
def billing_risk_source(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    rows, total = crm_api.billing_risk_rows(db, page=page, per_page=per_page)
    return _finish_read_response(db, _envelope(rows, {**meta, "total": total}))


@router.post(
    "/payments",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_crm_service_auth)],
    tags=["payments"],
)
def record_crm_payment(
    payload: dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record a payment a customer made in the CRM (installation / subscription)
    into the ledger so it settles the invoice and shows in the customer portal.

    Body: ``{subscriber_id, amount, external_ref, paid_at?, memo?,
    invoice_external_ref?, currency?}``. Idempotent on ``external_ref``.
    """
    errors: dict[str, list[str]] = {}
    subscriber_id = str(payload.get("subscriber_id") or "").strip()
    if not subscriber_id:
        errors.setdefault("subscriber_id", []).append("Required.")
    external_ref = str(payload.get("external_ref") or "").strip()
    if not external_ref:
        errors.setdefault("external_ref", []).append("Required.")
    amount_raw = payload.get("amount")
    try:
        amount = Decimal(str(amount_raw))
    except (InvalidOperation, TypeError, ValueError):
        errors.setdefault("amount", []).append("Must be a number.")
        amount = Decimal("0")
    else:
        if amount <= 0:
            errors.setdefault("amount", []).append("Must be greater than 0.")
    if errors:
        _error(status.HTTP_400_BAD_REQUEST, "Invalid payment payload.", errors)

    paid_at_raw = payload.get("paid_at")
    paid_at = None
    if paid_at_raw:
        try:
            paid_at = datetime.fromisoformat(str(paid_at_raw).replace("Z", "+00:00"))
        except ValueError:
            _error(
                status.HTTP_400_BAD_REQUEST,
                "Invalid payment payload.",
                {"paid_at": ["Must be ISO 8601."]},
            )

    invoice_external_ref = payload.get("invoice_external_ref")
    invoice_external_ref = (
        str(invoice_external_ref).strip()
        if invoice_external_ref not in (None, "")
        else None
    )

    try:
        payment = crm_api.record_external_payment(
            db,
            subscriber_id=subscriber_id,
            amount=amount,
            external_ref=external_ref,
            paid_at=paid_at,
            memo=payload.get("memo"),
            invoice_external_ref=invoice_external_ref,
            currency=str(payload.get("currency") or "NGN"),
        )
    except LookupError:
        _error(status.HTTP_404_NOT_FOUND, "Subscriber not found.")
    except ValueError as exc:
        _error(status.HTTP_400_BAD_REQUEST, str(exc))

    return _envelope(
        {
            "id": str(payment.id),
            "amount": str(payment.amount),
            "status": payment.status.value,
            "account_id": str(payment.account_id) if payment.account_id else None,
            "external_id": payment.external_id,
        }
    )


@router.post(
    "/subscriptions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_crm_service_auth)],
    tags=["subscriptions"],
)
def create_crm_subscription(
    payload: dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a subscription for a subscriber from a CRM sale (+ its first
    invoice). Body: ``{subscriber_id, offer_ref|offer_id|offer_code,
    external_ref, unit_price?, start_at?}``. Idempotent on ``external_ref``."""
    errors: dict[str, list[str]] = {}
    subscriber_id = str(payload.get("subscriber_id") or "").strip()
    if not subscriber_id:
        errors.setdefault("subscriber_id", []).append("Required.")
    offer_ref = str(
        payload.get("offer_ref")
        or payload.get("offer_id")
        or payload.get("offer_code")
        or ""
    ).strip()
    if not offer_ref:
        errors.setdefault("offer_ref", []).append("Required (offer id or code).")
    external_ref = str(payload.get("external_ref") or "").strip()
    if not external_ref:
        errors.setdefault("external_ref", []).append("Required.")
    if errors:
        _error(status.HTTP_400_BAD_REQUEST, "Invalid subscription payload.", errors)

    start_at = None
    start_raw = payload.get("start_at")
    if start_raw:
        try:
            start_at = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
        except ValueError:
            _error(
                status.HTTP_400_BAD_REQUEST,
                "Invalid subscription payload.",
                {"start_at": ["Must be ISO 8601."]},
            )

    try:
        result = crm_api.create_subscription(
            db,
            subscriber_id=subscriber_id,
            offer_ref=offer_ref,
            external_ref=external_ref,
            unit_price=payload.get("unit_price"),
            start_at=start_at,
        )
    except LookupError as exc:
        _error(status.HTTP_404_NOT_FOUND, str(exc).capitalize())

    subscription = result["subscription"]
    invoice = result["invoice"]
    return _envelope(
        {
            "subscription_id": str(subscription.id) if subscription else None,
            "invoice_id": str(invoice.id) if invoice else None,
            "status": subscription.status.value if subscription else None,
            "created": result["created"],
        }
    )


@router.get("/offers", dependencies=[Depends(require_crm_service_auth)])
def catalog_offers(
    q: str | None = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The plan catalog (offers + recurring price) for the CRM to pick from when
    quoting a subscription — sub owns the plans; the CRM reads them."""
    return _envelope(crm_api.list_catalog_offers(db, q=q, active_only=active_only))


@router.get("/infrastructure/assets", dependencies=[Depends(require_crm_service_auth)])
def infrastructure_assets(
    q: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Pickable infrastructure items — OLTs (Huawei/Ubiquiti), their PON ports,
    and basestations — for raising an infrastructure/outage ticket."""
    return _envelope(crm_api.list_infrastructure_assets(db, q=q))


@router.get("/ncc/subscribers", dependencies=[Depends(require_crm_service_auth)])
def ncc_subscriber_report(
    as_of: str | None = None,
    statuses: str | None = None,
    reseller_id: str | None = None,
    access_capacity_gbps: str | None = None,
    unutilized_capacity_mbps: str | None = None,
    points_of_presence: str | None = None,
    data_usage_tb: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The NCC quarterly Subscriber & Capacity aggregate, for the CRM's
    regulatory-pack aggregator. Same parameters as the admin report (period-end
    ``as_of``, comma-separated ``statuses``, optional ``reseller_id``, and the
    manual capacity figures)."""
    from app.services import ncc_subscriber_report as ncc

    params = ncc.parse_report_params(
        as_of=as_of,
        statuses=statuses,
        reseller_id=reseller_id,
        capacity={
            "access_capacity_gbps": access_capacity_gbps,
            "unutilized_capacity_mbps": unutilized_capacity_mbps,
            "points_of_presence": points_of_presence,
            "data_usage_tb": data_usage_tb,
        },
    )
    return _envelope(ncc.build_ncc_subscriber_report(db, params))


@router.get("/outages/impact", dependencies=[Depends(require_crm_service_auth)])
def outage_impact(
    node_id: str | None = None,
    basestation_id: str | None = None,
    olt_id: str | None = None,
    pon_port_id: str | None = None,
    fdh_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Subscribers affected by a failed infrastructure asset.

    Pass one of: ``node_id`` (a monitored NetworkDevice — switch/router, with
    LLDP downstream expansion), ``basestation_id`` (a PopSite), ``olt_id`` (all
    ONTs on an OLT), ``pon_port_id`` (only the ONTs on that PON port), or
    ``fdh_id`` (active subscriptions behind an FDH cabinet). OLT and PON-port
    resolution are vendor-agnostic (Huawei + Ubiquiti). The ``coverage`` block
    flags where the e2e chain is incomplete so the caller can fall back to manual
    selection.
    """
    from app.models.network import FdhCabinet
    from app.models.network_monitoring import NetworkDevice, PopSite

    if not any([node_id, basestation_id, olt_id, pon_port_id, fdh_id]):
        raise HTTPException(
            status_code=400,
            detail=(
                "One of node_id, basestation_id, olt_id, pon_port_id "
                "or fdh_id is required"
            ),
        )

    node = None
    if node_id:
        node = db.get(NetworkDevice, crm_api.coerce_subscriber_id(node_id))
        if node is None:
            raise HTTPException(status_code=404, detail="Network device not found")
    basestation = None
    if basestation_id:
        basestation = db.get(PopSite, crm_api.coerce_subscriber_id(basestation_id))
        if basestation is None:
            raise HTTPException(status_code=404, detail="Basestation not found")
    if fdh_id:
        fdh = db.get(FdhCabinet, crm_api.coerce_subscriber_id(fdh_id))
        if fdh is None:
            raise HTTPException(status_code=404, detail="FDH cabinet not found")

    return _envelope(
        crm_api.outage_impact(
            db,
            node=node,
            basestation=basestation,
            olt_id=crm_api.coerce_subscriber_id(olt_id) if olt_id else None,
            pon_port_id=crm_api.coerce_subscriber_id(pon_port_id)
            if pon_port_id
            else None,
            fdh_id=crm_api.coerce_subscriber_id(fdh_id) if fdh_id else None,
        )
    )


@router.get("/outages", dependencies=[Depends(require_crm_service_auth)])
def list_outages(
    status_filter: str | None = None,
    basestation_id: str | None = None,
    node_id: str | None = None,
    resolved_within_hours: int = 24,
    page: int = 1,
    per_page: int = 100,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Outage incidents for the CRM/mobile backend. The default "active" view is
    operator ``open`` plus debounced-real classifier incidents (``confirmed``/
    ``clearing``), plus anything resolved within ``resolved_within_hours``
    (default 24); ``suspected``/``discarded`` are excluded from the default. Each
    row carries scope (node/basestation/FDH + name), ``detection_source``
    (operator vs classifier), ``state``, ``status_presentation``, lifecycle
    timestamps, ``confidence`` and ``mttr_seconds``. ``status_filter`` narrows to
    a single lifecycle state, ``basestation_id`` and ``node_id`` narrow the
    scope."""
    from app.services.topology.outage import OUTAGE_STATUS_VALUES

    _valid_status = OUTAGE_STATUS_VALUES
    if status_filter is not None and status_filter not in _valid_status:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid query parameters.",
            {"status_filter": [f"Must be one of {', '.join(_valid_status)}."]},
        )
    page = max(1, page)
    per_page = max(1, min(per_page, 500))
    rows, total = crm_api.list_outage_incidents(
        db,
        status=status_filter,
        basestation_id=basestation_id,
        node_id=node_id,
        resolved_within_hours=max(0, min(resolved_within_hours, 24 * 30)),
        page=page,
        per_page=per_page,
    )
    return _envelope(rows, {"page": page, "per_page": per_page, "total": total})


@router.get("/outages/{incident_id}", dependencies=[Depends(require_crm_service_auth)])
def outage_detail(
    incident_id: str,
    limit: int = 200,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """One incident with the affected subscriptions (id, subscriber name,
    service address, status), capped at ``limit`` (max 500) with
    ``affected_total``/``affected_truncated`` for the full size. Membership is
    derived via the same topology resolvers as the declare-time snapshot."""
    row = crm_api.outage_incident_detail(db, incident_id, limit=max(1, min(limit, 500)))
    if row is None:
        _error(status.HTTP_404_NOT_FOUND, "Outage incident not found.")
    return _envelope(row)


@router.get("/service-extensions", dependencies=[Depends(require_crm_service_auth)])
def service_extensions(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    rows, total = crm_api.service_extension_rows(db, page=page, per_page=per_page)
    return _envelope(rows, {**meta, "total": total})


@router.get(
    "/service-extensions/{extension_id}",
    dependencies=[Depends(require_crm_service_auth)],
)
def service_extension_detail(
    extension_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    row = crm_api.service_extension_detail(db, extension_id)
    if row is None:
        _error(status.HTTP_404_NOT_FOUND, "Service extension not found.")
    return _envelope(row)


@router.get(
    "/subscribers/{subscriber_id}/service-extensions",
    dependencies=[Depends(require_crm_service_auth)],
)
def subscriber_service_extensions(
    subscriber_id: str, db: Session = Depends(get_db)
) -> dict[str, Any]:
    subscriber = _subscriber_or_404(db, subscriber_id)
    return _envelope(crm_api.service_extensions_for_subscriber(db, subscriber.id))


@router.get("/finance/transactions", dependencies=[Depends(require_crm_service_auth)])
def finance_transactions(
    request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    rows, total = crm_api.transaction_rows(
        db,
        customer_id=_parse_customer_filter(request),
        date_from=_parse_date_filter(request, "date_from"),
        date_to=_parse_date_filter(request, "date_to"),
        page=page,
        per_page=per_page,
    )
    return _envelope(rows, {**meta, "total": total})


@router.get("/finance/payments", dependencies=[Depends(require_crm_service_auth)])
def finance_payments(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    page, per_page, meta = _pagination(request)
    rows, total = crm_api.payment_rows(
        db,
        customer_id=_parse_customer_filter(request),
        date_from=_parse_date_filter(request, "date_from"),
        date_to=_parse_date_filter(request, "date_to"),
        page=page,
        per_page=per_page,
    )
    return _envelope(rows, {**meta, "total": total})


@router.post(
    "/credits",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_crm_service_auth)],
    tags=["credits"],
)
def create_crm_credit(
    payload: dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Issue a referral reward as account credit. Body:
    ``{subscriber_id, amount, external_ref, reason?, currency?}``. ``external_ref``
    is REQUIRED and is the idempotency key (a repeat call returns the existing
    entry). Individual subscribers only (reseller float wallets are never
    credited here).
    """
    errors: dict[str, list[str]] = {}
    subscriber_id = str(payload.get("subscriber_id") or "").strip()
    if not subscriber_id:
        errors.setdefault("subscriber_id", []).append("Required.")
    amount = Decimal("0")
    try:
        amount = Decimal(str(payload.get("amount")))
    except (InvalidOperation, TypeError, ValueError):
        errors.setdefault("amount", []).append("Must be a number.")
    else:
        if amount <= 0:
            errors.setdefault("amount", []).append("Must be greater than 0.")
    # external_ref is the idempotency key. It is required so a retry/redelivery
    # cannot issue the same account credit twice.
    external_ref = payload.get("external_ref")
    external_ref = str(external_ref).strip() if external_ref not in (None, "") else None
    if not external_ref:
        errors.setdefault("external_ref", []).append("Required (idempotency key).")
    if errors:
        _error(status.HTTP_400_BAD_REQUEST, "Invalid credit payload.", errors)

    currency = str(payload.get("currency") or "NGN").strip().upper() or "NGN"
    reason = payload.get("reason")
    reason = str(reason).strip() if reason not in (None, "") else None

    try:
        credit_note = crm_api.create_account_credit(
            db,
            subscriber_id=subscriber_id,
            amount=amount,
            reason=reason,
            external_ref=external_ref,
            currency=currency,
        )
    except LookupError:
        _error(status.HTTP_404_NOT_FOUND, "Subscriber not found.")

    return _envelope(
        {
            "id": str(credit_note.id),
            "account_id": str(credit_note.account_id),
            "amount": str(credit_note.total),
            "currency": credit_note.currency,
            "credit_number": credit_note.credit_number,
            "type": "account_credit",
        }
    )


@router.post(
    "/invoices",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_crm_service_auth)],
    tags=["invoices"],
)
def create_crm_invoice(
    payload: dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Create a one-time installation invoice for a subscriber (CRM-driven).

    Replaces the CRM's old Splynx installation-invoice call. Body:
    ``{subscriber_id, amount, description, external_ref?, currency?}``.
    """
    errors: dict[str, list[str]] = {}
    subscriber_id = str(payload.get("subscriber_id") or "").strip()
    if not subscriber_id:
        errors.setdefault("subscriber_id", []).append("Required.")
    description = str(payload.get("description") or "").strip()
    if not description:
        errors.setdefault("description", []).append("Required.")
    amount_raw = payload.get("amount")
    amount = Decimal("0")
    try:
        amount = Decimal(str(amount_raw))
    except (InvalidOperation, TypeError, ValueError):
        errors.setdefault("amount", []).append("Must be a number.")
    else:
        if amount <= 0:
            errors.setdefault("amount", []).append("Must be greater than 0.")
    if errors:
        _error(status.HTTP_400_BAD_REQUEST, "Invalid invoice payload.", errors)

    currency = str(payload.get("currency") or "NGN").strip().upper() or "NGN"
    external_ref = payload.get("external_ref")
    external_ref = str(external_ref).strip() if external_ref not in (None, "") else None

    try:
        invoice = crm_api.create_installation_invoice(
            db,
            subscriber_id=subscriber_id,
            amount=amount,
            description=description,
            external_ref=external_ref,
            currency=currency,
        )
    except LookupError:
        _error(status.HTTP_404_NOT_FOUND, "Subscriber not found.")

    return _envelope(
        {
            "id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "total": str(invoice.total),
            "status": invoice.status.value,
            "account_id": str(invoice.account_id),
        }
    )


@router.post(
    "/subscriptions/{subscription_id}/radio-mac",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_crm_service_auth)],
    tags=["provisioning"],
)
def register_subscription_radio_mac(
    subscription_id: str,
    payload: dict[str, Any] = Body(default_factory=dict),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Register the customer's wireless-radio MAC at install time.

    Called by the field/mobile app at turn-up so the radio is traceable by
    construction instead of waiting for the UISP sync's MAC guess. Body:
    ``{mac_address}``. Idempotent: re-posting the same MAC for the same
    subscriber returns the existing device with ``created: false``. A MAC
    already bound to a DIFFERENT subscriber is rejected with 409 and an
    unmatched-radio ops review item is opened.
    """
    from app.services import radio_registration

    mac = payload.get("mac_address") or payload.get("mac")
    if not str(mac or "").strip():
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid radio MAC payload.",
            {"mac_address": ["Required."]},
        )
    try:
        result = radio_registration.register_radio_mac(
            db,
            subscription_id=subscription_id,
            mac=str(mac),
            source=radio_registration.SOURCE_CRM_API,
        )
    except LookupError:
        _error(status.HTTP_404_NOT_FOUND, "Subscription not found.")
    except radio_registration.InvalidMacError as exc:
        _error(
            status.HTTP_400_BAD_REQUEST,
            "Invalid radio MAC payload.",
            {"mac_address": [str(exc)]},
        )
    except radio_registration.MacConflictError as exc:
        _error(status.HTTP_409_CONFLICT, str(exc))

    device = result.device
    return _envelope(
        {
            "id": str(device.id),
            "mac_address": device.mac_address,
            "device_type": device.device_type.value,
            "subscriber_id": str(device.subscriber_id),
            "created": result.created,
            "subscription_mac_stamped": result.subscription_mac_stamped,
            "uisp_confirmed": device.uisp_device_id is not None,
            "warnings": result.warnings,
        }
    )
