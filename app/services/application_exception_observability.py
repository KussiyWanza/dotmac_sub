"""Typed, redacted observations for application and payment failures.

This module is intentionally an observer: it neither decides payment state nor
retries provider work.  The payment owner remains the sole settlement writer.
"""

from __future__ import annotations

from enum import StrEnum

from app.metrics import APPLICATION_EXCEPTIONS, PAYMENT_VERIFICATION_OUTCOMES


class ExceptionSurface(StrEnum):
    HTTP = "http"


class PaymentVerificationChannel(StrEnum):
    CUSTOMER_PORTAL = "customer_portal"
    API = "api"


class PaymentVerificationOutcome(StrEnum):
    SETTLED = "settled"
    PENDING_PROVIDER_CONFIRMATION = "pending_provider_confirmation"
    BUSINESS_REFUSAL = "business_refusal"
    UNEXPECTED_FAILURE = "unexpected_failure"


def exception_fingerprint(exc: BaseException) -> str:
    """Return a bounded, non-sensitive exception fingerprint."""

    return type(exc).__name__[:80]


def record_unhandled_http_exception(exc: BaseException) -> str:
    """Count one unexpected HTTP failure and return its safe fingerprint."""

    fingerprint = exception_fingerprint(exc)
    APPLICATION_EXCEPTIONS.labels(ExceptionSurface.HTTP.value, fingerprint).inc()
    return fingerprint


def record_payment_verification_outcome(
    *,
    channel: PaymentVerificationChannel,
    outcome: PaymentVerificationOutcome,
) -> None:
    """Record a payment-verification observation without payment identifiers."""

    PAYMENT_VERIFICATION_OUTCOMES.labels(channel.value, outcome.value).inc()
