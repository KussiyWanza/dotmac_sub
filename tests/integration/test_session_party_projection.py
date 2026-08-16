"""PostgreSQL proofs for migration 534 and the session bound pair.

SQLite cannot prove a foreign key is enforced, an index exists, or that a
transaction rolls back a projection write. These run on the migrated schema.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.auth import Session as AuthSession
from app.models.auth import SessionStatus
from app.services import staff_party_authentication as resolver
from tests.staff_identity_fixtures import add_bound_staff_user

pytestmark = pytest.mark.integration


def test_migration_534_created_the_foreign_key_and_index(db_session) -> None:
    fk = db_session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'sessions'::regclass AND contype = 'f' "
            "AND conname = 'fk_sessions_party_id_parties'"
        )
    ).scalar_one_or_none()
    index = db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes WHERE indexname = 'ix_sessions_party_id'"
        )
    ).scalar_one_or_none()

    assert fk == "fk_sessions_party_id_parties"
    assert index == "ix_sessions_party_id"


def test_party_id_is_nullable_so_pre_migration_sessions_survive(db_session) -> None:
    """The column must accept NULL, or deploy 1 would strand 1,240 sessions."""

    nullable = db_session.execute(
        text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'sessions' AND column_name = 'party_id'"
        )
    ).scalar_one()

    assert nullable == "YES"


def test_the_foreign_key_refuses_an_unknown_party(db_session) -> None:
    """Proven by attempting it, not by trusting the catalog."""

    user, _person = add_bound_staff_user(db_session, email=f"fk-{uuid4().hex}@x.test")
    db_session.flush()
    session = AuthSession(
        system_user_id=user.id,
        party_id=uuid4(),  # no such Party
        status=SessionStatus.active,
        token_hash=f"fk-canary-{uuid4().hex}",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


def test_a_persisted_matching_pair_resolves_after_reload(db_session) -> None:
    """Rollback safety: deploy 1 reads a populated party_id correctly.

    This is what makes deploy 2 safe to roll back without reversing the data
    migration — deploy 1's reader must honour the values the backfill wrote.
    """

    user, person = add_bound_staff_user(db_session, email=f"pair-{uuid4().hex}@x.test")
    session = AuthSession(
        system_user_id=user.id,
        party_id=person.id,
        status=SessionStatus.active,
        token_hash=f"pair-{uuid4().hex}",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.get(AuthSession, session.id)
    resolved = resolver.resolve_staff_principal_by_party(
        db_session, reloaded.party_id, reloaded.system_user_id
    )

    assert resolved.id == user.id


def test_a_persisted_mismatched_pair_fails_closed_after_reload(db_session) -> None:
    """The other half. A matching pair alone passes even when the field is ignored."""

    _user, person = add_bound_staff_user(db_session, email=f"mm-{uuid4().hex}@x.test")
    impostor, _ = add_bound_staff_user(db_session, email=f"imp-{uuid4().hex}@x.test")
    session = AuthSession(
        system_user_id=impostor.id,
        party_id=person.id,
        status=SessionStatus.active,
        token_hash=f"mm-{uuid4().hex}",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)
    db_session.commit()
    db_session.expire_all()

    reloaded = db_session.get(AuthSession, session.id)
    with pytest.raises(resolver.StaffProjectionError) as exc:
        resolver.resolve_staff_principal_by_party(
            db_session, reloaded.party_id, reloaded.system_user_id
        )

    assert exc.value.refusal is resolver.StaffProjectionRefusal.projection_conflict


def test_a_rolled_back_projection_write_leaves_no_party_id(db_session) -> None:
    """The projection is transactional, so a failed backfill batch leaves no trace."""

    user, person = add_bound_staff_user(db_session, email=f"tx-{uuid4().hex}@x.test")
    session = AuthSession(
        system_user_id=user.id,
        status=SessionStatus.active,
        token_hash=f"tx-{uuid4().hex}",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(session)
    db_session.commit()

    savepoint = db_session.begin_nested()
    session.party_id = person.id
    db_session.flush()
    savepoint.rollback()
    db_session.expire_all()

    assert db_session.get(AuthSession, session.id).party_id is None
