"""Typed, database-free DotMac ERP connector runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.services.dotmac_erp.client import (
    DotMacERPClient,
    DotMacERPError,
    DotMacERPRateLimitError,
    DotMacERPTransientError,
)
from app.services.dotmac_erp.operational_contracts import ErpOperationalSyncCommand
from app.services.integrations.backoffice_contracts import (
    ERP_INVENTORY_CAPABILITY,
    ERP_OPERATIONAL_SYNC_CAPABILITY,
    ERP_OUTBOX_CAPABILITY,
    ERP_REGULATORY_CAPABILITY,
    ERP_STATUS_CAPABILITY,
    WORKFORCE_ATTENDANCE_PUNCH_CAPABILITY,
    WORKFORCE_ATTENDANCE_READ_CAPABILITY,
)
from app.services.integrations.manifest import ConnectorManifest
from app.services.integrations.runtime import (
    HealthResult,
    OperationEnvelope,
    OperationResult,
    OperationStatus,
    ValidationResult,
)

_FLOW_ENDPOINTS = {
    "expense_claim": "/api/v1/sync/sub/expense-claims",
    "material_request": "/api/v1/sync/sub/material-requests",
    "purchase_order": "/api/v1/sync/sub/purchase-orders",
    "purchase_invoice": "/api/v1/sync/sub/purchase-invoices",
}


class DotmacErpRunner:
    """Execute only the explicitly declared ERP operations."""

    def __init__(self, client_override: DotMacERPClient | None = None) -> None:
        self._client_override = client_override

    def _client(
        self,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
        *,
        interactive: bool = False,
    ) -> DotMacERPClient:
        if self._client_override is not None:
            return self._client_override
        return DotMacERPClient(
            base_url=str(config.get("base_url") or ""),
            token=secret_material.get("service_credentials") or "",
            timeout=int(
                config.get("interactive_timeout_seconds") or 5
                if interactive
                else config.get("timeout_seconds") or 30
            ),
            retries=int(
                config.get("interactive_max_retries") or 1
                if interactive
                else config.get("max_retries") or 3
            ),
        )

    def validate(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        if not str(config.get("base_url") or "").strip():
            return ValidationResult(valid=False, error_codes=("base_url_missing",))
        if self._client_override is None and not secret_material.get(
            "service_credentials"
        ):
            return ValidationResult(
                valid=False,
                error_codes=("service_credentials_missing",),
            )
        try:
            self._client(config, secret_material).list_inventory_warehouses()
        except DotMacERPError:
            return ValidationResult(valid=False, error_codes=("erp_unreachable",))
        except Exception:
            return ValidationResult(valid=False, error_codes=("validation_failed",))
        return ValidationResult(valid=True)

    def execute(
        self,
        envelope: OperationEnvelope,
        *,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> OperationResult:
        action = str(envelope.payload.get("action") or "")
        params = envelope.payload.get("params") or {}
        if not isinstance(params, dict):
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code="params_invalid",
            )
        try:
            is_attendance = envelope.capability_id in {
                WORKFORCE_ATTENDANCE_READ_CAPABILITY,
                WORKFORCE_ATTENDANCE_PUNCH_CAPABILITY,
            }
            output = self._execute_action(
                self._client(config, secret_material, interactive=is_attendance),
                capability_id=envelope.capability_id,
                action=action,
                params=params,
                idempotency_key=envelope.idempotency_key,
            )
        except DotMacERPRateLimitError as exc:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.retryable,
                error_code="erp_rate_limited",
                retry_after_seconds=exc.retry_after,
            )
        except DotMacERPTransientError:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.retryable,
                error_code="erp_transport_retryable",
            )
        except DotMacERPError as exc:
            detail = (exc.response or {}).get("detail")
            error_code = detail.get("code") if isinstance(detail, dict) else None
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code=str(error_code or "erp_operation_rejected"),
            )
        except (KeyError, TypeError, ValueError):
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code="operation_invalid",
            )
        except Exception:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.failed,
                error_code="connector_failed",
            )
        return OperationResult(
            operation_id=envelope.operation_id,
            status=OperationStatus.succeeded,
            output=output,
        )

    def _execute_action(
        self,
        client: DotMacERPClient,
        *,
        capability_id: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if capability_id == ERP_OUTBOX_CAPABILITY:
            if action == "deliver_outbox":
                endpoint = _FLOW_ENDPOINTS[str(params["flow"])]
                return client.post(
                    endpoint,
                    dict(params["payload"]),
                    idempotency_key=str(
                        params.get("idempotency_key") or idempotency_key
                    ),
                    expected_status_codes={200, 201},
                )
            if action == "upload_purchase_invoice_attachment":
                return client.upload_purchase_invoice_attachment(
                    str(params["purchase_invoice_id"]),
                    dict(params["payload"]),
                    idempotency_key=str(
                        params.get("idempotency_key") or idempotency_key
                    ),
                )
        elif capability_id == ERP_STATUS_CAPABILITY:
            if action == "expense_claim_status":
                return {"item": client.get_expense_claim_status(str(params["omni_id"]))}
            if action == "material_request_status":
                return {
                    "item": client.get_material_request_status(str(params["omni_id"]))
                }
            if action == "purchase_invoice_status":
                return {
                    "item": client.get_purchase_invoice_status(
                        str(params["source_invoice_id"])
                    )
                }
        elif capability_id == ERP_INVENTORY_CAPABILITY:
            if action == "list_inventory":
                return client.list_inventory(**params)
            if action == "get_inventory_item":
                return {"item": client.get_inventory_item(str(params["item_id"]))}
            if action == "list_warehouses":
                return {"items": client.list_inventory_warehouses()}
            if action == "list_categories":
                return {"items": client.list_inventory_categories()}
            if action == "list_expense_categories":
                return {"items": client.get_expense_categories()}
            if action == "list_available_serials":
                return client.list_available_serials(**params)
        elif capability_id == ERP_OPERATIONAL_SYNC_CAPABILITY:
            if action == "sync_operational_domains":
                command = ErpOperationalSyncCommand.model_validate(params["payload"])
                return client.sync_operational_domains(command).model_dump(mode="json")
        elif capability_id == ERP_REGULATORY_CAPABILITY:
            if action == "get_ncc_financials":
                return client.get_ncc_financials(**params)
            if action == "get_ncc_staff_headcount":
                return client.get_ncc_staff_headcount()
        elif capability_id == WORKFORCE_ATTENDANCE_READ_CAPABILITY:
            if action == "attendance_today":
                return client.get_attendance_today(
                    str(params["subject"]), str(params["request_id"])
                )
        elif capability_id == WORKFORCE_ATTENDANCE_PUNCH_CAPABILITY:
            if action in {"attendance_check_in", "attendance_check_out"}:
                punch_action = (
                    "check-in" if action.endswith("check_in") else "check-out"
                )
                return client.punch_attendance(
                    punch_action,
                    str(params["subject"]),
                    dict(params["location"]),
                    idempotency_key=str(
                        params.get("idempotency_key") or idempotency_key
                    ),
                    request_id=str(params["request_id"]),
                )
        raise ValueError("unsupported ERP capability operation")

    def health(
        self,
        *,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> HealthResult:
        result = self.validate(
            manifest=manifest,
            config=config,
            secret_material=secret_material,
        )
        return HealthResult(
            status="healthy" if result.valid else "unavailable",
            details={"error_codes": list(result.error_codes)},
        )

    def cancel(self, operation_id: UUID) -> bool:
        return False
