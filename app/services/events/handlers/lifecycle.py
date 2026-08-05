"""Read-only lifecycle consequences for the event transport."""

import logging

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.services.connectivity_reconciler import connectivity_shadow_diff
from app.services.events.types import SUBSCRIPTION_LIFECYCLE_MAP, Event

logger = logging.getLogger(__name__)


class LifecycleHandler:
    """Observe lifecycle events without creating authoritative history."""

    def _observe_connectivity_shadow_diff(self, db: Session, event: Event) -> None:
        """Record desired-vs-actual connectivity drift after a lifecycle event.

        Step 2d is observability only: lifecycle transitions trigger the
        read-only shadow diff so production can show what the reconciler would
        change before any legacy writer is absorbed. Failures are swallowed so
        lifecycle auditing is never blocked by metrics/logging.
        """
        if not event.subscription_id:
            return
        try:
            # Savepoint-isolated: the lifecycle record is already flushed by the
            # caller, so even a DB-level failure inside the read-only diff rolls
            # back only to here and never discards the audit write.
            with db.begin_nested():
                subscription = db.get(Subscription, event.subscription_id)
                if subscription and subscription.subscriber_id:
                    connectivity_shadow_diff(db, subscription.subscriber_id)
        except Exception as exc:
            logger.warning(
                "connectivity shadow-diff observation failed for subscription %s: %s",
                event.subscription_id,
                exc,
            )

    def handle(self, db: Session, event: Event) -> None:
        """Run read-only consequences for a mapped lifecycle event.

        The lifecycle command appends evidence atomically with status. Events
        are asynchronous transport and therefore cannot reconstruct either the
        authoritative effective time or the command's replay identity.

        Args:
            db: Database session
            event: The event to process
        """
        # Check if this is a subscription lifecycle event
        lifecycle_type_str = SUBSCRIPTION_LIFECYCLE_MAP.get(event.event_type)
        if lifecycle_type_str is None:
            return

        # Need a subscription_id to record lifecycle
        if not event.subscription_id:
            logger.warning(
                f"Cannot record lifecycle for {event.event_type.value}: "
                "no subscription_id"
            )
            return

        self._observe_connectivity_shadow_diff(db, event)

        logger.info(
            "Observed lifecycle event %s for subscription %s",
            lifecycle_type_str,
            event.subscription_id,
        )
