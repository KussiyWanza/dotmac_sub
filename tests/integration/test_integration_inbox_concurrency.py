"""PostgreSQL concurrency contract for verified integration receipts."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

from sqlalchemy.orm import sessionmaker

from app.models.integration_platform import IntegrationInbox
from app.services.integrations import inbox
from app.services.integrations.whatsapp_capability import WHATSAPP_RECEIVE_CAPABILITY
from app.services.locking import lock_for_update
from tests.test_integration_whatsapp_capability import install_whatsapp


def test_concurrent_provider_replay_creates_and_claims_one_receipt(engine) -> None:
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as setup:
        _installation, bindings = install_whatsapp(setup)
        binding_id = bindings[WHATSAPP_RECEIVE_CAPABILITY].id
        setup.commit()

    ready = Barrier(2)

    def receive() -> tuple[str, bool]:
        with session_factory() as session:
            ready.wait(timeout=5)
            receipt, should_process = inbox.receive_and_claim_verified(
                session,
                capability_binding_id=binding_id,
                provider_event_id="meta:concurrent-replay",
                event_type="whatsapp.meta.webhook",
                payload={"entry": [{"id": "same-event"}]},
            )
            return str(receipt.id), should_process

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: receive(), range(2)))

    assert len({receipt_id for receipt_id, _claimed in outcomes}) == 1
    assert sorted(claimed for _receipt_id, claimed in outcomes) == [False, True]
    with session_factory() as check:
        assert (
            check.query(IntegrationInbox)
            .filter(IntegrationInbox.capability_binding_id == binding_id)
            .filter(IntegrationInbox.provider_event_id == "meta:concurrent-replay")
            .count()
            == 1
        )


def test_concurrent_reclaim_of_one_expired_lease_settles_on_one_attempt(
    engine,
) -> None:
    """Two redeliveries racing to reclaim the SAME expired-lease claim must not
    both succeed. `claim_for_processing`'s reclaim mutation is only made safe
    by the capability-binding lock `receive_verified` takes before reading the
    receipt (SQLite's StaticPool gives every session the same connection, so
    it cannot exercise real blocking — this needs Postgres)."""

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as setup:
        _installation, bindings = install_whatsapp(setup)
        binding_id = bindings[WHATSAPP_RECEIVE_CAPABILITY].id
        original_claim_time = datetime.now(UTC) - timedelta(hours=1)
        receipt, should_process = inbox.receive_and_claim_verified(
            setup,
            capability_binding_id=binding_id,
            provider_event_id="meta:concurrent-reclaim",
            event_type="whatsapp.meta.webhook",
            payload={"entry": [{"id": "stuck-event"}]},
            now=original_claim_time,
        )
        setup.commit()
        assert should_process is True
        assert receipt.attempt_count == 1

    ready = Barrier(2)
    reclaim_time = datetime.now(UTC)

    def reclaim() -> bool:
        with session_factory() as session:
            ready.wait(timeout=5)
            _reclaimed, should_process_again = inbox.receive_and_claim_verified(
                session,
                capability_binding_id=binding_id,
                provider_event_id="meta:concurrent-reclaim",
                event_type="whatsapp.meta.webhook",
                payload={"entry": [{"id": "stuck-event"}]},
                now=reclaim_time,
            )
            return should_process_again

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: reclaim(), range(2)))

    # Exactly one of the two racing redeliveries reclaimed the row; the other
    # observed a claim that was, by the time it acquired the binding lock,
    # already live again under the new attempt.
    assert sorted(outcomes) == [False, True]
    with session_factory() as check:
        final = (
            check.query(IntegrationInbox)
            .filter(IntegrationInbox.capability_binding_id == binding_id)
            .filter(IntegrationInbox.provider_event_id == "meta:concurrent-reclaim")
            .one()
        )
        assert final.attempt_count == 2
        assert final.state == "processing"


def test_concurrent_reclaim_never_clobbers_an_in_flight_completion(engine) -> None:
    """A redelivery reclaiming an expired-lease receipt must block on -- not
    race -- a claimant that is still actually working the row, and must never
    overwrite that claimant's eventual `processed` commit with a stale
    `processing` snapshot.

    This is the reclaim-vs-completion direction, not reclaim-vs-reclaim (the
    test above): worker A claims, commits (releasing the binding lock), then
    takes its OWN row lock on the receipt via `lock_for_update` (mirroring
    `process_claimed_payment_webhook`) while never re-acquiring the binding
    lock. A's lease has already expired. Worker B's redelivery reclaim
    (`receive_verified`) reaches the binding lock freely (A isn't holding it)
    and must then block on A's row lock rather than reading a stale snapshot
    and later clobbering A's commit -- proven by `get_receipt`/
    `receive_verified`'s row-level `FOR UPDATE`, not by the binding lock
    (which does not protect this direction at all).
    """

    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with session_factory() as setup:
        _installation, bindings = install_whatsapp(setup)
        binding_id = bindings[WHATSAPP_RECEIVE_CAPABILITY].id
        expired_claim_time = datetime.now(UTC) - timedelta(hours=1)
        receipt, should_process = inbox.receive_and_claim_verified(
            setup,
            capability_binding_id=binding_id,
            provider_event_id="meta:reclaim-vs-completion",
            event_type="whatsapp.meta.webhook",
            payload={"entry": [{"id": "in-flight-event"}]},
            now=expired_claim_time,
        )
        setup.commit()
        assert should_process is True
        receipt_id = receipt.id
        claimed_attempt = receipt.attempt_count

    worker_a_locked = Barrier(2, timeout=5)

    def worker_a() -> str:
        with session_factory() as session:
            # Simulate `process_claimed_payment_webhook`'s row lock, taken
            # once and held for the duration of "processing" -- with no
            # binding lock re-acquired, exactly like the real command.
            locked = lock_for_update(session, IntegrationInbox, receipt_id)
            worker_a_locked.wait()
            # Give worker B's row-level FOR UPDATE a real chance to reach
            # Postgres and start waiting on this lock before it is released.
            # A synchronization primitive can't observe "B is now blocked
            # inside the database"; a short, generous sleep is the standard
            # way to make that race deterministic in a test.
            time.sleep(0.5)
            inbox.mark_processed(
                locked,
                consequence={"status": "ok"},
                claimed_attempt=claimed_attempt,
            )
            session.commit()
            return "committed"

    def worker_b() -> bool:
        with session_factory() as session:
            worker_a_locked.wait()
            # `receive_verified`'s binding lock is free (A already released
            # it), so B reaches the row-level FOR UPDATE and blocks there
            # until A's commit above releases it.
            _reclaimed, should_process_again = inbox.receive_and_claim_verified(
                session,
                capability_binding_id=binding_id,
                provider_event_id="meta:reclaim-vs-completion",
                event_type="whatsapp.meta.webhook",
                payload={"entry": [{"id": "in-flight-event"}]},
                now=datetime.now(UTC),
            )
            return should_process_again

    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(worker_a)
        future_b = executor.submit(worker_b)
        assert future_a.result(timeout=15) == "committed"
        # B must not have reclaimed a receipt that A had already completed.
        assert future_b.result(timeout=15) is False

    with session_factory() as check:
        final = check.get(IntegrationInbox, receipt_id)
        # A's commit must be the one standing -- not clobbered by B reading a
        # stale ("processing") snapshot before A's commit was visible to it.
        assert final.state == "processed"
        assert final.attempt_count == claimed_attempt
        assert final.consequence_json == {"status": "ok"}
