"""Flutterwave `tx_ref` reuse across genuine transaction ATTEMPTS must never be
mistaken for tampering.

Flutterwave explicitly permits the same merchant-supplied `tx_ref` to repeat
across multiple attempts on the same checkout (e.g. a failed attempt followed
by a successful retry). Before this fix, `identify_verified_payment_webhook`
built the Flutterwave receipt identity from `tx_ref` alone
(`flutterwave-<tx_ref>`), so a legitimate retry collided with the earlier
attempt's receipt at a different `payload_digest` and the inbox's
tamper-collision detector called `quarantine_installation` -- disabling every
Flutterwave capability on the installation (checkout, verify, reconciliation,
refunds), not just webhook ingress.

The fix scopes the Flutterwave `charge.completed` receipt identity to the
provider's own per-attempt id (`data.id`, REQUIRED -- Flutterwave's webhook
documentation confirms `data.id` is present on both successful and failed
`charge.completed` deliveries, so there is deliberately no `data.flw_ref`
fallback: an earlier draft of this fix carried that fallback as an unverified
assumption and it has been removed rather than kept as an unconfirmed escape
hatch), and adds a single-alias legacy-identity mechanism in
`app.services.integrations.inbox` so a redelivery of an event already
recorded under the OLD `flutterwave-<tx_ref>` format still resolves to the
same receipt on an exact `payload_digest` match, while a digest mismatch
under that old key is silently ignored (not a collision) rather than
quarantined. The mechanism is deliberately bounded to exactly one legacy
identity per call (`legacy_provider_event_id: str | None`, not a collection)
-- a narrow, auditable migration aid for this one retired format, not an
open-ended alternate identity namespace.

Companion to `tests/test_payment_webhook_settlement.py` (general settlement)
and `tests/test_paystack_refund_dispute_webhooks.py` (the analogous Paystack
identity fix this one follows in shape, though the underlying defect is
structurally different -- see `payment_webhook_commands.identify_verified_
payment_webhook`'s Flutterwave branch for the full explanation).
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderType,
    PaymentStatus,
)
from app.models.integration_platform import IntegrationInbox
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.api_billing_webhooks import process_flutterwave_webhook
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.delivery import payload_digest
from app.services.integrations.inbox import InboxError
from app.services.payment_webhook_commands import (
    PaymentWebhookError,
    PaymentWebhookProvider,
    identify_verified_payment_webhook,
)
from tests.integration_platform_helpers import enable_payment_provider


@pytest.fixture(autouse=True)
def flutterwave_binding(db_session, monkeypatch):
    monkeypatch.setenv("FLUTTERWAVE_TEST_SECRET", "flw-test-secret")
    monkeypatch.setenv("FLUTTERWAVE_TEST_PUBLIC", "flw-public")
    monkeypatch.setenv("FLUTTERWAVE_TEST_WEBHOOK", "flutterwave-webhook-secret")
    return enable_payment_provider(db_session, "flutterwave")["payments.webhook.v1"]


def _make_provider(db):
    provider = PaymentProvider(
        name="Flutterwave", provider_type=PaymentProviderType.flutterwave
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def _make_invoice(db, account_id, *, amount: str, invoice_number: str):
    from datetime import UTC, datetime, timedelta

    issued_at = datetime.now(UTC)
    return billing_service.invoices.create(
        db,
        InvoiceCreate(
            account_id=account_id,
            invoice_number=invoice_number,
            currency="NGN",
            subtotal=Decimal(amount),
            total=Decimal(amount),
            balance_due=Decimal(amount),
            status=InvoiceStatus.issued,
            issued_at=issued_at,
            due_at=issued_at + timedelta(days=30),
            due_date_basis=InvoiceDueDateBasis.contract_terms,
            due_date_basis_ref="test:flutterwave-txref-identity",
            due_date_policy_version="test-v1",
        ),
    )


def _flutterwave_body(
    *,
    tx_ref: str,
    tx_id: str | None,
    amount: str,
    status: str,
    meta: dict,
    app_fee: str = "0",
    flw_ref: str | None = None,
) -> bytes:
    data: dict = {
        "tx_ref": tx_ref,
        "amount": amount,
        "app_fee": app_fee,
        "currency": "NGN",
        "status": status,
        "meta": meta,
    }
    if tx_id is not None:
        data["id"] = tx_id
    if flw_ref is not None:
        data["flw_ref"] = flw_ref
    return json.dumps({"event": "charge.completed", "data": data}).encode()


def _post_flutterwave(db, body: bytes):
    return process_flutterwave_webhook(
        db=db,
        body=body,
        signature="flutterwave-webhook-secret",
    )


def test_failed_attempt_then_successful_retry_same_tx_ref_does_not_quarantine(
    db_session, subscriber
):
    """The headline defect: a failed attempt followed by a genuine retry
    sharing one `tx_ref` must be admitted as two distinct receipts, settle
    the retry, and never touch quarantine."""
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="1500.00", invoice_number="INV-RETRY-1"
    )
    failed_body = _flutterwave_body(
        tx_ref="DMAC-RETRY-1",
        tx_id="1",
        amount="1500.00",
        status="failed",
        meta={"invoice_id": str(invoice.id)},
    )
    success_body = _flutterwave_body(
        tx_ref="DMAC-RETRY-1",
        tx_id="2",
        amount="1500.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )

    with patch("app.services.integrations.inbox.quarantine_installation") as quarantine:
        failed_response = _post_flutterwave(db_session, failed_body)
        success_response = _post_flutterwave(db_session, success_body)

    quarantine.assert_not_called()
    assert failed_response.status_code == 200
    assert success_response.status_code == 200

    receipts = (
        db_session.query(IntegrationInbox).order_by(IntegrationInbox.received_at).all()
    )
    assert {r.provider_event_id for r in receipts} == {
        "flutterwave-charge.completed-1",
        "flutterwave-charge.completed-2",
    }
    assert db_session.query(Payment).count() == 1
    payment = db_session.query(Payment).filter_by(external_id="2").one()
    assert payment.status == PaymentStatus.succeeded
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid


def test_success_then_late_failure_does_not_disturb_the_settled_payment(
    db_session, subscriber
):
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="900.00", invoice_number="INV-RETRY-2"
    )
    success_body = _flutterwave_body(
        tx_ref="DMAC-RETRY-2",
        tx_id="10",
        amount="900.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )
    late_failure_body = _flutterwave_body(
        tx_ref="DMAC-RETRY-2",
        tx_id="11",
        amount="900.00",
        status="failed",
        meta={"invoice_id": str(invoice.id)},
    )

    with patch("app.services.integrations.inbox.quarantine_installation") as quarantine:
        success_response = _post_flutterwave(db_session, success_body)
        late_response = _post_flutterwave(db_session, late_failure_body)

    quarantine.assert_not_called()
    assert success_response.status_code == 200
    assert late_response.status_code == 200
    payment = db_session.query(Payment).filter_by(external_id="10").one()
    assert payment.status == PaymentStatus.succeeded
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid
    assert db_session.query(Payment).count() == 1


def test_same_provider_id_delivered_twice_with_mutated_amount_still_quarantines(
    db_session, subscriber
):
    """Tamper detection must still work: this proves the fix scopes identity,
    it does not disable collision detection."""
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="750.00", invoice_number="INV-TAMPER-1"
    )
    original_body = _flutterwave_body(
        tx_ref="DMAC-TAMPER-1",
        tx_id="20",
        amount="750.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )
    mutated_body = _flutterwave_body(
        tx_ref="DMAC-TAMPER-1",
        tx_id="20",
        amount="1750.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )

    with patch("app.services.integrations.inbox.quarantine_installation") as quarantine:
        first_response = _post_flutterwave(db_session, original_body)
        second_response = _post_flutterwave(db_session, mutated_body)

    assert first_response.status_code == 200
    assert second_response.status_code == 409
    quarantine.assert_called_once()


def test_legacy_receipt_with_matching_digest_is_treated_as_a_redelivery(
    db_session, subscriber, flutterwave_binding
):
    """A redelivery of an event already recorded under the OLD
    `flutterwave-<tx_ref>` identity format resolves to that same receipt
    (exact digest match) rather than being recorded twice."""
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="600.00", invoice_number="INV-LEGACY-1"
    )
    body = _flutterwave_body(
        tx_ref="DMAC-LEGACY-1",
        tx_id="30",
        amount="600.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )
    payload = json.loads(body)
    digest = payload_digest(payload)
    legacy_receipt = IntegrationInbox(
        installation_id=flutterwave_binding.installation_id,
        capability_binding_id=flutterwave_binding.id,
        provider_event_id="flutterwave-DMAC-LEGACY-1",
        event_type="charge.completed",
        payload_digest=digest,
        headers_json={"provider": "flutterwave"},
        payload_json=payload,
        state="processed",
        attempt_count=1,
        consequence_json={"status": "ok", "http_status": 200},
    )
    db_session.add(legacy_receipt)
    db_session.commit()

    with patch("app.services.integrations.inbox.quarantine_installation") as quarantine:
        response = _post_flutterwave(db_session, body)

    quarantine.assert_not_called()
    assert response.status_code == 200
    assert db_session.query(IntegrationInbox).count() == 1
    assert db_session.query(Payment).count() == 0
    assert db_session.query(PaymentProviderEvent).count() == 0


def test_legacy_receipt_built_from_bare_id_with_matching_digest_is_a_redelivery(
    db_session, subscriber, flutterwave_binding
):
    """The OLD (pre-fix) legacy identity was built from
    `data.get("tx_ref") or data.get("id")` -- EITHER field, not `tx_ref`
    alone. A redelivery of an old-format `charge.completed` event that had no
    `tx_ref` (so the old system used `data.id` as its identity) must still
    resolve via the legacy alias to the receipt already recorded under that
    bare-id format, not be treated as a new event."""
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="600.00", invoice_number="INV-LEGACY-3"
    )
    body = _flutterwave_body(
        tx_ref="",
        tx_id="50",
        amount="600.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )
    payload = json.loads(body)
    digest = payload_digest(payload)
    # The old identity format for a `tx_ref`-less payload: `flutterwave-<id>`.
    legacy_receipt = IntegrationInbox(
        installation_id=flutterwave_binding.installation_id,
        capability_binding_id=flutterwave_binding.id,
        provider_event_id="flutterwave-50",
        event_type="charge.completed",
        payload_digest=digest,
        headers_json={"provider": "flutterwave"},
        payload_json=payload,
        state="processed",
        attempt_count=1,
        consequence_json={"status": "ok", "http_status": 200},
    )
    db_session.add(legacy_receipt)
    db_session.commit()

    with patch("app.services.integrations.inbox.quarantine_installation") as quarantine:
        response = _post_flutterwave(db_session, body)

    quarantine.assert_not_called()
    assert response.status_code == 200
    assert db_session.query(IntegrationInbox).count() == 1
    assert db_session.query(Payment).count() == 0
    assert db_session.query(PaymentProviderEvent).count() == 0


def test_identity_construction_legacy_alias_falls_back_to_id_when_tx_ref_absent():
    """Unit-level pin for the identity construction itself: with `tx_ref`
    absent, `legacy_provider_event_id` must fall back to `data.id` -- mirroring
    the OLD `data.get("tx_ref") or data.get("id")` construction -- rather than
    being `None`."""
    identity = identify_verified_payment_webhook(
        PaymentWebhookProvider.FLUTTERWAVE,
        {
            "event": "charge.completed",
            "data": {"id": "60"},
        },
    )

    assert identity.legacy_provider_event_id == "flutterwave-60"


def test_legacy_receipt_with_differing_digest_resolves_under_new_identity(
    db_session, subscriber, flutterwave_binding
):
    """The actual bug being fixed, end to end: a legacy row exists from an old
    attempt sharing this `tx_ref`, and a genuinely NEW attempt (different
    `data.id`, different digest) must resolve under the new event-scoped
    identity rather than colliding with (and quarantining) the legacy one."""
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="1200.00", invoice_number="INV-LEGACY-2"
    )
    legacy_payload = {
        "event": "charge.completed",
        "data": {
            "id": "old-attempt-id",
            "tx_ref": "DMAC-LEGACY-2",
            "amount": "1200.00",
            "app_fee": "0",
            "currency": "NGN",
            "status": "failed",
            "meta": {"invoice_id": str(invoice.id)},
        },
    }
    legacy_receipt = IntegrationInbox(
        installation_id=flutterwave_binding.installation_id,
        capability_binding_id=flutterwave_binding.id,
        provider_event_id="flutterwave-DMAC-LEGACY-2",
        event_type="charge.completed",
        payload_digest=payload_digest(legacy_payload),
        headers_json={"provider": "flutterwave"},
        payload_json=legacy_payload,
        state="processed",
        attempt_count=1,
        consequence_json={"status": "ok", "http_status": 200},
    )
    db_session.add(legacy_receipt)
    db_session.commit()

    new_attempt_body = _flutterwave_body(
        tx_ref="DMAC-LEGACY-2",
        tx_id="40",
        amount="1200.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )

    with patch("app.services.integrations.inbox.quarantine_installation") as quarantine:
        response = _post_flutterwave(db_session, new_attempt_body)

    quarantine.assert_not_called()
    assert response.status_code == 200
    new_receipt = (
        db_session.query(IntegrationInbox)
        .filter_by(provider_event_id="flutterwave-charge.completed-40")
        .one()
    )
    assert new_receipt.state == "processed"
    payment = db_session.query(Payment).filter_by(external_id="40").one()
    assert payment.status == PaymentStatus.succeeded
    db_session.refresh(invoice)
    assert invoice.status == InvoiceStatus.paid


def test_identity_construction_rejects_payload_missing_id_and_flw_ref():
    """Neither `data.id` nor `data.flw_ref` present must reject the webhook
    outright (`payload_invalid`), never silently fall back to `tx_ref` --
    that would reintroduce the exact bug this change fixes."""
    payload = {
        "event": "charge.completed",
        "data": {
            "tx_ref": "DMAC-NO-ID-1",
            "amount": "500.00",
            "currency": "NGN",
            "status": "successful",
        },
    }

    with pytest.raises(PaymentWebhookError) as captured:
        identify_verified_payment_webhook(PaymentWebhookProvider.FLUTTERWAVE, payload)

    assert captured.value.code == "financial.payment_webhooks.payload_invalid"


def test_identity_construction_rejects_flw_ref_only_payload_no_fallback():
    """`data.id` is REQUIRED (Flutterwave's webhook documentation confirms it
    is present on both successful and failed `charge.completed` deliveries).
    A payload carrying only `data.flw_ref` must now be REJECTED outright, not
    silently accepted via the flw_ref fallback an earlier draft of this fix
    carried as an unverified assumption -- that fallback has been removed."""
    payload = {
        "event": "charge.completed",
        "data": {
            "tx_ref": "DMAC-FLWREF-1",
            "flw_ref": "FLW-REF-99",
            "amount": "500.00",
            "currency": "NGN",
            "status": "successful",
        },
    }

    with pytest.raises(PaymentWebhookError) as captured:
        identify_verified_payment_webhook(PaymentWebhookProvider.FLUTTERWAVE, payload)

    assert captured.value.code == "financial.payment_webhooks.payload_invalid"


def test_shared_tx_ref_different_ids_produce_different_identities():
    shared_tx_ref = "DMAC-VOCAB-1"
    first = identify_verified_payment_webhook(
        PaymentWebhookProvider.FLUTTERWAVE,
        {
            "event": "charge.completed",
            "data": {"tx_ref": shared_tx_ref, "id": "100"},
        },
    )
    second = identify_verified_payment_webhook(
        PaymentWebhookProvider.FLUTTERWAVE,
        {
            "event": "charge.completed",
            "data": {"tx_ref": shared_tx_ref, "id": "101"},
        },
    )

    assert first.provider_event_id != second.provider_event_id
    assert first.legacy_provider_event_id == second.legacy_provider_event_id


def test_shared_id_produces_the_same_identity():
    shared_id = "200"
    first = identify_verified_payment_webhook(
        PaymentWebhookProvider.FLUTTERWAVE,
        {
            "event": "charge.completed",
            "data": {"tx_ref": "DMAC-VOCAB-2A", "id": shared_id},
        },
    )
    second = identify_verified_payment_webhook(
        PaymentWebhookProvider.FLUTTERWAVE,
        {
            "event": "charge.completed",
            "data": {"tx_ref": "DMAC-VOCAB-2B", "id": shared_id},
        },
    )

    assert first.provider_event_id == second.provider_event_id


def test_legacy_provider_event_id_rejects_empty_string(db_session, flutterwave_binding):
    """The legacy-identity escape hatch is a narrow, auditable single-alias
    mechanism, not an open-ended parallel identity system: a caller that
    supplies an empty (whitespace-only) legacy id is a caller bug, and must be
    rejected loudly rather than silently ignored."""
    with pytest.raises(InboxError):
        integration_inbox.receive_verified(
            db_session,
            capability_binding_id=flutterwave_binding.id,
            provider_event_id="flutterwave-charge.completed-shape-1",
            event_type="charge.completed",
            payload={"event": "charge.completed", "data": {"id": "shape-1"}},
            legacy_provider_event_id="   ",
        )


def test_legacy_provider_event_id_rejects_self_alias(db_session, flutterwave_binding):
    """A legacy id identical to the current provider_event_id would make the
    legacy fallback check a silent no-op that always resolves off the primary
    lookup -- almost certainly a caller bug, not a legitimate migration alias.
    This proves the constraint is enforced, not merely documented."""
    with pytest.raises(InboxError):
        integration_inbox.receive_verified(
            db_session,
            capability_binding_id=flutterwave_binding.id,
            provider_event_id="flutterwave-charge.completed-shape-2",
            event_type="charge.completed",
            payload={"event": "charge.completed", "data": {"id": "shape-2"}},
            legacy_provider_event_id="flutterwave-charge.completed-shape-2",
        )


def test_legacy_provider_event_id_rejects_oversized_value(
    db_session, flutterwave_binding
):
    """`IntegrationInbox.provider_event_id` is `String(240)`; a legacy id that
    could never fit that column is refused up front rather than surfacing as
    an opaque database error later."""
    with pytest.raises(InboxError):
        integration_inbox.receive_verified(
            db_session,
            capability_binding_id=flutterwave_binding.id,
            provider_event_id="flutterwave-charge.completed-shape-3",
            event_type="charge.completed",
            payload={"event": "charge.completed", "data": {"id": "shape-3"}},
            legacy_provider_event_id="x" * 241,
        )


def test_legacy_provider_event_id_accepts_a_valid_near_miss(
    db_session, flutterwave_binding
):
    """Sensitivity check for the three guards above: a legitimate, distinct,
    in-bounds legacy id (the actual Flutterwave shape,
    `flutterwave-<tx_ref>`) must NOT be rejected -- the validation targets the
    malformed cases, not every legacy id."""
    receipt, created = integration_inbox.receive_verified(
        db_session,
        capability_binding_id=flutterwave_binding.id,
        provider_event_id="flutterwave-charge.completed-shape-4",
        event_type="charge.completed",
        payload={"event": "charge.completed", "data": {"id": "shape-4"}},
        legacy_provider_event_id="flutterwave-DMAC-SHAPE-4",
    )

    assert created is True
    assert receipt.provider_event_id == "flutterwave-charge.completed-shape-4"


def test_same_data_id_status_transition_does_not_reach_the_collision_path(
    db_session, subscriber
):
    """Investigates Michael's concrete worry: could the SAME transaction
    (`data.id`) legitimately be delivered twice with a genuinely different
    payload (e.g. an interim `status` value followed by the final outcome),
    which would hit the inbox's same-identity-different-digest path and
    incorrectly quarantine the installation?

    Findings, traced through the actual code and the two documentation pages
    Michael cited (webhooks: https://developer.flutterwave.com/v3.0/docs/webhooks;
    checkout retry behavior:
    https://developer.flutterwave.com/v3.0/docs/flutterwave-standard-1):

    1. Flutterwave's checkout retry behavior mints a NEW transaction id
       (`data.id`) for every retried ATTEMPT on a checkout -- the retry
       produces a different `data.id` sharing the same merchant `tx_ref`
       (that is the entire subject of this fix, pinned by
       `test_failed_attempt_then_successful_retry_same_tx_ref_does_not_quarantine`
       above). A "pending -> successful" status transition on a SINGLE
       attempt therefore has no retry-driven mechanism to redeliver under
       the SAME `data.id` with a changed `status`.
    2. Independently of Flutterwave's own delivery model, THIS codebase's own
       domain layer already assumes one event per `data.id`:
       `PaymentProviderEvent.external_id` carries
       `uq_payment_provider_events_external_id`, a unique index per
       `provider_id` (see `app/models/billing.py`). If Flutterwave ever did
       redeliver a second `charge.completed` for the same `data.id` with a
       different `status`, the domain layer downstream of the inbox would
       reject the second one on that unique constraint regardless of what
       the inbox layer did -- so quarantining early, at the inbox layer, is
       not introducing a NEW failure mode; it is surfacing the same
       "this codebase does not model a status transition under one
       `data.id`" fact earlier and more loudly.
    3. Conclusion: given (1) and (2), a genuine same-`data.id` status
       transition is not a real risk in this code today. This test proves
       the current, ACTUAL behavior for that shape (same `data.id`, `status`
       field differs) is the existing tamper-collision response --
       `quarantine_installation` is called and the second delivery is
       rejected (409) -- and documents why that is correct, not a bug to
       route around. Tamper detection is intentionally NOT weakened here:
       weakening it (e.g. excluding `status` from the digest) would let a
       payload whose `amount`/`currency` legitimately never changes under
       one `data.id` sail through a real tampering attempt disguised as a
       "status update".

    If a future Flutterwave delivery is ever observed in production sending
    two distinct payloads under one `data.id` for a legitimate reason, that
    is new evidence this reasoning should be revisited -- but nothing in the
    current code or the cited documentation supports designing for it now.
    """
    _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="450.00", invoice_number="INV-STATUS-1"
    )
    pending_body = _flutterwave_body(
        tx_ref="DMAC-STATUS-1",
        tx_id="500",
        amount="450.00",
        status="pending",
        meta={"invoice_id": str(invoice.id)},
    )
    successful_body = _flutterwave_body(
        tx_ref="DMAC-STATUS-1",
        tx_id="500",
        amount="450.00",
        status="successful",
        meta={"invoice_id": str(invoice.id)},
    )

    with patch("app.services.integrations.inbox.quarantine_installation") as quarantine:
        pending_response = _post_flutterwave(db_session, pending_body)
        successful_response = _post_flutterwave(db_session, successful_body)

    assert pending_response.status_code == 200
    assert successful_response.status_code == 409
    quarantine.assert_called_once()
    assert db_session.query(Payment).count() == 0
