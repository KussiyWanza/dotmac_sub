"""Owner-command sessions for event-handler consumers.

A projection handler runs inside the dispatcher's savepoint-isolated child
session, which always carries an open transaction — an owner command cannot
run there. This helper opens a fresh, transaction-free Session on the same
bind the dispatch machinery itself uses, so receipted consumer commands run
identically in production and in the test harness (where the bind is a
Connection with an external transaction, guarded by
``execute_owner_command``'s connection savepoint).
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy.orm import Session


@contextmanager
def owner_session(db: Session) -> Generator[Session, None, None]:
    session = Session(bind=db.get_bind(), autoflush=False)
    try:
        yield session
    finally:
        session.close()
