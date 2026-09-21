"""Allowlisted transport evidence. Provider text is never safe by default."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DELIVERY_DIAGNOSTIC_KEY = "delivery_diagnostic"


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
    "expense_requester_unmatched": "ERP could not match the expense requester to an employee.",
    "expense_approver_ineligible": "The selected ERP expense approver is no longer eligible.",
    "expense_destination_invalid": "The verified ERP payment destination is invalid.",
    "expense_destination_mismatch": "The verified ERP payment destination does not belong to this expense.",
    "expense_destination_expired": "The verified ERP payment destination expired; verify it again.",
    "expense_destination_incomplete": "The verified ERP payment destination is incomplete.",
    "expense_category_unknown": "An expense category is not available in ERP.",
    "expense_category_limit_exceeded": "The expense amount exceeds the ERP category limit.",
    "expense_line_amount_invalid": "An expense line amount must be greater than zero.",
    "expense_date_invalid": "ERP rejected an invalid expense date.",
    "expense_draft_validation_failed": "ERP rejected the expense draft validation; inspect ERP logs using the request ID.",
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


def diagnostic_evidence(diagnostic: OperationDiagnostic) -> dict[str, object]:
    """Serialize allowlisted delivery evidence without provider response text."""

    return diagnostic.model_dump(mode="json", exclude_none=True)


def parse_diagnostic_evidence(value: object) -> OperationDiagnostic | None:
    """Parse persisted diagnostic evidence, failing closed when it is malformed."""

    try:
        return OperationDiagnostic.model_validate(value)
    except (TypeError, ValueError):
        return None


def safe_diagnostic_summary(diagnostic: OperationDiagnostic) -> str:
    """Render only allowlisted text plus typed correlation evidence."""

    message = _MESSAGES.get(diagnostic.code, _MESSAGES["request_rejected"])
    evidence = [f"code={diagnostic.code}"]
    if diagnostic.http_status is not None:
        evidence.append(f"status={diagnostic.http_status}")
    if diagnostic.request_id is not None:
        evidence.append(f"request_id={diagnostic.request_id}")
    return f"{message} ({'; '.join(evidence)})"
