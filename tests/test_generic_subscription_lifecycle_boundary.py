"""Generic subscription updates cannot change lifecycle or enforcement state."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.catalog import SubscriptionStatus
from app.models.enforcement_lock import EnforcementReason
from app.schemas.catalog import SubscriptionTechnicalUpdate, SubscriptionUpdate
from app.services import catalog as catalog_service
from app.services.account_lifecycle import get_active_locks, suspend_subscription


def test_generic_api_contract_excludes_lifecycle_fields() -> None:
    with pytest.raises(ValidationError):
        SubscriptionTechnicalUpdate.model_validate({"status": "suspended"})
    with pytest.raises(ValidationError):
        SubscriptionTechnicalUpdate.model_validate(
            {"next_billing_at": "2026-07-25T00:00:00Z"}
        )


def test_technical_subscription_edit_preserves_lifecycle_lock(
    db_session, subscriber, subscription
):
    subscription_id = subscription.id
    subscription.status = SubscriptionStatus.active
    db_session.commit()
    suspend_subscription(
        db_session,
        str(subscription_id),
        reason=EnforcementReason.admin,
        source="admin:pytest",
        emit=False,
    )
    db_session.commit()

    updated = catalog_service.subscriptions.update(
        db_session,
        str(subscription_id),
        SubscriptionUpdate(service_description="router metadata corrected"),
    )

    locks = get_active_locks(db_session, subscription_id=str(subscription_id))
    assert updated.status == SubscriptionStatus.suspended
    assert [(lock.reason, lock.source) for lock in locks] == [
        (EnforcementReason.admin, "admin:pytest")
    ]
