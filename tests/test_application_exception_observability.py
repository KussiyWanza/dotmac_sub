from app.services.application_exception_observability import (
    PaymentVerificationChannel,
    PaymentVerificationOutcome,
    exception_fingerprint,
    record_payment_verification_outcome,
    record_unhandled_http_exception,
)


def test_exception_observation_uses_safe_class_fingerprint(monkeypatch) -> None:
    captured: list[tuple[str, str]] = []

    class _Counter:
        def labels(self, surface: str, fingerprint: str):
            captured.append((surface, fingerprint))
            return self

        def inc(self) -> None:
            return None

    monkeypatch.setattr(
        "app.services.application_exception_observability.APPLICATION_EXCEPTIONS",
        _Counter(),
    )
    exc = RuntimeError("customer data must never be emitted")

    assert record_unhandled_http_exception(exc) == "RuntimeError"
    assert captured == [("http", "RuntimeError")]
    assert exception_fingerprint(exc) == "RuntimeError"


def test_payment_outcome_has_only_closed_labels(monkeypatch) -> None:
    captured: list[tuple[str, str]] = []

    class _Counter:
        def labels(self, channel: str, outcome: str):
            captured.append((channel, outcome))
            return self

        def inc(self) -> None:
            return None

    monkeypatch.setattr(
        "app.services.application_exception_observability.PAYMENT_VERIFICATION_OUTCOMES",
        _Counter(),
    )
    record_payment_verification_outcome(
        channel=PaymentVerificationChannel.API,
        outcome=PaymentVerificationOutcome.BUSINESS_REFUSAL,
    )
    assert captured == [("api", "business_refusal")]
