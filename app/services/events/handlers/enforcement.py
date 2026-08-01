"""Event-driven enforcement for sessions and FUP actions."""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.catalog import Subscription
from app.services import fup_enforcement
from app.services import radius as radius_service
from app.services import radius_reject as radius_reject_service
from app.services.enforcement import (
    _resolve_effective_profile,
    apply_radius_profile_to_account,
    disconnect_account_sessions,
    disconnect_subscription_sessions,
    remove_subscription_address_list_block,
    update_subscription_sessions,
)
from app.services.enforcement_event_policy import (
    FupEnforcementAction,
    ResolveFupEventPolicy,
    parse_fup_action_override,
    resolve_fup_event_policy,
    resolve_session_refresh_policy,
)
from app.services.events.types import Event, EventType
from app.services.fup_state import (
    ApplyFupRuntimeState,
    FupRuntimeStateError,
)

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        EventType.subscription_suspended,
        EventType.subscription_disabled,
        EventType.subscription_canceled,
        EventType.subscription_expired,
        EventType.subscription_activated,
        EventType.subscription_resumed,
        EventType.subscription_upgraded,
        EventType.subscription_downgraded,
        EventType.subscriber_throttled,
        EventType.usage_exhausted,
        EventType.payment_received,
        EventType.account_credit_deposited,
        EventType.invoice_overdue,
    }
)


def _reject_reason_from_event_payload(payload: dict) -> str:
    raw = str((payload or {}).get("reason") or "").strip().lower()
    if raw in {"dunning", "negative_balance", "negative-balance"}:
        return "negative"
    return "blocked"


class EnforcementProjectionError(RuntimeError):
    """A durable enforcement event did not complete every owned consequence."""


def _raise_incomplete(operation: str, errors: list[str]) -> None:
    if errors:
        raise EnforcementProjectionError(f"{operation} incomplete: {'; '.join(errors)}")


class EnforcementHandler:
    """Handler that applies session enforcement based on events."""

    def handle(self, db: Session, event: Event) -> None:
        if event.event_type == EventType.subscription_suspended:
            self._handle_subscription_block(db, event, "suspended")
        elif event.event_type == EventType.subscription_disabled:
            self._handle_subscription_block(db, event, "disabled")
        elif event.event_type == EventType.subscription_canceled:
            self._handle_subscription_cancel(db, event)
        elif event.event_type == EventType.subscription_expired:
            self._handle_subscription_block(db, event, "expired")
        elif event.event_type == EventType.subscription_activated:
            self._handle_subscription_restore(db, event)
        elif event.event_type == EventType.subscription_resumed:
            self._handle_subscription_restore(db, event)
        elif event.event_type in (
            EventType.subscription_upgraded,
            EventType.subscription_downgraded,
        ):
            self._handle_subscription_speed_change(db, event)
        elif event.event_type == EventType.subscriber_throttled:
            self._handle_account_throttle(db, event)
        elif event.event_type == EventType.subscriber_unthrottled:
            self._handle_account_unthrottle(db, event)
        elif event.event_type == EventType.usage_exhausted:
            self._handle_usage_exhausted(db, event)
        elif event.event_type in {
            EventType.payment_received,
            EventType.account_credit_deposited,
        }:
            self._handle_payment_received(db, event)
        elif event.event_type == EventType.invoice_overdue:
            self._handle_invoice_overdue(db, event)

    def _enqueue_subscription_session_cleanup(
        self, subscription_id: str, *, reason: str
    ) -> None:
        from app.tasks.enforcement import cleanup_subscription_block_sessions

        cleanup_subscription_block_sessions.delay(str(subscription_id), reason=reason)

    def _enforce_subscription_block(
        self,
        db: Session,
        subscription_id: str,
        *,
        reason: str = "suspended",
        reject_reason: str = "blocked",
        terminal: bool = False,
    ) -> None:
        """Apply RADIUS reject, block/remove credentials, disconnect sessions,
        and add address-list block for a single subscription. Callable both
        from the event-driven path and directly after ``emit=False`` lifecycle
        calls. Each step is individually guarded so one failure does not
        prevent the remaining enforcement actions.

        When ``terminal=True`` (subscription canceled), credentials are
        fully removed. Otherwise (suspended) they're flagged with a single
        ``Auth-Type := Reject`` row, so unblock is a single DELETE rather
        than a full credential rebuild."""
        subscription = db.get(Subscription, subscription_id)
        errors: list[str] = []

        # RADIUS reject IP is source state for the projection.
        try:
            radius_reject_service.enforce_subscription_reject_ip(
                db, str(subscription_id), reject_reason=reject_reason
            )
        except Exception as exc:
            logger.error(
                "Failed to apply RADIUS reject for subscription %s: %s",
                subscription_id,
                exc,
            )
            errors.append(f"reject_state:{exc}")

        # Materialize all configured targets synchronously. Session CoA is a
        # consequence and must not run after a partial projection.
        projection_ready = False
        if subscription:
            try:
                result = radius_service.reconcile_subscription_connectivity(
                    db, str(subscription_id)
                )
                projection_ready = result.ok
                if not projection_ready:
                    errors.append(f"radius_projection:{result.disposition.value}")
            except Exception as exc:
                logger.error(
                    "Failed to project blocked RADIUS state for subscription %s: %s",
                    subscription_id,
                    exc,
                )
                errors.append(f"radius_projection:{exc}")

        if projection_ready:
            try:
                self._enqueue_subscription_session_cleanup(
                    str(subscription_id), reason=reason
                )
            except Exception as exc:
                logger.error(
                    "Failed to enqueue session cleanup for subscription %s: %s",
                    subscription_id,
                    exc,
                )
                errors.append(f"session_cleanup_enqueue:{exc}")
        else:
            logger.error(
                "Session cleanup deferred for subscription %s: external RADIUS "
                "projection is incomplete",
                subscription_id,
            )
        _raise_incomplete("subscription_block", errors)

    def _handle_subscription_block(
        self, db: Session, event: Event, reason: str
    ) -> None:
        from app.services.account_lifecycle import compute_account_status

        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            logger.debug("Skipping session disconnect: event missing subscription_id")
            return

        subscription = db.get(Subscription, subscription_id)
        errors: list[str] = []

        # Recompute account status (defensive — lifecycle already called this,
        # but non-migrated callers may emit events without lifecycle).
        if subscription:
            try:
                compute_account_status(db, str(subscription.subscriber_id))
            except ValueError:
                logger.error(
                    "Subscriber not found for subscription %s", subscription_id
                )
                errors.append("account_status:subscriber_not_found")
            except Exception as exc:
                logger.error(
                    "Failed to recompute account status for subscription %s: %s",
                    subscription_id,
                    exc,
                )
                errors.append(f"account_status:{exc}")

        reject_reason = _reject_reason_from_event_payload(event.payload)
        self._enforce_subscription_block(
            db,
            str(subscription_id),
            reason=reason,
            reject_reason=reject_reason,
            terminal=(reason == "canceled"),
        )
        _raise_incomplete("subscription_status", errors)

    def _handle_subscription_cancel(self, db: Session, event: Event) -> None:
        self._handle_subscription_block(db, event, "canceled")
        self._stage_local_secret_retirement(db, event)

    def _stage_local_secret_retirement(self, db: Session, event: Event) -> None:
        """Retire a shadowing NAS-local secret once cancellation has projected.

        Ordering matters: ``_handle_subscription_block`` raises through
        ``_raise_incomplete`` when the terminal RADIUS projection did not
        complete, so reaching this line means RADIUS has stopped serving the
        login and its absence is expected rather than a hazard.

        The retirement itself is best-effort by design. Cancellation is
        authoritative and already decided; a NAS that is unreachable must leave
        a retryable, visible ``NetworkOperation`` failure rather than roll the
        lifecycle transition back.
        """
        from app.services.nas.local_secret_policy import stage_terminal_retirement

        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            return
        stage_terminal_retirement(
            db,
            subscription_id=str(subscription_id),
            event_reference=str(getattr(event, "id", "") or "subscription.canceled"),
        )

    def _handle_subscription_restore(self, db: Session, event: Event) -> None:
        from app.services.account_lifecycle import compute_account_status

        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            return
        refresh_enabled = resolve_session_refresh_policy(db).enabled

        subscription = db.get(Subscription, subscription_id)
        errors: list[str] = []

        # Recompute account status
        if subscription:
            try:
                compute_account_status(db, str(subscription.subscriber_id))
            except ValueError:
                logger.error(
                    "Subscriber not found for subscription %s", subscription_id
                )
                errors.append("account_status:subscriber_not_found")
            except Exception as exc:
                logger.error(
                    "Failed to recompute account status for subscription %s: %s",
                    subscription_id,
                    exc,
                )
                errors.append(f"account_status:{exc}")

        # Clear desired reject state, then synchronously project every target.
        projection_ready = False
        try:
            radius_reject_service.enforce_subscription_reject_ip(
                db, str(subscription_id)
            )
        except Exception as exc:
            logger.error(
                "Failed to clear RADIUS reject source state for subscription %s: %s",
                subscription_id,
                exc,
            )
            errors.append(f"reject_state:{exc}")
        if subscription:
            try:
                result = radius_service.reconcile_subscription_connectivity(
                    db, str(subscription_id)
                )
                projection_ready = result.ok
                if not projection_ready:
                    errors.append(f"radius_projection:{result.disposition.value}")
            except Exception as exc:
                logger.error(
                    "Failed to project restored RADIUS state for subscription %s: %s",
                    subscription_id,
                    exc,
                )
                errors.append(f"radius_projection:{exc}")

        # Refresh sessions and remove address block
        try:
            if refresh_enabled and projection_ready:
                disconnect_subscription_sessions(
                    db, str(subscription_id), reason="restore"
                )
            if projection_ready:
                remove_subscription_address_list_block(db, str(subscription_id))
            else:
                logger.error(
                    "Restore consequences deferred for subscription %s: external "
                    "RADIUS projection is incomplete",
                    subscription_id,
                )
        except Exception as exc:
            logger.error(
                "Failed to restore sessions for subscription %s: %s",
                subscription_id,
                exc,
            )
            errors.append(f"session_restore:{exc}")
        _raise_incomplete("subscription_restore", errors)

    def _handle_subscription_speed_change(self, db: Session, event: Event) -> None:
        """Handle mid-session speed change via CoA-Update."""
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        if not subscription_id:
            logger.warning(
                "Skipping speed change enforcement: missing subscription_id."
            )
            return
        try:
            projection = radius_service.reconcile_subscription_connectivity(
                db, str(subscription_id)
            )
            if not projection.ok:
                raise RuntimeError("RADIUS projection did not converge")
            updated = update_subscription_sessions(
                db,
                str(subscription_id),
                reason=event.event_type.value,
            )
            logger.info(
                "Speed change enforcement: %s sessions updated for subscription %s.",
                updated,
                subscription_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to apply speed change for subscription %s: %s",
                subscription_id,
                exc,
            )
            raise EnforcementProjectionError(
                f"subscription speed projection failed: {exc}"
            ) from exc

    def _handle_account_throttle(self, db: Session, event: Event) -> None:
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            logger.debug("Skipping throttle enforcement: event missing account_id")
            return
        errors: list[str] = []
        projection_ready = False
        try:
            radius_service.sync_account_credentials_to_radius(db, account_id)
            projection_ready = True
        except Exception as exc:
            logger.error(
                "Failed to project throttle for account %s: %s",
                account_id,
                exc,
            )
            errors.append(f"radius_projection:{exc}")
        refresh_enabled = resolve_session_refresh_policy(db).enabled
        try:
            if refresh_enabled and projection_ready:
                disconnect_account_sessions(db, str(account_id), reason="throttle")
        except Exception as exc:
            logger.error(
                "Failed to disconnect sessions for account %s: %s",
                account_id,
                exc,
            )
            errors.append(f"session_refresh:{exc}")
        _raise_incomplete("account_throttle", errors)

    def _handle_account_unthrottle(self, db: Session, event: Event) -> None:
        """Push the restored profile to RADIUS as promptly as the throttle landed.

        The throttle enqueued a refresh and the release did not, so a customer who
        paid stayed rate-limited until the next scheduled sweep. Mirror it.

        Sessions are refreshed so the new (faster) profile applies to the live
        session rather than only to the next reconnect.
        """
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            logger.debug("Skipping unthrottle enforcement: event missing account_id")
            return
        errors: list[str] = []
        projection_ready = False
        try:
            radius_service.sync_account_credentials_to_radius(db, account_id)
            projection_ready = True
        except Exception as exc:
            logger.error(
                "Failed to project unthrottle for account %s: %s",
                account_id,
                exc,
            )
            errors.append(f"radius_projection:{exc}")
        refresh_enabled = resolve_session_refresh_policy(db).enabled
        try:
            if refresh_enabled and projection_ready:
                disconnect_account_sessions(db, str(account_id), reason="unthrottle")
        except Exception as exc:
            logger.error(
                "Failed to refresh sessions after unthrottle for account %s: %s",
                account_id,
                exc,
            )
            errors.append(f"session_refresh:{exc}")
        _raise_incomplete("account_unthrottle", errors)

    def _handle_usage_exhausted(self, db: Session, event: Event) -> None:
        subscription_id = event.subscription_id or event.payload.get("subscription_id")
        account_id = event.account_id or event.payload.get("account_id")
        if not subscription_id or not account_id:
            logger.warning(
                "Skipping FUP enforcement: event missing subscription_id=%s account_id=%s",
                subscription_id,
                account_id,
            )
            return
        policy = resolve_fup_event_policy(
            db,
            ResolveFupEventPolicy(
                requested_action=parse_fup_action_override(event.payload.get("action"))
            ),
        )
        action = policy.action
        if action is FupEnforcementAction.NONE:
            return

        # Resolve offer_id and rule_id from payload for state tracking
        offer_id = event.payload.get("offer_id")
        rule_id = event.payload.get("rule_id")
        cap_resets_at_raw = event.payload.get("cap_resets_at")
        # Set when a FUP "block" is downgraded to a full suspend (no captive
        # redirect), so the persisted state distinguishes it from a non-payment
        # suspension.
        fup_block_downgraded = False

        fup_access_mode = None
        if action is FupEnforcementAction.BLOCK:
            from app.models.enforcement_lock import AccessRestrictionMode
            from app.models.subscriber import Subscriber
            from app.services.walled_garden_policy import (
                resolve_walled_garden_decision,
            )

            subscriber = db.get(Subscriber, account_id)
            if subscriber is None:
                return
            decision = resolve_walled_garden_decision(
                db,
                subscriber,
                requested_mode=AccessRestrictionMode.captive,
            )
            fup_access_mode = decision.effective_mode
            action = FupEnforcementAction.SUSPEND
            fup_block_downgraded = fup_access_mode == AccessRestrictionMode.hard_reject
            if fup_block_downgraded:
                logger.warning(
                    "fup_block_downgraded_to_suspend subscription=%s account=%s "
                    "rule=%s reason=%s",
                    subscription_id,
                    account_id,
                    rule_id,
                    decision.reason,
                )
        if action is FupEnforcementAction.SUSPEND:
            from app.models.enforcement_lock import (
                AccessRestrictionMode,
                EnforcementReason,
            )
            from app.services.account_lifecycle import suspend_subscription

            fup_source = f"fup_rule:{rule_id}" if rule_id else "fup_exhausted"
            try:
                suspend_subscription(
                    db,
                    str(subscription_id),
                    reason=EnforcementReason.fup,
                    source=fup_source,
                    access_mode=fup_access_mode or AccessRestrictionMode.hard_reject,
                    emit=False,  # prevent re-entrant dispatch
                )
                # Apply RADIUS enforcement directly (emit=False skips the
                # event-driven path that would normally do this).
                self._enforce_subscription_block(
                    db, str(subscription_id), reason="fup_suspend"
                )
                self._persist_fup_state(
                    db,
                    str(subscription_id),
                    offer_id,
                    rule_id,
                    action_status="blocked",
                    evaluated_at=event.occurred_at,
                    cap_resets_at=cap_resets_at_raw,
                    notes=(
                        "FUP cap: block downgraded to suspend (captive not enabled)"
                        if fup_block_downgraded
                        else "FUP suspension applied"
                    ),
                )
            except FupRuntimeStateError:
                raise
            except ValueError as e:
                logger.info(
                    "Skipped FUP suspension for subscription %s: %s",
                    subscription_id,
                    e,
                )
            except Exception as exc:
                logger.error(
                    "Failed to apply FUP suspension for subscription %s: %s",
                    subscription_id,
                    exc,
                )
                raise
            return
        throttle_profile_id = policy.required_throttle_profile_id()
        # Capture the subscriber's current full-speed profile BEFORE the
        # throttle overwrites it, so the period-reset lift can restore it. The
        # offer's effective profile is the durable "should be" value.
        original_profile_id = None
        _sub_for_profile = db.get(Subscription, subscription_id)
        if _sub_for_profile is not None:
            _orig = _resolve_effective_profile(db, _sub_for_profile)
            original_profile_id = str(_orig.id) if _orig else None
        try:
            updated = apply_radius_profile_to_account(
                db, str(account_id), str(throttle_profile_id)
            )
            if updated:
                if policy.refresh_sessions:
                    disconnect_account_sessions(
                        db, str(account_id), reason="fup_throttle"
                    )
                self._persist_fup_state(
                    db,
                    str(subscription_id),
                    offer_id,
                    rule_id,
                    action_status="throttled",
                    evaluated_at=event.occurred_at,
                    throttle_profile_id=str(throttle_profile_id),
                    original_profile_id=original_profile_id,
                    cap_resets_at=cap_resets_at_raw,
                    notes="FUP throttle applied",
                )
        except FupRuntimeStateError:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to apply FUP throttle for subscription %s: %s",
                subscription_id,
                exc,
            )
            raise

    def _persist_fup_state(
        self,
        db: Session,
        subscription_id: str,
        offer_id: str | None,
        rule_id: str | None,
        *,
        action_status: str,
        evaluated_at: datetime,
        throttle_profile_id: str | None = None,
        original_profile_id: str | None = None,
        cap_resets_at: str | None = None,
        notes: str | None = None,
    ) -> None:
        """Persist FUP enforcement state for restart resilience."""
        if not offer_id:
            # Try to resolve from subscription
            subscription = db.get(Subscription, subscription_id)
            if subscription:
                offer_id = str(subscription.offer_id) if subscription.offer_id else None
        if not offer_id:
            raise FupRuntimeStateError(
                code="access.fup_runtime_state.offer_required",
                message="FUP runtime state requires a canonical subscription offer.",
            )
        from app.models.fup_state import FupActionStatus

        status_map = {
            "none": FupActionStatus.none,
            "throttled": FupActionStatus.throttled,
            "blocked": FupActionStatus.blocked,
            "notified": FupActionStatus.notified,
        }
        try:
            parsed_resets_at = (
                datetime.fromisoformat(cap_resets_at) if cap_resets_at else None
            )
            command = ApplyFupRuntimeState(
                subscription_id=UUID(subscription_id),
                offer_id=UUID(offer_id),
                rule_id=UUID(rule_id) if rule_id else None,
                action_status=status_map.get(action_status, FupActionStatus.none),
                throttle_profile_id=(
                    UUID(throttle_profile_id) if throttle_profile_id else None
                ),
                original_profile_id=(
                    UUID(original_profile_id) if original_profile_id else None
                ),
                cap_resets_at=parsed_resets_at,
                evaluated_at=evaluated_at,
                notes=notes,
            )
        except FupRuntimeStateError:
            raise
        except (TypeError, ValueError) as exc:
            raise FupRuntimeStateError(
                code="access.fup_runtime_state.invalid_event_evidence",
                message="FUP runtime event evidence is invalid.",
            ) from exc
        fup_enforcement.stage_fup_runtime_state(db, command)

    def _handle_payment_received(self, db: Session, event: Event) -> None:
        """Submit payment observation to the financial-access reconciler."""
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            return
        from app.services import collections as collections_service

        invoice_id = event.payload.get("invoice_id")
        restored = collections_service.restore_account_services(
            db,
            str(account_id),
            invoice_id=str(invoice_id) if invoice_id else None,
        )
        from app.services.account_lifecycle import compute_account_status

        compute_account_status(db, str(account_id))
        if restored:
            logger.info(
                "Auto-restored %d subscription(s) for account %s after payment",
                restored,
                account_id,
            )

    def _handle_invoice_overdue(self, db: Session, event: Event) -> None:
        """Treat invoice-overdue as observation; dunning owns every action."""
        account_id = event.account_id or event.payload.get("account_id")
        if not account_id:
            return
        logger.info(
            "Invoice overdue observed for account %s; dunning policy owns "
            "notification, throttle, suspension, and rejection",
            account_id,
        )
