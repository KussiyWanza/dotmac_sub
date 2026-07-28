from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.models.dispatch import TechnicianProfile
from app.models.field_expense import FieldExpenseRequest, FieldExpenseRequestItem
from app.models.field_worklog import FieldWorkLog
from app.models.work_order import WorkOrder
from app.schemas.project import ProjectCreate
from app.services import project_costs
from app.services.projects import projects


def test_project_cost_summary_uses_native_worklogs_and_approved_expenses(
    db_session, subscriber, monkeypatch
):
    project = projects.create(
        db_session,
        ProjectCreate(name="Costed build", subscriber_id=subscriber.id),
    )
    order = WorkOrder(
        subscriber_id=subscriber.id,
        project_id=project.id,
        title="Install",
    )
    profile = TechnicianProfile(person_id=uuid4(), crm_person_id="cost-tech")
    db_session.add_all([order, profile])
    db_session.flush()
    db_session.add(
        FieldWorkLog(
            work_order_mirror_id=order.id,
            author_technician_id=profile.id,
            person_id=profile.person_id,
            start_at=datetime.now(UTC),
            minutes=90,
        )
    )
    expense = FieldExpenseRequest(
        work_order_mirror_id=order.id,
        requested_by_technician_id=profile.id,
        requested_by_person_id=profile.person_id,
        status="approved",
        purpose="Drop materials",
    )
    db_session.add(expense)
    db_session.flush()
    db_session.add(
        FieldExpenseRequestItem(
            expense_request_id=expense.id,
            category_code="materials",
            description="Drop cable",
            amount=Decimal("500.00"),
        )
    )
    db_session.commit()

    def _setting(_db, _domain, key):
        return "120.00" if key == "default_labor_hourly_rate" else "NGN"

    monkeypatch.setattr(project_costs.settings_spec, "resolve_value", _setting)
    summary = project_costs.project_cost_summary(db_session, str(project.id))

    assert summary.labor_minutes == 90
    assert summary.labor_cost == Decimal("180.00")
    assert summary.expense_total == Decimal("500.00")
    assert summary.total_cost == Decimal("680.00")
    assert summary.currency == "NGN"
