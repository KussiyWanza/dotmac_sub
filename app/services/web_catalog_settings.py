"""Service helpers for admin catalog settings web routes.

Provides paginated list+count queries, bulk deletes, CSV exports,
and nested-child sync logic so that routes remain thin wrappers.
"""

from __future__ import annotations

import csv
import logging
from decimal import Decimal
from io import StringIO
from typing import Any, cast

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import (
    AddOn,
    AddOnType,
    BillingCycle,
    DunningAction,
    PolicySet,
    PriceType,
    PriceUnit,
    ProrationPolicy,
    RefundPolicy,
    RegionZone,
    SlaProfile,
    UsageAllowance,
)
from app.schemas.catalog import (
    AddOnCreate,
    AddOnPriceCreate,
    AddOnPriceUpdate,
    AddOnUpdate,
    PolicyDunningStepCreate,
    PolicyDunningStepUpdate,
    PolicySetCreate,
    PolicySetUpdate,
    RegionZoneCreate,
    RegionZoneUpdate,
    SlaProfileCreate,
    SlaProfileUpdate,
    UsageAllowanceCreate,
    UsageAllowanceUpdate,
)
from app.services import catalog as catalog_service
from app.services import catalog_billing_governance
from app.services.common import coerce_uuid

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paginated list result type
# ---------------------------------------------------------------------------


class PaginatedResult:
    """Holds items, total count, and total pages for a paginated query."""

    __slots__ = ("items", "total", "total_pages")

    def __init__(self, items: list[Any], total: int, total_pages: int) -> None:
        self.items = items
        self.total = total
        self.total_pages = total_pages


# ---------------------------------------------------------------------------
# Settings overview counts
# ---------------------------------------------------------------------------


def settings_overview_counts(db: Session) -> dict[str, int]:
    """Return total and active counts for each catalog-settings entity type."""
    counts: dict[str, int] = {}
    for model, prefix in [
        (RegionZone, "region_zones"),
        (UsageAllowance, "usage_allowances"),
        (SlaProfile, "sla_profiles"),
        (PolicySet, "policy_sets"),
        (AddOn, "add_ons"),
    ]:
        # mypy doesn't understand `model` is one of the explicit ORM classes above.
        model_id = model.id  # type: ignore[attr-defined]
        model_is_active = model.is_active  # type: ignore[attr-defined]
        total = db.execute(select(func.count(model_id))).scalar() or 0
        active = (
            db.execute(
                select(func.count(model_id)).where(model_is_active.is_(True))
            ).scalar()
            or 0
        )
        counts[f"{prefix}_count"] = total
        counts[f"{prefix}_active"] = active
    return counts


# ---------------------------------------------------------------------------
# Paginated list helpers
# ---------------------------------------------------------------------------


def list_region_zones_paginated(
    db: Session,
    *,
    is_active: bool | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated region zones with optional status and search filters."""
    stmt = select(RegionZone)
    count_stmt = select(func.count(RegionZone.id))

    if is_active is not None:
        stmt = stmt.where(RegionZone.is_active == is_active)
        count_stmt = count_stmt.where(RegionZone.is_active == is_active)
    if search:
        term = f"%{search.strip()}%"
        search_filter = or_(RegionZone.name.ilike(term), RegionZone.code.ilike(term))
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(RegionZone.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def region_zone_form_defaults() -> dict[str, object]:
    """Return blank defaults for the region-zone form."""
    return {"name": "", "code": "", "description": "", "is_active": True}


def region_zone_form_context(
    db: Session,
    *,
    zone_id: str | None = None,
) -> dict[str, object] | None:
    """Return region-zone form context, or None when editing a missing zone."""
    if zone_id is None:
        return {
            "zone": region_zone_form_defaults(),
            "action_url": "/admin/catalog/settings/region-zones",
        }
    try:
        zone_obj = catalog_service.region_zones.get(db=db, zone_id=zone_id)
    except Exception:
        return None
    return {
        "zone": {
            "id": str(zone_obj.id),
            "name": zone_obj.name,
            "code": zone_obj.code or "",
            "description": zone_obj.description or "",
            "is_active": zone_obj.is_active,
        },
        "action_url": f"/admin/catalog/settings/region-zones/{zone_id}/edit",
    }


def parse_region_zone_form(form) -> dict[str, object]:
    """Parse region-zone form fields into template-friendly values."""

    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    return {
        "name": form_str("name").strip(),
        "code": form_str("code").strip(),
        "description": form_str("description").strip(),
        "is_active": form_str("is_active") == "true",
    }


def _region_zone_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "name": values["name"],
        "code": values["code"] or None,
        "description": values["description"] or None,
        "is_active": values["is_active"],
    }


def create_region_zone_from_form(db: Session, *, form) -> None:
    """Validate and create a region zone from form data."""
    values = parse_region_zone_form(form)
    payload = RegionZoneCreate.model_validate(_region_zone_payload(values))
    catalog_service.region_zones.create(db=db, payload=payload)


def update_region_zone_from_form(db: Session, *, zone_id: str, form) -> None:
    """Validate and update a region zone from form data."""
    values = parse_region_zone_form(form)
    payload = RegionZoneUpdate.model_validate(_region_zone_payload(values))
    catalog_service.region_zones.update(db=db, zone_id=zone_id, payload=payload)


def delete_region_zone(db: Session, *, zone_id: str) -> None:
    """Deactivate a region zone."""
    catalog_service.region_zones.delete(db=db, zone_id=zone_id)


def list_usage_allowances_paginated(
    db: Session,
    *,
    is_active: bool | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated usage allowances with optional filters."""
    stmt = select(UsageAllowance)
    count_stmt = select(func.count(UsageAllowance.id))

    if is_active is not None:
        stmt = stmt.where(UsageAllowance.is_active == is_active)
        count_stmt = count_stmt.where(UsageAllowance.is_active == is_active)
    if search:
        term = f"%{search.strip()}%"
        search_filter = UsageAllowance.name.ilike(term)
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(UsageAllowance.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def usage_allowance_form_defaults() -> dict[str, object]:
    """Return blank defaults for the usage-allowance form."""
    return {
        "name": "",
        "included_gb": "",
        "overage_rate": "",
        "overage_cap_gb": "",
        "is_active": True,
    }


def usage_allowance_form_context(
    db: Session,
    *,
    allowance_id: str | None = None,
) -> dict[str, object] | None:
    """Return usage-allowance form context, or None when missing."""
    if allowance_id is None:
        return {
            "allowance": usage_allowance_form_defaults(),
            "action_url": "/admin/catalog/settings/usage-allowances",
        }
    try:
        obj = catalog_service.usage_allowances.get(
            db=db,
            allowance_id=allowance_id,
        )
    except Exception:
        return None
    return {
        "allowance": {
            "id": str(obj.id),
            "name": obj.name,
            "included_gb": obj.included_gb or "",
            "overage_rate": obj.overage_rate or "",
            "overage_cap_gb": obj.overage_cap_gb or "",
            "is_active": obj.is_active,
        },
        "action_url": (f"/admin/catalog/settings/usage-allowances/{allowance_id}/edit"),
    }


def parse_usage_allowance_form(form) -> dict[str, object]:
    """Parse usage-allowance form fields into template-friendly values."""

    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    return {
        "name": form_str("name").strip(),
        "included_gb": form_str("included_gb").strip(),
        "overage_rate": form_str("overage_rate").strip(),
        "overage_cap_gb": form_str("overage_cap_gb").strip(),
        "is_active": form_str("is_active") == "true",
    }


def _optional_int(value: object) -> int | None:
    raw = str(value or "").strip()
    return int(raw) if raw else None


def _usage_allowance_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "name": values["name"],
        "included_gb": _optional_int(values["included_gb"]),
        "overage_rate": values["overage_rate"] or None,
        "overage_cap_gb": _optional_int(values["overage_cap_gb"]),
        "is_active": values["is_active"],
    }


def create_usage_allowance_from_form(db: Session, *, form) -> None:
    """Validate and create a usage allowance from form data."""
    values = parse_usage_allowance_form(form)
    payload = UsageAllowanceCreate.model_validate(_usage_allowance_payload(values))
    catalog_service.usage_allowances.create(db=db, payload=payload)


def update_usage_allowance_from_form(
    db: Session,
    *,
    allowance_id: str,
    form,
) -> None:
    """Validate and update a usage allowance from form data."""
    values = parse_usage_allowance_form(form)
    payload = UsageAllowanceUpdate.model_validate(_usage_allowance_payload(values))
    catalog_service.usage_allowances.update(
        db=db,
        allowance_id=allowance_id,
        payload=payload,
    )


def delete_usage_allowance(db: Session, *, allowance_id: str) -> None:
    """Deactivate a usage allowance."""
    catalog_service.usage_allowances.delete(db=db, allowance_id=allowance_id)


def list_sla_profiles_paginated(
    db: Session,
    *,
    is_active: bool | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated SLA profiles with optional filters."""
    stmt = select(SlaProfile)
    count_stmt = select(func.count(SlaProfile.id))

    if is_active is not None:
        stmt = stmt.where(SlaProfile.is_active == is_active)
        count_stmt = count_stmt.where(SlaProfile.is_active == is_active)
    if search:
        term = f"%{search.strip()}%"
        search_filter = SlaProfile.name.ilike(term)
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(SlaProfile.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def sla_profile_form_defaults() -> dict[str, object]:
    """Return blank defaults for the SLA-profile form."""
    return {
        "name": "",
        "uptime_percent": "",
        "response_time_hours": "",
        "resolution_time_hours": "",
        "credit_percent": "",
        "notes": "",
        "is_active": True,
    }


def sla_profile_form_context(
    db: Session,
    *,
    profile_id: str | None = None,
) -> dict[str, object] | None:
    """Return SLA-profile form context, or None when missing."""
    if profile_id is None:
        return {
            "profile": sla_profile_form_defaults(),
            "action_url": "/admin/catalog/settings/sla-profiles",
        }
    try:
        obj = catalog_service.sla_profiles.get(db=db, profile_id=profile_id)
    except Exception:
        return None
    return {
        "profile": {
            "id": str(obj.id),
            "name": obj.name,
            "uptime_percent": obj.uptime_percent or "",
            "response_time_hours": obj.response_time_hours or "",
            "resolution_time_hours": obj.resolution_time_hours or "",
            "credit_percent": obj.credit_percent or "",
            "notes": obj.notes or "",
            "is_active": obj.is_active,
        },
        "action_url": f"/admin/catalog/settings/sla-profiles/{profile_id}/edit",
    }


def parse_sla_profile_form(form) -> dict[str, object]:
    """Parse SLA-profile form fields into template-friendly values."""

    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    return {
        "name": form_str("name").strip(),
        "uptime_percent": form_str("uptime_percent").strip(),
        "response_time_hours": form_str("response_time_hours").strip(),
        "resolution_time_hours": form_str("resolution_time_hours").strip(),
        "credit_percent": form_str("credit_percent").strip(),
        "notes": form_str("notes").strip(),
        "is_active": form_str("is_active") == "true",
    }


def _sla_profile_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "name": values["name"],
        "uptime_percent": values["uptime_percent"] or None,
        "response_time_hours": _optional_int(values["response_time_hours"]),
        "resolution_time_hours": _optional_int(values["resolution_time_hours"]),
        "credit_percent": values["credit_percent"] or None,
        "notes": values["notes"] or None,
        "is_active": values["is_active"],
    }


def create_sla_profile_from_form(db: Session, *, form) -> None:
    """Validate and create an SLA profile from form data."""
    values = parse_sla_profile_form(form)
    payload = SlaProfileCreate.model_validate(_sla_profile_payload(values))
    catalog_service.sla_profiles.create(db=db, payload=payload)


def update_sla_profile_from_form(db: Session, *, profile_id: str, form) -> None:
    """Validate and update an SLA profile from form data."""
    values = parse_sla_profile_form(form)
    payload = SlaProfileUpdate.model_validate(_sla_profile_payload(values))
    catalog_service.sla_profiles.update(
        db=db,
        profile_id=profile_id,
        payload=payload,
    )


def delete_sla_profile(db: Session, *, profile_id: str) -> None:
    """Deactivate an SLA profile."""
    catalog_service.sla_profiles.delete(db=db, profile_id=profile_id)


def list_policy_sets_paginated(
    db: Session,
    *,
    is_active: bool | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated policy sets with optional filters."""
    stmt = select(PolicySet)
    count_stmt = select(func.count(PolicySet.id))

    if is_active is not None:
        stmt = stmt.where(PolicySet.is_active == is_active)
        count_stmt = count_stmt.where(PolicySet.is_active == is_active)
    if search:
        term = f"%{search.strip()}%"
        search_filter = PolicySet.name.ilike(term)
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(PolicySet.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def policy_set_form_defaults() -> dict[str, object]:
    """Return blank defaults for the policy-set form."""
    return {
        "name": "",
        "proration_policy": "immediate",
        "downgrade_policy": "next_cycle",
        "trial_days": "",
        "trial_card_required": False,
        "grace_days": "",
        "refund_policy": "none",
        "refund_window_days": "",
        "is_active": True,
        "dunning_steps": [],
    }


def policy_set_form_options() -> dict[str, list[str]]:
    """Return enum option lists for policy-set forms."""
    return {
        "proration_policies": [item.value for item in ProrationPolicy],
        "refund_policies": [item.value for item in RefundPolicy],
        "dunning_actions": [item.value for item in DunningAction],
    }


def policy_set_form_context(
    db: Session,
    *,
    policy_id: str | None = None,
) -> dict[str, object] | None:
    """Return policy-set form context, or None when editing a missing policy."""
    if policy_id is None:
        return {
            "policy": policy_set_form_defaults(),
            "action_url": "/admin/catalog/settings/policy-sets",
            **policy_set_form_options(),
        }
    try:
        obj = catalog_service.policy_sets.get(db=db, policy_id=policy_id)
    except Exception:
        return None

    steps = catalog_service.policy_dunning_steps.list(
        db=db,
        policy_set_id=policy_id,
        order_by="day_offset",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    return {
        "policy": {
            "id": str(obj.id),
            "name": obj.name,
            "proration_policy": obj.proration_policy.value
            if obj.proration_policy
            else "immediate",
            "downgrade_policy": obj.downgrade_policy.value
            if obj.downgrade_policy
            else "next_cycle",
            "trial_days": obj.trial_days or "",
            "trial_card_required": obj.trial_card_required,
            "grace_days": obj.grace_days if obj.grace_days is not None else "",
            "refund_policy": obj.refund_policy.value if obj.refund_policy else "none",
            "refund_window_days": obj.refund_window_days or "",
            "is_active": obj.is_active,
            "dunning_steps": [
                {
                    "id": str(step.id),
                    "day_offset": step.day_offset,
                    "action": step.action.value,
                    "note": step.note or "",
                }
                for step in steps
            ],
        },
        "action_url": f"/admin/catalog/settings/policy-sets/{policy_id}/edit",
        **policy_set_form_options(),
    }


def parse_policy_dunning_steps_form(
    form,
    *,
    include_ids: bool,
) -> list[dict[str, str]]:
    """Parse indexed dunning-step fields from the policy-set form."""

    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    steps: list[dict[str, str]] = []
    index = 0
    while True:
        day_value = form.get(f"dunning_steps[{index}][day_offset]")
        if not isinstance(day_value, str):
            break
        action = form_str(f"dunning_steps[{index}][action]").strip()
        note = form_str(f"dunning_steps[{index}][note]").strip()
        if day_value.strip() and action:
            step = {
                "day_offset": day_value.strip(),
                "action": action,
                "note": note,
            }
            if include_ids:
                step["id"] = form_str(f"dunning_steps[{index}][id]").strip()
            steps.append(step)
        index += 1
    return steps


def parse_policy_set_form(
    form, *, include_dunning_ids: bool = False
) -> dict[str, object]:
    """Parse policy-set form fields into template-friendly values."""

    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    return {
        "name": form_str("name").strip(),
        "proration_policy": form_str("proration_policy", "immediate").strip(),
        "downgrade_policy": form_str("downgrade_policy", "next_cycle").strip(),
        "trial_days": form_str("trial_days").strip(),
        "trial_card_required": form_str("trial_card_required") == "true",
        "grace_days": form_str("grace_days").strip(),
        "refund_policy": form_str("refund_policy", "none").strip(),
        "refund_window_days": form_str("refund_window_days").strip(),
        "is_active": form_str("is_active") == "true",
        "dunning_steps": parse_policy_dunning_steps_form(
            form,
            include_ids=include_dunning_ids,
        ),
    }


def _policy_set_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "name": values["name"],
        "proration_policy": ProrationPolicy(str(values["proration_policy"])),
        "downgrade_policy": ProrationPolicy(str(values["downgrade_policy"])),
        "trial_days": _optional_int(values["trial_days"]),
        "trial_card_required": values["trial_card_required"],
        "grace_days": _optional_int(values["grace_days"]),
        "refund_policy": RefundPolicy(str(values["refund_policy"])),
        "refund_window_days": _optional_int(values["refund_window_days"]),
        "is_active": values["is_active"],
    }


def create_policy_set_from_form(db: Session, *, form) -> None:
    """Validate and create a policy set with submitted dunning steps."""
    values = parse_policy_set_form(form)
    payload = PolicySetCreate.model_validate(_policy_set_payload(values))
    created = catalog_service.policy_sets.create(db=db, payload=payload)
    create_dunning_steps(
        db,
        str(created.id),
        values["dunning_steps"],  # type: ignore[arg-type]
    )


def update_policy_set_from_form(db: Session, *, policy_id: str, form) -> None:
    """Validate and update a policy set with submitted dunning steps."""
    values = parse_policy_set_form(form, include_dunning_ids=True)
    payload = PolicySetUpdate.model_validate(_policy_set_payload(values))
    catalog_service.policy_sets.update(db=db, policy_id=policy_id, payload=payload)
    sync_dunning_steps(
        db,
        policy_id,
        values["dunning_steps"],  # type: ignore[arg-type]
    )


def delete_policy_set(db: Session, *, policy_id: str) -> None:
    """Deactivate a policy set."""
    catalog_service.policy_sets.delete(db=db, policy_id=policy_id)


def list_add_ons_paginated(
    db: Session,
    *,
    is_active: bool | None,
    addon_type: str | None,
    search: str | None,
    page: int,
    per_page: int,
) -> PaginatedResult:
    """Return paginated add-ons with optional filters."""
    stmt = select(AddOn)
    count_stmt = select(func.count(AddOn.id))

    if is_active is not None:
        stmt = stmt.where(AddOn.is_active == is_active)
        count_stmt = count_stmt.where(AddOn.is_active == is_active)
    if addon_type:
        stmt = stmt.where(AddOn.addon_type == addon_type)
        count_stmt = count_stmt.where(AddOn.addon_type == addon_type)
    if search:
        term = f"%{search.strip()}%"
        search_filter = AddOn.name.ilike(term)
        stmt = stmt.where(search_filter)
        count_stmt = count_stmt.where(search_filter)

    total = db.execute(count_stmt).scalar() or 0
    total_pages = (total + per_page - 1) // per_page if total else 1
    offset = (page - 1) * per_page
    items = db.scalars(
        stmt.order_by(AddOn.name.asc()).offset(offset).limit(per_page)
    ).all()
    return PaginatedResult(items=list(items), total=total, total_pages=total_pages)


def add_on_form_defaults() -> dict[str, object]:
    """Return blank defaults for the add-on form."""
    return {
        "name": "",
        "addon_type": "custom",
        "description": "",
        "is_active": True,
        "prices": [],
    }


def add_on_form_options() -> dict[str, list[str]]:
    """Return enum option lists for add-on forms."""
    return {
        "addon_types": [item.value for item in AddOnType],
        "price_types": [item.value for item in PriceType],
        "billing_cycles": [item.value for item in BillingCycle],
        "price_units": [item.value for item in PriceUnit],
    }


def add_on_form_context(
    db: Session,
    *,
    addon_id: str | None = None,
) -> dict[str, object] | None:
    """Return add-on form context, or None when editing a missing add-on."""
    if addon_id is None:
        return {
            "addon": add_on_form_defaults(),
            "action_url": "/admin/catalog/settings/add-ons",
            **add_on_form_options(),
        }
    try:
        obj = catalog_service.add_ons.get(db=db, add_on_id=addon_id)
    except Exception:
        return None

    prices = catalog_service.add_on_prices.list(
        db=db,
        add_on_id=addon_id,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    return {
        "addon": {
            "id": str(obj.id),
            "name": obj.name,
            "addon_type": obj.addon_type.value if obj.addon_type else "custom",
            "description": obj.description or "",
            "is_active": obj.is_active,
            "prices": [
                {
                    "id": str(price.id),
                    "price_type": price.price_type.value,
                    "amount": str(price.amount),
                    "currency": price.currency,
                    "billing_cycle": price.billing_cycle.value
                    if price.billing_cycle
                    else "",
                    "unit": price.unit.value if price.unit else "",
                    "description": price.description or "",
                }
                for price in prices
            ],
        },
        "action_url": f"/admin/catalog/settings/add-ons/{addon_id}/edit",
        **add_on_form_options(),
    }


def parse_add_on_prices_form(form, *, include_ids: bool) -> list[dict[str, str]]:
    """Parse indexed price fields from the add-on form."""

    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    prices: list[dict[str, str]] = []
    index = 0
    while True:
        amount = form.get(f"prices[{index}][amount]")
        if not isinstance(amount, str):
            break
        price_type = form_str(f"prices[{index}][price_type]").strip()
        currency = form_str(f"prices[{index}][currency]", "NGN").strip()
        billing_cycle = form_str(f"prices[{index}][billing_cycle]").strip()
        unit = form_str(f"prices[{index}][unit]").strip()
        description = form_str(f"prices[{index}][description]").strip()
        if amount.strip() and price_type:
            price = {
                "price_type": price_type,
                "amount": amount.strip(),
                "currency": currency,
                "billing_cycle": billing_cycle,
                "unit": unit,
                "description": description,
            }
            if include_ids:
                price["id"] = form_str(f"prices[{index}][id]").strip()
            prices.append(price)
        index += 1
    return prices


def parse_add_on_form(form, *, include_price_ids: bool = False) -> dict[str, object]:
    """Parse add-on form fields into template-friendly values."""

    def form_str(key: str, default: str = "") -> str:
        value = form.get(key, default)
        return value if isinstance(value, str) else default

    return {
        "name": form_str("name").strip(),
        "addon_type": form_str("addon_type", "custom").strip(),
        "description": form_str("description").strip(),
        "is_active": form_str("is_active") == "true",
        "prices": parse_add_on_prices_form(form, include_ids=include_price_ids),
    }


def _add_on_payload(values: dict[str, object]) -> dict[str, object]:
    return {
        "name": values["name"],
        "addon_type": AddOnType(str(values["addon_type"])),
        "description": values["description"] or None,
        "is_active": values["is_active"],
    }


def create_add_on_from_form(
    db: Session,
    *,
    form,
    actor_id: str | None = None,
) -> None:
    """Validate and create an add-on with submitted prices."""
    values = parse_add_on_form(form)
    submitted_prices = cast(list[dict[str, str]], values["prices"])
    price_types = [price["price_type"] for price in submitted_prices]
    if len(price_types) != len(set(price_types)):
        raise ValueError("Only one active add-on price is allowed per price type")
    payload = AddOnCreate.model_validate(_add_on_payload(values))
    created = catalog_service.add_ons.create(db=db, payload=payload)
    create_addon_prices(
        db,
        str(created.id),
        values["prices"],  # type: ignore[arg-type]
        actor_id=actor_id,
    )


def update_add_on_from_form(
    db: Session,
    *,
    addon_id: str,
    form,
    actor_id: str | None = None,
) -> None:
    """Validate and update an add-on with submitted prices."""
    values = parse_add_on_form(form, include_price_ids=True)
    sync_addon_prices(
        db,
        addon_id,
        values["prices"],  # type: ignore[arg-type]
        actor_id=actor_id,
        validate_only=True,
    )
    payload = AddOnUpdate.model_validate(_add_on_payload(values))
    catalog_service.add_ons.update(db=db, add_on_id=addon_id, payload=payload)
    sync_addon_prices(
        db,
        addon_id,
        values["prices"],  # type: ignore[arg-type]
        actor_id=actor_id,
    )


def delete_add_on(db: Session, *, addon_id: str) -> None:
    """Deactivate an add-on."""
    catalog_service.add_ons.delete(db=db, add_on_id=addon_id)


# ---------------------------------------------------------------------------
# Bulk delete helpers
# ---------------------------------------------------------------------------


def bulk_delete_region_zones(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of region zones."""
    for zone_id in ids:
        try:
            catalog_service.region_zones.delete(db=db, zone_id=zone_id)
        except Exception:
            logger.debug("Skipping region zone %s during bulk delete", zone_id)


def bulk_delete_usage_allowances(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of usage allowances."""
    for allowance_id in ids:
        try:
            catalog_service.usage_allowances.delete(db=db, allowance_id=allowance_id)
        except Exception:
            logger.debug("Skipping usage allowance %s during bulk delete", allowance_id)


def bulk_delete_sla_profiles(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of SLA profiles."""
    for profile_id in ids:
        try:
            catalog_service.sla_profiles.delete(db=db, profile_id=profile_id)
        except Exception:
            logger.debug("Skipping SLA profile %s during bulk delete", profile_id)


def bulk_delete_policy_sets(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of policy sets."""
    for policy_id in ids:
        try:
            catalog_service.policy_sets.delete(db=db, policy_id=policy_id)
        except Exception:
            logger.debug("Skipping policy set %s during bulk delete", policy_id)


def bulk_delete_add_ons(db: Session, ids: list[str]) -> None:
    """Deactivate a batch of add-ons."""
    for addon_id in ids:
        try:
            catalog_service.add_ons.delete(db=db, add_on_id=addon_id)
        except Exception:
            logger.debug("Skipping add-on %s during bulk delete", addon_id)


# ---------------------------------------------------------------------------
# CSV export helpers
# ---------------------------------------------------------------------------


def export_region_zones_csv(db: Session) -> str:
    """Return CSV content for all region zones."""
    zones = db.scalars(select(RegionZone).order_by(RegionZone.name.asc())).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Code", "Description", "Active"])
    for z in zones:
        writer.writerow(
            [
                str(z.id),
                z.name,
                z.code or "",
                z.description or "",
                "Yes" if z.is_active else "No",
            ]
        )
    return output.getvalue()


def export_usage_allowances_csv(db: Session) -> str:
    """Return CSV content for all usage allowances."""
    allowances = db.scalars(
        select(UsageAllowance).order_by(UsageAllowance.name.asc())
    ).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "ID",
            "Name",
            "Included GB",
            "Overage Rate",
            "Overage Cap GB",
            "Active",
        ]
    )
    for a in allowances:
        writer.writerow(
            [
                str(a.id),
                a.name,
                a.included_gb or "",
                a.overage_rate or "",
                a.overage_cap_gb or "",
                "Yes" if a.is_active else "No",
            ]
        )
    return output.getvalue()


def export_sla_profiles_csv(db: Session) -> str:
    """Return CSV content for all SLA profiles."""
    profiles = db.scalars(select(SlaProfile).order_by(SlaProfile.name.asc())).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "ID",
            "Name",
            "Uptime %",
            "Response Hours",
            "Resolution Hours",
            "Credit %",
            "Notes",
            "Active",
        ]
    )
    for p in profiles:
        writer.writerow(
            [
                str(p.id),
                p.name,
                p.uptime_percent or "",
                p.response_time_hours or "",
                p.resolution_time_hours or "",
                p.credit_percent or "",
                p.notes or "",
                "Yes" if p.is_active else "No",
            ]
        )
    return output.getvalue()


def export_policy_sets_csv(db: Session) -> str:
    """Return CSV content for all policy sets."""
    policies = db.scalars(select(PolicySet).order_by(PolicySet.name.asc())).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "ID",
            "Name",
            "Proration Policy",
            "Downgrade Policy",
            "Trial Days",
            "Trial Card Required",
            "Grace Days",
            "Suspension Action",
            "Refund Policy",
            "Refund Window Days",
            "Active",
        ]
    )
    for p in policies:
        writer.writerow(
            [
                str(p.id),
                p.name,
                p.proration_policy.value if p.proration_policy else "",
                p.downgrade_policy.value if p.downgrade_policy else "",
                p.trial_days or "",
                "Yes" if p.trial_card_required else "No",
                p.grace_days if p.grace_days is not None else "",
                p.suspension_action.value if p.suspension_action else "",
                p.refund_policy.value if p.refund_policy else "",
                p.refund_window_days or "",
                "Yes" if p.is_active else "No",
            ]
        )
    return output.getvalue()


def export_add_ons_csv(db: Session) -> str:
    """Return CSV content for all add-ons."""
    add_ons = db.scalars(select(AddOn).order_by(AddOn.name.asc())).all()
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Type", "Description", "Active"])
    for a in add_ons:
        writer.writerow(
            [
                str(a.id),
                a.name,
                a.addon_type.value if a.addon_type else "",
                a.description or "",
                "Yes" if a.is_active else "No",
            ]
        )
    return output.getvalue()


# ---------------------------------------------------------------------------
# Dunning step sync (policy set create + update)
# ---------------------------------------------------------------------------


def create_dunning_steps(
    db: Session,
    policy_set_id: str,
    dunning_steps: list[dict[str, str]],
) -> None:
    """Create dunning steps for a newly-created policy set."""
    policy_uuid = coerce_uuid(policy_set_id)
    for step in dunning_steps:
        step_payload = PolicyDunningStepCreate(
            policy_set_id=policy_uuid,
            day_offset=int(step["day_offset"]),
            action=DunningAction(step["action"]),
            note=step["note"] or None,
        )
        catalog_service.policy_dunning_steps.create(db=db, payload=step_payload)


def sync_dunning_steps(
    db: Session,
    policy_id: str,
    dunning_steps: list[dict[str, str]],
) -> None:
    """Reconcile submitted dunning steps against the existing set.

    Deletes steps that were removed, updates existing ones, and creates new ones.
    """
    existing_steps = catalog_service.policy_dunning_steps.list(
        db=db,
        policy_set_id=policy_id,
        order_by="day_offset",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    existing_ids = {str(s.id) for s in existing_steps}
    submitted_ids = {s["id"] for s in dunning_steps if s.get("id")}

    # Delete removed steps
    for step in existing_steps:
        if str(step.id) not in submitted_ids:
            catalog_service.policy_dunning_steps.delete(db=db, step_id=str(step.id))

    # Update or create steps
    for step in dunning_steps:
        if step.get("id") and step["id"] in existing_ids:
            catalog_service.policy_dunning_steps.update(
                db=db,
                step_id=step["id"],
                payload=PolicyDunningStepUpdate(
                    day_offset=int(step["day_offset"]),
                    action=DunningAction(step["action"]),
                    note=step["note"] or None,
                ),
            )
        else:
            catalog_service.policy_dunning_steps.create(
                db=db,
                payload=PolicyDunningStepCreate(
                    policy_set_id=coerce_uuid(policy_id),
                    day_offset=int(step["day_offset"]),
                    action=DunningAction(step["action"]),
                    note=step["note"] or None,
                ),
            )


# ---------------------------------------------------------------------------
# Add-on price sync (add-on create + update)
# ---------------------------------------------------------------------------


def create_addon_prices(
    db: Session,
    add_on_id: str,
    prices: list[dict[str, str]],
    *,
    actor_id: str | None = None,
) -> None:
    """Create prices for a newly-created add-on."""
    price_types = [price["price_type"] for price in prices]
    if len(price_types) != len(set(price_types)):
        raise ValueError("Only one active add-on price is allowed per price type")
    addon_uuid = coerce_uuid(add_on_id)
    for price in prices:
        price_payload = AddOnPriceCreate(
            add_on_id=addon_uuid,
            price_type=PriceType(price["price_type"]),
            amount=Decimal(price["amount"]),
            currency=price["currency"] or "NGN",
            billing_cycle=BillingCycle(price["billing_cycle"])
            if price["billing_cycle"]
            else None,
            unit=PriceUnit(price["unit"]) if price["unit"] else None,
            description=price["description"] or None,
        )
        catalog_service.add_on_prices.create(
            db=db,
            payload=price_payload,
            actor_id=actor_id,
            actor_type="user",
        )


def sync_addon_prices(
    db: Session,
    addon_id: str,
    prices: list[dict[str, str]],
    *,
    actor_id: str | None = None,
    validate_only: bool = False,
) -> None:
    """Reconcile submitted add-on prices against the existing set.

    Deletes prices that were removed, updates existing ones, and creates new ones.
    """
    existing_prices = catalog_service.add_on_prices.list(
        db=db,
        add_on_id=addon_id,
        is_active=None,
        order_by="created_at",
        order_dir="asc",
        limit=100,
        offset=0,
    )
    existing_ids = {str(p.id) for p in existing_prices}
    submitted_ids = {p["id"] for p in prices if p.get("id")}
    submitted_types = [price["price_type"] for price in prices]
    if len(submitted_types) != len(set(submitted_types)):
        raise ValueError("Only one active add-on price is allowed per price type")

    # Validate the complete financial mutation before the first service method
    # commits, so a blocked live-price edit cannot leave a partially-updated
    # add-on form behind.
    for existing in existing_prices:
        if str(existing.id) not in submitted_ids:
            catalog_billing_governance.assert_add_on_price_update_safe(
                db,
                existing,
                {"is_active": False},
            )
    by_id = {str(existing.id): existing for existing in existing_prices}
    for price in prices:
        existing = by_id.get(str(price.get("id") or ""))
        if existing is None:
            continue
        candidate = {
            "price_type": PriceType(price["price_type"]),
            "amount": Decimal(price["amount"]),
            "currency": price["currency"] or "NGN",
            "billing_cycle": BillingCycle(price["billing_cycle"])
            if price["billing_cycle"]
            else None,
            "unit": PriceUnit(price["unit"]) if price["unit"] else None,
        }
        changes = catalog_billing_governance.billing_field_changes(
            existing,
            candidate,
        )
        catalog_billing_governance.assert_add_on_price_update_safe(
            db,
            existing,
            changes,
        )

    if validate_only:
        return

    # Delete removed prices
    for price in existing_prices:
        if str(price.id) not in submitted_ids:
            catalog_service.add_on_prices.delete(
                db=db,
                price_id=str(price.id),
                actor_id=actor_id,
                actor_type="user",
            )

    # Update or create prices
    for price in prices:
        if price.get("id") and price["id"] in existing_ids:
            catalog_service.add_on_prices.update(
                db=db,
                price_id=price["id"],
                payload=AddOnPriceUpdate(
                    price_type=PriceType(price["price_type"]),
                    amount=Decimal(price["amount"]),
                    currency=price["currency"] or "NGN",
                    billing_cycle=BillingCycle(price["billing_cycle"])
                    if price["billing_cycle"]
                    else None,
                    unit=PriceUnit(price["unit"]) if price["unit"] else None,
                    description=price["description"] or None,
                ),
                actor_id=actor_id,
                actor_type="user",
            )
        else:
            catalog_service.add_on_prices.create(
                db=db,
                payload=AddOnPriceCreate(
                    add_on_id=coerce_uuid(addon_id),
                    price_type=PriceType(price["price_type"]),
                    amount=Decimal(price["amount"]),
                    currency=price["currency"] or "NGN",
                    billing_cycle=BillingCycle(price["billing_cycle"])
                    if price["billing_cycle"]
                    else None,
                    unit=PriceUnit(price["unit"]) if price["unit"] else None,
                    description=price["description"] or None,
                ),
                actor_id=actor_id,
                actor_type="user",
            )
