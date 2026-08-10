"""Service helpers for bulk service activation."""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models.catalog import (
    AccessCredential,
    CatalogOffer,
    NasDevice,
    OfferStatus,
    PlanCategory,
    Subscription,
    SubscriptionStatus,
)
from app.models.network_monitoring import PopSite
from app.models.subscriber import Reseller, Subscriber, SubscriberStatus
from app.services import domain_settings as domain_settings_service
from app.services import job_log_store
from app.services.account_lifecycle import (
    activate_subscription,
    clear_account_lifecycle_override,
    transition_subscription_status,
)
from app.services.audit_adapter import record_audit_event
from app.services.auth_flow import hash_service_secret
from app.services.catalog.subscriptions import (
    apply_offer_radius_profile,
    contracted_amount_for_offer,
)

logger = logging.getLogger(__name__)

TAB_TO_CATEGORY: dict[str, PlanCategory] = {
    "internet": PlanCategory.internet,
    "recurring": PlanCategory.recurring,
    "bundle": PlanCategory.bundle,
}
BULK_ACTIVATION_JOBS_KEY = "bulk_activation_jobs_log"
BULK_ACTIVATION_DEFAULT_PAGE_SIZE = 200


@dataclass
class BulkFilters:
    tab: str
    reseller_id: str | None
    subscriber_status: str | None
    pop_site_id: str | None
    date_from: str | None
    date_to: str | None
    custom_attr_key: str | None
    custom_attr_value: str | None


@dataclass
class BulkMapping:
    offer_id: str | None
    activation_date: str | None
    nas_device_id: str | None
    ipv4_assignment: str | None
    static_ipv4: str | None
    mac_address: str | None
    login_prefix: str | None
    login_suffix: str | None
    service_password_mode: str | None
    service_password_manual: str | None
    skip_active_service_check: bool
    set_subscribers_active: bool


def _normalize_tab(tab: str | None) -> str:
    normalized = (tab or "internet").strip().lower()
    if normalized not in TAB_TO_CATEGORY:
        return "internet"
    return normalized


def _date_window(
    date_from: str | None, date_to: str | None
) -> tuple[datetime | None, datetime | None]:
    start_dt: datetime | None = None
    end_dt: datetime | None = None
    if date_from:
        d = date.fromisoformat(date_from)
        start_dt = datetime.combine(d, time.min, tzinfo=UTC)
    if date_to:
        d = date.fromisoformat(date_to)
        end_dt = datetime.combine(d + timedelta(days=1), time.min, tzinfo=UTC)
    return start_dt, end_dt


def parse_filters(form: dict[str, Any]) -> BulkFilters:
    return BulkFilters(
        tab=_normalize_tab(str(form.get("tab") or "internet")),
        reseller_id=str(form.get("reseller_id") or "").strip() or None,
        subscriber_status=str(form.get("subscriber_status") or "").strip() or None,
        pop_site_id=str(form.get("pop_site_id") or "").strip() or None,
        date_from=str(form.get("date_from") or "").strip() or None,
        date_to=str(form.get("date_to") or "").strip() or None,
        custom_attr_key=str(form.get("custom_attr_key") or "").strip() or None,
        custom_attr_value=str(form.get("custom_attr_value") or "").strip() or None,
    )


def parse_mapping(form: dict[str, Any]) -> BulkMapping:
    return BulkMapping(
        offer_id=str(form.get("offer_id") or "").strip() or None,
        activation_date=str(form.get("activation_date") or "").strip() or None,
        nas_device_id=str(form.get("nas_device_id") or "").strip() or None,
        ipv4_assignment=str(form.get("ipv4_assignment") or "dynamic").strip().lower()
        or "dynamic",
        static_ipv4=str(form.get("static_ipv4") or "").strip() or None,
        mac_address=str(form.get("mac_address") or "").strip() or None,
        login_prefix=str(form.get("login_prefix") or "").strip() or None,
        login_suffix=str(form.get("login_suffix") or "").strip() or None,
        service_password_mode=str(form.get("service_password_mode") or "auto")
        .strip()
        .lower()
        or "auto",
        service_password_manual=str(form.get("service_password_manual") or "").strip()
        or None,
        skip_active_service_check=bool(form.get("skip_active_service_check")),
        set_subscribers_active=bool(form.get("set_subscribers_active")),
    )


def _matching_subscribers(db: Session, filters: BulkFilters) -> list[Subscriber]:
    query = db.query(Subscriber).filter(Subscriber.is_active.is_(True))
    if filters.reseller_id:
        query = query.filter(Subscriber.reseller_id == filters.reseller_id)
    if filters.subscriber_status:
        try:
            query = query.filter(
                Subscriber.status == SubscriberStatus(filters.subscriber_status)
            )
        except ValueError:
            return []
    start_dt, end_dt = _date_window(filters.date_from, filters.date_to)
    if start_dt:
        query = query.filter(Subscriber.created_at >= start_dt)
    if end_dt:
        query = query.filter(Subscriber.created_at < end_dt)
    candidates = query.order_by(Subscriber.created_at.desc()).all()

    if filters.pop_site_id:
        filtered: list[Subscriber] = []
        for subscriber in candidates:
            has_site = any(
                subscription.provisioning_nas_device is not None
                and str(subscription.provisioning_nas_device.pop_site_id or "")
                == filters.pop_site_id
                for subscription in subscriber.subscriptions
            )
            if has_site:
                filtered.append(subscriber)
        candidates = filtered
    if filters.custom_attr_key:
        attr_key = filters.custom_attr_key
        attr_val = filters.custom_attr_value
        filtered = []
        for subscriber in candidates:
            metadata = subscriber.metadata_ or {}
            if attr_key not in metadata:
                continue
            if attr_val is not None and str(metadata.get(attr_key)) != attr_val:
                continue
            filtered.append(subscriber)
        candidates = filtered
    return candidates


def _category_for_tab(tab: str) -> PlanCategory:
    return TAB_TO_CATEGORY.get(_normalize_tab(tab), PlanCategory.internet)


def _subscriber_category_subscriptions(
    db: Session, subscriber_id: str, tab: str
) -> list[Subscription]:
    category = _category_for_tab(tab)
    return (
        db.query(Subscription)
        .options(
            joinedload(Subscription.offer),
            joinedload(Subscription.provisioning_nas_device),
        )
        .join(CatalogOffer, Subscription.offer_id == CatalogOffer.id)
        .filter(Subscription.subscriber_id == subscriber_id)
        .filter(CatalogOffer.plan_category == category)
        .order_by(Subscription.created_at.desc())
        .all()
    )


def _preview_row(
    subscriber: Subscriber, existing: Subscription | None, mapping: BulkMapping
) -> tuple[str, str | None]:
    if (
        existing
        and existing.status == SubscriptionStatus.active
        and not mapping.skip_active_service_check
    ):
        return "skip_active_exists", "Active subscription exists"
    if existing:
        return "update", None
    return "create", None


def build_preview(
    db: Session,
    *,
    filters: BulkFilters,
    mapping: BulkMapping,
    limit: int = BULK_ACTIVATION_DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    candidates = _matching_subscribers(db, filters)
    rows: list[dict[str, Any]] = []
    counts = {"create": 0, "update": 0, "skip": 0}
    for subscriber in candidates[: max(limit, 1)]:
        existing_rows = _subscriber_category_subscriptions(
            db, str(subscriber.id), filters.tab
        )
        existing = existing_rows[0] if existing_rows else None
        action, reason = _preview_row(subscriber, existing, mapping)
        if action == "create":
            counts["create"] += 1
        elif action == "update":
            counts["update"] += 1
        else:
            counts["skip"] += 1
        rows.append(
            {
                "subscriber_id": str(subscriber.id),
                "subscriber_name": subscriber.full_name,
                "subscriber_email": subscriber.email,
                "subscriber_status": subscriber.status.value
                if subscriber.status
                else "",
                "existing_subscription_id": str(existing.id) if existing else "",
                "existing_offer_name": existing.offer.name
                if existing and existing.offer
                else "",
                "existing_subscription_status": existing.status.value
                if existing
                else "",
                "action": action,
                "reason": reason or "",
            }
        )
    return {
        "rows": rows,
        "total_matches": len(candidates),
        "shown": len(rows),
        "counts": counts,
    }


def _job_entries(db: Session) -> list[dict[str, Any]]:
    return job_log_store.read_json_list(
        db, domain_settings_service.provisioning_settings, BULK_ACTIVATION_JOBS_KEY
    )


def _save_jobs(db: Session, jobs: list[dict[str, Any]]) -> None:
    job_log_store.save_json_list(
        db,
        domain_settings_service.provisioning_settings,
        BULK_ACTIVATION_JOBS_KEY,
        jobs,
        limit=200,
        is_secret=False,
        is_active=True,
    )


def list_jobs(db: Session, *, limit: int = 20) -> list[dict[str, Any]]:
    return _job_entries(db)[:limit]


def get_job(db: Session, job_id: str) -> dict[str, Any] | None:
    return job_log_store.get_job(_job_entries(db), job_id)


def upsert_job(db: Session, payload: dict[str, Any]) -> dict[str, Any]:
    jobs, merged = job_log_store.upsert_job(_job_entries(db), payload)
    _save_jobs(db, jobs)
    return merged


def create_job(
    db: Session,
    *,
    filters: BulkFilters,
    mapping: BulkMapping,
    actor_id: str | None,
) -> dict[str, Any]:
    if not mapping.offer_id:
        raise ValueError("Plan assignment is required")
    preview = build_preview(db, filters=filters, mapping=mapping, limit=5000)
    job_id = str(uuid.uuid4())
    return upsert_job(
        db,
        {
            "job_id": job_id,
            "status": "queued",
            "progress_percent": 0,
            "queued_at": datetime.now(UTC).isoformat(),
            "started_at": None,
            "completed_at": None,
            "error": None,
            "result": None,
            "counts": {"activated": 0, "failed": 0, "skipped": 0},
            "total_matches": preview["total_matches"],
            "filters": filters.__dict__,
            "mapping": mapping.__dict__,
            "actor_id": actor_id,
        },
    )


def _log_bulk_activation_audit(
    db: Session,
    *,
    actor_id: str | None,
    job_id: str,
    is_success: bool,
    summary: dict[str, Any],
) -> None:
    record_audit_event(
        db,
        actor_id=actor_id,
        action="bulk_activate",
        entity_type="service_activation",
        entity_id=job_id,
        status_code=200 if is_success else 500,
        is_success=is_success,
        metadata=summary,
    )


def _activation_datetime(value: str | None) -> datetime:
    if value:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    return datetime.now(UTC)


def _compute_login(subscriber: Subscriber, mapping: BulkMapping) -> str:
    base = (
        subscriber.subscriber_number
        or subscriber.account_number
        or subscriber.email.split("@", 1)[0]
    )
    return f"{mapping.login_prefix or ''}{base}{mapping.login_suffix or ''}"


def _upsert_access_credential(
    db: Session, *, subscriber: Subscriber, username: str, password: str
) -> None:
    credential = (
        db.query(AccessCredential)
        .filter(AccessCredential.subscriber_id == subscriber.id)
        .order_by(AccessCredential.created_at.desc())
        .first()
    )
    if credential:
        credential.username = username
        credential.secret_hash = hash_service_secret(password)
        credential.is_active = True
        return
    db.add(
        AccessCredential(
            subscriber_id=subscriber.id,
            username=username,
            secret_hash=hash_service_secret(password),
            is_active=True,
        )
    )


def execute_job(db: Session, *, job_id: str) -> dict[str, Any]:
    job = get_job(db, job_id)
    if not job:
        raise ValueError("Bulk activation job not found")
    filters = BulkFilters(**(job.get("filters") or {}))
    mapping = BulkMapping(**(job.get("mapping") or {}))
    actor_id = str(job.get("actor_id") or "").strip() or None
    upsert_job(
        db,
        {
            "job_id": job_id,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "error": None,
        },
    )

    candidates = _matching_subscribers(db, filters)
    total = len(candidates)
    # ``static_ipv4`` is ONE address for the whole job, so applying it across a
    # multi-candidate batch hands the same address to every subscriber. Nothing
    # downstream catches that: IPAM has a partial-unique index on the active
    # assignment, but ``Subscription.ipv4_address`` is an unconstrained
    # compatibility projection, so the collision lands silently and surfaces
    # later as ``duplicate_served_projection``. A scalar static address is only
    # coherent for a batch of exactly one.
    if mapping.ipv4_assignment == "static" and mapping.static_ipv4 and total > 1:
        message = (
            "A static IPv4 cannot be applied to a bulk batch of "
            f"{total} subscribers — it would assign {mapping.static_ipv4} to "
            "every one of them. Narrow the filters to a single subscriber, or "
            "activate with dynamic addressing and assign static addresses "
            "through IPAM."
        )
        return upsert_job(
            db,
            {
                "job_id": job_id,
                "status": "failed",
                "completed_at": datetime.now(UTC).isoformat(),
                "error": message,
                "counts": {"activated": 0, "failed": 0, "skipped": total},
            },
        )
    activated = 0
    failed = 0
    skipped = 0
    activation_at = _activation_datetime(mapping.activation_date)
    offer = db.get(CatalogOffer, mapping.offer_id) if mapping.offer_id else None
    nas_id = uuid.UUID(mapping.nas_device_id) if mapping.nas_device_id else None

    for idx, subscriber in enumerate(candidates, start=1):
        try:
            existing_rows = _subscriber_category_subscriptions(
                db, str(subscriber.id), filters.tab
            )
            existing = existing_rows[0] if existing_rows else None
            action, _reason = _preview_row(subscriber, existing, mapping)
            if action.startswith("skip"):
                skipped += 1
            else:
                target = existing
                if target is None:
                    if offer is None:
                        raise ValueError("Offer is required")
                    from app.services.billing_profile import (
                        resolve_subscription_billing_mode_for_write,
                    )

                    target_billing_mode = resolve_subscription_billing_mode_for_write(
                        db,
                        account_id=subscriber.id,
                        offer_id=offer.id,
                    )
                    target = Subscription(
                        subscriber_id=subscriber.id,
                        offer_id=offer.id,
                        status=SubscriptionStatus.pending,
                        billing_mode=target_billing_mode,
                        # Snapshot the contracted amount from the same owner
                        # Subscriptions.create uses. Without it the row is born
                        # with unit_price NULL, and prepaid enforcement is
                        # blocked for that account from its first collectible
                        # cycle with no path back.
                        unit_price=contracted_amount_for_offer(db, offer.id),
                    )
                    apply_offer_radius_profile(db, target, sync_credentials=False)
                    db.add(target)
                elif offer is not None:
                    from app.services.billing_profile import (
                        resolve_subscription_billing_mode_for_write,
                    )

                    target_billing_mode = resolve_subscription_billing_mode_for_write(
                        db,
                        account_id=subscriber.id,
                        offer_id=offer.id,
                    )
                    previous_offer_id = target.offer_id
                    target.offer_id = offer.id
                    target.billing_mode = target_billing_mode
                    apply_offer_radius_profile(
                        db,
                        target,
                        previous_offer_id=previous_offer_id,
                    )
                elif target.radius_profile_id is None:
                    apply_offer_radius_profile(db, target)
                desired_status = (
                    SubscriptionStatus.active
                    if activation_at <= datetime.now(UTC)
                    else SubscriptionStatus.pending
                )
                target.start_at = activation_at
                target.provisioning_nas_device_id = nas_id
                target.mac_address = mapping.mac_address or target.mac_address
                login = _compute_login(subscriber, mapping)
                target.login = login
                password = (
                    mapping.service_password_manual
                    if mapping.service_password_mode == "manual"  # noqa: S105
                    and mapping.service_password_manual
                    else secrets.token_urlsafe(12)
                )
                _upsert_access_credential(
                    db, subscriber=subscriber, username=login, password=password
                )
                db.flush()
                # Bulk activation calls ``activate_subscription(emit=False)``, so
                # ``subscription_activated`` never fires and the provisioning
                # handler's ``ensure_ip_assignments_for_subscription`` never runs.
                # This path used to compensate by writing ``ipv4_address``
                # directly, which produced a served address with no IPAM record —
                # the ``assignment_missing`` cohort. Ask the allocator for the
                # assignment instead; it creates the exact-service IPAssignment
                # and sets the column as its projection.
                if mapping.ipv4_assignment == "static" and mapping.static_ipv4:
                    from app.services.provisioning_helpers import (
                        ensure_ipv4_assignment_for_subscription,
                    )

                    ensure_ipv4_assignment_for_subscription(
                        db,
                        str(target.id),
                        {"ipv4_address": mapping.static_ipv4},
                    )
                if desired_status == SubscriptionStatus.active:
                    if target.status == SubscriptionStatus.pending:
                        activate_subscription(
                            db, str(target.id), start_at=activation_at, emit=False
                        )
                    elif target.status != SubscriptionStatus.active:
                        transition_subscription_status(
                            db,
                            str(target.id),
                            SubscriptionStatus.active,
                            reason="Bulk provisioning activation",
                            source=f"bulk_activation:{job_id}",
                            emit=False,
                        )
                    from app.services.radius import sync_account_credentials_to_radius

                    sync_account_credentials_to_radius(db, subscriber.id)
                elif target.status != SubscriptionStatus.pending:
                    raise ValueError(
                        "Existing subscription cannot be moved back to pending"
                    )
                if mapping.set_subscribers_active:
                    clear_account_lifecycle_override(
                        db,
                        str(subscriber.id),
                        reason="Bulk provisioning activation",
                        source=f"bulk_activation:{job_id}",
                    )
                activated += 1
            db.commit()
        except Exception:
            db.rollback()
            failed += 1
            # Without this the per-row cause is unrecoverable: the job reports a
            # bare ``failed`` count and the operator has nothing to act on.
            logger.exception(
                "Bulk activation job %s failed for subscriber %s",
                job_id,
                subscriber.id,
            )
        if total:
            pct = int((idx / total) * 100)
            upsert_job(
                db,
                {
                    "job_id": job_id,
                    "progress_percent": pct,
                    "counts": {
                        "activated": activated,
                        "failed": failed,
                        "skipped": skipped,
                    },
                },
            )

    result = {
        "activated": activated,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "tab": filters.tab,
        "offer_id": mapping.offer_id,
    }
    final = upsert_job(
        db,
        {
            "job_id": job_id,
            "status": "completed" if failed == 0 else "partial",
            "completed_at": datetime.now(UTC).isoformat(),
            "progress_percent": 100,
            "result": result,
            "counts": {"activated": activated, "failed": failed, "skipped": skipped},
        },
    )
    _log_bulk_activation_audit(
        db,
        actor_id=actor_id,
        job_id=job_id,
        is_success=(failed == 0),
        summary=result,
    )
    return final


def page_options(db: Session, *, tab: str) -> dict[str, Any]:
    normalized_tab = _normalize_tab(tab)
    category = _category_for_tab(normalized_tab)
    offers = (
        db.query(CatalogOffer)
        .filter(CatalogOffer.plan_category == category)
        .filter(CatalogOffer.status == OfferStatus.active)
        .filter(CatalogOffer.is_active.is_(True))
        .order_by(CatalogOffer.name.asc())
        .all()
    )
    resellers = (
        db.query(Reseller)
        .filter(Reseller.is_active.is_(True))
        .order_by(Reseller.name.asc())
        .all()
    )
    pop_sites = (
        db.query(PopSite)
        .filter(PopSite.is_active.is_(True))
        .order_by(PopSite.name.asc())
        .all()
    )
    nas_devices = (
        db.query(NasDevice)
        .filter(NasDevice.is_active.is_(True))
        .order_by(NasDevice.name.asc())
        .all()
    )
    return {
        "tabs": ["internet", "recurring", "bundle"],
        "tab": normalized_tab,
        "offers": offers,
        "resellers": resellers,
        "pop_sites": pop_sites,
        "nas_devices": nas_devices,
        "subscriber_statuses": [s.value for s in SubscriberStatus],
        "jobs": list_jobs(db, limit=20),
    }
