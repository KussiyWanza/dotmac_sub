"""Hidden, read-only Selfcare customer retention work queue."""

from __future__ import annotations

import logging
from collections import Counter
from typing import TypedDict

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import crm_api
from app.services.auth_dependencies import require_permission
from app.web.templates import templates

router = APIRouter(tags=["web-admin-customer-retention"])
logger = logging.getLogger(__name__)


def _float_value(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        return float(value or 0)
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value or 0))


class RetentionRow(TypedDict):
    customer_id: str
    name: str
    phone: str
    email: str
    status: str
    location: str
    plan: str
    balance: float
    next_bill_date: str
    billing_start_date: str
    blocked_date: str
    risk_segment: str
    recommended_action: str


def _risk_segment(row: dict[str, object]) -> str:
    if str(row.get("blocked_date") or "").strip():
        return "Suspended"
    if _float_value(row.get("balance")) > 0:
        return "Due Soon"
    return "Active"


def _recommended_action(segment: str, balance: float) -> str:
    if segment == "Suspended":
        return "Review the blocked account and confirm restore conditions."
    if balance >= 50000:
        return "Prioritize the account for billing follow-up."
    return "Review billing position and keep the account under observation."


def _normalize_rows(raw_rows: list[dict[str, object]]) -> list[RetentionRow]:
    rows: list[RetentionRow] = []
    for raw in raw_rows:
        balance = _float_value(raw.get("balance"))
        segment = _risk_segment(raw)
        if segment == "Active":
            continue
        customer_id = str(raw.get("id") or "").strip()
        if not customer_id:
            continue
        rows.append(
            {
                "customer_id": customer_id,
                "name": str(raw.get("name") or "Unknown customer"),
                "phone": str(raw.get("phone") or ""),
                "email": str(raw.get("email") or ""),
                "status": str(raw.get("status") or "Unknown"),
                "location": str(raw.get("location") or "Unknown location"),
                "plan": str(raw.get("service_plan") or "N/A"),
                "balance": balance,
                "next_bill_date": str(raw.get("next_bill_date") or ""),
                "billing_start_date": str(raw.get("billing_start_date") or ""),
                "blocked_date": str(raw.get("blocked_date") or ""),
                "risk_segment": segment,
                "recommended_action": _recommended_action(segment, balance),
            }
        )
    rows.sort(
        key=lambda row: (
            0 if row["risk_segment"] == "Suspended" else 1,
            -row["balance"],
            row["name"].casefold(),
        )
    )
    return rows


def _load_rows(db: Session, search: str | None) -> list[RetentionRow]:
    try:
        raw_rows, _total = crm_api.billing_risk_rows(db, page=1, per_page=10000)
    except Exception as exc:
        logger.warning(
            "customer_retention_billing_risk_unavailable",
            extra={"error_type": type(exc).__name__},
        )
        return []
    rows = _normalize_rows(raw_rows)
    term = str(search or "").strip().casefold()
    if not term:
        return rows
    return [
        row
        for row in rows
        if any(
            term in value.casefold()
            for value in (
                row["customer_id"],
                row["name"],
                row["phone"],
                row["email"],
                row["location"],
            )
        )
    ]


def _base_context(request: Request, db: Session, active_page: str) -> dict[str, object]:
    from app.web.admin import get_current_user, get_sidebar_stats

    return {
        "request": request,
        "current_user": get_current_user(request),
        "sidebar_stats": get_sidebar_stats(db),
        "active_page": active_page,
        "active_menu": "reports",
    }


@router.get(
    "/customer-retention",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def customer_retention_tracker(
    request: Request,
    search: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = _load_rows(db, search)
    segments = Counter(row["risk_segment"] for row in rows)
    total = len(rows)
    segment_breakdown = [
        {
            "segment": segment,
            "count": count,
            "share_pct": round((count / total) * 100, 1) if total else 0,
        }
        for segment, count in segments.most_common()
    ]
    context = _base_context(request, db, "customer-retention")
    context.update(
        {
            "rows": rows,
            "search": search or "",
            "tracked_count": total,
            "revenue_at_risk": round(sum(row["balance"] for row in rows), 2),
            "segment_breakdown": segment_breakdown,
            "crm_state_unavailable": True,
        }
    )
    return templates.TemplateResponse(
        "admin/reports/customer_retention_tracker.html", context
    )


@router.get(
    "/customer-retention/{customer_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_permission("reports:billing:read"))],
)
def customer_retention_profile(
    customer_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    rows = _load_rows(db, customer_id)
    customer = next((row for row in rows if row["customer_id"] == customer_id), None)
    context = _base_context(request, db, "customer-retention")
    context.update(
        {
            "customer": customer,
            "customer_id": customer_id,
            "crm_state_unavailable": True,
        }
    )
    return templates.TemplateResponse(
        "admin/reports/customer_retention_profile.html", context
    )
