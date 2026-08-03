from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.customer_portal_flow_common import _resolve_next_billing_date


def test_next_billing_date_is_projected_in_configured_wat(db_session):
    subscription = SimpleNamespace(
        next_billing_at=datetime(2026, 8, 5, 23, tzinfo=UTC),
    )

    assert _resolve_next_billing_date(db_session, subscription).isoformat() == (
        "2026-08-06"
    )


def test_next_billing_date_treats_legacy_naive_values_as_utc(db_session):
    subscription = SimpleNamespace(
        next_billing_at=datetime(2026, 8, 5, 23),
    )

    assert _resolve_next_billing_date(db_session, subscription).isoformat() == (
        "2026-08-06"
    )
