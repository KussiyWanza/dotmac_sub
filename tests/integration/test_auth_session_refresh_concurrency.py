"""PostgreSQL row-lock proof for concurrent refresh-token rotation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.models.event_store import EventStore
from app.models.subscriber import Subscriber
from app.services.auth_session_refresh import (
    RefreshDisposition,
    RefreshSessionCommand,
    hash_refresh_token,
    renew_authentication_session,
)
from app.services.owner_commands import CommandContext
from app.services.subscriber import _default_reseller_id

pytestmark = pytest.mark.integration


def test_concurrent_refresh_rotates_once_and_replays_once(engine) -> None:
    if engine.dialect.name != "postgresql":
        pytest.fail("refresh row-lock evidence requires PostgreSQL")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    old_token = "postgres-concurrent-refresh-token"
    now = datetime.now(UTC)
    with factory() as setup:
        subscriber = Subscriber(
            first_name="Refresh",
            last_name="Concurrency",
            email=f"refresh-concurrency-{now.timestamp()}@example.test",
            reseller_id=_default_reseller_id(setup),
        )
        setup.add(subscriber)
        setup.flush()
        subscriber_id = subscriber.id
        session = AuthSession(
            subscriber_id=subscriber_id,
            status=SessionStatus.active,
            token_hash=hash_refresh_token(old_token),
            ip_address="203.0.113.9",
            user_agent="postgres-browser/1",
            expires_at=now + timedelta(days=1),
        )
        setup.add(session)
        setup.commit()
        session_id = session.id

    barrier = Barrier(2)

    def renew() -> RefreshDisposition:
        with factory() as worker:
            barrier.wait(timeout=10)
            outcome = renew_authentication_session(
                db=worker,
                command=RefreshSessionCommand(
                    context=CommandContext.system(
                        actor="pytest:postgres-auth-refresh",
                        scope="authentication:session",
                        reason="Verify concurrent refresh row lock",
                        idempotency_key=f"refresh:{hash_refresh_token(old_token)}",
                    ),
                    refresh_token=old_token,
                    client_ip="203.0.113.9",
                    user_agent="postgres-browser/1",
                ),
            )
            return outcome.disposition

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(renew), pool.submit(renew))
            dispositions = sorted(
                (future.result(timeout=20) for future in futures),
                key=lambda disposition: disposition.value,
            )
        assert sorted(dispositions) == [
            RefreshDisposition.DUPLICATE,
            RefreshDisposition.ROTATED,
        ]
        with factory() as verify:
            persisted = verify.get(AuthSession, session_id)
            assert persisted is not None
            assert persisted.status is SessionStatus.active
            assert persisted.previous_token_hash == hash_refresh_token(old_token)
    finally:
        with factory() as cleanup:
            cleanup.execute(
                delete(EventStore).where(EventStore.subscriber_id == subscriber_id)
            )
            persisted = cleanup.get(AuthSession, session_id)
            if persisted is not None:
                cleanup.delete(persisted)
            subscriber = cleanup.get(Subscriber, subscriber_id)
            if subscriber is not None:
                cleanup.delete(subscriber)
            cleanup.commit()
