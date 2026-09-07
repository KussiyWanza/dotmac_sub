"""Typed, database-free DotMac ERP connector runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from app.services.dotmac_erp.client import (
    DotMacERPAuthError,
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
    ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
    ERP_STATUS_CAPABILITY,
    WORKFORCE_ATTENDANCE_PUNCH_CAPABILITY,
    WORKFORCE_ATTENDANCE_READ_CAPABILITY,
)
from app.services.integrations.diagnostics import safe_diagnostic
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
            retries=min(
                3,
                max(
                    0,
                    int(
                        config.get("interactive_max_retries", 1)
                        if interactive
                        else config.get("max_retries", 3)
                    ),
                ),
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

    def validate_capability(
        self,
        *,
        capability_id: str,
        manifest: ConnectorManifest,
        config: Mapping[str, Any],
        secret_material: Mapping[str, str],
    ) -> ValidationResult:
        if capability_id not in {
            ERP_OPERATIONAL_SYNC_CAPABILITY,
            ERP_STAFF_ACCESS_RECONCILE_CAPABILITY,
        }:
            return self.validate(
                manifest=manifest,
                config=config,
                secret_material=secret_material,
            )
        if not str(config.get("base_url") or "").strip():
            return ValidationResult(valid=False, error_codes=("base_url_missing",))
        if self._client_override is None and not secret_material.get(
            "service_credentials"
        ):
            return ValidationResult(
                valid=False,
                error_codes=("service_credentials_missing",),
            )
        client = self._client(config, secret_material)
        try:
            if capability_id == ERP_OPERATIONAL_SYNC_CAPABILITY:
                client.sync_operational_domains(ErpOperationalSyncCommand())
            else:
                client.get_staff_access_projection(
                    entity="account_status",
                    limit=1,
                )
        except DotMacERPAuthError:
            if capability_id == ERP_OPERATIONAL_SYNC_CAPABILITY:
                return ValidationResult(
                    valid=False,
                    error_codes=("erp_operational_scope_missing",),
                    details={"required_scope": "sub:domain:write"},
                )
            return ValidationResult(
                valid=False,
                error_codes=("erp_staff_access_scope_missing",),
                details={"required_scope": "sub:staff_access:read"},
            )
        except DotMacERPError:
            error_code = (
                "erp_operational_sync_unavailable"
                if capability_id == ERP_OPERATIONAL_SYNC_CAPABILITY
                else "erp_staff_access_projection_unavailable"
            )
            return ValidationResult(
                valid=False,
                error_codes=(error_code,),
            )
        except Exception:
            return ValidationResult(valid=False, error_codes=("validation_failed",))
        finally:
            if self._client_override is None:
                client.close()
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
        correlation_id = uuid4()
        safe_operation = (
            action
            if action
            in {
                "sync_operational_domains",
                "deliver_outbox",
                "upload_purchase_invoice_attachment",
                "expense_claim_status",
                "material_request_status",
                "purchase_invoice_status",
                "list_inventory",
                "get_inventory_item",
                "list_warehouses",
                "list_categories",
                "list_expense_categories",
                "list_available_serials",
                "get_ncc_financials",
                "get_ncc_staff_headcount",
                "read_staff_access_projection",
                "attendance_today",
                "attendance_check_in",
                "attendance_check_out",
            }
            else "unsupported_operation"
        )
        client = None
        try:
            is_attendance = envelope.capability_id in {
                WORKFORCE_ATTENDANCE_READ_CAPABILITY,
                WORKFORCE_ATTENDANCE_PUNCH_CAPABILITY,
            }
            client = self._client(config, secret_material, interactive=is_attendance)
            if envelope.capability_id == ERP_OPERATIONAL_SYNC_CAPABILITY:
                # This feed holds its admission lock until the remote result.
                # Keep the whole transport budget bounded under the normal
                # transaction idle timeout; 429 is deferred by the owner.
                client.retries = min(client.retries, 2)
                client.timeout = min(client.timeout, 20)
            output = self._execute_action(
                client,
                capability_id=envelope.capability_id,
                action=action,
                params=params,
                idempotency_key=envelope.idempotency_key,
            )
        except DotMacERPError as exc:
            retryable = isinstance(
                exc, (DotMacERPRateLimitError, DotMacERPTransientError)
            )
            diagnostic = exc.diagnostic.model_copy(
                update={
                    "operation": safe_operation,
                    "operation_id": envelope.operation_id,
                    "correlation_id": correlation_id,
                    "retry_after_seconds": (
                        min(86400, max(1, exc.retry_after or 60))
                        if isinstance(exc, DotMacERPRateLimitError)
                        else None
                    ),
                }
            )
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.retryable
                if retryable
                else OperationStatus.rejected,
                error_code=diagnostic.code,
                diagnostic=diagnostic,
                retry_after_seconds=(
                    min(86400, max(1, exc.retry_after or 60))
                    if isinstance(exc, DotMacERPRateLimitError)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.rejected,
                error_code="operation_invalid",
                diagnostic=safe_diagnostic(code="invalid_response").model_copy(
                    update={
                        "operation": safe_operation,
                        "operation_id": envelope.operation_id,
                        "correlation_id": correlation_id,
                    }
                ),
            )
        except Exception:
            return OperationResult(
                operation_id=envelope.operation_id,
                status=OperationStatus.failed,
                error_code="connector_failed",
                diagnostic=safe_diagnostic().model_copy(
                    update={
                        "operation": safe_operation,
                        "operation_id": envelope.operation_id,
                        "correlation_id": correlation_id,
                    }
                ),
            )
        finally:
            if client is not None and self._client_override is None:
                client.close()
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
                return {
                    "item": client.get_expense_claim_status(
                        str(params["source_claim_id"])
                    )
                }
            if action == "material_request_status":
                return {
                    "item": client.get_material_request_status(
                        str(params["source_request_id"])
                    )
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
        elif capability_id == ERP_STAFF_ACCESS_RECONCILE_CAPABILITY:
            if action == "read_staff_access_projection":
                entity = str(params["entity"])
                if entity not in {"leave_restriction", "account_status"}:
                    raise ValueError("unsupported staff access projection entity")
                return client.get_staff_access_projection(
                    entity=cast(Literal["leave_restriction", "account_status"], entity),
                    limit=int(params.get("limit") or 500),
                )
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
