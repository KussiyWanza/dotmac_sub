"""Native project cost projection over field worklogs and approved expenses."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.domain_settings import SettingDomain
from app.models.field_expense import FieldExpenseRequest, FieldExpenseRequestItem
from app.models.field_worklog import FieldWorkLog
from app.models.work_order import WorkOrder
from app.schemas.project import ProjectCostSummary
from app.services import projects as projects_service
from app.services import settings_spec
from app.services.common import coerce_uuid

_COSTED_EXPENSE_STATUSES = ("approved", "paid")
_ZERO = Decimal("0.00")


def _decimal_setting(
    db: Session, domain: SettingDomain, key: str, default: str
) -> Decimal:
    value = settings_spec.resolve_value(db, domain, key)
    try:
        return Decimal(str(value if value is not None else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def project_cost_summary(db: Session, project_id: str) -> ProjectCostSummary:
    """Return authoritative native field cost totals for one project."""
    project = projects_service.projects.get(db, project_id)
    project_uuid = coerce_uuid(str(project.id))
    labor_minutes = int(
        db.query(func.coalesce(func.sum(FieldWorkLog.minutes), 0))
        .join(WorkOrder, WorkOrder.id == FieldWorkLog.work_order_mirror_id)
        .filter(WorkOrder.project_id == project_uuid)
        .filter(WorkOrder.is_active.is_(True))
        .filter(FieldWorkLog.is_active.is_(True))
        .scalar()
        or 0
    )
    hourly_rate = _decimal_setting(
        db, SettingDomain.projects, "default_labor_hourly_rate", "0.00"
    )
    labor_cost = (Decimal(labor_minutes) * hourly_rate / Decimal(60)).quantize(
        Decimal("0.01")
    )
    expense_total = (
        db.query(func.coalesce(func.sum(FieldExpenseRequestItem.amount), _ZERO))
        .join(
            FieldExpenseRequest,
            FieldExpenseRequest.id == FieldExpenseRequestItem.expense_request_id,
        )
        .join(
            WorkOrder,
            WorkOrder.id == FieldExpenseRequest.work_order_mirror_id,
        )
        .filter(
            WorkOrder.project_id == project_uuid,
            WorkOrder.is_active.is_(True),
            FieldExpenseRequest.is_active.is_(True),
            FieldExpenseRequest.status.in_(_COSTED_EXPENSE_STATUSES),
        )
        .scalar()
    )
    expense_total = Decimal(expense_total or _ZERO).quantize(Decimal("0.01"))
    currency = str(
        settings_spec.resolve_value(db, SettingDomain.billing, "default_currency")
        or "NGN"
    )
    return ProjectCostSummary(
        project_id=project_uuid,
        currency=currency,
        labor_minutes=labor_minutes,
        labor_hourly_rate=hourly_rate,
        labor_cost=labor_cost,
        expense_total=expense_total,
        total_cost=labor_cost + expense_total,
    )
