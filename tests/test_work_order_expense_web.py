from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.dispatch import (
    DispatchQueueStatus,
    TechnicianProfile,
    WorkOrderAssignmentQueue,
)
from app.models.field_attachment import FieldAttachment
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.field_expense import FieldExpenseRequest, FieldExpenseRequestItem
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import web_work_order_expenses as expense_web
from app.services.backoffice import ExpenseCategoryView
from app.services.field import attachments as attachments_module
from app.services.field import expense_categories as expense_categories_module
from app.services.field.expense_requests import (
    ExpenseCategoryRule,
    ExpenseReceiptUploadInput,
    ExpenseRequestAccessMode,
    ExpenseRequestLineInput,
    ExpenseWorkOrderIdentity,
    FieldExpenseRequestError,
    StaffWorkOrderAccess,
    SubmitFieldExpenseRequest,
    submit_field_expense_request_command,
)
from app.services.file_storage import FileValidationError
from app.services.owner_commands import CommandContext

_APPROVER_ERP_ID = uuid4()
_APPROVER_USER_ID = uuid4()


def _approvers():
    return (
        expense_web.ExpenseApproverView(
            erp_employee_id=_APPROVER_ERP_ID,
            system_user_id=_APPROVER_USER_ID,
            display_name="Expense Approver",
            email="approver@example.com",
        ),
    )


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


def _work_order(
    db_session,
    public_id: str,
    *,
    assigned: bool = True,
) -> WorkOrder:
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
    if assigned:
        technician = TechnicianProfile(person_id=uuid4(), is_active=True)
        db_session.add(technician)
        db_session.flush()
        db_session.add(
            WorkOrderAssignmentQueue(
                work_order_mirror_id=row.id,
                status=DispatchQueueStatus.assigned,
                assigned_technician_id=technician.id,
            )
        )
        db_session.flush()
    return row


def _context(user: SystemUser, request_id):
    return CommandContext(
        command_id=request_id,
        correlation_id=request_id,
        actor=f"user:{user.id}",
        scope="operations:dispatch:read",
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
    overrides.pop("category_rules", None)
    work_order_identity = overrides.pop(
        "work_order_identity",
        ExpenseWorkOrderIdentity(public_id=work_order.public_id),
    )
    values = {
        "context": _context(user, request_id),
        "requester_person_id": None,
        "work_order": work_order_identity,
        "request_id": request_id,
        "purpose": "Travel for fibre repair",
        "expense_date": date.today(),
        "currency": "NGN",
        "notes": "Customer outage",
        "items": (_line(),),
        "access_mode": ExpenseRequestAccessMode.STAFF_WORK_ORDER,
        "staff_access": StaffWorkOrderAccess(global_access=True),
    }
    values.update(overrides)
    return SubmitFieldExpenseRequest(**values)


def _enable_expense_flow(db_session) -> None:
    flow = FieldErpSyncFlow.expense_claim.value
    ownership = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == flow)
        .one_or_none()
    )
    if ownership is None:
        db_session.add(SyncFlowOwnership(flow=flow, owner=SyncFlowOwner.sub.value))
    else:
        ownership.owner = SyncFlowOwner.sub.value


@pytest.fixture(autouse=True)
def _authoritative_expense_rules(monkeypatch):
    monkeypatch.setattr(
        expense_categories_module,
        "list_expense_categories",
        lambda _db, _query: (
            ExpenseCategoryView(
                category_code="transport",
                category_name="Transport",
                requires_receipt=False,
                max_amount_per_claim=Decimal("10000.00"),
            ),
        ),
    )


@pytest.fixture(autouse=True)
def _expense_delivery_owner(db_session):
    _enable_expense_flow(db_session)


def _valid_form(*, amount: str = "2500.00") -> expense_web.WorkOrderExpenseFormInput:
    return expense_web.WorkOrderExpenseFormInput(
        request_id=str(uuid4()),
        purpose="Travel for fibre repair",
        expense_date=date.today().isoformat(),
        currency="ngn",
        notes="Customer outage",
        selected_approver_id=str(_APPROVER_ERP_ID),
        payment_destination_mode="erp_profile",
        bank_code="",
        account_number="",
        beneficiary_name="",
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
    command = _command(user, work_order, staff_access=None)
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(
            db_session,
            command,
        )

    assert exc.value.code.endswith("work_order_unauthorized")
    assert db_session.query(FieldExpenseRequest).count() == 0


@pytest.mark.parametrize("public_id", ["", "unknown-work-order"])
def test_owner_rejects_missing_or_unknown_work_order_identity(db_session, public_id):
    user = _user(db_session, "UnknownWorkOrder")
    work_order = _work_order(db_session, "known-work-order")
    command = _command(
        user,
        work_order,
        work_order_identity=ExpenseWorkOrderIdentity(public_id=public_id),
    )
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(db_session, command)

    assert exc.value.code.endswith("work_order_not_found")
    assert db_session.query(FieldExpenseRequest).count() == 0


def test_owner_rejects_inactive_work_order(db_session):
    user = _user(db_session, "InactiveWorkOrder")
    work_order = _work_order(db_session, "inactive-work-order")
    work_order.is_active = False
    command = _command(user, work_order)
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(db_session, command)

    assert exc.value.code.endswith("work_order_not_found")


def test_owner_rejects_attachment_from_another_work_order(db_session):
    user = _user(db_session, "CrossWorkOrder")
    target = _work_order(db_session, "target-work-order")
    other = _work_order(db_session, "other-work-order")
    stored = StoredFile(
        entity_type="field_attachment",
        entity_id=other.public_id,
        original_filename="receipt.pdf",
        storage_key_or_relative_path="attachments/other/receipt.pdf",
        file_size=8,
        content_type="application/pdf",
        storage_provider="s3",
    )
    db_session.add(stored)
    db_session.flush()
    attachment = FieldAttachment(
        work_order_mirror_id=other.id,
        stored_file_id=stored.id,
        kind="document",
        file_name="receipt.pdf",
        mime_type="application/pdf",
        size_bytes=8,
        uploaded_by_person_id=user.id,
        uploaded_by_system_user_id=user.id,
    )
    db_session.add(attachment)
    db_session.flush()
    command = _command(
        user,
        target,
        items=(_line(receipt_attachment_id=attachment.id),),
    )
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(db_session, command)

    assert exc.value.code.endswith("invalid_request")
    assert "Receipt attachment not found" in exc.value.message


def test_owner_rejects_attachment_owned_by_another_requester(db_session):
    user = _user(db_session, "AttachmentRequester")
    other = _user(db_session, "AttachmentOwner")
    work_order = _work_order(db_session, "same-work-order-wrong-owner")
    stored = StoredFile(
        entity_type="field_attachment",
        entity_id=work_order.public_id,
        original_filename="receipt.pdf",
        storage_key_or_relative_path="attachments/wrong-owner/receipt.pdf",
        file_size=8,
        content_type="application/pdf",
        storage_provider="s3",
    )
    db_session.add(stored)
    db_session.flush()
    attachment = FieldAttachment(
        work_order_mirror_id=work_order.id,
        stored_file_id=stored.id,
        kind="document",
        file_name="receipt.pdf",
        mime_type="application/pdf",
        size_bytes=8,
        uploaded_by_person_id=other.id,
        uploaded_by_system_user_id=other.id,
    )
    db_session.add(attachment)
    db_session.flush()
    command = _command(
        user,
        work_order,
        items=(_line(receipt_attachment_id=attachment.id),),
    )
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(db_session, command)

    assert exc.value.code.endswith("invalid_request")
    assert "Receipt attachment not found" in exc.value.message


def test_assigned_technician_can_submit_using_public_work_order_identity(db_session):
    user = _user(db_session, "AssignedTechnician")
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        is_active=True,
    )
    db_session.add(profile)
    work_order = _work_order(db_session, "assigned-field-work-order", assigned=False)
    db_session.add(
        WorkOrderAssignmentQueue(
            work_order_mirror_id=work_order.id,
            status=DispatchQueueStatus.assigned,
            assigned_technician_id=profile.id,
        )
    )
    command = _command(
        user,
        work_order,
        requester_person_id=user.id,
        access_mode=ExpenseRequestAccessMode.FIELD_ASSIGNMENT,
        staff_access=None,
    )
    db_session.commit()

    outcome = submit_field_expense_request_command(db_session, command)

    assert outcome.status == "submitted"
    assert outcome.work_order_id == work_order.public_id


def test_staff_command_rejects_unassigned_work_order(db_session):
    user = _user(db_session, "Chidi")
    work_order = _work_order(
        db_session,
        "sub-expense-unassigned",
        assigned=False,
    )
    command = _command(user, work_order)
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(db_session, command)

    assert exc.value.code.endswith("work_order_unassigned")
    assert exc.value.message == "Assign a technician first."
    assert db_session.query(FieldExpenseRequest).count() == 0


def test_assigned_queue_entry_satisfies_assignment_requirement(db_session):
    work_order = _work_order(db_session, "sub-expense-queue-assigned")
    db_session.commit()

    eligibility = expense_web.evaluate_expense_work_order_eligibility(
        db_session,
        work_order=work_order,
    )

    assert eligibility.allowed is True
    assert eligibility.reason is None


@pytest.mark.parametrize("amount", ["", "invalid", "0", "-1"])
def test_form_rejects_invalid_or_non_positive_amounts(amount):
    with pytest.raises(expense_web.WorkOrderExpenseFormError) as exc:
        expense_web.validate_work_order_expense_form(
            _valid_form(amount=amount), category_rules=_rules(), approvers=_approvers()
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
        selected_approver_id=empty.selected_approver_id,
        payment_destination_mode=empty.payment_destination_mode,
        bank_code=empty.bank_code,
        account_number=empty.account_number,
        beneficiary_name=empty.beneficiary_name,
        lines=(),
    )
    with pytest.raises(expense_web.WorkOrderExpenseFormError) as zero_exc:
        expense_web.validate_work_order_expense_form(
            empty, category_rules=_rules(), approvers=_approvers()
        )
    assert any(error.field == "lines" for error in zero_exc.value.errors)

    with pytest.raises(expense_web.WorkOrderExpenseFormError) as policy_exc:
        expense_web.validate_work_order_expense_form(
            _valid_form(amount="2500"),
            category_rules=_rules(receipt=True, maximum="2000"),
            approvers=_approvers(),
        )
    assert {error.field for error in policy_exc.value.errors} >= {
        "line.lineone.receipt",
        "lines",
    }


def test_receipt_fields_are_optional_unless_the_category_requires_evidence():
    prepared = expense_web.validate_work_order_expense_form(
        _valid_form(), category_rules=_rules(receipt=False), approvers=_approvers()
    )

    assert prepared.lines[0].receipt_url is None
    assert prepared.lines[0].receipt_upload is None

    form = _valid_form()
    line = form.lines[0]
    with_url = expense_web.WorkOrderExpenseFormInput(
        request_id=form.request_id,
        purpose=form.purpose,
        expense_date=form.expense_date,
        currency=form.currency,
        notes=form.notes,
        selected_approver_id=form.selected_approver_id,
        payment_destination_mode="expense_override",
        bank_code="058",
        account_number="0123456789",
        beneficiary_name="Field Technician",
        lines=(
            expense_web.ExpenseLineFormInput(
                key=line.key,
                category_code=line.category_code,
                description=line.description,
                amount=line.amount,
                expense_date=line.expense_date,
                vendor_name=line.vendor_name,
                receipt_url="https://example.com/receipt.pdf",
                notes=line.notes,
            ),
        ),
    )

    prepared_with_url = expense_web.validate_work_order_expense_form(
        with_url, category_rules=_rules(receipt=True), approvers=_approvers()
    )

    assert prepared_with_url.lines[0].receipt_url == ("https://example.com/receipt.pdf")
    assert prepared_with_url.lines[0].receipt_upload is None


def test_owner_enforces_receipt_required_category(db_session, monkeypatch):
    monkeypatch.setattr(
        expense_categories_module,
        "list_expense_categories",
        lambda _db, _query: (
            ExpenseCategoryView(
                category_code="transport",
                category_name="Transport",
                requires_receipt=True,
                max_amount_per_claim=Decimal("10000.00"),
            ),
        ),
    )
    user = _user(db_session, "ReceiptRequired")
    work_order = _work_order(db_session, "receipt-required-work-order")
    command = _command(user, work_order)
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(db_session, command)

    assert exc.value.code.endswith("invalid_request")
    assert "receipt is required" in exc.value.message.lower()


def test_owner_accepts_supported_receipt_url_for_required_category(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        expense_categories_module,
        "list_expense_categories",
        lambda _db, _query: (
            ExpenseCategoryView(
                category_code="transport",
                category_name="Transport",
                requires_receipt=True,
                max_amount_per_claim=Decimal("10000.00"),
            ),
        ),
    )
    user = _user(db_session, "ReceiptUrl")
    work_order = _work_order(db_session, "receipt-url-work-order")
    command = _command(
        user,
        work_order,
        items=(_line(receipt_url="https://receipts.example/expense.pdf"),),
    )
    db_session.commit()

    outcome = submit_field_expense_request_command(db_session, command)

    assert outcome.items[0].receipt_url == "https://receipts.example/expense.pdf"


def test_owner_rejects_unsafe_receipt_url(db_session):
    user = _user(db_session, "UnsafeReceiptUrl")
    work_order = _work_order(db_session, "unsafe-receipt-url-work-order")
    command = _command(
        user,
        work_order,
        items=(_line(receipt_url="http://127.0.0.1/private-receipt"),),
    )
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        submit_field_expense_request_command(db_session, command)

    assert exc.value.code.endswith("receipt_url_invalid")


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


def test_staff_receipt_upload_avoids_legacy_subscriber_uploader_fk(
    db_session, monkeypatch
):
    class _StageUploads:
        @staticmethod
        def stage_upload(**kwargs):
            assert kwargs["uploaded_by"] is None
            stored = StoredFile(
                entity_type=kwargs["entity_type"],
                entity_id=kwargs["entity_id"],
                original_filename=kwargs["original_filename"],
                storage_key_or_relative_path="attachments/receipt.pdf",
                file_size=len(kwargs["data"]),
                content_type=kwargs["content_type"],
                storage_provider="s3",
                uploaded_by=kwargs["uploaded_by"],
                owner_subscriber_id=kwargs["owner_subscriber_id"],
            )
            kwargs["db"].add(stored)
            kwargs["db"].flush()
            return stored

        @staticmethod
        def stream_file(stored):
            return type(
                "Stream",
                (),
                {
                    "chunks": (b"%PDF-1.4",),
                    "content_type": stored.content_type,
                },
            )()

    monkeypatch.setattr(attachments_module, "file_uploads", _StageUploads())
    user = _user(db_session, "StaffReceipt")
    work_order = _work_order(db_session, "sub-expense-staff-receipt")
    upload = ExpenseReceiptUploadInput(
        file_name="receipt.pdf",
        mime_type="application/pdf",
        content=b"%PDF-1.4",
        client_ref=uuid4(),
    )
    command = _command(
        user,
        work_order,
        items=(_line(receipt_upload=upload),),
        category_rules=_rules(receipt=True),
    )
    db_session.commit()

    outcome = submit_field_expense_request_command(db_session, command)

    stored_file = db_session.query(StoredFile).one()
    attachment = db_session.query(FieldAttachment).one()
    assert outcome.items[0].receipt_attachment_id == attachment.id
    assert stored_file.uploaded_by is None
    assert attachment.uploaded_by_system_user_id == user.id


def test_unassigned_work_order_disables_expense_action(db_session, monkeypatch):
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
    user = _user(db_session, "Dapo")
    work_order = _work_order(
        db_session,
        "sub-expense-panel-unassigned",
        assigned=False,
    )
    db_session.commit()

    panel = expense_web.build_work_order_expense_panel(
        db_session,
        work_order_public_id=work_order.public_id,
        actor_system_user_id=user.id,
    )

    assert panel.create_action.allowed is False
    assert panel.create_action.reason == "Assign a technician first."


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
    assert panel.create_action.permission == "operations:dispatch:read"
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


@pytest.mark.parametrize(
    ("status", "state", "label"),
    [
        (
            "submitted",
            expense_web.ExpenseDeliveryState.AWAITING_APPROVAL,
            "Awaiting manager approval",
        ),
        (
            "canceled",
            expense_web.ExpenseDeliveryState.NOT_APPLICABLE,
            "Not sent to ERP",
        ),
        (
            "approved",
            expense_web.ExpenseDeliveryState.UNAVAILABLE,
            "ERP delivery evidence is unavailable",
        ),
    ],
)
def test_delivery_label_distinguishes_not_started_from_unavailable(
    status, state, label
):
    request = FieldExpenseRequest(status=status)

    assert expense_web._delivery_state(request, None) is state
    assert expense_web._delivery_label(request, None) == label


def test_failed_delivery_detail_uses_typed_safe_diagnostic():
    request_id = uuid4()
    event = FieldErpSyncEvent(
        flow=FieldErpSyncFlow.expense_claim.value,
        entity_type="field_expense_request",
        entity_id=uuid4(),
        idempotency_key=f"expense:{uuid4()}",
        payload={},
        status=FieldErpSyncStatus.dead.value,
        erp_response={
            "delivery_diagnostic": {
                "http_status": 422,
                "code": "validation_error",
                "message": "private provider detail",
                "request_id": str(request_id),
            }
        },
    )

    detail = expense_web._delivery_detail(event)

    assert detail is not None
    assert "ERP rejected request validation" in detail
    assert f"request_id={request_id}" in detail
    assert "private provider detail" not in detail


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
        selected_approver_id=form.selected_approver_id,
        payment_destination_mode="expense_override",
        bank_code="058",
        account_number="0123456789",
        beneficiary_name="Field Technician",
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
    assert preserved.account_number == ""
    assert preserved.beneficiary_name == "Field Technician"
    assert errors[0].field == "line.lineone.receipt"


def test_work_order_template_owns_context_and_supports_responsive_lines():
    source = Path("templates/admin/dispatch/work_order_detail.html").read_text(
        encoding="utf-8"
    )
    expense_form = next(form for form in source.split("</form>") if "/expenses" in form)

    assert "components/forms/csrf_input.html" in expense_form
    assert 'name="work_order_id"' not in expense_form
    assert 'name="client_ref"' in expense_form
    assert 'name="selected_approver_id"' in expense_form
    assert 'name="payment_destination_mode"' in expense_form
    assert 'name="account_number"' in expense_form
    assert 'autocomplete="off"' in expense_form
    assert "does not change the technician's ERP profile" in expense_form
    assert "data-expense-line" in expense_form
    assert "data-add-expense-line" in expense_form
    assert "data-remove-expense-line" in expense_form
    assert "data-expense-total" in expense_form
    assert "md:grid-cols-2" in expense_form
    assert source.count(">New Expense Claim<") >= 2
    assert 'aria-describedby="expense-creation-unavailable"' in source
    assert 'id="expense-creation-unavailable"' in source
    assert (
        'Title <span class="text-rose-600" aria-hidden="true">*</span><input' in source
    )
    assert (
        'Technician <span class="text-rose-600" aria-hidden="true">*</span><select'
        in source
    )
    assert "receipt.required" not in expense_form
    assert 'name="receipt_file_{{ line.key }}"' in expense_form
    assert 'name="receipt_file_{{ line.key }}" required' not in expense_form
    assert 'name="receipt_url_{{ line.key }}" required' not in expense_form
    assert "data-receipt-required-marker hidden" in expense_form
    assert "'(required — choose one)'" in source
    assert "receiptUrl.setCustomValidity" in source
    assert "receiptFile.files?.length" in source
    assert (
        "When a receipt is required, provide either a receipt URL or an uploaded file."
        in source
    )

    required_names = {
        match.group(1)
        for tag in re.findall(
            r"<(?:input|select|textarea)\b[^>]*\brequired\b[^>]*>",
            expense_form,
        )
        if (match := re.search(r'name="([^"]+)"', tag))
    }
    assert required_names == {
        "purpose",
        "expense_date",
        "currency",
        "selected_approver_id",
        "category_code_{{ line.key }}",
        "amount_{{ line.key }}",
        "description_{{ line.key }}",
    }

    line_template = source.split("<template data-expense-line-template>", 1)[1].split(
        "</template>", 1
    )[0]
    template_required_names = {
        match.group(1)
        for tag in re.findall(
            r"<(?:input|select|textarea)\b[^>]*\brequired\b[^>]*>",
            line_template,
        )
        if (match := re.search(r'name="([^"]+)"', tag))
    }
    assert template_required_names == {
        "category_code___KEY__",
        "amount___KEY__",
        "description___KEY__",
    }
