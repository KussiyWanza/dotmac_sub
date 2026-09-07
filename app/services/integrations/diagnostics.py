"""Allowlisted transport evidence. Provider text is never safe by default."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OperationDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    http_status: int | None = Field(default=None, ge=100, le=599)
    code: str = Field(max_length=120)
    message: str = Field(max_length=240)
    operation: str = Field(default="erp_request", max_length=120)
    operation_id: UUID | None = None
    correlation_id: UUID | None = None
    request_id: UUID | None = None
    retry_after_seconds: int | None = Field(default=None, ge=1, le=86400)


_MESSAGES = {
    "authentication_failed": "ERP authentication failed; review the service credential.",
    "permission_denied": "ERP permission denied; review the required operation scope.",
    "not_found": "ERP resource or endpoint was not found.",
    "validation_error": "ERP rejected request validation; inspect redacted ERP validation evidence.",
    "conflict": "ERP reported a conflict; inspect source identity mappings.",
    "rate_limited": "ERP rate limited the request.",
    "transport_unavailable": "ERP transport is temporarily unavailable.",
    "request_rejected": "ERP rejected the request; inspect ERP logs using the request ID.",
    "invalid_response": "ERP returned an unexpected status or response contract.",
    "configuration_unavailable": "ERP capability configuration is unavailable or invalid.",
    "item_rejected": "ERP reported item errors; watermarks were preserved.",
}


def safe_diagnostic(
    *, status: int | None = None, body: object = None, code: str = "request_rejected"
) -> OperationDiagnostic:
    """Never copy messages, validation input, arbitrary codes, HTML or headers.

    Recognized machine codes can refine the status-derived explanation. Unknown
    codes and messages require receiving-side evidence, not heuristic redaction.
    """
    status_codes = {
        401: "authentication_failed",
        403: "permission_denied",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "rate_limited",
        500: "transport_unavailable",
        502: "transport_unavailable",
        503: "transport_unavailable",
        504: "transport_unavailable",
    }
    code = status_codes.get(status, code) if status is not None else code
    if isinstance(body, dict):
        detail = body.get("detail")
        error = body.get("error")
        candidates = (
            detail.get("code") if isinstance(detail, dict) else None,
            error.get("code") if isinstance(error, dict) else None,
            body.get("code"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate in _MESSAGES:
                code = candidate
                break
    if code not in _MESSAGES:
        code = "request_rejected"
    return OperationDiagnostic(http_status=status, code=code, message=_MESSAGES[code])
