"""ERP re-home PR 2 — expense-claim flow (map + enqueue + write-back + reconcile).

Everything runs against a MOCKED ERP: the outbox uses a fake client, and the
status reconcile is fed a canned response. The flow is proven end-to-end WITHOUT
a live ERP and, crucially, WITHOUT flipping ownership in prod — tests set
``sync_flow_ownership.expense_claim = sub`` in-test only. The default (crm) path
is asserted to send nothing (the inert guarantee).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.models.dispatch import TechnicianProfile
from app.models.field_attachment import FieldAttachment
from app.models.field_erp_sync import (
    FieldErpSyncEvent,
    FieldErpSyncFlow,
    FieldErpSyncStatus,
    SyncFlowOwner,
    SyncFlowOwnership,
)
from app.models.field_expense import FieldExpenseRequest
from app.models.stored_file import StoredFile
from app.models.subscriber import Subscriber, UserType
from app.models.system_user import SystemUser
from app.models.work_order import WorkOrder
from app.services import backoffice
from app.services.backoffice import BackofficeDeliveryView, ExpenseCategoryView
from app.services.db_session_adapter import db_session_adapter
from app.services.dotmac_erp import expense_sync, outbox
from app.services.dotmac_erp.client import DotMacERPError, DotMacERPTransientError
from app.services.dotmac_erp.expense_form_contracts import (
    ExpenseApproverOption,
    ExpenseDestinationMode,
    ExpenseProfileDestination,
    InspectExpenseDestination,
    VerifiedExpenseDestination,
    VerifyExpenseDestination,
)
from app.services.field import attachments as attachments_module
from app.services.field import expense_recovery as expense_recovery_module
from app.services.field import expense_requests as expense_requests_module
from app.services.field.attachments import ResolvedExpenseReceiptAttachment
from app.services.field.expense_recovery import (
    PreviewExpenseDeliveryRecovery,
    PreviewExpensePaymentDeliveryRecovery,
    preview_expense_delivery_recovery,
    preview_expense_payment_delivery_recovery,
)
from app.services.field.expense_requests import (
    ApproveFieldExpenseRequest,
    ExpenseErpSyncStatus,
    ExpenseReceiptUploadInput,
    ExpenseRequestLineInput,
    ExpenseWorkOrderIdentity,
    FieldExpenseRequestError,
    GetFieldExpenseFormContext,
    InitiateFieldExpensePayment,
    RecoverExpenseDelivery,
    RecoverExpensePaymentDelivery,
    RejectFieldExpenseRequest,
    ResolveFieldExpenseSubmissionContext,
    SelectedExpenseApprover,
    SubmitFieldExpenseRequest,
    VerifiedExpenseDestinationInput,
    VerifyFieldExpenseDestination,
    approve_field_expense_request_command,
    get_field_expense_form_context,
    initiate_field_expense_payment_command,
    recover_expense_delivery,
    recover_expense_payment_delivery,
    reject_field_expense_request_command,
    resolve_field_expense_submission_context,
    submit_field_expense_request_command,
    verify_field_expense_destination,
)
from app.services.integrations.backoffice_contracts import (
    ERP_OUTBOX_CAPABILITY,
    ErpExpenseClaimDraftCommand,
    ErpExpenseClaimDraftOutcome,
    ErpExpenseDraftLineOutcome,
    ErpExpensePaymentCommand,
    ErpExpensePaymentOutcome,
)
from app.services.integrations.diagnostics import (
    DELIVERY_DIAGNOSTIC_KEY,
    diagnostic_evidence,
    safe_diagnostic,
)
from app.services.owner_commands import CommandContext
from tests.integration_platform_helpers import enable_erp_capability

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def test_field_projection_exposes_only_safe_expense_delivery_diagnostic():
    request_id = uuid4()
    diagnostic = safe_diagnostic(status=422).model_copy(
        update={"message": "private provider detail", "request_id": request_id}
    )
    delivery = BackofficeDeliveryView(
        flow_owner="sub",
        sub_owns_delivery=True,
        event_id=uuid4(),
        event_status=FieldErpSyncStatus.dead.value,
        attempts=1,
        last_error="private provider detail",
        queued_at=None,
        updated_at=None,
        sent_at=None,
        diagnostic=diagnostic,
    )

    error = expense_requests_module._expense_sync_error(delivery)

    assert error is not None
    assert "ERP rejected request validation" in error
    assert f"request_id={request_id}" in error
    assert "private provider detail" not in error


def _seed_ownership(db, *, sub_flows: set[str] | None = None) -> None:
    sub_flows = sub_flows or set()
    for flow in FieldErpSyncFlow:
        owner = (
            SyncFlowOwner.sub.value
            if flow.value in sub_flows
            else SyncFlowOwner.crm.value
        )
        db.add(SyncFlowOwnership(flow=flow.value, owner=owner))
    db.flush()


def _user(db, name: str = "Expense") -> SystemUser:
    user = SystemUser(
        first_name=name,
        last_name="Tech",
        display_name=f"{name} Tech",
        email=f"{name.lower()}-{uuid4().hex[:8]}@example.com",
        user_type=UserType.system_user,
    )
    db.add(user)
    db.flush()
    return user


def _profile(
    db, user: SystemUser, crm_person_id: str = "crm-expense-tech"
) -> TechnicianProfile:
    profile = TechnicianProfile(
        person_id=user.id,
        system_user_id=user.id,
        crm_person_id=crm_person_id,
        title="Installer",
    )
    db.add(profile)
    db.flush()
    return profile


def _subscriber(db) -> Subscriber:
    subscriber = Subscriber(
        first_name="Expense",
        last_name="Customer",
        email=f"expense-{uuid4().hex[:8]}@example.com",
    )
    db.add(subscriber)
    db.flush()
    return subscriber


def _work_order(db, subscriber: Subscriber, **overrides) -> WorkOrder:
    row = WorkOrder(
        crm_work_order_id=overrides.pop("crm_work_order_id", "wo-expense"),
        subscriber_id=subscriber.id,
        title=overrides.pop("title", "Field expense"),
        status=overrides.pop("status", "in_progress"),
        assigned_to_crm_person_id=overrides.pop(
            "assigned_to_crm_person_id", "crm-expense-tech"
        ),
        crm_ticket_id=overrides.pop("crm_ticket_id", "crm-ticket-77"),
        crm_project_id=overrides.pop("crm_project_id", "crm-project-88"),
        scheduled_start=overrides.pop("scheduled_start", datetime.now(UTC)),
        **overrides,
    )
    db.add(row)
    db.flush()
    return row


def _auth(user: SystemUser) -> dict:
    return {
        "principal_id": str(user.id),
        "person_id": str(user.id),
        "subscriber_id": str(user.id),
        "principal_type": "system_user",
        "roles": [],
        "scopes": [],
    }


def _items(**overrides):
    item = {
        "category_code": "transport",
        "category_name": "Transport",
        "description": "Bike delivery",
        "amount": "2500.00",
        "expense_date": date.today(),
        "vendor_name": "Rider",
        "receipt_url": None,
        "receipt_attachment_id": None,
        "notes": "Urgent part pickup",
    }
    item.update(overrides)
    return [item]


def _receipt_attachment(
    db, request: FieldExpenseRequest, attachment_id, *, file_name: str
) -> FieldAttachment:
    content = f"receipt:{attachment_id}".encode()
    stored = StoredFile(
        entity_type="field_attachment",
        entity_id=request.work_order_mirror.public_id,
        original_filename=file_name,
        storage_key_or_relative_path=f"attachments/{attachment_id}",
        file_size=len(content),
        content_type="application/pdf",
        checksum=hashlib.sha256(content).hexdigest(),
        storage_provider="s3",
    )
    db.add(stored)
    db.flush()
    attachment = FieldAttachment(
        id=attachment_id,
        work_order_mirror_id=request.work_order_mirror_id,
        stored_file_id=stored.id,
        kind="document",
        file_name=file_name,
        mime_type="application/pdf",
        size_bytes=len(content),
        uploaded_by_person_id=request.requested_by_person_id,
        uploaded_by_system_user_id=request.requested_by_system_user_id,
    )
    db.add(attachment)
    db.flush()
    return attachment


def _enable_receipt_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    class _StageUploads:
        def __init__(self) -> None:
            self.contents: dict[UUID, bytes] = {}

        def stage_upload(self, **kwargs):
            content = kwargs["data"]
            stored = StoredFile(
                entity_type=kwargs["entity_type"],
                entity_id=kwargs["entity_id"],
                original_filename=kwargs["original_filename"],
                storage_key_or_relative_path=f"attachments/{uuid4().hex}",
                file_size=len(content),
                content_type=kwargs["content_type"],
                checksum=hashlib.sha256(content).hexdigest(),
                storage_provider="s3",
                uploaded_by=kwargs["uploaded_by"],
                owner_subscriber_id=kwargs["owner_subscriber_id"],
            )
            kwargs["db"].add(stored)
            kwargs["db"].flush()
            self.contents[stored.id] = content
            return stored

        def stream_file(self, stored):
            return type(
                "Stream",
                (),
                {
                    "chunks": (self.contents[stored.id],),
                    "content_type": stored.content_type,
                },
            )()

    monkeypatch.setattr(attachments_module, "file_uploads", _StageUploads())


def _make_submitted_request(
    db,
    *,
    crm_work_order_id="wo-exp",
    items: list[dict] | None = None,
    self_approver: bool = False,
) -> FieldExpenseRequest:
    """Create and submit a request through the real domain service."""
    flow = FieldErpSyncFlow.expense_claim.value
    ownership = (
        db.query(SyncFlowOwnership).filter(SyncFlowOwnership.flow == flow).one_or_none()
    )
    if ownership is None:
        db.add(SyncFlowOwnership(flow=flow, owner=SyncFlowOwner.sub.value))
    else:
        ownership.owner = SyncFlowOwner.sub.value
    crm_person_id = f"crm-tech-{uuid4().hex[:8]}"
    user = _user(db)
    approver = user if self_approver else _user(db, "Approver")
    _profile(db, user, crm_person_id=crm_person_id)
    subscriber = _subscriber(db)
    work_order = _work_order(
        db,
        subscriber,
        crm_work_order_id=crm_work_order_id,
        assigned_to_crm_person_id=crm_person_id,
    )
    request_id = uuid4()
    user_id = user.id
    command = SubmitFieldExpenseRequest(
        context=CommandContext(
            command_id=request_id,
            correlation_id=request_id,
            actor=f"user:{user_id}",
            scope="field:expense_requests:write",
            reason="test expense submission",
            idempotency_key=str(request_id),
        ),
        requester_person_id=user_id,
        work_order=ExpenseWorkOrderIdentity(public_id=work_order.public_id),
        request_id=request_id,
        purpose="Transport for extra drop cable",
        expense_date=date.today(),
        currency="NGN",
        notes="Customer site was missing materials",
        items=tuple(ExpenseRequestLineInput(**item) for item in (items or _items())),
        selected_approver=SelectedExpenseApprover(
            erp_employee_id=uuid4(),
            system_user_id=approver.id,
            display_name=approver.display_name,
            email=approver.email,
        ),
        payment_destination=VerifiedExpenseDestinationInput(
            mode="erp_profile",
            destination_token="verified-expense-destination-token",
            bank_code="058",
            bank_name="Example Bank",
            masked_account_number="******6789",
            verified_beneficiary_name=user.display_name,
            verified_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        ),
    )
    db.commit()
    outcome = submit_field_expense_request_command(db, command)
    return db.get(FieldExpenseRequest, outcome.id)


@pytest.fixture(autouse=True)
def _expense_categories(monkeypatch):
    monkeypatch.setattr(
        "app.services.field.expense_categories.list_expense_categories",
        lambda _db, _query: (
            ExpenseCategoryView(
                category_code="transport",
                category_name="Transport",
                requires_receipt=False,
                max_amount_per_claim=None,
            ),
        ),
    )


def _approve(
    db,
    request: FieldExpenseRequest,
    *,
    reviewer_id: UUID | None = None,
):
    request_id = request.id
    resolved_reviewer_id = reviewer_id or request.selected_approver_system_user_id
    assert resolved_reviewer_id is not None
    command_id = uuid4()
    db.commit()
    return approve_field_expense_request_command(
        db=db,
        command=ApproveFieldExpenseRequest(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{resolved_reviewer_id}",
                scope="operations:expense_request:write",
                reason=f"approve_expense_request:{request_id}",
                idempotency_key=str(command_id),
            ),
            expense_request_id=request_id,
            reviewer_system_user_id=resolved_reviewer_id,
        ),
    )


class _FakeERPClient:
    """Mocked ERP client for outbox + reconcile: canned responses in order."""

    def __init__(
        self,
        post_outcomes=None,
        status_outcomes=None,
        upload_outcomes=None,
    ):
        self._post = list(post_outcomes or [])
        self._status = list(status_outcomes or [])
        self._uploads = list(upload_outcomes or [])
        self.posts: list[dict] = []
        self.status_calls: list[str] = []
        self.closed = False
        self._draft_outcome = None
        self._claim_id = uuid4()
        self._claim_number = "EXP-DRAFT"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def post(self, path, payload, idempotency_key=None, expected_status_codes=None):
        self.posts.append(
            {"path": path, "payload": payload, "idempotency_key": idempotency_key}
        )
        outcome = self._post.pop(0) if self._post else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def create_expense_claim_draft(self, command, *, idempotency_key):
        self.posts.append(
            {
                "path": "/api/v1/sync/sub/expense-claims/drafts",
                "payload": command.model_dump(mode="json"),
                "idempotency_key": idempotency_key,
            }
        )
        if self._draft_outcome is None:
            queued_transition = self._post[0] if self._post else {}
            claim_id = (
                queued_transition.get("claim_id")
                if isinstance(queued_transition, dict)
                else None
            ) or uuid4()
            claim_number = (
                queued_transition.get("claim_number")
                if isinstance(queued_transition, dict)
                else None
            ) or "EXP-DRAFT"
            self._claim_id = claim_id
            self._claim_number = claim_number
            self._draft_outcome = type(
                "DraftOutcome",
                (),
                {
                    "claim_id": claim_id,
                    "claim_number": claim_number,
                    "status": "draft",
                    "items": tuple(
                        type(
                            "DraftLine",
                            (),
                            {
                                "source_line_id": item.source_line_id,
                                "item_id": uuid4(),
                            },
                        )()
                        for item in command.items
                    ),
                },
            )()
        return self._draft_outcome

    def upload_expense_receipt(self, command):
        self.posts.append(
            {
                "path": "receipt",
                "payload": {
                    "source_claim_id": str(command.source_claim_id),
                    "source_attachment_id": str(command.source_attachment_id),
                },
                "idempotency_key": command.idempotency_key,
            }
        )
        outcome = self._uploads.pop(0) if self._uploads else None
        if isinstance(outcome, Exception):
            raise outcome
        return type("ReceiptOutcome", (), {"created": True})()

    def approve_expense_claim(self, command, *, idempotency_key):
        self.posts.append(
            {
                "path": f"/api/v1/sync/sub/expense-claims/{command.source_claim_id}/approve",
                "payload": command.model_dump(mode="json", exclude_none=True),
                "idempotency_key": idempotency_key,
            }
        )
        supplied = self._post.pop(0) if self._post else {}
        outcome = (
            {
                "source_claim_id": str(command.source_claim_id),
                "claim_id": str(self._claim_id),
                "claim_number": self._claim_number,
                "status": "approved",
                **supplied,
            }
            if isinstance(supplied, dict)
            else supplied
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def submit_expense_claim(self, command, *, idempotency_key):
        self.posts.append(
            {
                "path": f"/api/v1/sync/sub/expense-claims/{command.source_claim_id}/submit",
                "payload": {},
                "idempotency_key": idempotency_key,
            }
        )
        outcome = {
            "source_claim_id": str(command.source_claim_id),
            "claim_id": str(self._draft_outcome.claim_id),
            "claim_number": self._draft_outcome.claim_number,
            "status": "submitted",
        }
        return outcome

    def reject_expense_claim(self, command, *, idempotency_key):
        self.posts.append(
            {
                "path": f"/api/v1/sync/sub/expense-claims/{command.source_claim_id}/reject",
                "payload": command.model_dump(mode="json", exclude_none=True),
                "idempotency_key": idempotency_key,
            }
        )
        outcome = (
            self._post.pop(0)
            if self._post
            else {
                "source_claim_id": str(command.source_claim_id),
                "claim_id": str(self._claim_id),
                "claim_number": self._claim_number,
                "status": "rejected",
            }
        )
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def initiate_expense_payment(
        self,
        command: ErpExpensePaymentCommand,
        *,
        idempotency_key: str,
    ) -> ErpExpensePaymentOutcome:
        self.posts.append(
            {
                "path": (
                    f"/api/v1/sync/sub/expense-claims/"
                    f"{command.source_claim_id}/payments"
                ),
                "payload": command.model_dump(
                    mode="json", exclude={"source_claim_id"}, exclude_none=True
                ),
                "idempotency_key": idempotency_key,
            }
        )
        supplied = self._post.pop(0) if self._post else {}
        if isinstance(supplied, Exception):
            raise supplied
        return ErpExpensePaymentOutcome.model_validate(
            {
                "source_claim_id": command.source_claim_id,
                "claim_id": self._claim_id,
                "claim_number": self._claim_number,
                "claim_status": "approved",
                "payment_intent_id": uuid4(),
                "payment_status": "processing",
                "retryable": False,
                **supplied,
            }
        )

    def get_expense_claim_status(self, source_claim_id):
        self.status_calls.append(source_claim_id)
        outcome = self._status.pop(0) if self._status else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


class _TypedOnlyPaymentERPClient(_FakeERPClient):
    def post(self, path, payload, idempotency_key=None, expected_status_codes=None):
        if str(path).endswith("/payments"):
            raise AssertionError(
                "Expense payments must not use the generic path sender"
            )
        return super().post(
            path,
            payload,
            idempotency_key=idempotency_key,
            expected_status_codes=expected_status_codes,
        )


class _ClaimBoundFakeERPClient(_FakeERPClient):
    """Fake ERP that enforces the real destination-token claim binding."""

    def __init__(self, *, requester: SystemUser, approver: SystemUser) -> None:
        super().__init__()
        self.requester = requester
        self.approver = approver
        self.approver_erp_id = uuid4()
        self.destination_claims: dict[str, tuple[UUID, VerifiedExpenseDestination]] = {}
        self.verify_claim_ids: list[UUID] = []
        self.inspect_claim_ids: list[UUID] = []
        self.draft_claim_ids: list[UUID] = []
        self.rejected_draft_claim_ids: list[UUID] = []

    def get_expense_approvers(
        self, *, requested_by_email: str
    ) -> tuple[ExpenseApproverOption, ...]:
        assert requested_by_email == self.requester.email
        return (
            ExpenseApproverOption(
                employee_id=self.approver_erp_id,
                display_name=self.approver.display_name,
                email=self.approver.email,
            ),
        )

    def get_expense_banks(self):
        return ()

    def get_expense_profile_destination(
        self, *, requested_by_email: str
    ) -> ExpenseProfileDestination:
        assert requested_by_email == self.requester.email
        return ExpenseProfileDestination(available=False)

    def verify_expense_destination(
        self, command: VerifyExpenseDestination
    ) -> VerifiedExpenseDestination:
        assert command.requested_by_email == self.requester.email
        now = datetime.now(UTC)
        verified = VerifiedExpenseDestination(
            destination_token=f"claim-bound-token:{command.source_claim_id}",
            mode=command.mode,
            bank_code="058",
            bank_name="Example Bank",
            masked_account_number="******6789",
            verified_beneficiary_name=self.requester.display_name,
            verified_at=now,
            expires_at=now + timedelta(minutes=10),
        )
        self.verify_claim_ids.append(command.source_claim_id)
        self.destination_claims[verified.destination_token] = (
            command.source_claim_id,
            verified,
        )
        return verified

    def inspect_expense_destination(
        self, command: InspectExpenseDestination
    ) -> VerifiedExpenseDestination:
        self.inspect_claim_ids.append(command.source_claim_id)
        binding = self.destination_claims.get(command.destination_token)
        if binding is None or binding[0] != command.source_claim_id:
            raise DotMacERPError(
                "expense_destination_mismatch",
                status_code=422,
                response={"code": "expense_destination_mismatch"},
            )
        return binding[1]

    def create_expense_claim_draft(
        self,
        command: ErpExpenseClaimDraftCommand,
        *,
        idempotency_key: str,
    ) -> ErpExpenseClaimDraftOutcome:
        binding = self.destination_claims.get(command.payment_destination_token or "")
        if binding is None or binding[0] != command.source_claim_id:
            self.rejected_draft_claim_ids.append(command.source_claim_id)
            raise DotMacERPError(
                "expense_destination_mismatch",
                status_code=422,
                response={"code": "expense_destination_mismatch"},
            )
        self.draft_claim_ids.append(command.source_claim_id)
        outcome = super().create_expense_claim_draft(
            command,
            idempotency_key=idempotency_key,
        )
        return ErpExpenseClaimDraftOutcome(
            claim_id=outcome.claim_id,
            claim_number=outcome.claim_number,
            status=outcome.status,
            source_claim_id=command.source_claim_id,
            items=tuple(
                ErpExpenseDraftLineOutcome(
                    source_line_id=item.source_line_id,
                    item_id=item.item_id,
                )
                for item in outcome.items
            ),
        )


def test_form_context_omits_requester_even_when_erp_returns_them(
    db_session,
    monkeypatch,
):
    requester = _user(db_session, "SelfApprover")
    client = _ClaimBoundFakeERPClient(requester=requester, approver=requester)
    monkeypatch.setattr(
        expense_requests_module, "capability_client", lambda _db: client
    )

    context = get_field_expense_form_context(
        db_session,
        GetFieldExpenseFormContext(requester_system_user_id=requester.id),
    )

    assert context.approvers == ()


def test_submission_context_rejects_requester_as_approver_before_token_inspection(
    db_session,
    monkeypatch,
):
    requester = _user(db_session, "SelfApprover")
    client = _ClaimBoundFakeERPClient(requester=requester, approver=requester)
    monkeypatch.setattr(
        expense_requests_module, "capability_client", lambda _db: client
    )

    with pytest.raises(FieldExpenseRequestError) as raised:
        resolve_field_expense_submission_context(
            db_session,
            ResolveFieldExpenseSubmissionContext(
                requester_system_user_id=requester.id,
                selected_approver_erp_id=client.approver_erp_id,
                source_claim_id=uuid4(),
                destination_token="self-approver-token-that-is-never-inspected",
            ),
        )

    assert raised.value.code == "operations.expense_requests.approver_invalid"
    assert client.inspect_claim_ids == []


def _submit_with_claim_bound_destination(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FieldExpenseRequest, _ClaimBoundFakeERPClient, UUID]:
    """Exercise verification and inspection before the real submit owner."""
    crm_person_id = f"crm-claim-bound-{uuid4().hex[:8]}"
    requester = _user(db, "ClaimBound")
    approver = _user(db, "ClaimBoundApprover")
    _profile(db, requester, crm_person_id=crm_person_id)
    subscriber = _subscriber(db)
    work_order = _work_order(
        db,
        subscriber,
        crm_work_order_id=f"wo-claim-bound-{uuid4().hex[:8]}",
        assigned_to_crm_person_id=crm_person_id,
    )
    source_claim_id = uuid4()
    client = _ClaimBoundFakeERPClient(requester=requester, approver=approver)
    monkeypatch.setattr(
        expense_requests_module,
        "capability_client",
        lambda _db: client,
    )

    verified = verify_field_expense_destination(
        db,
        VerifyFieldExpenseDestination(
            requester_system_user_id=requester.id,
            source_claim_id=source_claim_id,
            mode=ExpenseDestinationMode.ERP_PROFILE,
            bank_code=None,
            account_number=None,
            beneficiary_name=None,
        ),
    )
    resolved = resolve_field_expense_submission_context(
        db,
        ResolveFieldExpenseSubmissionContext(
            requester_system_user_id=requester.id,
            selected_approver_erp_id=client.approver_erp_id,
            source_claim_id=source_claim_id,
            destination_token=verified.destination_token,
        ),
    )
    requester_id = requester.id
    work_order_public_id = work_order.public_id
    db.commit()
    outcome = submit_field_expense_request_command(
        db,
        SubmitFieldExpenseRequest(
            context=CommandContext(
                command_id=source_claim_id,
                correlation_id=source_claim_id,
                actor=f"user:{requester_id}",
                scope="field:expense_requests:write",
                reason="test claim-bound expense submission",
                idempotency_key=str(source_claim_id),
            ),
            requester_person_id=requester_id,
            work_order=ExpenseWorkOrderIdentity(public_id=work_order_public_id),
            request_id=source_claim_id,
            purpose="Claim-bound transport",
            expense_date=date.today(),
            currency="NGN",
            notes=None,
            items=tuple(ExpenseRequestLineInput(**item) for item in _items()),
            selected_approver=resolved.selected_approver,
            payment_destination=resolved.payment_destination,
        ),
    )
    request = db.get(FieldExpenseRequest, outcome.id)
    assert request is not None
    return request, client, source_claim_id


# ---------------------------------------------------------------------------
# Payload mapping fidelity (vs CRM's _map_expense_request shape)
# ---------------------------------------------------------------------------


def test_payload_mapping_matches_neutral_erp_contract(db_session):
    request = _make_submitted_request(db_session)
    request.selected_approver_erp_id = uuid4()
    request.payment_destination_token = "enc:opaque-destination-token"
    payload = expense_sync.build_expense_claim_payload(request)

    assert payload["source_claim_id"] == str(request.id)
    assert payload["purpose"] == "Transport for extra drop cable"
    assert payload["claim_date"] == date.today().isoformat()
    assert payload["requested_by_email"] == request.requested_by_system_user.email
    assert payload["requested_approver_id"] == str(request.selected_approver_erp_id)
    assert payload["payment_destination_token"] == "enc:opaque-destination-token"
    assert "recipient_account_number" not in payload
    # Neutral source references come from retained work-order provenance.
    assert payload["ticket_source_reference"] == "crm-ticket-77"
    assert payload["project_source_reference"] == "crm-project-88"
    assert payload["currency_code"] == "NGN"
    assert payload["remarks"] == "Customer site was missing materials"

    assert len(payload["items"]) == 1
    line = payload["items"][0]
    assert line["category_code"] == "transport"
    assert line["description"] == "Bike delivery"
    # amount stringified into claimed_amount (CRM parity).
    assert line["claimed_amount"] == "2500.00"
    assert line["expense_date"] == date.today().isoformat()
    assert line["vendor_name"] == "Rider"
    assert line["notes"] == "Urgent part pickup"


def test_claim_bound_destination_accepts_complete_canonical_identity_path(
    db_session,
    monkeypatch,
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request, client, source_claim_id = _submit_with_claim_bound_destination(
        db_session,
        monkeypatch,
    )

    assert request.id == source_claim_id
    assert request.client_ref == source_claim_id
    assert client.verify_claim_ids == [source_claim_id]
    assert client.inspect_claim_ids == [source_claim_id]

    _approve(db_session, request)
    rows = _outbox_rows(db_session, request)
    assert len(rows) == 2
    delivery = rows[-1]
    assert delivery.payload["source_claim_id"] == str(source_claim_id)
    assert delivery.idempotency_key.startswith(f"exp-{source_claim_id}-approved-")
    assert delivery.idempotency_key.endswith("-v3")

    delivered = outbox.deliver_pending(db_session, client=client)

    assert delivered.accepted == 2
    assert client.draft_claim_ids == [source_claim_id]
    approval = next(post for post in client.posts if post["path"].endswith("/approve"))
    assert approval["payload"]["source_claim_id"] == str(source_claim_id)

    db_session.refresh(request)
    client._status.append(
        {
            "claim_id": request.expense_claim_reference,
            "claim_number": request.expense_claim_number,
            "status": "approved",
            "source_claim_id": str(source_claim_id),
        }
    )
    refreshed = expense_sync.refresh_expense_claim_statuses(
        db_session,
        client=client,
    )

    assert refreshed["processed"] == 1
    assert client.status_calls == [str(source_claim_id)]


def test_local_delivery_guard_rejects_changed_draft_source_claim_id(
    db_session,
    monkeypatch,
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request, client, source_claim_id = _submit_with_claim_bound_destination(
        db_session,
        monkeypatch,
    )
    _approve(db_session, request)
    delivery = _outbox_rows(db_session, request)[0]
    changed_source_claim_id = uuid4()
    delivery.payload = {
        **delivery.payload,
        "source_claim_id": str(changed_source_claim_id),
    }
    db_session.commit()

    delivered = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(delivery)
    db_session.refresh(request)
    assert changed_source_claim_id != source_claim_id
    assert client.draft_claim_ids == []
    assert client.rejected_draft_claim_ids == []
    assert delivered.dead == 1
    assert delivery.status == FieldErpSyncStatus.dead.value
    assert request.expense_claim_reference is None


def test_approval_rejects_legacy_token_bearing_claim_identity_mismatch(
    db_session,
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    request_id = request.id
    request.client_ref = uuid4()
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as raised:
        _approve(db_session, request)

    persisted = db_session.get(FieldExpenseRequest, request_id)
    assert persisted is not None
    assert raised.value.code == (
        "operations.expense_requests.claim_identity_inconsistent"
    )
    assert persisted.status == "submitted"
    assert persisted.approved_at is None
    assert persisted.payment_destination_locked_at is None
    rows = _outbox_rows(db_session, persisted)
    assert len(rows) == 1
    assert rows[0].payload["_expense_action"] == "expense_submit_v3"


def test_payment_does_not_enqueue_for_legacy_claim_identity_mismatch(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    manager = _user(db_session, "MismatchedPaymentManager")
    request.client_ref = uuid4()
    request.status = "approved"
    request.approved_at = datetime.now(UTC)
    command_id = uuid4()
    manager_id = manager.id
    request_id = request.id
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as raised:
        initiate_field_expense_payment_command(
            db_session,
            command=InitiateFieldExpensePayment(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor=f"user:{manager_id}",
                    scope="operations:expense_request:pay",
                    reason=f"pay_expense_request:{request_id}",
                    idempotency_key=str(command_id),
                ),
                expense_request_id=request_id,
                manager_system_user_id=manager_id,
            ),
        )

    assert raised.value.code == (
        "operations.expense_requests.claim_identity_inconsistent"
    )
    rows = _outbox_rows(db_session, request)
    assert len(rows) == 1
    assert rows[0].payload["_expense_action"] == "expense_submit_v3"


def test_approval_release_idempotency_key_is_stable(db_session):
    request = _make_submitted_request(db_session)
    key1 = expense_sync.expense_release_idempotency_key(request)
    key2 = expense_sync.expense_release_idempotency_key(request)
    assert key1 == key2 == f"exp-{request.id}-approved-release-v2"


def test_new_submission_uses_one_uuid_and_one_v3_event(db_session):
    request = _make_submitted_request(db_session)

    rows = _outbox_rows(db_session, request)
    assert request.id == request.client_ref
    assert len(rows) == 1
    assert rows[0].idempotency_key == f"exp-{request.id}-submitted-v3"
    assert rows[0].payload["_expense_action"] == "expense_submit_v3"
    assert rows[0].payload["source_claim_id"] == str(request.id)


def test_submission_owner_rejects_requester_as_selected_approver(db_session):
    existing_request_count = db_session.query(FieldExpenseRequest).count()

    with pytest.raises(FieldExpenseRequestError) as raised:
        _make_submitted_request(db_session, self_approver=True)

    assert raised.value.code == "operations.expense_requests.approver_invalid"
    assert db_session.query(FieldExpenseRequest).count() == existing_request_count


def test_eligibility_requires_manager_approval(db_session):
    request = _make_submitted_request(db_session)
    assert "cannot be synced" in expense_sync.expense_claim_eligibility_error(request)

    request.status = "approved"
    request.approved_at = datetime.now(UTC)
    assert expense_sync.expense_claim_eligibility_error(request) is None

    request.status = "draft"
    assert "cannot be synced" in expense_sync.expense_claim_eligibility_error(request)


def test_only_selected_approver_can_approve(db_session):
    request = _make_submitted_request(db_session)
    unselected_reviewer = _user(db_session, "Unselected Approver")
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as exc:
        _approve(db_session, request, reviewer_id=unselected_reviewer.id)

    assert exc.value.code == "operations.expense_requests.approver_mismatch"


def test_approval_owner_rejects_historical_self_selected_claim_before_mutation(
    db_session,
):
    request = _make_submitted_request(db_session)
    requester_id = request.requested_by_system_user_id
    assert requester_id is not None
    requester = request.requested_by_system_user
    assert requester is not None
    request.requested_by_system_user_id = None
    request.selected_approver_system_user_id = requester_id
    request.selected_approver_email = requester.email
    request.selected_approver_name = requester.display_name
    request_id = request.id
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as raised:
        _approve(db_session, request, reviewer_id=requester_id)

    persisted = db_session.get(FieldExpenseRequest, request_id)
    assert persisted is not None
    assert raised.value.code == "operations.expense_requests.approver_invalid"
    assert persisted.status == "submitted"
    assert persisted.approved_at is None
    assert persisted.payment_destination_locked_at is None
    assert [
        event.payload["_expense_action"]
        for event in _outbox_rows(db_session, persisted)
    ] == ["expense_submit_v3"]


# ---------------------------------------------------------------------------
# Enqueue on submission and approval — gated by ownership
# ---------------------------------------------------------------------------


def _outbox_rows(db, request) -> list[FieldErpSyncEvent]:
    return (
        db.query(FieldErpSyncEvent)
        .filter(FieldErpSyncEvent.entity_id == request.id)
        .all()
    )


def test_submit_atomically_enqueues_v3_event(db_session):
    request = _make_submitted_request(db_session)
    rows = _outbox_rows(db_session, request)
    assert len(rows) == 1
    assert rows[0].payload["_expense_action"] == "expense_submit_v3"


def test_approval_fails_closed_before_ownership_cutover(db_session):
    request = _make_submitted_request(db_session)
    ownership = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )
    ownership.owner = SyncFlowOwner.crm.value
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as raised:
        _approve(db_session, request)

    request = db_session.get(FieldExpenseRequest, request.id)
    assert raised.value.code.endswith("erp_delivery_not_configured")
    assert request.status == "submitted"
    assert request.approved_at is None
    assert len(_outbox_rows(db_session, request)) == 1


def test_adapter_failure_rolls_back_approval_for_safe_retry(db_session, monkeypatch):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    request = _make_submitted_request(db_session)
    monkeypatch.setattr(backoffice, "enqueue_expense_decision", fail_enqueue)

    with pytest.raises(FieldExpenseRequestError) as raised:
        _approve(db_session, request)

    request = db_session.get(FieldExpenseRequest, request.id)
    assert raised.value.code.endswith("erp_staging_failed")
    assert request.status == "submitted"
    assert request.approved_at is None
    assert len(_outbox_rows(db_session, request)) == 1


def test_approval_enqueues_with_owner_and_enabled_capability(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    assert len(_outbox_rows(db_session, request)) == 1

    outcome = _approve(db_session, request)

    rows = _outbox_rows(db_session, request)
    assert len(rows) == 2
    row = rows[-1]
    assert row.flow == FieldErpSyncFlow.expense_claim.value
    assert row.idempotency_key.startswith(f"exp-{request.id}-approved-")
    assert row.idempotency_key.endswith("-v3")
    assert row.payload["_expense_action"] == "expense_approve_v3"
    assert row.payload["_depends_on_idempotency_key"] == (
        f"exp-{request.id}-submitted-v3"
    )
    assert row.status == FieldErpSyncStatus.pending.value
    assert outcome.status == "approved"
    assert outcome.erp_sync_status is ExpenseErpSyncStatus.PENDING
    assert outcome.erp_sync_event_id == row.id


def test_approval_waits_for_submission_without_consuming_attempt(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    submission, approval = _outbox_rows(db_session, request)
    submission.status = FieldErpSyncStatus.dead.value
    db_session.commit()

    result = outbox.deliver_pending(db_session, client=_FakeERPClient())

    db_session.refresh(approval)
    assert result.processed == 0
    assert approval.status == FieldErpSyncStatus.pending.value
    assert approval.attempts == 0


def test_token_bound_to_different_request_is_refused_before_approval(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    request.client_ref = uuid4()
    db_session.commit()

    with pytest.raises(FieldExpenseRequestError) as raised:
        _approve(db_session, request)

    assert raised.value.code == (
        "operations.expense_requests.claim_identity_inconsistent"
    )


def test_approval_stages_before_delivery_capability_is_enabled(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)

    outcome = _approve(db_session, request)

    assert outcome.erp_sync_status is ExpenseErpSyncStatus.PENDING
    assert len(_outbox_rows(db_session, request)) == 2


def test_reapprove_reuses_the_same_outbox_row(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    first = _approve(db_session, request)
    request = db_session.get(FieldExpenseRequest, request.id)
    # Re-enqueue directly with the same (stable) key → idempotent, no duplicate.
    second = _approve(db_session, request)
    assert first.erp_sync_event_id == second.erp_sync_event_id
    assert len(_outbox_rows(db_session, request)) == 2


def test_payment_stages_after_approval_with_a_distinct_permission(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    manager = _user(db_session, "PayingManager")
    command_id = uuid4()
    db_session.commit()

    command = InitiateFieldExpensePayment(
        context=CommandContext(
            command_id=command_id,
            correlation_id=command_id,
            actor=f"user:{manager.id}",
            scope="operations:expense_request:pay",
            reason=f"pay_expense_request:{request.id}",
            idempotency_key=str(command_id),
        ),
        expense_request_id=request.id,
        manager_system_user_id=manager.id,
    )
    db_session_adapter.release_read_transaction(db_session)
    outcome = initiate_field_expense_payment_command(
        db_session,
        command=command,
    )

    rows = _outbox_rows(db_session, request)
    payment = next(
        row for row in rows if row.payload["_expense_action"] == "initiate_payment"
    )
    assert outcome.payment_status == "queued"
    assert outcome.erp_sync_event_id == payment.id
    assert payment.payload["_depends_on_idempotency_key"].startswith(
        f"exp-{request.id}-approved-"
    )
    assert payment.payload["_depends_on_idempotency_key"].endswith("-v3")
    assert payment.payload["initiated_by_email"] == manager.email


def test_payment_delivery_uses_typed_capability_and_writes_erp_projection(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    manager = _user(db_session, "PaymentDeliveryManager")
    command_id = uuid4()
    payment_intent_id = uuid4()
    expense_request_id = request.id
    manager_id = manager.id
    manager_email = manager.email
    db_session.commit()

    db_session_adapter.release_read_transaction(db_session)
    initiate_field_expense_payment_command(
        db_session,
        command=InitiateFieldExpensePayment(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{manager_id}",
                scope="operations:expense_request:pay",
                reason=f"pay_expense_request:{expense_request_id}",
                idempotency_key=str(command_id),
            ),
            expense_request_id=expense_request_id,
            manager_system_user_id=manager_id,
        ),
    )
    client = _TypedOnlyPaymentERPClient(
        post_outcomes=[
            {"status": "approved"},
            {
                "claim_status": "approved",
                "payment_intent_id": str(payment_intent_id),
                "payment_status": "processing",
                "retryable": False,
            },
        ]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    rows = _outbox_rows(db_session, request)
    payment = next(
        row for row in rows if row.payload["_expense_action"] == "initiate_payment"
    )
    payment_post = client.posts[-1]
    assert result.accepted == 3
    assert payment.status == FieldErpSyncStatus.accepted.value
    assert payment_post["path"] == (
        f"/api/v1/sync/sub/expense-claims/{expense_request_id}/payments"
    )
    payment_payload = payment_post["payload"]
    assert payment_payload["command_id"] == str(command_id)
    assert payment_payload["initiated_by_email"] == manager_email
    assert datetime.fromisoformat(
        str(payment_payload["initiated_at"]).replace("Z", "+00:00")
    ) == datetime.fromisoformat(
        str(payment.payload["initiated_at"]).replace("Z", "+00:00")
    )
    assert payment_post["idempotency_key"] == (
        f"exp-{expense_request_id}-pay-{command_id}-v1"
    )
    assert request.metadata_["erp_payment"]["status"] == "processing"
    assert request.metadata_["erp_payment"]["intent_id"] == str(payment_intent_id)


# ---------------------------------------------------------------------------
# Outbox delivery → write-back onto the source row
# ---------------------------------------------------------------------------


def test_delivery_accepted_writes_erp_fields_back(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[
            {
                "claim_id": "ERP-CLAIM-1",
                "claim_number": "EXP-0001",
                "status": "approved",
                "source_claim_id": str(request.id),
            },
        ]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert result.accepted == 2
    assert request.expense_claim_reference == "ERP-CLAIM-1"
    assert request.expense_claim_number == "EXP-0001"
    assert request.expense_claim_status == "approved"
    # ERP transport status cannot rewind the local approval decision.
    assert request.status == "approved"
    assert client.posts[0]["path"] == "/api/v1/sync/sub/expense-claims/drafts"
    assert client.posts[1]["path"] == (
        f"/api/v1/sync/sub/expense-claims/{request.id}/submit"
    )
    assert client.posts[2]["path"] == (
        f"/api/v1/sync/sub/expense-claims/{request.id}/approve"
    )


def test_delivery_approved_maps_terminal_status(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[
            {"claim_id": "ERP-2", "claim_number": "EXP-2", "status": "approved"},
        ]
    )

    outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert request.expense_claim_status == "approved"
    assert request.status == "approved"
    assert request.approved_at is not None


def test_delivery_rejected_keeps_failure_evidence_on_outbox(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[{"status": "rejected", "rejection_reason": "over budget"}]
    )

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[-1]
    assert result.dead == 1
    assert row.status == FieldErpSyncStatus.dead.value
    assert request.expense_claim_status == "submitted"
    assert request.status == "approved"
    assert request.rejection_reason is None


def test_partial_receipt_failure_reuses_claim_and_uploads_only_missing_receipts(
    db_session, monkeypatch
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    _enable_receipt_staging(monkeypatch)
    receipt_client_refs = (uuid4(), uuid4())
    request = _make_submitted_request(
        db_session,
        items=[
            _items(
                description="Taxi",
                receipt_upload=ExpenseReceiptUploadInput(
                    file_name="taxi.pdf",
                    mime_type="application/pdf",
                    content=b"taxi receipt",
                    client_ref=receipt_client_refs[0],
                ),
            )[0],
            _items(
                description="Hotel",
                amount="5000.00",
                receipt_upload=ExpenseReceiptUploadInput(
                    file_name="hotel.pdf",
                    mime_type="application/pdf",
                    content=b"hotel receipt",
                    client_ref=receipt_client_refs[1],
                ),
            )[0],
        ],
    )
    attachment_ids = tuple(item.receipt_attachment_id for item in request.items)
    assert all(attachment_id is not None for attachment_id in attachment_ids)

    def resolve_receipt(_db, *, work_order_id, attachment_id, allowed_owner_ids):
        assert work_order_id == request.work_order_mirror_id
        assert request.requested_by_system_user_id in allowed_owner_ids
        content = f"receipt:{attachment_id}".encode()
        return ResolvedExpenseReceiptAttachment(
            attachment_id=attachment_id,
            work_order_id=work_order_id,
            file_name=f"{attachment_id}.pdf",
            mime_type="application/pdf",
            size_bytes=len(content),
            checksum_sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )

    monkeypatch.setattr(
        attachments_module, "resolve_expense_receipt_attachment", resolve_receipt
    )
    db_session.commit()
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[{"status": "approved"}],
        upload_outcomes=[
            None,
            DotMacERPTransientError("private upstream detail"),
            None,
        ],
    )

    first = outbox.deliver_pending(db_session, client=client)
    row = _outbox_rows(db_session, request)[0]
    assert first.retried == 1
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.last_error == "ERP expense release is temporarily unavailable"

    second = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(row)
    assert second.accepted == 2
    assert row.status == FieldErpSyncStatus.accepted.value
    draft_posts = [entry for entry in client.posts if entry["path"].endswith("/drafts")]
    receipt_posts = [entry for entry in client.posts if entry["path"] == "receipt"]
    approval_posts = [
        entry for entry in client.posts if entry["path"].endswith("/approve")
    ]
    assert len(draft_posts) == 2
    assert [entry["payload"]["source_attachment_id"] for entry in receipt_posts] == [
        str(attachment_ids[0]),
        str(attachment_ids[1]),
        str(attachment_ids[1]),
    ]
    assert {entry["payload"]["source_claim_id"] for entry in receipt_posts} == {
        str(request.id)
    }
    assert len(approval_posts) == 1
    assert row.erp_response["uploaded_source_attachment_ids"] == sorted(
        str(value) for value in attachment_ids
    )


def test_permanent_receipt_failure_is_dead_with_safe_diagnostics(
    db_session, monkeypatch
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    _enable_receipt_staging(monkeypatch)
    request = _make_submitted_request(
        db_session,
        items=_items(
            receipt_upload=ExpenseReceiptUploadInput(
                file_name="private-person-name.pdf",
                mime_type="application/pdf",
                content=b"private receipt bytes",
                client_ref=uuid4(),
            )
        ),
    )
    attachment_id = request.items[0].receipt_attachment_id
    assert attachment_id is not None
    content = b"private receipt bytes"
    monkeypatch.setattr(
        attachments_module,
        "resolve_expense_receipt_attachment",
        lambda _db, *, work_order_id, attachment_id, allowed_owner_ids: (
            ResolvedExpenseReceiptAttachment(
                attachment_id=attachment_id,
                work_order_id=work_order_id,
                file_name="private-person-name.pdf",
                mime_type="application/pdf",
                size_bytes=len(content),
                checksum_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            )
        ),
    )
    db_session.commit()
    _approve(db_session, request)
    request_id = uuid4()
    client = _FakeERPClient(
        upload_outcomes=[
            DotMacERPError(
                "credential and private file detail",
                diagnostic=safe_diagnostic(status=422).model_copy(
                    update={"request_id": request_id}
                ),
            )
        ]
    )

    result = outbox.deliver_pending(db_session, client=client)

    row = _outbox_rows(db_session, request)[0]
    assert result.dead == 1
    assert row.status == FieldErpSyncStatus.dead.value
    assert row.last_error == (
        "ERP rejected request validation; inspect redacted ERP validation evidence. "
        f"(code=validation_error; status=422; request_id={request_id})"
    )
    assert row.erp_response["delivery_diagnostic"]["request_id"] == str(request_id)
    assert row.erp_response["delivery_diagnostic"]["code"] == "validation_error"
    diagnostic = " ".join(result.errors)
    assert "credential" not in diagnostic
    assert "private-person-name" not in diagnostic
    assert "private receipt bytes" not in diagnostic


def test_dead_event_recovery_is_previewed_linked_and_non_destructive(
    db_session, monkeypatch
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    legacy = outbox.enqueue(
        db_session,
        flow=FieldErpSyncFlow.expense_claim,
        entity_type="field_expense_request",
        entity_id=request.id,
        idempotency_key=expense_sync.expense_release_idempotency_key(request),
        payload=expense_sync.build_approved_expense_release_payload(
            request,
            decision_id=uuid4(),
            decided_by_email=request.selected_approver_email,
            decided_at=datetime.now(UTC),
        ),
        isolate=False,
    )
    original = legacy
    original.status = FieldErpSyncStatus.dead.value
    original.last_error = "ERP expense release was rejected"
    original.erp_response = {
        "claim_status": "draft",
        "claim_id": "63d11a42-c707-4ddb-ad01-3a0523b520dd",
        "uploaded_source_attachment_ids": [],
    }
    original_id = original.id
    db_session.commit()
    erp = _FakeERPClient(status_outcomes=[{"status": "draft"}, {"status": "draft"}])
    monkeypatch.setattr(
        expense_recovery_module,
        "capability_client",
        lambda _db: erp,
    )

    preview = preview_expense_delivery_recovery(
        db_session,
        PreviewExpenseDeliveryRecovery(dead_event_id=original_id),
    )
    db_session.commit()
    command_id = uuid4()
    outcome = recover_expense_delivery(
        db_session,
        command=RecoverExpenseDelivery(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="user:recovery-operator",
                scope="operations:expense_request:write",
                reason="explicit expense delivery recovery",
                idempotency_key=str(command_id),
            ),
            dead_event_id=original_id,
            preview_fingerprint=preview.fingerprint,
        ),
    )

    original = db_session.get(FieldErpSyncEvent, original_id)
    replacement = db_session.get(FieldErpSyncEvent, outcome.replacement_event_id)
    assert original.status == FieldErpSyncStatus.dead.value
    assert original.last_error == "ERP expense release was rejected"
    assert replacement.status == FieldErpSyncStatus.pending.value
    assert replacement.payload["_replaces_event_id"] == str(original_id)
    assert replacement.idempotency_key.endswith("expense-delivery-recovery.v1")
    assert replacement.erp_response["claim_status"] == "draft"


def _permission_denied_payment_event(
    db_session: Session,
    request: FieldExpenseRequest,
) -> FieldErpSyncEvent:
    event = expense_sync.enqueue_expense_payment(
        db_session,
        request,
        command_id=uuid4(),
        initiated_by_email="payment.manager@example.com",
        initiated_at=datetime.now(UTC),
        isolate=False,
    )
    diagnostic = safe_diagnostic(status=403).model_copy(
        update={
            "operation": "initiate_expense_payment",
            "request_id": uuid4(),
        }
    )
    event.status = FieldErpSyncStatus.dead.value
    event.attempts = 1
    event.last_error = "ERP permission denied"
    event.erp_response = {
        DELIVERY_DIAGNOSTIC_KEY: diagnostic_evidence(diagnostic),
    }
    db_session.commit()
    return event


def test_permission_denied_payment_recovery_requeues_same_idempotent_event(
    db_session,
    monkeypatch,
):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    event = _permission_denied_payment_event(db_session, request)
    event_id = event.id
    idempotency_key = event.idempotency_key
    original_count = len(_outbox_rows(db_session, request))
    erp = _FakeERPClient(
        status_outcomes=[{"status": "approved"}, {"status": "approved"}]
    )
    monkeypatch.setattr(expense_recovery_module, "capability_client", lambda _db: erp)

    preview = preview_expense_payment_delivery_recovery(
        db_session,
        PreviewExpensePaymentDeliveryRecovery(dead_event_id=event_id),
    )
    db_session.commit()
    command_id = uuid4()
    outcome = recover_expense_payment_delivery(
        db_session,
        command=RecoverExpensePaymentDelivery(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor="user:payment-recovery-operator",
                scope="operations:expense_request:pay",
                reason="recover permission-denied payment delivery",
                idempotency_key=str(command_id),
            ),
            dead_event_id=event_id,
            preview_fingerprint=preview.fingerprint,
        ),
    )

    db_session.expire_all()
    recovered = db_session.get(FieldErpSyncEvent, event_id)
    assert recovered is not None
    assert outcome.event_id == event_id
    assert outcome.idempotency_key == idempotency_key
    assert outcome.replayed is False
    assert recovered.status == FieldErpSyncStatus.pending.value
    assert recovered.attempts == 1
    assert len(_outbox_rows(db_session, request)) == original_count
    recovery_evidence = request.metadata_["expense_payment_delivery_recoveries"][-1]
    assert recovery_evidence["event_id"] == str(event_id)
    assert recovery_evidence["idempotency_key"] == idempotency_key


def test_payment_recovery_rejects_approval_write_scope(db_session, monkeypatch):
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    event = _permission_denied_payment_event(db_session, request)
    event_id = event.id
    erp = _FakeERPClient(status_outcomes=[{"status": "approved"}])
    monkeypatch.setattr(expense_recovery_module, "capability_client", lambda _db: erp)
    preview = preview_expense_payment_delivery_recovery(
        db_session,
        PreviewExpensePaymentDeliveryRecovery(dead_event_id=event_id),
    )
    db_session.commit()
    command_id = uuid4()

    with pytest.raises(
        expense_recovery_module.ExpenseDeliveryRecoveryError,
        match="payment permission",
    ):
        recover_expense_payment_delivery(
            db_session,
            command=RecoverExpensePaymentDelivery(
                context=CommandContext(
                    command_id=command_id,
                    correlation_id=command_id,
                    actor="user:approval-only-operator",
                    scope="operations:expense_request:write",
                    reason="attempt payment recovery with approval scope",
                    idempotency_key=str(command_id),
                ),
                dead_event_id=event_id,
                preview_fingerprint=preview.fingerprint,
            ),
        )

    db_session.expire_all()
    assert (
        db_session.get(FieldErpSyncEvent, event_id).status
        == FieldErpSyncStatus.dead.value
    )


def test_payment_recovery_fails_closed_when_erp_has_payment_evidence(
    db_session,
    monkeypatch,
):
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    event = _permission_denied_payment_event(db_session, request)
    erp = _FakeERPClient(
        status_outcomes=[
            {
                "status": "approved",
                "payment_status": "processing",
                "payment_intent_id": str(uuid4()),
            }
        ]
    )
    monkeypatch.setattr(expense_recovery_module, "capability_client", lambda _db: erp)

    with pytest.raises(
        expense_recovery_module.ExpenseDeliveryRecoveryError,
        match="unambiguous recovery",
    ):
        preview_expense_payment_delivery_recovery(
            db_session,
            PreviewExpensePaymentDeliveryRecovery(dead_event_id=event.id),
        )


def test_local_rejection_enqueues_ordered_erp_delivery(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    reviewer_id = request.selected_approver_system_user_id
    assert reviewer_id is not None
    expense_request_id = request.id
    command_id = uuid4()
    db_session.commit()

    outcome = reject_field_expense_request_command(
        db_session,
        command=RejectFieldExpenseRequest(
            context=CommandContext(
                command_id=command_id,
                correlation_id=command_id,
                actor=f"user:{reviewer_id}",
                scope="operations:expense_request:write",
                reason=f"reject_expense_request:{expense_request_id}",
                idempotency_key=str(command_id),
            ),
            expense_request_id=expense_request_id,
            reviewer_system_user_id=reviewer_id,
            reason="Missing receipt",
        ),
    )

    assert outcome.status == "rejected"
    assert outcome.erp_sync_event_id is not None
    rows = _outbox_rows(db_session, request)
    assert len(rows) == 2
    rejected = rows[-1]
    assert rejected.payload["_expense_action"] == "expense_reject_v3"
    assert rejected.payload["_depends_on_idempotency_key"] == (
        f"exp-{request.id}-submitted-v3"
    )


# ---------------------------------------------------------------------------
# Ownership guard — the inert guarantee
# ---------------------------------------------------------------------------


def test_historical_preapproval_event_is_never_delivered(db_session):
    # expense_claim left at the seeded default (crm) — must NOT be sent.
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    submission = _outbox_rows(db_session, request)[0]
    submission.status = FieldErpSyncStatus.accepted.value
    outbox.enqueue(
        db_session,
        flow=FieldErpSyncFlow.expense_claim,
        entity_type="field_expense_request",
        entity_id=request.id,
        idempotency_key=f"exp-{request.id}-submit-v1",
        payload={"_expense_action": "submit", "source_claim_id": str(request.id)},
        isolate=False,
    )
    db_session.commit()
    client = _FakeERPClient(post_outcomes=[{"claim_id": "SHOULD-NOT-HAPPEN"}])

    result = outbox.deliver_pending(db_session, client=client)

    db_session.refresh(request)
    assert result.skipped_preapproval == 1
    assert client.posts == []
    assert request.expense_claim_reference is None
    row = _outbox_rows(db_session, request)[-1]
    assert row.status == FieldErpSyncStatus.pending.value
    assert row.attempts == 0


# ---------------------------------------------------------------------------
# Status reconcile
# ---------------------------------------------------------------------------


def test_refresh_updates_status_for_in_flight_claim(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    request = _make_submitted_request(db_session)
    request.expense_system = "dotmac_erp"
    request.expense_claim_reference = "ERP-CLAIM-9"
    request.expense_claim_status = "submitted"
    db_session.commit()

    client = _FakeERPClient(
        status_outcomes=[
            {"claim_id": "ERP-CLAIM-9", "claim_number": "EXP-9", "status": "approved"}
        ]
    )
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    db_session.refresh(request)
    assert result["processed"] == 1
    assert result["updated"] == 1
    assert client.status_calls == [str(request.id)]
    assert request.expense_claim_status == "approved"
    # ERP polling is transport evidence only; manager approval remains local authority.
    assert request.status == "submitted"


def test_refresh_drains_a_sent_row_that_the_linked_query_could_never_select(
    db_session,
):
    """A ``sent`` row has no reference BY DEFINITION — the old query excluded it
    forever (the dead end). The widened poller must find it via the outbox and
    drain it to ``accepted`` once ERP returns a real id.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    # Simulate a historical sent delivery whose projection write-back was lost.
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    row.status = FieldErpSyncStatus.sent.value
    row.erp_response = {}
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    assert request.expense_claim_reference is None
    assert row.status == FieldErpSyncStatus.sent.value

    client = _FakeERPClient(
        status_outcomes=[
            {
                "claim_id": "ERP-CLAIM-LATE",
                "claim_number": "EXP-LATE",
                "status": "approved",
            }
        ]
    )
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    assert row.status == FieldErpSyncStatus.accepted.value
    assert request.expense_claim_reference == "ERP-CLAIM-LATE"
    assert result["processed"] == 1
    assert result["updated"] == 1
    assert client.status_calls == [str(request.id)]


# ---------------------------------------------------------------------------
# Write-back repair — a delivered claim whose write-back was lost, no re-emit
# ---------------------------------------------------------------------------


def test_repair_restores_a_dropped_expense_claim_writeback(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[{"claim_id": "ERP-CLAIM-REPAIR", "status": "approved"}]
    )
    outbox.deliver_pending(db_session, client=client)
    db_session.refresh(request)
    assert request.expense_claim_reference == "ERP-CLAIM-REPAIR"

    # Simulate a DROPPED write-back: the outbox row is terminal but the
    # request's own reference never landed.
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    row_ids_before = {row.id for row in _outbox_rows(db_session, request)}

    result = expense_sync.repair_expense_claim_writebacks(db_session)

    db_session.refresh(request)
    assert result["repaired"] == 1
    assert request.expense_claim_reference == "ERP-CLAIM-REPAIR"
    # No re-emit: submission and approval retain the same two delivery rows.
    rows = _outbox_rows(db_session, request)
    assert {row.id for row in rows} == row_ids_before
    assert len(rows) == 2
    assert sum(row.status == FieldErpSyncStatus.accepted.value for row in rows) == 2


def test_repair_makes_no_erp_call_and_no_writeback_for_a_crm_owned_flow(db_session):
    """Michael's finding, applied to expense claims: ownership can move back
    to CRM after a row was delivered while sub owned the flow — the repair
    must re-check ownership on every run, not assume the past delivery still
    means current ownership. No write-back and no ERP call may happen when
    the flow is currently CRM-owned; the row must be counted under
    ``skipped_not_owned``, not ``repaired``.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    client = _FakeERPClient(
        post_outcomes=[{"claim_id": "ERP-CLAIM-REPAIR", "status": "approved"}]
    )
    outbox.deliver_pending(db_session, client=client)
    db_session.refresh(request)
    assert request.expense_claim_reference == "ERP-CLAIM-REPAIR"

    # Simulate a DROPPED write-back, same as the repaired-case test above.
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()

    # Ownership moves back to CRM before the scheduled repair runs again.
    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )
    ownership_row.owner = SyncFlowOwner.crm.value
    db_session.commit()

    result = expense_sync.repair_expense_claim_writebacks(db_session)

    db_session.refresh(request)
    assert result["repaired"] == 0
    assert result["processed"] == 0
    # Both accepted lifecycle responses are deliberately skipped.
    assert result["skipped_not_owned"] == 2
    # No re-apply happened: the request's own reference is still missing.
    assert request.expense_claim_reference is None
    assert request.expense_claim_status is None


def test_repair_stops_mid_batch_when_ownership_flips(db_session, monkeypatch):
    """``repair_expense_claim_writebacks`` makes no live ERP call, but it does
    a real DB write/commit per row across a batch (up to 500) — the ownership
    gate is re-checked before EVERY row, not once before the loop, so a
    mid-batch flip stops the rows reached after it in this same run. Unlike
    the poll functions, there's no live network round trip inside a single
    row's own processing here, so a single pre-row check (no post-apply
    recheck) is sufficient — nothing external can change ownership again
    between that check and the synchronous re-apply for the SAME row.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)

    first = _make_submitted_request(db_session, crm_work_order_id="wo-repair-first")
    _approve(db_session, first)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(
            post_outcomes=[{"claim_id": "ERP-REPAIR-A", "status": "approved"}]
        ),
    )
    db_session.refresh(first)
    assert first.expense_claim_reference == "ERP-REPAIR-A"

    second = _make_submitted_request(db_session, crm_work_order_id="wo-repair-second")
    _approve(db_session, second)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(
            post_outcomes=[{"claim_id": "ERP-REPAIR-B", "status": "approved"}]
        ),
    )
    db_session.refresh(second)
    assert second.expense_claim_reference == "ERP-REPAIR-B"

    # Simulate a dropped write-back for both, same as the single-row repair
    # tests above.
    first.expense_claim_reference = None
    first.expense_claim_status = None
    second.expense_claim_reference = None
    second.expense_claim_status = None
    db_session.commit()

    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )

    # No live ERP call to hook here (unlike the poll tests' fake-client
    # override) — simulate the mid-batch flip as a plain ownership mutation
    # that happens right after the first row's own re-apply, by wrapping the
    # exact function the loop calls per row.
    original_apply_claim_response = expense_sync.apply_claim_response
    calls: list[str] = []

    def _apply_then_flip_after_first(request, response):
        original_apply_claim_response(request, response)
        calls.append(str(request.id))
        if len(calls) == 1:
            ownership_row.owner = SyncFlowOwner.crm.value
            db_session.commit()

    monkeypatch.setattr(
        expense_sync, "apply_claim_response", _apply_then_flip_after_first
    )

    result = expense_sync.repair_expense_claim_writebacks(db_session)

    db_session.refresh(first)
    db_session.refresh(second)
    # Only one row's response was ever (re-)applied.
    assert len(calls) == 1
    assert result["processed"] == 1
    assert result["repaired"] == 1
    # Each fully-submitted-and-approved request stages TWO accepted outbox
    # events (the submit and the approve transitions — see
    # `test_repair_makes_no_erp_call_and_no_writeback_for_a_crm_owned_flow`'s
    # `skipped_not_owned == 2` for one request), so two requests here produce
    # FOUR candidate rows total. Exactly one of `first`'s two rows is reached
    # before the flip and gets repaired; the OTHER of `first`'s two rows and
    # BOTH of `second`'s rows are all reached after the flip and are skipped
    # by the per-row ownership check (which runs before the
    # already-has-a-reference check, so the second of `first`'s own rows is
    # counted here too, not silently filtered by already having a reference).
    assert result["skipped_not_owned"] == 3
    assert first.expense_claim_reference == "ERP-REPAIR-A"
    # The second row, reached only after the flip, must NOT be repaired.
    assert second.expense_claim_reference is None


def test_unlinked_status_poll_makes_no_erp_call_for_a_crm_owned_expense_flow(
    db_session,
):
    """The poll-drain path (``_poll_unlinked_expense_claims``, reached via
    ``refresh_expense_claim_statuses``) makes a real ERP call
    (``get_expense_claim_status``). It must be skipped for a currently
    CRM-owned flow, even though the row was delivered while sub owned it.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    row.status = FieldErpSyncStatus.sent.value
    row.erp_response = {}
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    assert request.expense_claim_reference is None
    assert row.status == FieldErpSyncStatus.sent.value

    # Ownership moves back to CRM before the poll runs.
    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )
    ownership_row.owner = SyncFlowOwner.crm.value
    db_session.commit()

    client = _FakeERPClient(
        status_outcomes=[{"claim_id": "SHOULD-NOT-HAPPEN", "status": "approved"}]
    )
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    assert client.status_calls == []
    assert result["skipped_not_owned"] == 2
    db_session.refresh(request)
    assert request.expense_claim_reference is None
    row = _outbox_rows(db_session, request)[0]
    assert row.status == FieldErpSyncStatus.sent.value


def test_repair_never_writes_back_a_rejected_rows_response(db_session):
    """A rejected/dead row's stored response must never be applied as if ERP
    had accepted it — repair is restricted to accepted/sent rows only."""
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    submission, row = _outbox_rows(db_session, request)
    submission.status = FieldErpSyncStatus.dead.value
    submission.erp_response = None
    row.status = FieldErpSyncStatus.dead.value
    row.erp_response = {
        "status": "rejected",
        "rejection_reason": "over budget",
        # A rejected response could still technically carry an id; repair must
        # not treat that as an acceptance.
        "claim_id": "ERP-SHOULD-NOT-LINK",
    }
    request.expense_claim_reference = None
    db_session.commit()

    result = expense_sync.repair_expense_claim_writebacks(db_session)

    db_session.refresh(request)
    assert result["repaired"] == 0
    assert request.expense_claim_reference is None


def test_refresh_skips_unsynced_and_terminal_requests(db_session):
    # Not synced yet (no erp id) → excluded.
    unsynced = _make_submitted_request(db_session, crm_work_order_id="wo-a")
    # Synced but already paid (terminal) → excluded from the in-flight poll.
    paid = _make_submitted_request(db_session, crm_work_order_id="wo-b")
    paid.expense_system = "dotmac_erp"
    paid.expense_claim_reference = "ERP-PAID"
    paid.status = "paid"
    db_session.commit()

    client = _FakeERPClient(status_outcomes=[])
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    assert result["processed"] == 0
    assert client.status_calls == []
    assert unsynced.expense_claim_reference is None


# ---------------------------------------------------------------------------
# Operator-visible diagnostic surface — delivered-but-unlinked rows per flow
# ---------------------------------------------------------------------------


def test_diagnostics_reports_raw_count_and_oldest_age_with_no_threshold_flag(
    db_session,
):
    """The surface is informational only: count + oldest age, no "stale"/alert
    framing, regardless of how old the row is (Michael's design decision:
    "Do not treat it as an alert/SLA. Show count and oldest age as
    informational data until each flow has an explicitly owned threshold.").
    """
    from datetime import timedelta

    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    assert row.status == FieldErpSyncStatus.accepted.value
    # Backdate well past any plausible threshold — must still be reported
    # plainly, not filtered or flagged.
    row.created_at = datetime.now(UTC) - timedelta(hours=48)
    db_session.commit()

    report = outbox.delivered_unlinked_diagnostics(db_session)

    flow_report = report[FieldErpSyncFlow.expense_claim.value]
    assert flow_report["count"] == 2
    assert flow_report["oldest_age_hours"] >= 24
    assert "stale" not in flow_report
    assert "stale_after_hours" not in flow_report


def test_diagnostics_reports_a_fresh_unlinked_row_without_filtering_it(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()
    assert row.status == FieldErpSyncStatus.accepted.value

    report = outbox.delivered_unlinked_diagnostics(db_session)

    flow_report = report[FieldErpSyncFlow.expense_claim.value]
    assert flow_report["count"] == 2
    assert flow_report["oldest_age_hours"] < 24
    assert "stale" not in flow_report


def test_diagnostics_excludes_a_row_whose_writeback_actually_landed(db_session):
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(post_outcomes=[{"claim_id": "ERP-LINKED-OK"}]),
    )
    db_session.refresh(request)
    assert request.expense_claim_reference == "ERP-LINKED-OK"

    report = outbox.delivered_unlinked_diagnostics(db_session)

    flow_report = report[FieldErpSyncFlow.expense_claim.value]
    assert flow_report["count"] == 0


# ---------------------------------------------------------------------------
# ERP expense-claim payment outcome observation — extracted projection helper
# ---------------------------------------------------------------------------


def test_apply_erp_expense_payment_outcome_marks_approved_claim_paid():
    request = FieldExpenseRequest(status="approved", paid_at=None)
    observed_at = datetime(2026, 1, 5, tzinfo=UTC)

    expense_sync._apply_erp_expense_payment_outcome(
        request, claim_status="paid", observed_at=observed_at
    )

    assert request.status == "paid"
    assert request.paid_at == observed_at


def test_apply_erp_expense_payment_outcome_is_idempotent_on_replay():
    already_paid_at = datetime(2025, 12, 1, tzinfo=UTC)
    request = FieldExpenseRequest(status="paid", paid_at=already_paid_at)
    later_observed_at = datetime(2026, 1, 5, tzinfo=UTC)

    expense_sync._apply_erp_expense_payment_outcome(
        request, claim_status="paid", observed_at=later_observed_at
    )

    assert request.status == "paid"
    assert request.paid_at == already_paid_at


def test_apply_erp_expense_payment_outcome_ignores_non_paid_claim_status():
    request = FieldExpenseRequest(status="approved", paid_at=None)
    observed_at = datetime(2026, 1, 5, tzinfo=UTC)

    expense_sync._apply_erp_expense_payment_outcome(
        request, claim_status="approved", observed_at=observed_at
    )

    assert request.status == "approved"
    assert request.paid_at is None


@pytest.mark.parametrize(
    "starting_status", ["submitted", "rejected", "canceled", "paid"]
)
def test_apply_erp_expense_payment_outcome_only_fires_from_approved(starting_status):
    request = FieldExpenseRequest(status=starting_status, paid_at=None)
    observed_at = datetime(2026, 1, 5, tzinfo=UTC)

    expense_sync._apply_erp_expense_payment_outcome(
        request, claim_status="paid", observed_at=observed_at
    )

    assert request.status == starting_status
    assert request.paid_at is None


def test_linked_status_poll_makes_no_erp_call_for_a_crm_owned_expense_flow(
    db_session,
):
    """The linked-claim poll loop inside ``refresh_expense_claim_statuses``
    (the ``for request in pending`` loop, distinct from
    ``_poll_unlinked_expense_claims`` above) makes a real ERP call
    (``get_expense_claim_status``) for every reference-bearing, in-flight
    request. It must be skipped once ownership moves back to CRM, mirroring
    the unlinked-poll and repair-sweep ownership guards.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(
            post_outcomes=[
                {"claim_id": "ERP-3", "claim_number": "EXP-3", "status": "approved"}
            ]
        ),
    )
    db_session.refresh(request)
    assert request.expense_claim_reference == "ERP-3"
    assert request.status == "approved"

    # Ownership moves back to CRM before the poll runs.
    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )
    ownership_row.owner = SyncFlowOwner.crm.value
    db_session.commit()

    client = _FakeERPClient(status_outcomes=[{"claim_id": "ERP-3", "status": "paid"}])
    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    assert client.status_calls == []
    assert result["skipped_not_owned"] >= 1
    db_session.refresh(request)
    assert request.status == "approved"
    assert request.expense_claim_status == "approved"


def test_write_back_dispatch_skips_expense_projection_when_flow_not_owned_by_sub(
    db_session,
):
    """``_dispatch_flow_writeback`` also runs later from a poll
    (``record_polled_outcome``), not just right after the original send.
    Ownership can move back to CRM in between — the expense-claim projection
    must be skipped rather than writing a stale/unauthorized state onto the
    request.
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)
    request = _make_submitted_request(db_session)
    _approve(db_session, request)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(request)
    row = _outbox_rows(db_session, request)[0]
    # `_FakeERPClient.approve_expense_claim` always merges a real (randomly
    # generated) `claim_id` into its outcome regardless of `post_outcomes`
    # content (it's listed before `**supplied` in the merge, so an empty
    # `post_outcomes=[{}]` cannot suppress it) — so the initial delivery
    # genuinely links the request right away, while ownership is still sub.
    # Make that explicit rather than assuming otherwise.
    assert request.expense_claim_reference is not None

    # Simulate a dropped write-back (e.g. a later rollback), same pattern as
    # the repair tests above — this is the realistic precondition for a
    # SECOND, later (re-)dispatch attempt from a poll.
    request.expense_claim_reference = None
    request.expense_claim_status = None
    db_session.commit()

    # Ownership moves back to CRM before this row's write-back is
    # (re)dispatched from a later poll.
    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )
    ownership_row.owner = SyncFlowOwner.crm.value
    row.status = FieldErpSyncStatus.accepted.value
    row.erp_response = {"claim_id": "SHOULD-NOT-LINK", "status": "approved"}
    db_session.commit()

    outbox._dispatch_flow_writeback(db_session, row)

    db_session.refresh(request)
    assert request.expense_claim_reference is None
    assert request.expense_claim_status is None


def test_linked_status_poll_stops_mid_batch_when_ownership_flips(db_session):
    """The per-flow ownership gate inside the linked-claim poll loop is
    checked BOTH before the ``get_expense_claim_status`` network call (so an
    already-flipped ownership skips the call entirely) AND again right after
    it returns and before ``apply_claim_response`` is called (so a flip that
    happens WHILE that row's own call was in flight still blocks the write).
    Here the flip happens as a side effect of the FIRST row's own call, so
    that row's own post-call recheck — not just the second row's pre-call
    recheck — is what must catch it: neither row's response ends up applied.

    SCOPE NOTE: the flip and both ownership checks here run through the SAME
    ``db_session`` as the code under test, so this proves same-session
    visibility only — a change committed on this session becomes visible to
    a later query on this session. It does NOT prove cross-session/genuinely
    concurrent visibility (e.g. a different Celery worker's commit): that
    depends on ``flow_owned_by_sub`` never returning an already
    identity-mapped, un-refreshed object across sessions, which is a
    separate, not-yet-verified property (see the debt register).
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)

    first = _make_submitted_request(db_session, crm_work_order_id="wo-first")
    _approve(db_session, first)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(
            post_outcomes=[
                {"claim_id": "ERP-A", "claim_number": "EXP-A", "status": "approved"}
            ]
        ),
    )
    db_session.refresh(first)
    assert first.expense_claim_reference == "ERP-A"

    second = _make_submitted_request(db_session, crm_work_order_id="wo-second")
    _approve(db_session, second)
    outbox.deliver_pending(
        db_session,
        client=_FakeERPClient(
            post_outcomes=[
                {"claim_id": "ERP-B", "claim_number": "EXP-B", "status": "approved"}
            ]
        ),
    )
    db_session.refresh(second)
    assert second.expense_claim_reference == "ERP-B"

    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )

    class _FlipOwnershipAfterFirstCallClient(_FakeERPClient):
        """Simulates ownership moving to CRM mid-batch, right after the first
        real ERP network call the loop makes."""

        def get_expense_claim_status(self, source_claim_id):
            result = super().get_expense_claim_status(source_claim_id)
            if len(self.status_calls) == 1:
                ownership_row.owner = SyncFlowOwner.crm.value
                db_session.commit()
            return result

    client = _FlipOwnershipAfterFirstCallClient(
        status_outcomes=[
            {"claim_id": "ERP-A", "status": "paid"},
            {"claim_id": "ERP-B", "status": "paid"},
        ]
    )

    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    # Only the first row made an ERP call at all — its own call is where the
    # flip happens, so the second row's PRE-call check already blocks it
    # before any second call is made.
    assert len(client.status_calls) == 1
    # Both rows end up skipped: the first via the POST-call recheck (the flip
    # happened while its own call was in flight), the second via the
    # PRE-call check (ownership was already flipped by the time it's reached).
    assert result["skipped_not_owned"] == 2
    assert result["updated"] == 0
    db_session.refresh(first)
    db_session.refresh(second)
    # The first row's response ("paid") must NOT be applied — the ownership
    # flip happened while its own network call was in flight, and the
    # post-call recheck must catch that before `apply_claim_response` runs.
    assert first.status == "approved"
    # The second row, reached only after the flip, must NOT be written either.
    assert second.status == "approved"


def test_unlinked_status_poll_stops_mid_batch_when_ownership_flips(db_session):
    """Same TOCTOU concern as the linked-claim poll loop, but for
    ``_poll_unlinked_expense_claims``: its own pre-call ``flow_owned_by_sub``
    check (re-checked on every iteration, not once before the loop) stops the
    SECOND row before it ever calls ERP. The FIRST row — the one whose own
    call is where the flip happens — is protected differently: this path
    never calls ``apply_claim_response`` directly, it routes through
    ``outbox.record_polled_outcome`` -> ``_dispatch_flow_writeback``, and
    that function's own ownership guard (added when the write-back path was
    first gated) runs AFTER the network call returns — so it catches the
    first row's flip-during-flight case too. Neither row ends up linked.

    SCOPE NOTE: the flip and every ownership check here run through the SAME
    ``db_session`` as the code under test, so this proves same-session
    visibility only — a change committed on this session becomes visible to
    a later query on this session. It does NOT prove cross-session/genuinely
    concurrent visibility (e.g. a different Celery worker's commit): that
    depends on ``flow_owned_by_sub`` never returning an already
    identity-mapped, un-refreshed object across sessions, which is a
    separate, not-yet-verified property (see the debt register).
    """
    _seed_ownership(db_session, sub_flows={FieldErpSyncFlow.expense_claim.value})
    enable_erp_capability(db_session, ERP_OUTBOX_CAPABILITY)

    first = _make_submitted_request(db_session, crm_work_order_id="wo-unlinked-first")
    _approve(db_session, first)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(first)
    first_row = _outbox_rows(db_session, first)[0]
    first_row.status = FieldErpSyncStatus.sent.value
    first_row.erp_response = {}
    first.expense_claim_reference = None
    first.expense_claim_status = None
    db_session.commit()

    second = _make_submitted_request(db_session, crm_work_order_id="wo-unlinked-second")
    _approve(db_session, second)
    outbox.deliver_pending(db_session, client=_FakeERPClient(post_outcomes=[{}]))
    db_session.refresh(second)
    second_row = _outbox_rows(db_session, second)[0]
    second_row.status = FieldErpSyncStatus.sent.value
    second_row.erp_response = {}
    second.expense_claim_reference = None
    second.expense_claim_status = None
    db_session.commit()

    ownership_row = (
        db_session.query(SyncFlowOwnership)
        .filter(SyncFlowOwnership.flow == FieldErpSyncFlow.expense_claim.value)
        .one()
    )

    class _FlipOwnershipAfterFirstUnlinkedCallClient(_FakeERPClient):
        """Simulates ownership moving to CRM mid-batch, right after the first
        real ERP network call the unlinked-poll loop makes."""

        def get_expense_claim_status(self, source_claim_id):
            result = super().get_expense_claim_status(source_claim_id)
            if len(self.status_calls) == 1:
                ownership_row.owner = SyncFlowOwner.crm.value
                db_session.commit()
            return result

    client = _FlipOwnershipAfterFirstUnlinkedCallClient(
        status_outcomes=[
            {
                "claim_id": "ERP-UNLINKED-A",
                "claim_number": "EXP-UA",
                "status": "approved",
            },
            {
                "claim_id": "ERP-UNLINKED-B",
                "claim_number": "EXP-UB",
                "status": "approved",
            },
        ]
    )

    result = expense_sync.refresh_expense_claim_statuses(db_session, client=client)

    # Only the first row made an ERP call at all — its own call is where the
    # flip happens, so the second row's pre-call check already blocks it
    # before any second call is made.
    assert len(client.status_calls) == 1
    # The second row is counted here (its pre-call `flow_owned_by_sub` check
    # in `_poll_unlinked_expense_claims` fails and increments this counter
    # directly). The first row's block happens one layer down, inside
    # `outbox._dispatch_flow_writeback`'s own ownership guard (reached via
    # `record_polled_outcome`) — that guard only logs and skips the
    # projection, it does not feed back into this counter, so it does not
    # add to `skipped_not_owned` here.
    assert result["skipped_not_owned"] >= 1
    db_session.refresh(first)
    db_session.refresh(second)
    # The first row's own network call is where the flip happens, so by the
    # time `record_polled_outcome` -> `_dispatch_flow_writeback` runs for it,
    # ownership has ALREADY flipped (flow_owned_by_sub re-queries fresh, not
    # cached) — that guard blocks the projection for the first row too, not
    # just the second. Neither row ends up linked.
    assert first.expense_claim_reference is None
    # The second row, reached only after the flip, must NOT be linked either.
    assert second.expense_claim_reference is None
