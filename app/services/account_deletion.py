"""Customer self-service account deletion (soft-delete).

A subscriber can delete their account from within the app (App Store Guideline
5.1.1(v) / Play Data safety). DotMac is an ISP with active service, billing, and
statutory tax-record retention, so this is a **soft-delete**: the subscriber's
status is set to ``canceled`` (the documented "terminated / soft-deleted, record
preserved" state). That immediately blocks login (``web_customer_auth`` treats
canceled accounts as gone), so the account is deleted from the customer's side,
while the row and billing/tax records are preserved for the retention period;
operations then purge personal data per the privacy policy. The client signs the
user out after a successful request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.audit import AuditActorType
from app.models.subscriber import Subscriber, SubscriberStatus
from app.services.account_lifecycle import transition_account_status
from app.services.audit_adapter import record_audit_event

logger = logging.getLogger(__name__)

_REQUESTED_KEY = "account_deletion_requested_at"
_REASON_KEY = "account_deletion_reason"


def request_deletion(
    db: Session,
    subscriber_id: str,
    *,
    reason: str | None = None,
    request=None,
) -> dict:
    """Soft-delete the caller's account: set status=canceled + record the reason.

    Idempotent — re-requesting an already-canceled account is a no-op that still
    returns success. Returns a summary the API echoes back to the client.
    """
    subscriber = db.get(Subscriber, subscriber_id)
    if subscriber is None:
        raise HTTPException(status_code=404, detail="Subscriber account not found")

    now = datetime.now(UTC)
    already = subscriber.status == SubscriberStatus.canceled
    clean_reason = (reason or "").strip() or None

    if not already:
        transition_account_status(
            db,
            str(subscriber_id),
            SubscriberStatus.canceled,
            reason=clean_reason or "Customer requested account deletion",
            source="customer:self_service_deletion",
        )

    # Stamp who/why for operations + the eventual personal-data purge.
    subscriber.metadata_ = {
        **(subscriber.metadata_ or {}),
        _REQUESTED_KEY: now.isoformat(),
        _REASON_KEY: clean_reason,
    }
    db.commit()

    try:
        record_audit_event(
            db,
            actor_type=AuditActorType.user,
            actor_id=str(subscriber_id),
            action="account_deletion_requested",
            entity_type="subscriber",
            entity_id=str(subscriber_id),
            status_code=200,
            is_success=True,
            metadata={"reason": clean_reason, "already_canceled": already},
            defer_until_commit=False,
        )
    except Exception:  # noqa: BLE001 - audit must never block the request
        logger.warning("account-deletion audit failed", exc_info=True)

    _notify(db, subscriber)

    return {
        "status": "deleted",
        "requested_at": now,
        "already_requested": already,
    }


def _notify(db: Session, subscriber: Subscriber) -> None:
    """Best-effort confirmation to the customer (push + email)."""
    try:
        from app.models.notification import NotificationChannel
        from app.schemas.notification import NotificationCreate
        from app.services.notification import notifications as notifications_svc

        if not subscriber.email:
            return
        subject = "Your DotMac account has been closed"
        body = (
            "Your DotMac account has been closed at your request and you have been "
            "signed out. Your service will end and your personal data will be "
            "deleted in line with our privacy policy (some billing and tax records "
            "are retained where the law requires). To restore your account, contact "
            "support@dotmac.ng."
        )
        for channel in (NotificationChannel.push, NotificationChannel.email):
            try:
                notifications_svc.create_customer_notification(
                    db,
                    NotificationCreate(
                        channel=channel,
                        subscriber_id=subscriber.id,
                        recipient=subscriber.email,
                        subject=subject,
                        body=body,
                        category="account",
                        event_type="account_deletion_requested",
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.warning("account-deletion notification failed", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.warning("account-deletion notify block failed", exc_info=True)
