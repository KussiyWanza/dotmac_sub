from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
)
from app.models.field_expense import FieldExpenseRequest, FieldExpenseRequestItem
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import web_work_order_expenses as expense_web
from app.services.backoffice import ExpenseCategoryView
from app.services.field import attachments as attachments_module
from app.services.field.expense_requests import (
    ExpenseCategoryRule,
    ExpenseReceiptUploadInput,
    ExpenseRequestAccessMode,
    ExpenseRequestLineInput,
    FieldExpenseRequestError,
    SubmitFieldExpenseRequest,
    submit_field_expense_request_command,
)
from app.services.file_storage import FileValidationError
from app.services.owner_commands import CommandContext


def _user(db_session, name: str) -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Staff",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _work_order(db_session, public_id: str) -> WorkOrder:
    subscriber = Subscriber(
        first_name="Work",
        last_name="Order",
        email=f"work-order-{uuid4().hex[:8]}@example.com",
    )
    db_session.add(subscriber)
    db_session.flush()
    row = WorkOrder(
        public_id=public_id,
        subscriber_id=subscriber.id,
        title="Repair fibre drop",
        status="in_progress",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _context(user: SystemUser, request_id):
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"user:{user.id}",
        scope="operations:dispatch:write",
        reason="Create an expense from a work order",
        idempotency_key=str(request_id),
    )


def _line(**overrides) -> ExpenseRequestLineInput:
    values = {
        "category_code": "transport",
        "category_name": "Transport",
        "description": "Taxi to customer site",
        "amount": Decimal("2500.00"),
        "expense_date": date.today(),
        "vendor_name": "City Cab",
        "receipt_url": None,
        "receipt_attachment_id": None,
        "notes": None,
    }
    values.update(overrides)
    return ExpenseRequestLineInput(**values)


def _command(user: SystemUser, work_order: WorkOrder, **overrides):
    request_id = overrides.pop("request_id", uuid4())
    values = {
        "context": _context(user, request_id),
        "requester_person_id": None,
        "work_order_public_id": work_order.public_id,
        "request_id": request_id,
        "purpose": "Travel for fibre repair",
        "expense_date": date.today(),
        "currency": "NGN",
        "notes": "Customer outage",
        "items": (_line(),),
        "access_mode": ExpenseRequestAccessMode.STAFF_WORK_ORDER,
        "authorized_work_order_id": work_order.id,
        "category_rules": (
            ExpenseCategoryRule(
                category_code="transport",
                category_name="Transport",
                requires_receipt=False,
                max_amount_per_claim=Decimal("10000.00"),
            ),
        ),
    }
    values.update(overrides)
    return SubmitFieldExpenseRequest(**values)


def _valid_form(*, amount: str = "2500.00") -> expense_web.WorkOrderExpenseFormInput:
    return expense_web.WorkOrderExpenseFormInput(
        request_id=str(uuid4()),
        purpose="Travel for fibre repair",
        expense_date=date.today().isoformat(),
        currency="ngn",
        notes="Customer outage",
        lines=(
            expense_web.ExpenseLineFormInput(
                key="lineone",
                category_code="transport",
                description="Taxi to customer site",
                amount=amount,
                expense_date="",
                vendor_name="City Cab",
                receipt_url="",
                notes="",
            ),
        ),
    )


def _rules(*, receipt: bool = False, maximum: str = "10000.00"):
    return (
        ExpenseCategoryRule(
            category_code="transport",
            category_name="Transport",
            requires_receipt=receipt,
            max_amount_per_claim=Decimal(maximum),
        ),
    )


def test_staff_without_technician_profile_can_submit_for_authorized_work_order(
    db_session,
):
    user = _user(db_session, "Ada")
    work_order = _work_order(db_session, "sub-expense-staff")
    request_id = uuid4()
    command = _command(user, work_order, request_id=request_id)
    db_session.commit()

    created = submit_field_expense_request_command(db_session, command)
    replayed = submit_field_expense_request_command(db_session, command)

    assert replayed.id == created.id
    stored = db_session.get(FieldExpenseRequest, created.id)
    assert stored is not None
    assert stored.work_order_mirror_id == work_order.id
    assert stored.requested_by_technician_id is None
    assert stored.requested_by_system_user_id == user.id
    assert stored.requested_by_person_id == user.id
    assert stored.status == "submitted"
    assert stored.total_amount == Decimal("2500.00")


def test_staff_command_rejects_missing_exact_work_order_access_evidence(db_session):
    user = _user(db_session, "Bola")
    work_order = _work_order(db_session, "sub-expense-no-proof")
    command = _command(user, work_order, authorized_work_order_id=None)
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(
            db_session,
            command,
        )

    assert exc.value.code.endswith("work_order_not_found")
    assert db_session.query(FieldExpenseRequest).count() == 0


@pytest.mark.parametrize("amount", ["", "invalid", "0", "-1"])
def test_form_rejects_invalid_or_non_positive_amounts(amount):
    with pytest.raises(expense_web.WorkOrderExpenseFormError) as exc:
        expense_web.validate_work_order_expense_form(
            _valid_form(amount=amount), category_rules=_rules()
        )

    assert any(error.field == "line.lineone.amount" for error in exc.value.errors)


def test_form_enforces_zero_lines_category_maximum_and_required_receipt():
    empty = _valid_form()
    empty = expense_web.WorkOrderExpenseFormInput(
        request_id=empty.request_id,
        purpose=empty.purpose,
        expense_date=empty.expense_date,
        currency=empty.currency,
        notes=empty.notes,
        lines=(),
    )
    with pytest.raises(expense_web.WorkOrderExpenseFormError) as zero_exc:
        expense_web.validate_work_order_expense_form(empty, category_rules=_rules())
    assert any(error.field == "lines" for error in zero_exc.value.errors)

    with pytest.raises(expense_web.WorkOrderExpenseFormError) as policy_exc:
        expense_web.validate_work_order_expense_form(
            _valid_form(amount="2500"),
            category_rules=_rules(receipt=True, maximum="2000"),
        )
    assert {error.field for error in policy_exc.value.errors} >= {
        "line.lineone.receipt",
        "lines",
    }


def test_receipt_upload_failure_rolls_back_claim(db_session, monkeypatch):
    class _RejectUploads:
        @staticmethod
        def stage_upload(**_kwargs):
            raise FileValidationError("File extension not allowed")

    monkeypatch.setattr(attachments_module, "file_uploads", _RejectUploads())
    user = _user(db_session, "Chidi")
    work_order = _work_order(db_session, "sub-expense-bad-receipt")
    upload = ExpenseReceiptUploadInput(
        file_name="receipt.exe",
        mime_type="application/octet-stream",
        content=b"not-a-receipt",
        client_ref=uuid4(),
    )
    command = _command(
        user,
        work_order,
        items=(_line(receipt_upload=upload),),
        category_rules=_rules(receipt=True),
    )
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(
            db_session,
            command,
        )

    assert "File extension not allowed" in exc.value.message
    assert db_session.query(FieldExpenseRequest).count() == 0


def test_panel_isolates_claims_and_does_not_treat_sent_as_accepted(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        expense_web,
        "list_expense_categories",
        lambda _db, _query: (
            ExpenseCategoryView(
                category_code="transport",
                category_name="Transport",
                requires_receipt=False,
                max_amount_per_claim=Decimal("10000"),
            ),
        ),
    )
    owner = _user(db_session, "Emeka")
    other = _user(db_session, "Fola")
    work_order = _work_order(db_session, "sub-expense-panel")
    own_claim = FieldExpenseRequest(
        work_order_mirror_id=work_order.id,
        requested_by_person_id=owner.id,
        requested_by_system_user_id=owner.id,
        status="submitted",
        purpose="My transport",
        expense_date=date.today(),
        currency="NGN",
        client_ref=uuid4(),
    )
    own_claim.items.append(
        FieldExpenseRequestItem(
            category_code="transport",
            category_name="Transport",
            description="Taxi",
            amount=Decimal("2000"),
        )
    )
    other_claim = FieldExpenseRequest(
        work_order_mirror_id=work_order.id,
        requested_by_person_id=other.id,
        requested_by_system_user_id=other.id,
        status="paid",
        purpose="Someone else's claim",
        currency="NGN",
        client_ref=uuid4(),
    )
    db_session.add_all([own_claim, other_claim])
    db_session.flush()
    event = FieldErpSyncEvent(
        flow=FieldErpSyncFlow.expense_claim.value,
        entity_type="field_expense_request",
        entity_id=own_claim.id,
        idempotency_key=f"expense:{own_claim.id}",
        payload={},
        status=FieldErpSyncStatus.sent.value,
    )
    db_session.add(event)
    db_session.commit()

    panel = expense_web.build_work_order_expense_panel(
        db_session,
        work_order_public_id=work_order.public_id,
        actor_system_user_id=owner.id,
    )
    assert panel.create_action.permission == "operations:dispatch:write"
    assert [claim.purpose for claim in panel.claims] == ["My transport"]
    assert panel.claims[0].delivery_state is expense_web.ExpenseDeliveryState.PENDING
    assert panel.claims[0].delivery_label == "Delivered; awaiting ERP acceptance"

    event.status = FieldErpSyncStatus.accepted.value
    db_session.commit()
    accepted = expense_web.build_work_order_expense_panel(
        db_session,
        work_order_public_id=work_order.public_id,
        actor_system_user_id=owner.id,
    )
    assert (
        accepted.claims[0].delivery_state is expense_web.ExpenseDeliveryState.ACCEPTED
    )


def test_redisplay_preserves_values_and_explicitly_clears_file_input():
    form = _valid_form()
    upload = ExpenseReceiptUploadInput(
        file_name="receipt.pdf",
        mime_type="application/pdf",
        content=b"%PDF",
        client_ref=uuid4(),
    )
    line = form.lines[0]
    form = expense_web.WorkOrderExpenseFormInput(
        request_id=form.request_id,
        purpose=form.purpose,
        expense_date=form.expense_date,
        currency=form.currency,
        notes=form.notes,
        lines=(
            expense_web.ExpenseLineFormInput(
                key=line.key,
                category_code=line.category_code,
                description=line.description,
                amount=line.amount,
                expense_date=line.expense_date,
                vendor_name=line.vendor_name,
                receipt_url=line.receipt_url,
                notes=line.notes,
                receipt_upload=upload,
            ),
        ),
    )

    preserved, errors = expense_web.prepare_form_redisplay(form, ())

    assert preserved.request_id == form.request_id
    assert preserved.purpose == form.purpose
    assert preserved.lines[0].amount == form.lines[0].amount
    assert preserved.lines[0].receipt_upload is None
    assert errors[0].field == "line.lineone.receipt"


def test_work_order_template_owns_context_and_supports_responsive_lines():
    source = Path("templates/admin/dispatch/work_order_detail.html").read_text()
    expense_form = next(form for form in source.split("</form>") if "/expenses" in form)

    assert "components/forms/csrf_input.html" in expense_form
    assert 'name="work_order_id"' not in expense_form
    assert 'name="client_ref"' in expense_form
    assert "data-expense-line" in expense_form
    assert "data-add-expense-line" in expense_form
    assert "data-remove-expense-line" in expense_form
    assert "data-expense-total" in expense_form
    assert "md:grid-cols-2" in expense_form
