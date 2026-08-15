from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.services import party_identity_audit as identity_audit


def _facts(**overrides) -> identity_audit.SubscriberIdentityFacts:
    values = {
        "subscriber_id": uuid.uuid4(),
        "first_name": "Ada",
        "last_name": "Okafor",
        "email": f"ada-{uuid.uuid4().hex}@dotmac.ng",
        "phone": "+2348012345678",
        "account_status": "active",
        "is_active": True,
    }
    values.update(overrides)
    return identity_audit.SubscriberIdentityFacts(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({"has_active_subscription": True}, "active_subscriber"),
        ({"has_any_subscription": True}, "inactive_subscriber"),
        ({"has_customer_sales_order": True}, "customer"),
        ({"has_sales_order": True}, "lead"),
        ({"has_lead": True}, "lead"),
        ({"email_verified": True}, "verified_contact"),
        ({}, "unverified_record"),
    ),
)
def test_lifecycle_cohorts_follow_strongest_native_evidence(overrides, expected):
    cohort, _evidence, _contradictions = identity_audit.classify_lifecycle(
        _facts(**overrides)
    )

    assert cohort.value == expected


def test_audit_subscriber_identity_uses_the_read_only_snapshot_seam():
    """Behavioural proof lives on PostgreSQL; this pins the wiring."""

    source = Path("scripts/migration/audit_subscriber_identity.py").read_text(
        encoding="utf-8"
    )

    assert "read_only_snapshot_session" in source
    assert "SET TRANSACTION" not in source
