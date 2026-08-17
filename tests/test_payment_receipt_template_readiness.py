from app.models.notification import NotificationChannel, NotificationTemplate
from app.services.billing_enforcement_guards import notification_delivery_health
from app.services.billing_health import (
    PaymentReceiptTemplateStatus,
    payment_receipt_template_readiness,
)


def _receipt_template(
    *,
    body: str,
    subject: str = "Payment receipt {receipt_number}",
    is_active: bool = True,
) -> NotificationTemplate:
    return NotificationTemplate(
        code="payment_received",
        name="Payment Received",
        channel=NotificationChannel.email,
        subject=subject,
        body=body,
        is_active=is_active,
    )


def test_missing_payment_receipt_template_is_unready(db_session):
    readiness = payment_receipt_template_readiness(db_session)

    assert readiness.status is PaymentReceiptTemplateStatus.missing
    assert readiness.ready is False


def test_inactive_payment_receipt_template_is_unready(db_session):
    db_session.add(
        _receipt_template(
            body="Receipt {receipt_number}: {receipt_url}",
            is_active=False,
        )
    )
    db_session.flush()

    readiness = payment_receipt_template_readiness(db_session)

    assert readiness.status is PaymentReceiptTemplateStatus.inactive
    assert readiness.ready is False


def test_active_acknowledgement_without_receipt_fields_is_unready(db_session):
    db_session.add(
        _receipt_template(
            subject="Payment received",
            body="Thank you for paying {amount}.",
        )
    )
    db_session.flush()

    readiness = payment_receipt_template_readiness(db_session)

    assert readiness.status is PaymentReceiptTemplateStatus.incomplete_receipt
    assert readiness.ready is False


def test_receipt_fields_in_subject_do_not_make_the_body_usable(db_session):
    db_session.add(
        _receipt_template(
            subject="Receipt {receipt_number}: {receipt_url}",
            body="Thank you for paying {amount}.",
        )
    )
    db_session.flush()

    readiness = payment_receipt_template_readiness(db_session)

    assert readiness.status is PaymentReceiptTemplateStatus.incomplete_receipt
    assert readiness.ready is False


def test_invalid_payment_receipt_template_is_unready(db_session):
    db_session.add(_receipt_template(body="Thank you for paying {{amount}}."))
    db_session.flush()

    readiness = payment_receipt_template_readiness(db_session)

    assert readiness.status is PaymentReceiptTemplateStatus.invalid
    assert readiness.ready is False


def test_receipt_aware_template_is_ready_for_delivery_health(db_session):
    db_session.add(
        _receipt_template(
            body=(
                "Thank you for paying {amount}. Receipt {receipt_number}: {receipt_url}"
            )
        )
    )
    db_session.flush()

    readiness = payment_receipt_template_readiness(db_session)
    delivery_health = notification_delivery_health(db_session)

    assert readiness.status is PaymentReceiptTemplateStatus.ready
    assert readiness.ready is True
    assert delivery_health.ok is True
    assert delivery_health.details["payment_receipt_email_template_ready"] is True


def test_delivery_health_exposes_missing_receipt_template(db_session):
    health = notification_delivery_health(db_session)

    assert health.ok is False
    assert "payment_receipt_email_template_unready" in health.reasons
    assert health.details["payment_receipt_email_template_ready"] is False
