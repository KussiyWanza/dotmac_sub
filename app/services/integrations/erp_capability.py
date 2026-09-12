"""Sub-side facade for version-pinned back-office capabilities.

The historical module and class names remain for compatibility. Provider
selection belongs to the enabled/default capability binding, so a replacement
connector can implement the same contracts without changing Sub domain callers.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.schemas.erp_staff_access_webhook import ErpStaffAccessProjectionPage
from app.services.backoffice import ExpenseCategoryView
from app.services.dotmac_erp.client import DotMacERPError, DotMacERPTransientError
from app.services.dotmac_erp.expense_form_contracts import (
    ExpenseApproverOption,
    ExpenseBankOption,
    ExpenseDestinationMode,
    ExpenseProfileDestination,
    InspectExpenseDestination,
    VerifiedExpenseDestination,
    VerifyExpenseDestination,
)
from app.services.dotmac_erp.operational_contracts import (
    ErpOperationalSyncCommand,
    ErpOperationalSyncOutcome,
)
from app.services.integrations import installations
from app.services.integrations.backoffice_contracts import (
    ERP_EXPENSE_FORM_CAPABILITY,
    ERP_INVENTORY_CAPABILITY,
    ERP_OPERATIONAL_SYNC_CAPABILITY,
    ERP_OUTBOX_CAPABILITY,
    ERP_REGULATORY_CAPABILITY,
    ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
    ERP_STATUS_CAPABILITY,
    WORKFORCE_ATTENDANCE_PUNCH_CAPABILITY,
    WORKFORCE_ATTENDANCE_READ_CAPABILITY,
    ErpExpenseApprovalCommand,
    ErpExpenseClaimDraftCommand,
    ErpExpenseClaimDraftOutcome,
    ErpExpenseClaimTransitionOutcome,
    ErpExpenseReceiptUploadCommand,
    ErpExpenseReceiptUploadOutcome,
    ErpExpenseRejectionCommand,
    ErpExpenseSubmissionCommand,
)
from app.services.integrations.runtime import OperationStatus, OperationTrigger
from app.services.integrations.runtime_execution import (
    build_execution_context,
    make_operation_executor,
)

# Provider-neutral error names exposed to domain callers. The facade keeps the
# historical concrete exception identities for existing transport consumers.
ErpCapabilityError = DotMacERPError
ErpCapabilityTransientError = DotMacERPTransientError

__all__ = (
    "ErpCapabilityError",
    "ErpCapabilityTransientError",
    "ExpenseApproverOption",
    "ExpenseBankOption",
    "ExpenseDestinationMode",
    "InspectExpenseDestination",
    "VerifyExpenseDestination",
)


class ErpCapabilityClient:
    """Legacy-shaped facade whose every call passes through a typed binding."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def __enter__(self) -> ErpCapabilityClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        return None

    def _execute(
        self,
        capability_id: str,
        action: str,
        params: dict[str, Any],
        *,
        trigger: OperationTrigger,
        correlation_id: str,
    ) -> dict[str, Any]:
        binding = installations.require_enabled_capability_binding(
            self._db,
            capability_id=capability_id,
        )
        context = build_execution_context(
            self._db,
            capability_binding_id=binding.id,
        )
        result = make_operation_executor(
            context,
            correlation_id=correlation_id[:160],
            trigger=trigger,
            actor="integration.erp",
        )(action, params)
        if result.status == OperationStatus.succeeded:
            return dict(result.output)
        if result.status == OperationStatus.retryable:
            raise DotMacERPTransientError(
                result.error_code or "ERP operation retryable",
                diagnostic=result.diagnostic,
            )
        raise DotMacERPError(
            result.error_code or "ERP operation rejected", diagnostic=result.diagnostic
        )

    def post(
        self,
        path: str,
        payload: dict | list | None,
        idempotency_key: str | None = None,
        expected_status_codes=None,
    ) -> dict:
        flow_by_path = {
            "/api/v1/sync/sub/expense-claims": "expense_claim",
            "/api/v1/sync/sub/material-requests": "material_request",
            "/api/v1/sync/sub/purchase-orders": "purchase_order",
            "/api/v1/sync/sub/purchase-invoices": "purchase_invoice",
        }
        try:
            flow = flow_by_path[path]
        except KeyError as exc:
            raise DotMacERPError("ERP outbox path is not approved") from exc
        key = str(idempotency_key or "missing")
        return self._execute(
            ERP_OUTBOX_CAPABILITY,
            "deliver_outbox",
            {"flow": flow, "payload": payload or {}, "idempotency_key": key},
            trigger=OperationTrigger.scheduled,
            correlation_id=f"erp-outbox:{key}",
        )

    def get_expense_claim_status(self, source_claim_id: str) -> dict | None:
        return self._execute(
            ERP_STATUS_CAPABILITY,
            "expense_claim_status",
            {"source_claim_id": source_claim_id},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"erp-expense-status:{source_claim_id}",
        ).get("item")

    def get_material_request_status(self, source_request_id: str) -> dict | None:
        return self._execute(
            ERP_STATUS_CAPABILITY,
            "material_request_status",
            {"source_request_id": source_request_id},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"erp-material-status:{source_request_id}",
        ).get("item")

    def get_purchase_invoice_status(self, source_invoice_id: str) -> dict | None:
        return self._execute(
            ERP_STATUS_CAPABILITY,
            "purchase_invoice_status",
            {"source_invoice_id": source_invoice_id},
            trigger=OperationTrigger.reconcile,
            correlation_id=f"erp-purchase-invoice-status:{source_invoice_id}",
        ).get("item")

    def upload_purchase_invoice_attachment(
        self,
        purchase_invoice_id: str,
        payload: dict,
        idempotency_key: str | None = None,
    ) -> dict:
        key = str(idempotency_key or purchase_invoice_id)
        return self._execute(
            ERP_OUTBOX_CAPABILITY,
            "upload_purchase_invoice_attachment",
            {
                "purchase_invoice_id": purchase_invoice_id,
                "payload": payload,
                "idempotency_key": key,
            },
            trigger=OperationTrigger.scheduled,
            correlation_id=f"erp-invoice-attachment:{key}",
        )

    def create_expense_claim_draft(
        self,
        command: ErpExpenseClaimDraftCommand,
        *,
        idempotency_key: str,
    ) -> ErpExpenseClaimDraftOutcome:
        response = self._execute(
            ERP_OUTBOX_CAPABILITY,
            "create_expense_claim_draft",
            {
                "payload": command.model_dump(mode="json", exclude_none=True),
                "idempotency_key": idempotency_key,
            },
            trigger=OperationTrigger.scheduled,
            correlation_id=f"erp-expense-draft:{idempotency_key}",
        )
        return ErpExpenseClaimDraftOutcome.model_validate(response)

    def upload_expense_receipt(
        self,
        command: ErpExpenseReceiptUploadCommand,
    ) -> ErpExpenseReceiptUploadOutcome:
        response = self._execute(
            ERP_OUTBOX_CAPABILITY,
            "upload_expense_receipt",
            {
                "source_claim_id": str(command.source_claim_id),
                "item_id": str(command.item_id),
                "payload": command.model_dump(
                    mode="json",
                    exclude={"source_claim_id", "item_id"},
                ),
                "idempotency_key": command.idempotency_key,
            },
            trigger=OperationTrigger.scheduled,
            correlation_id=f"erp-expense-receipt:{command.idempotency_key}",
        )
        return ErpExpenseReceiptUploadOutcome.model_validate(response)

    def submit_expense_claim(
        self,
        command: ErpExpenseSubmissionCommand,
        *,
        idempotency_key: str,
    ) -> ErpExpenseClaimTransitionOutcome:
        response = self._execute(
            ERP_OUTBOX_CAPABILITY,
            "submit_expense_claim",
            {
                "source_claim_id": str(command.source_claim_id),
                "payload": {},
                "idempotency_key": idempotency_key,
            },
            trigger=OperationTrigger.scheduled,
            correlation_id=f"erp-expense-submit:{idempotency_key}",
        )
        return ErpExpenseClaimTransitionOutcome.model_validate(response)

    def approve_expense_claim(
        self,
        command: ErpExpenseApprovalCommand,
        *,
        idempotency_key: str,
    ) -> ErpExpenseClaimTransitionOutcome:
        response = self._execute(
            ERP_OUTBOX_CAPABILITY,
            "approve_expense_claim",
            {
                "source_claim_id": str(command.source_claim_id),
                "payload": command.model_dump(
                    mode="json", exclude={"source_claim_id"}, exclude_none=True
                ),
                "idempotency_key": idempotency_key,
            },
            trigger=OperationTrigger.scheduled,
            correlation_id=f"erp-expense-approve:{idempotency_key}",
        )
        return ErpExpenseClaimTransitionOutcome.model_validate(response)

    def reject_expense_claim(
        self,
        command: ErpExpenseRejectionCommand,
        *,
        idempotency_key: str,
    ) -> ErpExpenseClaimTransitionOutcome:
        response = self._execute(
            ERP_OUTBOX_CAPABILITY,
            "reject_expense_claim",
            {
                "source_claim_id": str(command.source_claim_id),
                "payload": command.model_dump(
                    mode="json", exclude={"source_claim_id"}, exclude_none=True
                ),
                "idempotency_key": idempotency_key,
            },
            trigger=OperationTrigger.scheduled,
            correlation_id=f"erp-expense-reject:{idempotency_key}",
        )
        return ErpExpenseClaimTransitionOutcome.model_validate(response)

    def list_inventory(self, **params) -> dict:
        return self._execute(
            ERP_INVENTORY_CAPABILITY,
            "list_inventory",
            params,
            trigger=OperationTrigger.interactive,
            correlation_id="erp-inventory:list",
        )

    def get_inventory_item(self, item_id: str) -> dict | None:
        return self._execute(
            ERP_INVENTORY_CAPABILITY,
            "get_inventory_item",
            {"item_id": item_id},
            trigger=OperationTrigger.interactive,
            correlation_id=f"erp-inventory:item:{item_id}",
        ).get("item")

    def list_inventory_warehouses(self) -> list[dict]:
        return list(
            self._execute(
                ERP_INVENTORY_CAPABILITY,
                "list_warehouses",
                {},
                trigger=OperationTrigger.interactive,
                correlation_id="erp-inventory:warehouses",
            ).get("items")
            or []
        )

    def list_inventory_categories(self) -> list[dict]:
        return list(
            self._execute(
                ERP_INVENTORY_CAPABILITY,
                "list_categories",
                {},
                trigger=OperationTrigger.interactive,
                correlation_id="erp-inventory:categories",
            ).get("items")
            or []
        )

    def get_expense_categories(self) -> tuple[ExpenseCategoryView, ...]:
        raw_items = self._execute(
            ERP_INVENTORY_CAPABILITY,
            "list_expense_categories",
            {},
            trigger=OperationTrigger.interactive,
            correlation_id="erp-expenses:categories",
        )
        items = raw_items.get("items") or []
        if not isinstance(items, list):
            raise DotMacERPError("ERP expense categories response is invalid")
        return tuple(_expense_category(item) for item in items)

    def get_expense_approvers(
        self, *, requested_by_email: str
    ) -> tuple[ExpenseApproverOption, ...]:
        output = self._execute(
            ERP_EXPENSE_FORM_CAPABILITY,
            "list_expense_approvers",
            {"requested_by_email": requested_by_email},
            trigger=OperationTrigger.interactive,
            correlation_id="erp-expenses:approvers",
        )
        return tuple(
            ExpenseApproverOption.model_validate(item)
            for item in output.get("items") or []
        )

    def get_expense_banks(self) -> tuple[ExpenseBankOption, ...]:
        output = self._execute(
            ERP_EXPENSE_FORM_CAPABILITY,
            "list_expense_banks",
            {},
            trigger=OperationTrigger.interactive,
            correlation_id="erp-expenses:banks",
        )
        return tuple(
            ExpenseBankOption.model_validate(item) for item in output.get("items") or []
        )

    def get_expense_profile_destination(
        self, *, requested_by_email: str
    ) -> ExpenseProfileDestination:
        output = self._execute(
            ERP_EXPENSE_FORM_CAPABILITY,
            "get_expense_profile_destination",
            {"requested_by_email": requested_by_email},
            trigger=OperationTrigger.interactive,
            correlation_id="erp-expenses:profile-destination",
        )
        return ExpenseProfileDestination.model_validate(output)

    def verify_expense_destination(
        self, command: VerifyExpenseDestination
    ) -> VerifiedExpenseDestination:
        output = self._execute(
            ERP_EXPENSE_FORM_CAPABILITY,
            "verify_expense_destination",
            {"payload": command.model_dump(mode="json", exclude_none=True)},
            trigger=OperationTrigger.interactive,
            correlation_id=f"erp-expenses:verify:{command.source_claim_id}",
        )
        return VerifiedExpenseDestination.model_validate(output)

    def inspect_expense_destination(
        self, command: InspectExpenseDestination
    ) -> VerifiedExpenseDestination:
        output = self._execute(
            ERP_EXPENSE_FORM_CAPABILITY,
            "inspect_expense_destination",
            {"payload": command.model_dump(mode="json")},
            trigger=OperationTrigger.interactive,
            correlation_id=f"erp-expenses:inspect:{command.source_claim_id}",
        )
        return VerifiedExpenseDestination.model_validate(output)

    def list_available_serials(self, **params) -> dict:
        return self._execute(
            ERP_INVENTORY_CAPABILITY,
            "list_available_serials",
            params,
            trigger=OperationTrigger.interactive,
            correlation_id="erp-inventory:available-serials",
        )

    def sync_operational_domains(
        self, command: ErpOperationalSyncCommand
    ) -> ErpOperationalSyncOutcome:
        response = self._execute(
            ERP_OPERATIONAL_SYNC_CAPABILITY,
            "sync_operational_domains",
            {"payload": command.model_dump(mode="json")},
            trigger=OperationTrigger.scheduled,
            correlation_id="erp-operational-context:sync",
        )
        return ErpOperationalSyncOutcome.model_validate(response)

    def get_ncc_financials(self, **params) -> dict:
        return self._execute(
            ERP_REGULATORY_CAPABILITY,
            "get_ncc_financials",
            params,
            trigger=OperationTrigger.interactive,
            correlation_id="erp-regulatory:ncc-financials",
        )

    def get_ncc_staff_headcount(self) -> dict:
        return self._execute(
            ERP_REGULATORY_CAPABILITY,
            "get_ncc_staff_headcount",
            {},
            trigger=OperationTrigger.interactive,
            correlation_id="erp-regulatory:ncc-staff",
        )

    def get_staff_access_projection(
        self,
        *,
        entity: Literal["leave_restriction", "account_status"],
        updated_after: datetime | None = None,
        limit: int = 500,
    ) -> ErpStaffAccessProjectionPage:
        params: dict[str, object] = {"entity": entity, "limit": limit}
        if updated_after is not None:
            params["updated_after"] = updated_after.isoformat()
        response = self._execute(
            ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
            "read_staff_access_projection",
            params,
            trigger=OperationTrigger.reconcile,
            correlation_id=f"erp-staff-access:{entity}",
        )
        return ErpStaffAccessProjectionPage.model_validate(response)

    def get_attendance_today(self, subject: str, request_id: str) -> dict:
        return self._execute(
            WORKFORCE_ATTENDANCE_READ_CAPABILITY,
            "attendance_today",
            {"subject": subject, "request_id": request_id},
            trigger=OperationTrigger.interactive,
            correlation_id=request_id,
        )

    def punch_attendance(
        self,
        action: str,
        subject: str,
        location: dict[str, Any],
        *,
        idempotency_key: str,
        request_id: str,
    ) -> dict:
        if action not in {"check_in", "check_out"}:
            raise DotMacERPError("Unsupported attendance action")
        return self._execute(
            WORKFORCE_ATTENDANCE_PUNCH_CAPABILITY,
            f"attendance_{action}",
            {
                "subject": subject,
                "location": location,
                "idempotency_key": idempotency_key,
                "request_id": request_id,
            },
            trigger=OperationTrigger.interactive,
            correlation_id=request_id,
        )


def capability_client(db: Session) -> ErpCapabilityClient:
    return ErpCapabilityClient(db)


def capability_enabled(db: Session, capability_id: str) -> bool:
    try:
        installations.require_enabled_capability_binding(
            db,
            capability_id=capability_id,
        )
    except installations.InstallationError:
        return False
    return True


def _expense_category(item: object) -> ExpenseCategoryView:
    if not isinstance(item, dict):
        raise DotMacERPError("ERP expense category record is invalid")
    category_code = str(
        item.get("category_code") or item.get("code") or item.get("id") or ""
    ).strip()
    category_name = str(
        item.get("category_name") or item.get("name") or item.get("label") or ""
    ).strip()
    if not category_code or not category_name:
        raise DotMacERPError("ERP expense category identity is invalid")
    raw_maximum = item.get("max_amount_per_claim")
    maximum: Decimal | None = None
    if raw_maximum not in (None, ""):
        try:
            maximum = Decimal(str(raw_maximum))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise DotMacERPError(
                "ERP expense category maximum amount is invalid"
            ) from exc
    return ExpenseCategoryView(
        category_code=category_code,
        category_name=category_name,
        requires_receipt=bool(item.get("requires_receipt", False)),
        max_amount_per_claim=maximum,
    )
