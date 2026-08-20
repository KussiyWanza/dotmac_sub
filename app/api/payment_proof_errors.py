"""Canonical HTTP mapping for transport-neutral payment-proof errors."""

from fastapi import HTTPException

from app.services.domain_errors import DomainError

_NOT_FOUND_SUFFIXES = (
    ".not_found",
    ".billing_account_not_found",
    ".file_not_found",
)
_CONFLICT_SUFFIXES = (
    ".already_reviewed",
    ".duplicate_transfer_reference",
    ".deposit_settlement_rejected",
    ".prepaid_funding_baseline_missing",
    ".active_caller_transaction",
    ".nested_owner_command",
    ".already_corrected",
    ".correction_original_was_corrected",
    ".correction_duplicate_not_verified",
    ".correction_original_not_verified",
    ".correction_reversal_unavailable",
    ".correction_idempotency_conflict",
    ".correction_stale_preview",
)
_INTERNAL_SUFFIXES = (
    ".command_contract_violation",
    ".nested_transaction_completion",
)


def payment_proof_http_status(exc: DomainError) -> int:
    """Map one stable domain code to its public HTTP status."""

    if exc.code.endswith(_NOT_FOUND_SUFFIXES):
        return 404
    if exc.code.endswith(_CONFLICT_SUFFIXES):
        return 409
    if exc.code.endswith(_INTERNAL_SUFFIXES):
        return 500
    return 400


def payment_proof_http_error(exc: DomainError) -> HTTPException:
    """Build the structured API error without leaking transport into the owner."""

    return HTTPException(
        status_code=payment_proof_http_status(exc),
        detail={"code": exc.code, "message": exc.message, "details": exc.details},
    )
