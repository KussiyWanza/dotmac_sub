"""Native self-serve installation quotes extracted from CRM.

Business logic extracted from CRM ``services/crm/portal_quotes.py`` (the
transport shell — portal principal/scope resolution and the mirror push —
dies with the portal-token subsystem). The customer drops a map pin for the
install address; this service:

  1. computes **feasibility** — distance from the pin to the nearest active
     fiber access point, now against sub's *native* ``fiber_access_points``
     (PostGIS), classifying coverage as covered / survey_required /
     out_of_area;
  2. builds an **estimate** from configurable settings (base fee + distance
     surcharge, or a flat bundle price) and the required **deposit**;
  3. **request_quote** creates a draft Lead + Quote carrying the pin,
     feasibility and estimate — the map-pinned ``install{latitude, longitude,
     address, region}`` block is stamped on both lead and quote metadata and
     is reused downstream for estimate/survey/billing (hard contract);
  4. **accept_with_deposit** (the tail of ``quote_deposits.verify_deposit``)
     accepts the quote — which fires the unchanged sales-service pipeline
     (sales order + install project) — and records the deposit on the sales
     order. Idempotent.

Native ownership deltas from the CRM source:

* Feasibility re-points at sub ``FiberAccessPoint`` (models/network.py); the
  CRM's own FAP copy is dropped. Coverage vocabulary unchanged.
* Price-book SKUs (CRM ``inventory_items``) are re-keyed to sub catalog
  offers: ``selfserve_quote_{bundle,base,distance}_offer_id`` settings
  replace the ``*_item_sku`` keys while inventory remains externally owned. Estimate line items
  set ``inventory_item_id=NULL`` natively; the priced offer is carried in
  line metadata ``sub_offer_id`` (§1.4 convention, already what the admin
  quote form writes).
* ``portal_scope.resolve_target_subscriber`` is replaced by the
  authenticated sub subscriber. The reseller surface reuses the same
  entrypoint on a managed account's subscriber_id
  (``api/reseller.my_reseller_quote_request``, IDOR-scoped by the route).
* ``lead_source="Portal"`` — the §2.1/risk-#7 fix (the raw ``"portal"``
   400'd in the old CRM vocabulary).
* New quote metadata stops writing ``subscriber_external_id`` (§1.4) — the
  row's own ``subscriber_id`` column is the link; the payload serializer
  still surfaces the key for imported provenance rows.

**Billing-safety invariant (risk #2):** the deposit money path is
``initiate_deposit`` → sub Invoice → provider → ``verify_and_record_payment``.
The accept here only *marks* the sales order (amount_paid / payment_status)
— it never creates a second payment. One ledger event per deposit, recorded
by billing, mirrored as SO bookkeeping.

All pricing lives in settings (``SettingDomain.projects``,
``selfserve_quote_*``) so the numbers are tunable per market without code
changes.
"""

from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException
from geoalchemy2.functions import ST_MakePoint, ST_SetSRID
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models.catalog import CatalogOffer, OfferPrice, PriceType
from app.models.domain_settings import SettingDomain
from app.models.network import FiberAccessPoint
from app.models.project import Project
from app.models.sales import (
    Quote,
    QuoteStatus,
    SalesOrder,
    SalesOrderPaymentStatus,
    SalesOrderStatus,
)
from app.models.subscriber import Subscriber
from app.schemas.sales import (
    LeadCreate,
    QuoteCreate,
    QuoteLineItemCreate,
    QuoteUpdate,
)
from app.services import control_registry, settings_spec
from app.services.common import coerce_uuid
from app.services.sales.service import leads, quote_line_items, quotes

logger = logging.getLogger(__name__)

_TWOPLACES = Decimal("0.01")
# §1.7 ProjectType.fiber_optics_installation — the enum itself arrives with
# the native projects migration; the metadata contract carries the string value.
_PROJECT_TYPE = "fiber_optics_installation"


def _money(value) -> Decimal:
    return Decimal(str(value or "0")).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _settings(db: Session) -> dict:
    """Resolved, typed self-serve quote settings (with placeholder defaults)."""

    def _dec(key: str) -> Decimal:
        return _money(settings_spec.resolve_value(db, SettingDomain.projects, key))

    def _int(key: str, fallback: int) -> int:
        raw = settings_spec.resolve_value(db, SettingDomain.projects, key)
        try:
            return int(str(raw))
        except (TypeError, ValueError):
            return fallback

    def _offer_id(key: str) -> str | None:
        raw = settings_spec.resolve_value(db, SettingDomain.projects, key)
        text = str(raw).strip() if raw else ""
        return text or None

    return {
        "enabled": bool(
            settings_spec.resolve_value(
                db, SettingDomain.projects, "selfserve_quote_enabled"
            )
        ),
        "base_fee": _dec("selfserve_quote_base_fee"),
        "free_radius_m": _int("selfserve_quote_free_radius_meters", 300),
        "fee_per_km": _dec("selfserve_quote_fee_per_km"),
        "deposit_percent": max(
            0, min(100, _int("selfserve_quote_deposit_percent", 50))
        ),
        "feasibility_radius_m": _int("selfserve_quote_feasibility_radius_meters", 2000),
        # SKU keys re-keyed to Sub catalog offers while inventory is external.
        "bundle_offer_id": _offer_id("selfserve_quote_bundle_offer_id"),
        "base_offer_id": _offer_id("selfserve_quote_base_offer_id"),
        "distance_offer_id": _offer_id("selfserve_quote_distance_offer_id"),
    }


def _priced_offer(db: Session, offer_id: str | None):
    """Resolve an active catalog offer and its price for estimate line items.

    Sub's replacement for the CRM price-book SKU lookup: one-time prices are
    preferred (install fees are one-off charges), falling back to any active
    price row. Returns ``(CatalogOffer, Decimal) | None``.
    """
    if not offer_id:
        return None
    offer_uuid = coerce_uuid(str(offer_id))
    if offer_uuid is None:
        return None
    offer = db.get(CatalogOffer, offer_uuid)
    if offer is None or not offer.is_active:
        return None
    prices = (
        db.query(OfferPrice)
        .filter(OfferPrice.offer_id == offer.id)
        .filter(OfferPrice.is_active.is_(True))
        .all()
    )
    if not prices:
        return None
    price = next((p for p in prices if p.price_type == PriceType.one_time), prices[0])
    return offer, _money(price.amount)


def _nearest_fiber_access_point(db: Session, latitude: float, longitude: float):
    """Nearest active fiber access point and its distance in metres (PostGIS).

    Same query the CRM ran, re-pointed at sub's native ``fiber_access_points``
    (§2.2 step 1) — projected to EPSG:3857 for a metre distance. Returns
    ``(FiberAccessPoint | None, float | None)``. Isolated so the pricing
    logic can be unit-tested without a spatial database.
    """
    point = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326)
    # Order by the KNN nearest-neighbour operator on the RAW 4326 geom so the
    # GiST index ``idx_fiber_access_points_geom`` is usable: wrapping the geom
    # in ``ST_Transform(..., 3857)`` (as the old query did, for a metre
    # distance) made the index unusable and forced a full scan + sort. The
    # ``<->`` bound-box KNN drives the index; the metre ``distance_m`` value is
    # still computed by the *same* EPSG:3857 transform, but now only for the
    # single winning row the LIMIT keeps — so the returned value is unchanged.
    distance = func.ST_Distance(
        func.ST_Transform(FiberAccessPoint.geom, 3857),
        func.ST_Transform(point, 3857),
    ).label("distance_m")
    row = (
        db.query(FiberAccessPoint, distance)
        .filter(FiberAccessPoint.is_active.is_(True))
        .filter(FiberAccessPoint.geom.isnot(None))
        .order_by(FiberAccessPoint.geom.op("<->")(point))
        .first()
    )
    if row is None:
        return None, None
    fap, dist = row
    return fap, (float(dist) if dist is not None else None)


def compute_feasibility(db: Session, latitude: float, longitude: float) -> dict:
    """Classify install feasibility from proximity to the nearest fiber plant.

    Coverage vocabulary unchanged (§2.2): ``covered | survey_required |
    out_of_area``, covered iff distance ≤ the feasibility radius setting.
    """
    cfg = _settings(db)
    fap, distance = _nearest_fiber_access_point(db, latitude, longitude)
    if fap is None or distance is None:
        return {
            "feasible": False,
            "coverage": "out_of_area",
            "nearest_fap_id": None,
            "nearest_fap_name": None,
            "distance_meters": None,
        }
    coverage = (
        "covered" if distance <= cfg["feasibility_radius_m"] else "survey_required"
    )
    return {
        "feasible": True,
        "coverage": coverage,
        "nearest_fap_id": str(fap.id),
        "nearest_fap_name": fap.name,
        "distance_meters": round(distance, 1),
    }


def compute_estimate(db: Session, feasibility: dict, currency: str) -> dict:
    """Build the estimate + deposit + quote line items from settings.

    For ``out_of_area`` / ``survey_required`` the estimate is **provisional**
    — a site survey confirms the true cost; only the base fee is quoted up
    front. Ports verbatim from the CRM (§2.2 step 2): bundle vs derived
    pricing, free radius, fee/km, deposit percent clamp, ROUND_HALF_UP 0.01.
    """
    cfg = _settings(db)
    coverage = feasibility.get("coverage")
    deposit_percent = cfg["deposit_percent"]

    bundle = _priced_offer(db, cfg["bundle_offer_id"])
    if bundle is not None:
        # Bundle mode: a flat catalog price, irrespective of distance.
        # Out-of-area (no nearby plant) still needs a survey → provisional.
        bundle_offer, bundle_price = bundle
        subtotal = bundle_price
        return {
            "currency": currency,
            "pricing_mode": "bundle",
            "base_fee": bundle_price,
            "distance_fee": Decimal("0.00"),
            "subtotal": subtotal,
            "deposit_percent": deposit_percent,
            "deposit_amount": _money(
                subtotal * Decimal(deposit_percent) / Decimal("100")
            ),
            "provisional": coverage == "out_of_area",
            "line_items": [
                {
                    "description": bundle_offer.name,
                    "unit_price": bundle_price,
                    "sub_offer_id": str(bundle_offer.id),
                }
            ],
        }

    # Derived mode: base + per-km distance surcharge. Unit prices come from
    # the catalog offers when configured, else the *_fee settings.
    free_radius_m = float(cfg["free_radius_m"])
    base = _priced_offer(db, cfg["base_offer_id"])
    base_fee = base[1] if base is not None else _money(cfg["base_fee"])
    base_desc = base[0].name if base is not None else "Fiber installation (base)"
    base_offer_id = str(base[0].id) if base is not None else None

    distance_priced = _priced_offer(db, cfg["distance_offer_id"])
    fee_per_km = (
        distance_priced[1] if distance_priced is not None else _money(cfg["fee_per_km"])
    )

    distance_m: float | None = None
    raw_distance = feasibility.get("distance_meters")
    if raw_distance is not None:
        try:
            distance_m = float(str(raw_distance))
        except (TypeError, ValueError):
            distance_m = None

    distance_fee = Decimal("0.00")
    billable_m = 0.0
    provisional = coverage != "covered"
    if coverage == "covered" and distance_m is not None:
        billable_m = max(0.0, distance_m - free_radius_m)
        if billable_m > 0:
            km = Decimal(str(billable_m)) / Decimal("1000")
            distance_fee = _money(km * fee_per_km)

    line_items: list[dict] = [
        {
            "description": base_desc,
            "unit_price": base_fee,
            "sub_offer_id": base_offer_id,
        }
    ]
    if distance_fee > 0:
        over_km = round(billable_m / 1000, 2)
        distance_name = (
            distance_priced[0].name
            if distance_priced is not None
            else "Distance surcharge"
        )
        line_items.append(
            {
                "description": f"{distance_name} ({over_km} km beyond free radius)",
                "unit_price": distance_fee,
                "sub_offer_id": (
                    str(distance_priced[0].id) if distance_priced is not None else None
                ),
            }
        )

    subtotal = _money(base_fee + distance_fee)
    return {
        "currency": currency,
        "pricing_mode": "derived",
        "base_fee": base_fee,
        "distance_fee": distance_fee,
        "subtotal": subtotal,
        "deposit_percent": deposit_percent,
        "deposit_amount": _money(subtotal * Decimal(deposit_percent) / Decimal("100")),
        "provisional": provisional,
        "line_items": line_items,
    }


def _resolve_currency(db: Session) -> str:
    currency = settings_spec.resolve_value(
        db, SettingDomain.billing, "default_currency"
    )
    return str(currency) if currency else "NGN"


def _quote_status(quote: Quote) -> str:
    """Sub stores quote status as a plain string (§1.7); tolerate enums."""
    return getattr(quote.status, "value", quote.status)


def _find_project_ids_for_quotes(db: Session, quote_ids) -> dict[str, str]:
    """Batch-resolve the install project id for a whole set of quotes.

    One query for the entire quote set (``WHERE metadata->>'quote_id' IN (…)``)
    instead of the per-quote JSON scan the list read paths used to issue — the
    N+1 fixed in H1. Keyed by ``str(quote_id)`` → ``str(project.id)``; quotes
    with no native install project are simply absent from the map.

    The old ``.first()`` had no ``ORDER BY``, so a quote referenced by more than
    one active project resolved to an arbitrary row. We make the pick
    deterministic here (earliest ``created_at``, then ``id``) and route the
    single-quote helper through this same resolver, so both paths agree — the
    common no-project / one-project cases are unaffected.
    """
    ids = [str(q) for q in quote_ids]
    if not ids:
        return {}
    key = Project.metadata_["quote_id"].as_string()
    rows = (
        db.query(key.label("quote_id"), Project.id, Project.created_at)
        .filter(key.in_(ids))
        .filter(Project.is_active.is_(True))
        .order_by(Project.created_at.asc(), Project.id.asc())
        .all()
    )
    mapping: dict[str, str] = {}
    for quote_id, project_id, _created_at in rows:
        # Rows arrive earliest-first; keep the first (earliest) per quote.
        if quote_id is not None and quote_id not in mapping:
            mapping[quote_id] = str(project_id)
    return mapping


def _find_project_id_for_quote(db: Session, quote_id) -> str | None:
    """Resolve the install project created from this quote.

    The CRM resolved the payload's ``project_id`` via
    ``_find_existing_project_for_quote``, idempotent on
    ``Project.metadata_["quote_id"]`` — the same key sub's native project
    pipeline stamps. Quotes whose install project predates the native wiring
    carry ``project_id: None`` (the mobile/web schemas treat it as optional).

    Thin wrapper over the batch resolver (batch of one) so the single-quote and
    list paths share identical selection + tie-break semantics.
    """
    return _find_project_ids_for_quotes(db, [quote_id]).get(str(quote_id))


# Mirror parity: quotes_mirror.read_for_subscriber counts these as closed.
_PORTAL_CLOSED_QUOTE_STATUSES = ("accepted", "rejected", "expired")

# Sentinel for build_portal_quote_payload's optional pre-resolved project id —
# distinct from ``None`` (a resolved "no install project" answer).
_UNSET_PROJECT_ID = "__unset__"


def native_read_enabled(db: Session) -> bool:
    """Select native quote reads or the CRM mirror.

    OFF (default) — ``/me/quotes``, the web portal page and the reseller
    views keep serving ``quotes_mirror``; ON — they serve sub's native
    ``quotes`` table via ``SelfServeQuotes.read_for_subscriber`` /
    ``build_portal_quote_payload``. Distinct from the native-write flag
    (``quotes_native_write_enabled``) so reads can flip first (§4.2 step 3).
    """
    return control_registry.is_enabled(db, "quotes.native_read")


def native_write_enabled(db: Session) -> bool:
    """Select native quote writes or CRM
    write-through. OFF (default) — quote requests and the deposit
    initiate/accept tail write through to the CRM via ``quotes_mirror``;
    ON — ``SelfServeQuotes.request_quote`` / ``accept_with_deposit`` own the
    write in sub's native ``quotes`` table (no CRM link required)."""
    return control_registry.is_enabled(db, "quotes.native_write")


class SelfServeQuotes:
    """Native self-serve quote flow (customer path — the authenticated sub
    subscriber replaces the CRM portal principal/scope, §2.2 step 3)."""

    @staticmethod
    def get_for_subscriber(db: Session, subscriber_id: str, quote_id: str) -> Quote:
        """Subscriber-scoped quote fetch; 404 outside the caller's scope
        (parity with the mirror lookup it replaces)."""
        quote = db.get(
            Quote,
            coerce_uuid(str(quote_id)),
            options=[selectinload(Quote.line_items)],
        )
        sub_uuid = coerce_uuid(str(subscriber_id))
        if (
            quote is None
            or not quote.is_active
            or sub_uuid is None
            or quote.subscriber_id != sub_uuid
        ):
            raise HTTPException(status_code=404, detail="Quote not found")
        return quote

    @staticmethod
    def list_for_subscribers(
        db: Session, subscriber_ids: list[str] | str
    ) -> list[Quote]:
        """Active quotes for one subscriber or a set (a reseller's customer
        subtree), newest first — the native counterpart of the mirror scans."""
        if isinstance(subscriber_ids, str):
            subscriber_ids = [subscriber_ids]
        uuids = [
            u for u in (coerce_uuid(str(s)) for s in subscriber_ids) if u is not None
        ]
        if not uuids:
            return []
        return (
            db.query(Quote)
            .options(selectinload(Quote.line_items))
            .filter(Quote.subscriber_id.in_(uuids))
            .filter(Quote.is_active.is_(True))
            .order_by(Quote.created_at.desc())
            .all()
        )

    @staticmethod
    def read_for_subscriber(db: Session, subscriber_id: str) -> dict:
        """Native ``GET /me/quotes`` / web-portal payload — the exact response
        shell ``quotes_mirror.read_for_subscriber`` served (§2.5):
        ``{quotes[], total, open}`` with ``build_portal_quote_payload`` items.
        Customer read surfaces use this owner behind
        ``quotes_native_read_enabled``."""
        rows = SelfServeQuotes.list_for_subscribers(db, subscriber_id)
        # H1: resolve every quote's install-project id in ONE query, then pass
        # each in — no per-quote metadata->>'quote_id' scan.
        project_ids = _find_project_ids_for_quotes(db, [q.id for q in rows])
        items = [
            build_portal_quote_payload(db, q, project_id=project_ids.get(str(q.id)))
            for q in rows
        ]
        open_count = sum(
            1 for i in items if i["status"] not in _PORTAL_CLOSED_QUOTE_STATUSES
        )
        return {"quotes": items, "total": len(items), "open": open_count}

    @staticmethod
    def request_quote(
        db: Session,
        subscriber_id: str,
        *,
        latitude: float,
        longitude: float,
        address: str | None = None,
        region: str | None = None,
        note: str | None = None,
    ) -> Quote:
        """Map-pinned quote request → draft Lead + Quote + estimate lines.

        The install pin (``latitude``/``longitude`` + ``address``/``region``)
        is stamped verbatim on both the lead and the quote metadata
        (``install{}``) — downstream estimate/survey/billing all read it from
        there. Do not drop or rename these keys.
        """
        cfg = _settings(db)
        if not cfg["enabled"]:
            raise HTTPException(
                status_code=403, detail="Self-serve quotes are not available"
            )

        subscriber = db.get(Subscriber, coerce_uuid(str(subscriber_id)))
        if subscriber is None:
            raise HTTPException(status_code=404, detail="Subscriber not found")

        feasibility = compute_feasibility(db, latitude, longitude)
        currency = _resolve_currency(db)
        estimate = compute_estimate(db, feasibility, currency)

        # The map-pin contract: reused downstream for estimate/survey/billing.
        install = {
            "latitude": latitude,
            "longitude": longitude,
            "address": address,
            "region": region,
        }
        lead = leads.create(
            db,
            LeadCreate(
                subscriber_id=subscriber.id,
                title="Self-serve installation request",
                address=address,
                region=region,
                notes=note,
                lead_source="Portal",
                metadata_={"source": "portal_self_serve", "install": install},
            ),
        )
        quote = quotes.create(
            db,
            QuoteCreate(
                subscriber_id=subscriber.id,
                lead_id=lead.id,
                status=QuoteStatus.draft,
                currency=currency,
                metadata_={
                    "source": "portal_self_serve",
                    "project_type": _PROJECT_TYPE,
                    "install": install,
                    "feasibility": feasibility,
                    "deposit_percent": estimate["deposit_percent"],
                    "estimate_provisional": estimate["provisional"],
                    "pricing_mode": estimate["pricing_mode"],
                },
            ),
        )
        for item in estimate["line_items"]:
            metadata = (
                {"sub_offer_id": item["sub_offer_id"]}
                if item.get("sub_offer_id")
                else None
            )
            quote_line_items.create(
                db,
                QuoteLineItemCreate(
                    quote_id=quote.id,
                    description=item["description"],
                    quantity=Decimal("1.000"),
                    unit_price=item["unit_price"],
                    metadata_=metadata,
                ),
            )
        db.refresh(quote)
        return quote

    @staticmethod
    def accept_with_deposit(
        db: Session,
        subscriber_id: str,
        quote_id: str,
        *,
        deposit_reference: str,
        deposit_amount: str | Decimal,
        provider: str | None = None,
    ) -> dict:
        """Accept a quote after the deposit is verified; record it; return
        the portal payload.

        Idempotent on the quote's accepted state — a repeat call (e.g. a
        payment-verify retry) returns the same already-created sales order.
        The accept fires the unchanged sales-service pipeline
        (``Quotes.update(status=accepted)`` → ``create_from_quote`` +
        ``_ensure_project_from_quote``, §2.2 step 4); the deposit is then
        only *marked* on the sales order — never a second payment (risk #2).
        """
        quote = SelfServeQuotes.get_for_subscriber(db, subscriber_id, quote_id)

        amount = _money(deposit_amount)
        already_accepted = _quote_status(quote) == QuoteStatus.accepted.value

        if not already_accepted:
            meta = dict(quote.metadata_ or {})
            meta["deposit"] = {
                "reference": deposit_reference,
                "amount": str(amount),
                "provider": provider,
                "paid": True,
            }
            quotes.update(
                db,
                str(quote.id),
                QuoteUpdate(status=QuoteStatus.accepted, metadata_=meta),
            )
            db.refresh(quote)

        _record_deposit_on_sales_order(db, quote, amount)
        return build_portal_quote_payload(db, quote, already_accepted=already_accepted)


def _record_deposit_on_sales_order(
    db: Session, quote: Quote, deposit_amount: Decimal
) -> SalesOrder | None:
    """Mark the deposit on the quote's sales order — SO bookkeeping only.

    Deliberately never creates a payment row: the sole ledger event for a
    deposit is ``verify_and_record_payment`` on the deposit invoice (risk #2).
    """
    sales_order = db.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).first()
    if sales_order is None:
        return None
    total = _money(sales_order.total)
    paid = _money(deposit_amount)
    previous_payment_status = sales_order.payment_status
    sales_order.deposit_required = True
    sales_order.deposit_paid = True
    sales_order.amount_paid = paid
    sales_order.balance_due = _money(max(Decimal("0.00"), total - paid))
    sales_order.payment_status = (
        SalesOrderPaymentStatus.paid.value
        if paid >= total and total > 0
        else SalesOrderPaymentStatus.partial.value
    )
    if sales_order.payment_status == SalesOrderPaymentStatus.paid.value:
        sales_order.status = SalesOrderStatus.paid.value
    from app.services import sales_orders as sales_order_service

    # A deposit that fully funds the order crosses the same funding edge as a
    # staff-recorded payment and must stage the same durable output. The
    # deposit's only ledger event stays the verified deposit-invoice payment
    # (risk #2), so the consumer must not record a second order payment.
    sales_order_service.stage_funding_transition(
        db,
        sales_order,
        previous_payment_status=previous_payment_status,
        record_order_payment=False,
    )
    db.commit()
    db.refresh(sales_order)
    return sales_order


def build_portal_quote_payload(
    db: Session,
    quote: Quote,
    *,
    already_accepted: bool = False,
    project_id: str | None = _UNSET_PROJECT_ID,
) -> dict:
    """Serialize a quote for the portal surface — the exact shape
    ``QuoteMirror.payload`` cached and mobile parses (§2.2 step 5, §2.5):
    money and quantities as **strings**, ``deposit_percent`` int, ``id`` =
    quote UUID. ``subscriber_external_id`` is imported provenance only —
    never written for new quotes (§1.4)."""
    meta = _as_dict(quote.metadata_)
    install = _as_dict(meta.get("install"))
    feasibility = _as_dict(meta.get("feasibility"))
    deposit_meta = _as_dict(meta.get("deposit"))

    total = _money(quote.total)
    deposit_percent = int(meta.get("deposit_percent") or 0)
    deposit_amount = (
        _money(deposit_meta["amount"])
        if deposit_meta.get("amount")
        else _money(total * Decimal(deposit_percent) / 100)
    )

    sales_order = db.query(SalesOrder).filter(SalesOrder.quote_id == quote.id).first()
    # ``project_id`` may be pre-resolved by a batch caller (H1: the list read
    # paths resolve the whole set in one query and pass it in). The sentinel
    # distinguishes "not supplied" from a resolved ``None`` (no install project)
    # so we never fall back to the per-quote scan for a quote we already know
    # has no project.
    if project_id is _UNSET_PROJECT_ID:
        project_id = _find_project_id_for_quote(db, quote.id)

    line_items = [
        {
            "description": li.description,
            "quantity": str(li.quantity),
            "unit_price": str(_money(li.unit_price)),
            "amount": str(_money(li.amount)),
        }
        for li in sorted(
            quote.line_items, key=lambda x: (x.created_at is None, x.created_at)
        )
    ]

    return {
        "id": str(quote.id),
        "status": _quote_status(quote),
        "currency": quote.currency,
        "subtotal": str(_money(quote.subtotal)),
        "tax_total": str(_money(quote.tax_total)),
        "total": str(total),
        "project_type": meta.get("project_type"),
        # Post-import this is the row's own column (§1.4); the legacy
        # metadata key remains only on imported rows as provenance.
        "subscriber_id": str(quote.subscriber_id),
        "subscriber_external_id": meta.get("subscriber_external_id"),
        "latitude": install.get("latitude"),
        "longitude": install.get("longitude"),
        "address": install.get("address"),
        "region": install.get("region"),
        "feasibility": {
            "coverage": feasibility.get("coverage"),
            "feasible": feasibility.get("feasible"),
            "distance_meters": feasibility.get("distance_meters"),
            "nearest_fap_name": feasibility.get("nearest_fap_name"),
        },
        "estimate_provisional": bool(meta.get("estimate_provisional")),
        "deposit_percent": deposit_percent,
        "deposit_amount": str(deposit_amount),
        "deposit_paid": bool(deposit_meta.get("paid")),
        "deposit_reference": deposit_meta.get("reference"),
        "line_items": line_items,
        "sales_order_id": str(sales_order.id) if sales_order else None,
        "project_id": project_id,
        "already_accepted": already_accepted,
        "created_at": quote.created_at.isoformat() if quote.created_at else None,
        "expires_at": quote.expires_at.isoformat() if quote.expires_at else None,
    }


selfserve_quotes = SelfServeQuotes()
