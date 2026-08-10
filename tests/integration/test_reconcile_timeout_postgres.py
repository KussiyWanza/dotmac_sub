"""PostgreSQL acceptance for the reconciler transaction timeout setting."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.network.reconcile import core as reconcile_core


def test_reconcile_timeout_setting_is_valid_and_transaction_local(
    db_session: Session,
) -> None:
    reconcile_core._widen_idle_in_transaction_timeout(db_session, 60)

    assert (
        db_session.scalar(
            text(
                "SELECT current_setting('idle_in_transaction_session_timeout')::interval "
                "= interval '90 seconds'"
            )
        )
        is True
    )
