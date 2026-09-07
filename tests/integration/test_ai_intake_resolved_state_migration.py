"""PostgreSQL proof for the terminal AI intake resolution state."""

from sqlalchemy import text
from sqlalchemy.orm import Session


def test_migrated_ai_intake_state_constraint_allows_resolved(
    db_session: Session,
) -> None:
    definition = db_session.execute(
        text(
            "SELECT pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conname = 'ck_ai_intake_sessions_state'"
        )
    ).scalar_one()

    assert "resolved" in str(definition)
