"""A processing claim a worker dies on must eventually surface again.

Before this change, `claim_for_processing` treated `processing` as
permanently un-claimable: a worker that claimed a receipt and then
died/crashed/timed out before calling `mark_processed`/`mark_failed` left the
row stuck forever, and the provider's retry got an empty-consequence 200 (see
`app/services/api_billing_webhooks.py`). These tests pin the reclaim
mechanism (`lease_expires_at` + `attempt_count` fence) added to
`app/services/integrations/inbox.py`. Reclaim itself has no per-capability
gating — every one of the 8 inbox domains gets a reclaimable lease. Only the
`claimed_attempt` completion fence is wired through payments
(`payment_webhook_commands.py`'s `ProcessClaimedPaymentWebhookCommand`); the
other 7 domains' completion paths (`mark_processed`/`mark_failed` called
without `claimed_attempt`) are unaffected by the fence and were not audited
for reclaim safety here — an open question for a follow-up, not solved by
this file.

The single most important test here is
`test_a_zombie_worker_cannot_complete_a_reclaimed_receipt`: it proves that a
stalled claimant that eventually resumes and finishes its (stale) work cannot
commit a second financial consequence for a receipt someone else has since
reclaimed and completed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.models.billing import (
    InvoiceDueDateBasis,
    InvoiceStatus,
    Payment,
    PaymentProvider,
    PaymentProviderEvent,
    PaymentProviderType,
)
from app.models.integration_platform import IntegrationInbox
from app.schemas.billing import InvoiceCreate
from app.services import billing as billing_service
from app.services.api_billing_webhooks import process_paystack_webhook
from app.services.db_session_adapter import db_session_adapter
from app.services.integrations import inbox as integration_inbox
from app.services.integrations.inbox import InboxError, InboxLeaseLost
from app.services.owner_commands import CommandContext
from app.services.payment_webhook_commands import (
    PROCESS_SCOPE,
    PaymentWebhookProvider,
    ProcessClaimedPaymentWebhookCommand,
    process_claimed_payment_webhook,
)
from tests.integration_platform_helpers import enable_payment_provider


@pytest.fixture(autouse=True)
def _payment_env(monkeypatch):
    monkeypatch.setenv("PAYSTACK_TEST_SECRET", "sk_test_webhook_secret")
    monkeypatch.setenv("PAYSTACK_TEST_PUBLIC", "pk_test_webhook")


def _webhook_binding_id(db):
    """Enable exactly one Paystack installation and return its webhook binding id."""

    bindings = enable_payment_provider(db, "paystack")
    return bindings["payments.webhook.v1"].id


def _make_provider(db):
    provider = PaymentProvider(
        name="Paystack", provider_type=PaymentProviderType.paystack
    )
    db.add(provider)
    db.commit()
    db.refresh(provider)
    return provider


def _make_invoice(db, account_id, *, amount: str, invoice_number: str):
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
            due_date_basis_ref="test:inbox-lease",
            due_date_policy_version="test-v1",
        ),
    )


def _paystack_body(
    *, reference: str, tx_id: str, amount_kobo: int, metadata: dict
) -> bytes:
    return json.dumps(
        {
            "event": "charge.success",
            "data": {
                "id": tx_id,
                "reference": reference,
                "amount": amount_kobo,
                "fees": 0,
                "currency": "NGN",
                "status": "success",
                "metadata": metadata,
            },
        }
    ).encode()


def _post_paystack(db, body: bytes):
    signature = hmac.new(b"sk_test_webhook_secret", body, hashlib.sha512).hexdigest()
    return process_paystack_webhook(db=db, body=body, signature=signature)


def _aware(value):
    """SQLite drops tzinfo on a `DateTime(timezone=True)` round trip."""

    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _claim_receipt(db, *, binding_id, provider_event_id, event_type, payload, now=None):
    receipt, should_process = integration_inbox.receive_and_claim_verified(
        db,
        capability_binding_id=binding_id,
        provider_event_id=provider_event_id,
        event_type=event_type,
        payload=payload,
        now=now,
    )
    return receipt, should_process


# ---------------------------------------------------------------------------
# inbox.py unit-level lease/reclaim behavior
# ---------------------------------------------------------------------------


def test_an_expired_claim_is_reclaimable_on_the_providers_retry(db_session):
    binding_id = _webhook_binding_id(db_session)
    now = datetime.now(UTC)
    receipt, should_process = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="lease-expiry-1",
        event_type="paystack.webhook",
        payload={"a": 1},
        now=now,
    )
    assert should_process is True
    assert receipt.attempt_count == 1
    expected_first_lease = now + integration_inbox.DEFAULT_LEASE_DURATION
    assert _aware(receipt.lease_expires_at) == expected_first_lease

    # The provider retries after the lease has expired (the original claimant
    # never called mark_processed/mark_failed).
    later = now + integration_inbox.DEFAULT_LEASE_DURATION + timedelta(seconds=1)
    reclaimed, should_process_again = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="lease-expiry-1",
        event_type="paystack.webhook",
        payload={"a": 1},
        now=later,
    )
    assert reclaimed.id == receipt.id
    assert should_process_again is True
    assert reclaimed.attempt_count == 2
    expected_lease = later + integration_inbox.DEFAULT_LEASE_DURATION
    assert _aware(reclaimed.lease_expires_at) == expected_lease


def test_a_live_claim_is_not_reclaimed(db_session):
    binding_id = _webhook_binding_id(db_session)
    now = datetime.now(UTC)
    receipt, should_process = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="lease-live-1",
        event_type="paystack.webhook",
        payload={"a": 1},
        now=now,
    )
    assert should_process is True

    # Retried well within the 2-minute lease: must not be reclaimed.
    soon = now + timedelta(seconds=30)
    still_claimed, should_process_again = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="lease-live-1",
        event_type="paystack.webhook",
        payload={"a": 1},
        now=soon,
    )
    assert still_claimed.id == receipt.id
    assert should_process_again is False
    assert still_claimed.attempt_count == 1
    assert still_claimed.state == "processing"


def test_the_stale_reclaimer_moves_only_expired_processing_receipts(db_session):
    binding_id = _webhook_binding_id(db_session)
    now = datetime.now(UTC)

    live, _ = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="sweep-live",
        event_type="paystack.webhook",
        payload={},
        now=now,
    )
    expired, _ = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="sweep-expired",
        event_type="paystack.webhook",
        payload={},
        now=now - timedelta(minutes=5),
    )
    processed, _ = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="sweep-processed",
        event_type="paystack.webhook",
        payload={},
        now=now - timedelta(minutes=5),
    )
    integration_inbox.mark_processed(processed, consequence={"status": "ok"})
    db_session.commit()

    reclaimed_count = integration_inbox.reclaim_stale_claims(
        db_session, now=now, grace=timedelta(minutes=1)
    )
    db_session.commit()

    assert reclaimed_count == 1
    db_session.refresh(live)
    db_session.refresh(expired)
    db_session.refresh(processed)
    assert live.state == "processing"
    assert expired.state == "retryable"
    assert expired.error_code == "inbox_claim_lease_expired"
    assert processed.state == "processed"


def test_a_stuck_receipt_can_be_replayed_by_an_operator(db_session):
    binding_id = _webhook_binding_id(db_session)
    now = datetime.now(UTC)
    stuck, _ = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="replay-stuck",
        event_type="paystack.webhook",
        payload={},
        now=now - timedelta(minutes=10),
    )
    db_session.commit()

    replayed = integration_inbox.replay_receipt(
        db_session, receipt_id=stuck.id, now=now
    )
    db_session.commit()

    assert replayed.state == "verified"
    assert replayed.lease_expires_at is None


def test_replay_refuses_a_live_lease_receipt(db_session):
    binding_id = _webhook_binding_id(db_session)
    now = datetime.now(UTC)
    live, _ = _claim_receipt(
        db_session,
        binding_id=binding_id,
        provider_event_id="replay-live",
        event_type="paystack.webhook",
        payload={},
        now=now,
    )
    db_session.commit()

    with pytest.raises(InboxError):
        integration_inbox.replay_receipt(db_session, receipt_id=live.id, now=now)


# ---------------------------------------------------------------------------
# The critical idempotency proof
# ---------------------------------------------------------------------------


def test_a_zombie_worker_cannot_complete_a_reclaimed_receipt(db_session, subscriber):
    """A worker claims a receipt, stalls past the lease, gets reclaimed and
    completed by a second delivery, then wakes up and tries to finish its
    stale work. It must not post a second payment."""

    provider = _make_provider(db_session)
    invoice = _make_invoice(
        db_session, subscriber.id, amount="1500.00", invoice_number="INV-ZOMBIE-1"
    )
    body = _paystack_body(
        reference="DMAC-ZOMBIE-1",
        tx_id="zombie-1",
        amount_kobo=150000,
        metadata={"invoice_id": str(invoice.id)},
    )
    signature = hmac.new(b"sk_test_webhook_secret", body, hashlib.sha512).hexdigest()

    binding_id = _webhook_binding_id(db_session)

    now = datetime.now(UTC)
    # Step 1: "worker A" claims the receipt (mirrors receive_and_claim_verified
    # inside _process_webhook), then stalls before calling
    # process_claimed_payment_webhook.
    receipt, should_process = integration_inbox.receive_and_claim_verified(
        db_session,
        capability_binding_id=binding_id,
        provider_event_id="paystack-DMAC-ZOMBIE-1",
        event_type="charge.success",
        payload=json.loads(body),
        headers={"provider": "paystack"},
        now=now,
    )
    db_session.commit()
    assert should_process is True
    stale_claimed_attempt = receipt.attempt_count
    receipt_id = receipt.id

    # Step 2: the lease expires; the provider retries and this webhook call
    # reclaims + fully completes the receipt ("worker B").
    response = process_paystack_webhook(db=db_session, body=body, signature=signature)
    assert response.status_code == 409  # still a live lease at "now"

    later = now + integration_inbox.DEFAULT_LEASE_DURATION + timedelta(seconds=5)
    (
        reclaimed_receipt,
        reclaimed_should_process,
    ) = integration_inbox.receive_and_claim_verified(
        db_session,
        capability_binding_id=binding_id,
        provider_event_id="paystack-DMAC-ZOMBIE-1",
        event_type="charge.success",
        payload=json.loads(body),
        headers={"provider": "paystack"},
        now=later,
    )
    db_session.commit()
    assert reclaimed_should_process is True
    reclaimed_attempt = reclaimed_receipt.attempt_count
    assert reclaimed_attempt == stale_claimed_attempt + 1
    assert reclaimed_receipt.state == "processing"  # reclaim does not change state

    # Step 3: "worker A" (still holding its now-stale claimed_attempt) finally
    # wakes up and tries to finish its work WHILE the receipt is still
    # 'processing' under worker B's newer attempt. The pre-existing
    # `receipt.state == "processing"` check alone would let this through
    # unchanged (reclaim never changes `state`) — proving the fence, not the
    # state check, is what has to catch this.
    #
    # `execute_owner_command` requires a transaction-free session at entry;
    # the attribute reads above re-opened an implicit SQLAlchemy read
    # transaction (post-commit attributes are expired), so release it first —
    # mirrors `db_session_adapter.release_read_transaction` immediately before
    # `process_claimed_payment_webhook` in `api_billing_webhooks.py`.
    db_session_adapter.release_read_transaction(db_session)
    with pytest.raises(InboxLeaseLost):
        process_claimed_payment_webhook(
            db_session,
            ProcessClaimedPaymentWebhookCommand(
                receipt_id=receipt_id,
                provider=PaymentWebhookProvider.PAYSTACK,
                claimed_attempt=stale_claimed_attempt,
            ),
            context=CommandContext.system(
                actor="test:worker-a-zombie",
                scope=PROCESS_SCOPE,
                reason="stale worker A resumes after being reclaimed",
                idempotency_key="paystack-DMAC-ZOMBIE-1",
            ),
        )

    # Worker A's attempted (stale) completion committed no payment: the fence
    # raised before the transaction committed, and execute_owner_command
    # rolled the whole thing back.
    assert db_session.query(Payment).filter_by(external_id="zombie-1").count() == 0
    stale_check = db_session.get(IntegrationInbox, receipt_id)
    assert stale_check.state == "processing"
    assert stale_check.attempt_count == reclaimed_attempt

    # Step 4: worker B, using the CURRENT attempt, completes normally.
    db_session_adapter.release_read_transaction(db_session)
    result = process_claimed_payment_webhook(
        db_session,
        ProcessClaimedPaymentWebhookCommand(
            receipt_id=receipt_id,
            provider=PaymentWebhookProvider.PAYSTACK,
            claimed_attempt=reclaimed_attempt,
        ),
        context=CommandContext.system(
            actor="test:worker-b",
            scope=PROCESS_SCOPE,
            reason="worker B completes the reclaimed receipt",
            idempotency_key="paystack-DMAC-ZOMBIE-1",
        ),
    )
    assert result.payment_id is not None

    # Exactly one payment and one provider event exist, and the invoice is
    # paid exactly once — no double settlement from the zombie's stale claim.
    payments = db_session.query(Payment).filter_by(external_id="zombie-1").all()
    assert len(payments) == 1
    events = (
        db_session.query(PaymentProviderEvent).filter_by(external_id="zombie-1").all()
    )
    assert len(events) == 1
    db_session.refresh(invoice)
    assert invoice.balance_due == Decimal("0.00")

    final_receipt = db_session.get(IntegrationInbox, receipt_id)
    assert final_receipt.state == "processed"
    assert final_receipt.attempt_count == stale_claimed_attempt + 1
    assert provider.id  # keep provider referenced for lint
