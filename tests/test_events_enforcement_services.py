"""Tests for the event system, enforcement service, and provisioning adapters.

Covers:
- Event types enum/constants
- Event dataclass serialization
- Event dispatcher: register_handler, dispatch, retry_event
- Enforcement handler: subscription block/restore, throttle, usage exhausted
- Lifecycle handler: subscription lifecycle events
- Notification handler: template lookup and notification creation
- Webhook handler: delivery creation and queuing
- Provisioning handler: auto IP allocation, service order provisioning
- Provisioning adapters: UnsupportedProvisioner, register/get provisioner
- Enforcement service helpers: _setting_bool, address list operations
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.models.billing import Invoice, InvoiceStatus, Payment, PaymentStatus
from app.models.catalog import (
    BillingMode,
    NasDevice,
    NasVendor,
    RadiusProfile,
    SubscriptionStatus,
)
from app.models.enforcement_lock import EnforcementLock
from app.models.notification import (
    Notification,
    NotificationChannel,
    NotificationTemplate,
)
from app.models.provisioning import ProvisioningVendor
from app.models.subscriber import SubscriberStatus as AccountStatus
from app.services.enforcement_event_policy import AccessEventPolicyError
from app.services.events.dispatcher import EventDispatcher
from app.services.events.handlers.enforcement import EnforcementHandler
from app.services.events.handlers.lifecycle import LifecycleHandler
from app.services.events.handlers.notification import (
    EVENT_NOTIFICATION_SPECS,
    EVENT_TYPE_TO_TEMPLATE,
    NotificationHandler,
)
from app.services.events.handlers.provisioning import ProvisioningHandler
from app.services.events.handlers.webhook import WebhookHandler
from app.services.events.types import (
    SUBSCRIPTION_LIFECYCLE_MAP,
    Event,
    EventType,
)
from app.services.provisioning_adapters import (
    ProvisioningResult,
    UnsupportedProvisioner,
    _resolve_connection,
    get_provisioner,
    register_provisioner,
)
from app.services.radius import (
    ConnectivityProjectionDisposition,
    SubscriptionConnectivityOutcome,
)

# ---------------------------------------------------------------------------
# EventType enum tests
# ---------------------------------------------------------------------------


class TestEventType:
    def test_subscriber_events_exist(self):
        assert EventType.subscriber_created.value == "subscriber.created"
        assert EventType.subscriber_updated.value == "subscriber.updated"
        assert EventType.subscriber_suspended.value == "subscriber.suspended"
        assert EventType.subscriber_reactivated.value == "subscriber.reactivated"
        assert EventType.subscriber_throttled.value == "subscriber.throttled"

    def test_subscription_events_exist(self):
        assert EventType.subscription_created.value == "subscription.created"
        assert EventType.subscription_activated.value == "subscription.activated"
        assert EventType.subscription_suspended.value == "subscription.suspended"
        assert EventType.subscription_resumed.value == "subscription.resumed"
        assert EventType.subscription_canceled.value == "subscription.canceled"
        assert EventType.subscription_disabled.value == "subscription.disabled"
        assert EventType.subscription_upgraded.value == "subscription.upgraded"
        assert EventType.subscription_downgraded.value == "subscription.downgraded"
        assert EventType.subscription_expiring.value == "subscription.expiring"
        assert EventType.subscription_expired.value == "subscription.expired"

    def test_billing_events_exist(self):
        assert EventType.invoice_created.value == "invoice.created"
        assert EventType.invoice_paid.value == "invoice.paid"
        assert EventType.payment_received.value == "payment.received"
        assert EventType.payment_failed.value == "payment.failed"
        assert EventType.payment_reversed.value == "payment.reversed"
        assert EventType.prepaid_service_renewed.value == "prepaid_service.renewed"

    def test_usage_events_exist(self):
        assert EventType.usage_recorded.value == "usage.recorded"
        assert EventType.usage_warning.value == "usage.warning"
        assert EventType.usage_exhausted.value == "usage.exhausted"
        assert EventType.usage_topped_up.value == "usage.topped_up"

    def test_provisioning_events_exist(self):
        assert EventType.provisioning_started.value == "provisioning.started"
        assert EventType.provisioning_completed.value == "provisioning.completed"
        assert EventType.provisioning_failed.value == "provisioning.failed"

    def test_network_events_exist(self):
        assert EventType.device_offline.value == "device.offline"
        assert EventType.device_online.value == "device.online"

    def test_custom_event_type(self):
        assert EventType.custom.value == "custom"

    def test_event_type_from_value(self):
        assert EventType("subscriber.created") == EventType.subscriber_created

    def test_event_type_invalid_value_raises(self):
        with pytest.raises(ValueError):
            EventType("nonexistent.event")

    def test_subscription_lifecycle_map_keys(self):
        expected_keys = {
            EventType.subscription_activated,
            EventType.subscription_suspended,
            EventType.subscription_resumed,
            EventType.subscription_disabled,
            EventType.subscription_canceled,
            EventType.subscription_upgraded,
            EventType.subscription_downgraded,
            EventType.subscription_expired,
        }
        assert set(SUBSCRIPTION_LIFECYCLE_MAP.keys()) == expected_keys

    def test_subscription_lifecycle_map_values(self):
        assert (
            SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_activated] == "activate"
        )
        assert SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_suspended] == "suspend"
        assert SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_resumed] == "resume"
        assert SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_disabled] == "other"
        assert SUBSCRIPTION_LIFECYCLE_MAP[EventType.subscription_canceled] == "cancel"


# ---------------------------------------------------------------------------
# Event dataclass tests
# ---------------------------------------------------------------------------


class TestEventDataclass:
    def test_event_defaults(self):
        event = Event(
            event_type=EventType.subscriber_created,
            payload={"name": "Test"},
        )
        assert event.event_type == EventType.subscriber_created
        assert event.payload == {"name": "Test"}
        assert isinstance(event.event_id, uuid.UUID)
        assert isinstance(event.occurred_at, datetime)
        assert event.actor is None
        assert event.subscriber_id is None

    def test_event_with_context(self):
        sub_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        event = Event(
            event_type=EventType.subscription_activated,
            payload={"plan": "basic"},
            actor="admin@example.com",
            subscriber_id=sub_id,
            account_id=acc_id,
        )
        assert event.actor == "admin@example.com"
        assert event.subscriber_id == sub_id
        assert event.account_id == acc_id

    def test_event_to_dict(self):
        event_id = uuid.uuid4()
        sub_id = uuid.uuid4()
        event = Event(
            event_type=EventType.invoice_created,
            payload={"amount": 100, "nested": {"id": uuid.uuid4()}},
            event_id=event_id,
            subscriber_id=sub_id,
            actor="system",
        )
        result = event.to_dict()
        assert result["event_id"] == str(event_id)
        assert result["event_type"] == "invoice.created"
        assert isinstance(result["occurred_at"], str)
        assert result["payload"]["amount"] == 100
        # Nested UUIDs should be serialized
        assert isinstance(result["payload"]["nested"]["id"], str)
        assert result["context"]["actor"] == "system"
        assert result["context"]["subscriber_id"] == str(sub_id)
        assert result["context"]["account_id"] is None

    def test_event_to_dict_with_list_payload(self):
        event = Event(
            event_type=EventType.custom,
            payload={"items": [uuid.uuid4(), uuid.uuid4()]},
        )
        result = event.to_dict()
        for item in result["payload"]["items"]:
            assert isinstance(item, str)


# ---------------------------------------------------------------------------
# EventDispatcher tests
# ---------------------------------------------------------------------------


class TestEventDispatcher:
    def test_register_handler(self):
        dispatcher = EventDispatcher()
        handler = MagicMock()
        dispatcher.register_handler(handler)
        assert handler in dispatcher._handlers

    def test_register_multiple_handlers(self):
        dispatcher = EventDispatcher()
        h1, h2, h3 = MagicMock(), MagicMock(), MagicMock()
        dispatcher.register_handler(h1)
        dispatcher.register_handler(h2)
        dispatcher.register_handler(h3)
        assert len(dispatcher._handlers) == 3

    def test_dispatch_calls_all_handlers(self, db_session):
        dispatcher = EventDispatcher()
        h1 = MagicMock()
        h2 = MagicMock()
        dispatcher.register_handler(h1)
        dispatcher.register_handler(h2)

        event = Event(
            event_type=EventType.subscriber_created,
            payload={"name": "Test"},
        )
        mock_db = MagicMock()
        dispatcher.dispatch(mock_db, event)

        h1.handle.assert_called_once_with(mock_db, event)
        h2.handle.assert_called_once_with(mock_db, event)

    def test_dispatch_handler_failure_does_not_stop_others(self, db_session):
        dispatcher = EventDispatcher()
        h1 = MagicMock()
        h1.handle.side_effect = RuntimeError("handler 1 failed")
        h1.__class__.__name__ = "FailingHandler"
        h2 = MagicMock()
        dispatcher.register_handler(h1)
        dispatcher.register_handler(h2)

        event = Event(
            event_type=EventType.subscriber_created,
            payload={},
        )
        mock_db = MagicMock()
        # Should not raise even though h1 fails
        dispatcher.dispatch(mock_db, event)

        # h2 should still be called
        h2.handle.assert_called_once_with(mock_db, event)

    def test_dispatch_persists_event_record(self, db_session):
        dispatcher = EventDispatcher()
        event = Event(
            event_type=EventType.payment_received,
            payload={"amount": 50},
        )
        mock_db = MagicMock()
        dispatcher.dispatch(mock_db, event)
        # Verify db.add was called (event persistence)
        mock_db.add.assert_called_once()

    def test_dispatch_pending_event_claims_existing_record(self, db_session):
        from app.models.event_store import EventStatus
        from app.services import event_store as event_store_service

        dispatcher = EventDispatcher()
        event = Event(event_type=EventType.custom, payload={"source": "outbox"})
        record = event_store_service.create_event_record(
            db_session,
            event,
            status=EventStatus.pending,
        )
        db_session.commit()

        assert dispatcher.dispatch_pending_event(db_session, record.id) is True
        db_session.commit()

        db_session.refresh(record)
        assert record.status == EventStatus.completed
        assert dispatcher.dispatch_pending_event(db_session, record.id) is False

    def test_dispatch_logs_structured_lifecycle(self, db_session, caplog):
        dispatcher = EventDispatcher()
        handler = MagicMock()
        dispatcher.register_handler(handler)

        event = Event(
            event_type=EventType.payment_received,
            payload={"amount": 50},
        )
        mock_db = MagicMock()

        caplog.set_level("INFO")
        dispatcher.dispatch(mock_db, event)

        start_record = next(
            record
            for record in caplog.records
            if record.getMessage() == "event_dispatch_start"
        )
        complete_record = next(
            record
            for record in caplog.records
            if record.getMessage() == "event_dispatch_complete"
        )

        assert start_record.event_id == str(event.event_id)
        assert start_record.event_type == EventType.payment_received.value
        assert start_record.handler_count == 1
        assert complete_record.failed_handler_count == 0

    def test_retry_event_calls_only_failed_handlers(self, db_session):
        dispatcher = EventDispatcher()
        h1 = MagicMock()
        h1.__class__ = type(
            "SuccessHandler", (), {"__name__": "SuccessHandler", "handle": h1.handle}
        )
        h1.__class__.__name__ = "SuccessHandler"
        h2 = MagicMock()
        h2.__class__ = type(
            "FailedHandler", (), {"__name__": "FailedHandler", "handle": h2.handle}
        )
        h2.__class__.__name__ = "FailedHandler"
        dispatcher.register_handler(h1)
        dispatcher.register_handler(h2)

        # Simulate a stored event record with a failure
        event_record = MagicMock()
        event_record.event_id = uuid.uuid4()
        event_record.event_type = "subscriber.created"
        event_record.payload = {"name": "retry test"}
        event_record.actor = None
        event_record.subscriber_id = None
        event_record.account_id = None
        event_record.subscription_id = None
        event_record.invoice_id = None
        event_record.service_order_id = None
        event_record.failed_handlers = [{"handler": "FailedHandler", "error": "boom"}]
        event_record.retry_count = 0

        mock_db = MagicMock()
        result = dispatcher.retry_event(mock_db, event_record)

        # Only h2 (FailedHandler) should be retried, not h1
        h1.handle.assert_not_called()
        h2.handle.assert_called_once()
        assert result is True

    def test_retry_event_returns_false_on_failure(self, db_session):
        dispatcher = EventDispatcher()
        h1 = MagicMock()
        h1.__class__.__name__ = "FailingHandler"
        h1.handle.side_effect = RuntimeError("still broken")
        dispatcher.register_handler(h1)

        event_record = MagicMock()
        event_record.event_id = uuid.uuid4()
        event_record.event_type = "subscriber.created"
        event_record.payload = {}
        event_record.actor = None
        event_record.subscriber_id = None
        event_record.account_id = None
        event_record.subscription_id = None
        event_record.invoice_id = None
        event_record.service_order_id = None
        event_record.failed_handlers = [{"handler": "FailingHandler", "error": "boom"}]
        event_record.retry_count = 0

        mock_db = MagicMock()
        result = dispatcher.retry_event(mock_db, event_record)

        assert result is False


# ---------------------------------------------------------------------------
# EnforcementHandler tests
# ---------------------------------------------------------------------------


class TestEnforcementHandler:
    @pytest.fixture(autouse=True)
    def _successful_radius_projection(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.events.handlers.enforcement.radius_service.reconcile_subscription_connectivity",
            lambda _db, subscription_id: SubscriptionConnectivityOutcome(
                subscription_id=str(subscription_id),
                disposition=ConnectivityProjectionDisposition.projected,
                requested_logins=1,
                projected_logins=1,
                projection_targets=1,
            ),
        )

    def _make_event(self, event_type, payload=None, **kwargs):
        return Event(
            event_type=event_type,
            payload=payload or {},
            **kwargs,
        )

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_subscription_suspended_disconnects_and_blocks(
        self, mock_reject_ip, mock_cleanup, db_session, subscription
    ):
        mock_reject_ip.return_value = {"ok": False}
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscription_suspended,
            subscription_id=subscription.id,
        )
        handler.handle(db_session, event)

        mock_cleanup.assert_called_once_with(str(subscription.id), reason="suspended")

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_subscription_block_sets_subscriber_status_suspended(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscription,
        subscriber,
    ):
        mock_reject_ip.return_value = {"ok": False}
        subscription.status = SubscriptionStatus.suspended
        db_session.add(subscription)
        db_session.commit()

        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscription_suspended,
            subscription_id=subscription.id,
        )
        handler.handle(db_session, event)
        db_session.refresh(subscriber)

        assert subscriber.status == AccountStatus.suspended

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_subscription_canceled_disconnects_and_blocks(
        self, mock_reject_ip, mock_cleanup, db_session, subscription
    ):
        mock_reject_ip.return_value = {"ok": False}
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscription_canceled,
            subscription_id=subscription.id,
        )
        handler.handle(db_session, event)

        mock_cleanup.assert_called_once_with(str(subscription.id), reason="canceled")

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_subscription_block_uses_payload_fallback(
        self, mock_reject_ip, mock_cleanup, db_session, subscription
    ):
        mock_reject_ip.return_value = {"ok": False}
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscription_suspended,
            payload={"subscription_id": str(subscription.id)},
        )
        handler.handle(db_session, event)
        mock_cleanup.assert_called_once_with(str(subscription.id), reason="suspended")

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_subscription_block_uses_negative_reject_reason_for_dunning(
        self, mock_reject_ip, mock_cleanup, db_session
    ):
        mock_reject_ip.return_value = {"ok": False}
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_suspended,
            subscription_id=sub_id,
            payload={"reason": "dunning"},
        )
        handler.handle(db_session, event)
        mock_reject_ip.assert_called_once_with(
            db_session, str(sub_id), reject_reason="negative"
        )

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_subscription_block_defaults_reject_reason_to_blocked(
        self, mock_reject_ip, mock_cleanup, db_session
    ):
        mock_reject_ip.return_value = {"ok": False}
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_suspended,
            subscription_id=sub_id,
            payload={"reason": "manual"},
        )
        handler.handle(db_session, event)
        mock_reject_ip.assert_called_once_with(
            db_session, str(sub_id), reject_reason="blocked"
        )

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    def test_subscription_block_skips_without_id(self, mock_cleanup, db_session):
        handler = EnforcementHandler()
        event = self._make_event(EventType.subscription_suspended)
        handler.handle(db_session, event)
        mock_cleanup.assert_not_called()

    @patch(
        "app.services.events.handlers.enforcement.remove_subscription_address_list_block"
    )
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_subscription_activated_restores(
        self,
        mock_settings,
        mock_disconnect,
        mock_remove_block,
        db_session,
        subscription,
    ):
        mock_settings.resolve_value.return_value = "true"
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscription_activated,
            subscription_id=subscription.id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_called_once_with(
            db_session, str(subscription.id), reason="restore"
        )
        mock_remove_block.assert_called_once_with(db_session, str(subscription.id))

    @patch(
        "app.services.events.handlers.enforcement.remove_subscription_address_list_block"
    )
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_subscription_resumed_restores(
        self,
        mock_settings,
        mock_disconnect,
        mock_remove_block,
        db_session,
        subscription,
    ):
        mock_settings.resolve_value.return_value = "true"
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscription_resumed,
            subscription_id=subscription.id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_called_once_with(
            db_session, str(subscription.id), reason="restore"
        )
        mock_remove_block.assert_called_once_with(db_session, str(subscription.id))

    @patch(
        "app.services.events.handlers.enforcement.remove_subscription_address_list_block"
    )
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.enforcement_event_policy.settings_spec")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_subscription_restore_sets_subscriber_status_active(
        self,
        mock_reject_ip,
        mock_settings,
        mock_disconnect,
        mock_remove_block,
        db_session,
        subscription,
        subscriber,
    ):
        mock_reject_ip.return_value = {"ok": False}
        mock_settings.resolve_value.return_value = "false"
        subscriber.status = AccountStatus.suspended
        subscription.status = SubscriptionStatus.active
        db_session.add_all([subscriber, subscription])
        db_session.commit()

        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscription_resumed,
            subscription_id=subscription.id,
        )
        handler.handle(db_session, event)
        db_session.refresh(subscriber)

        assert subscriber.status == AccountStatus.active

    @patch(
        "app.services.events.handlers.enforcement.remove_subscription_address_list_block"
    )
    @patch("app.services.events.handlers.enforcement.disconnect_subscription_sessions")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_subscription_restore_skips_disconnect_when_refresh_disabled(
        self,
        mock_settings,
        mock_disconnect,
        mock_remove_block,
        db_session,
        subscription,
    ):
        mock_settings.resolve_value.return_value = "false"
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscription_activated,
            subscription_id=subscription.id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_not_called()
        mock_remove_block.assert_called_once()

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_subscriber_throttled_disconnects_sessions(
        self, mock_settings, mock_disconnect, db_session
    ):
        mock_settings.resolve_value.return_value = "true"
        handler = EnforcementHandler()
        acc_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscriber_throttled,
            account_id=acc_id,
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_called_once_with(
            db_session, str(acc_id), reason="throttle"
        )

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_subscriber_throttled_skips_when_refresh_disabled(
        self, mock_settings, mock_disconnect, db_session
    ):
        mock_settings.resolve_value.return_value = "off"
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.subscriber_throttled,
            account_id=uuid.uuid4(),
        )
        handler.handle(db_session, event)
        mock_disconnect.assert_not_called()

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_subscriber_throttled_skips_without_account_id(
        self, mock_settings, mock_disconnect, db_session
    ):
        handler = EnforcementHandler()
        event = self._make_event(EventType.subscriber_throttled)
        handler.handle(db_session, event)
        mock_disconnect.assert_not_called()

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_raw_optin_without_policy_evidence_hard_blocks(
        self, mock_settings, mock_cleanup, db_session, subscription
    ):
        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "block"
            if key == "group_routing_enabled":
                return False
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect
        # The raw flag is not decision evidence and therefore fails closed.
        from app.models.subscriber import Subscriber

        sub_obj = db_session.get(Subscriber, subscription.subscriber_id)
        sub_obj.captive_redirect_enabled = True
        subscription.status = SubscriptionStatus.active
        db_session.flush()
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
        handler.handle(db_session, event)
        mock_cleanup.assert_called_once_with(str(subscription.id), reason="fup_suspend")
        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_block_action_not_opted_in_hard_blocks(
        self, mock_settings, mock_cleanup, db_session, subscription
    ):
        """Not opted into captive → the FUP 'block' action falls through to a
        hard suspend; no captive address-list is applied (opt-in, not every
        account)."""

        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "block"
            if key == "group_routing_enabled":
                return False
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect
        from app.models.subscriber import Subscriber

        sub_obj = db_session.get(Subscriber, subscription.subscriber_id)
        sub_obj.captive_redirect_enabled = False  # explicit: not opted in
        subscription.status = SubscriptionStatus.active
        db_session.flush()
        handler = EnforcementHandler()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
        handler.handle(db_session, event)
        # Distinguisher: the hard path changes lifecycle status to suspended
        # (→ Auth-Type := Reject via populate). The captive path would leave the
        # status untouched. _persist_fup_state notes are "FUP suspension applied".
        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_payload_block_overrides_global_throttle(
        self, mock_settings, db_session, subscription
    ):
        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "throttle"
            if key == "group_routing_enabled":
                return False
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect
        subscription.status = SubscriptionStatus.active
        db_session.flush()

        handler = EnforcementHandler()
        event = self._make_event(
            EventType.usage_exhausted,
            payload={"action": "block"},
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
        handler.handle(db_session, event)

        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_none_action_is_noop(self, mock_settings, db_session):
        mock_settings.resolve_value.return_value = "none"
        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=sub_id,
            account_id=acc_id,
        )
        # Should not raise or do anything
        handler.handle(db_session, event)

    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_skips_without_ids(self, mock_settings, db_session):
        handler = EnforcementHandler()
        event = self._make_event(EventType.usage_exhausted)
        handler.handle(db_session, event)
        mock_settings.resolve_value.assert_not_called()

    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_suspend_action(
        self, mock_settings, db_session, subscription
    ):
        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "suspend"
            if key == "group_routing_enabled":
                return False
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect

        # Set subscription to active
        subscription.status = SubscriptionStatus.active
        db_session.flush()

        handler = EnforcementHandler()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=subscription.id,
            account_id=subscription.subscriber_id,
        )
        handler.handle(db_session, event)

        # Should have updated status via lifecycle (emit=False, no event recursion)
        db_session.refresh(subscription)
        assert subscription.status == SubscriptionStatus.suspended

        # Should have created an enforcement lock
        from app.models.enforcement_lock import EnforcementReason
        from app.services.account_lifecycle import has_active_lock

        assert has_active_lock(db_session, str(subscription.id), EnforcementReason.fup)

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.events.handlers.enforcement.apply_radius_profile_to_account")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_throttle_action(
        self,
        mock_settings,
        mock_apply_profile,
        mock_disconnect,
        db_session,
        subscription,
    ):
        throttle_profile = RadiusProfile(
            name=f"FUP throttle {uuid.uuid4()}", is_active=True
        )
        db_session.add(throttle_profile)
        db_session.commit()
        throttle_profile_id = throttle_profile.id
        subscription_id = subscription.id
        account_id = subscription.subscriber_id

        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "throttle"
            if key == "fup_throttle_radius_profile_id":
                return str(throttle_profile_id)
            if key == "refresh_sessions_on_profile_change":
                return "true"
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect
        mock_apply_profile.return_value = 1

        handler = EnforcementHandler()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=subscription_id,
            account_id=account_id,
        )
        handler.handle(db_session, event)

        mock_apply_profile.assert_called_once_with(
            db_session, str(account_id), str(throttle_profile_id)
        )
        mock_disconnect.assert_called_once_with(
            db_session, str(account_id), reason="fup_throttle"
        )

    @patch("app.services.events.handlers.enforcement.disconnect_account_sessions")
    @patch("app.services.events.handlers.enforcement.apply_radius_profile_to_account")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_payload_reduce_speed_overrides_global_block(
        self,
        mock_settings,
        mock_apply_profile,
        mock_disconnect,
        db_session,
        subscription,
    ):
        throttle_profile = RadiusProfile(
            name=f"FUP throttle {uuid.uuid4()}", is_active=True
        )
        db_session.add(throttle_profile)
        db_session.commit()
        throttle_profile_id = throttle_profile.id
        subscription_id = subscription.id
        account_id = subscription.subscriber_id

        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "block"
            if key == "fup_throttle_radius_profile_id":
                return str(throttle_profile_id)
            if key == "refresh_sessions_on_profile_change":
                return "true"
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect
        mock_apply_profile.return_value = 1

        handler = EnforcementHandler()
        event = self._make_event(
            EventType.usage_exhausted,
            payload={"action": "reduce_speed"},
            subscription_id=subscription_id,
            account_id=account_id,
        )
        handler.handle(db_session, event)

        mock_apply_profile.assert_called_once_with(
            db_session, str(account_id), str(throttle_profile_id)
        )
        mock_disconnect.assert_called_once_with(
            db_session, str(account_id), reason="fup_throttle"
        )

    @patch("app.services.events.handlers.enforcement.apply_radius_profile_to_account")
    @patch("app.services.enforcement_event_policy.settings_spec")
    def test_usage_exhausted_throttle_without_profile_fails_visibly(
        self, mock_settings, mock_apply_profile, db_session
    ):
        def settings_side_effect(db, domain, key):
            if key == "fup_action":
                return "throttle"
            if key == "fup_throttle_radius_profile_id":
                return None
            return None

        mock_settings.resolve_value.side_effect = settings_side_effect

        handler = EnforcementHandler()
        sub_id = uuid.uuid4()
        acc_id = uuid.uuid4()
        event = self._make_event(
            EventType.usage_exhausted,
            subscription_id=sub_id,
            account_id=acc_id,
        )
        with pytest.raises(AccessEventPolicyError) as captured:
            handler.handle(db_session, event)

        assert captured.value.code == "access.event_policy.throttle_profile_required"
        mock_apply_profile.assert_not_called()

    def test_enforcement_handler_ignores_unrelated_events(self, db_session):
        handler = EnforcementHandler()
        event = self._make_event(EventType.invoice_created)
        # Should simply return without error
        handler.handle(db_session, event)


# ---------------------------------------------------------------------------
# LifecycleHandler tests
# ---------------------------------------------------------------------------


class TestLifecycleHandler:
    def _make_event(self, event_type, payload=None, **kwargs):
        return Event(
            event_type=event_type,
            payload=payload or {},
            **kwargs,
        )

    def test_lifecycle_activate_event_does_not_duplicate_owner_evidence(
        self, db_session, subscription
    ):
        handler = LifecycleHandler()
        event = self._make_event(
            EventType.subscription_activated,
            payload={
                "from_status": "pending",
                "to_status": "active",
                "reason": "payment received",
            },
            subscription_id=subscription.id,
            actor="admin",
        )
        from app.models.lifecycle import SubscriptionLifecycleEvent

        before = (
            db_session.query(SubscriptionLifecycleEvent)
            .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
            .count()
        )
        handler.handle(db_session, event)
        db_session.flush()
        after = (
            db_session.query(SubscriptionLifecycleEvent)
            .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
            .count()
        )
        assert after == before

    def test_lifecycle_suspend_event_does_not_duplicate_owner_evidence(
        self, db_session, subscription
    ):
        handler = LifecycleHandler()
        event = self._make_event(
            EventType.subscription_suspended,
            payload={
                "from_status": "active",
                "to_status": "suspended",
            },
            subscription_id=subscription.id,
        )
        from app.models.lifecycle import SubscriptionLifecycleEvent

        before = (
            db_session.query(SubscriptionLifecycleEvent)
            .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
            .count()
        )
        handler.handle(db_session, event)
        db_session.flush()
        after = (
            db_session.query(SubscriptionLifecycleEvent)
            .filter(SubscriptionLifecycleEvent.subscription_id == subscription.id)
            .count()
        )
        assert after == before

    def test_lifecycle_ignores_non_subscription_events(self, db_session):
        handler = LifecycleHandler()
        event = self._make_event(EventType.invoice_created, payload={})
        # Should not create any records
        handler.handle(db_session, event)

    def test_lifecycle_skips_when_no_subscription_id(self, db_session):
        handler = LifecycleHandler()
        event = self._make_event(
            EventType.subscription_activated,
            payload={"from_status": "pending", "to_status": "active"},
            # No subscription_id
        )
        # Should log warning but not raise
        handler.handle(db_session, event)


# ---------------------------------------------------------------------------
# NotificationHandler tests
# ---------------------------------------------------------------------------


class TestNotificationHandler:
    @pytest.fixture(autouse=True)
    def _route_spec_events_multichannel(self, db_session):
        """Event specs no longer declare channels — communications.channel_policy
        owns selection. These tests exercise the handler's multi-channel fan-out
        (template selection, recipient resolution, suppression), so route the
        spec categories to email+sms via the policy to supply that premise. The
        handler still only creates a row where an active template exists, so a
        test with one template still gets one row.
        """
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType
        from app.services.settings_cache import SettingsCache

        db_session.add_all(
            [
                DomainSetting(
                    domain=SettingDomain.notification,
                    key="notification_channel_policy",
                    value_type=SettingValueType.json,
                    value_json={
                        "categories": {
                            "service": ["email", "sms"],
                            "billing": ["email", "sms"],
                        }
                    },
                    is_active=True,
                ),
                # SMS is retired and therefore disabled by default, and the
                # handler skips a disabled channel regardless of routing. Enable
                # it here so the fan-out under test actually reaches two
                # channels — which also demonstrates the retirement is a
                # reversible config state, not a deletion.
                DomainSetting(
                    domain=SettingDomain.notification,
                    key="sms_enabled",
                    value_type=SettingValueType.boolean,
                    value_text="true",
                    is_active=True,
                ),
            ]
        )
        db_session.commit()
        SettingsCache.invalidate(
            SettingDomain.notification.value, "notification_channel_policy"
        )
        SettingsCache.invalidate(SettingDomain.notification.value, "sms_enabled")

    def _set_customer_balance_notifications(self, db_session, enabled: bool) -> None:
        from app.models.domain_settings import DomainSetting, SettingDomain
        from app.models.subscription_engine import SettingValueType

        db_session.add(
            DomainSetting(
                domain=SettingDomain.billing,
                key="customer_balance_notifications_enabled",
                value_type=SettingValueType.boolean,
                value_text="true" if enabled else "false",
                value_json=enabled,
                is_active=True,
            )
        )
        db_session.commit()

    def test_event_type_to_template_mapping_exists(self):
        expected = {
            EventType.subscriber_updated,
            EventType.subscription_upgraded,
            EventType.subscription_downgraded,
            EventType.subscription_expired,
            EventType.invoice_paid,
            EventType.payment_refunded,
            EventType.payment_reversed,
            EventType.service_order_created,
            EventType.service_order_assigned,
            EventType.service_order_completed,
        }
        assert expected.issubset(set(EVENT_TYPE_TO_TEMPLATE))

    def test_notification_handler_ignores_unmapped_events(self, db_session):
        handler = NotificationHandler()
        event = Event(
            event_type=EventType.device_offline,
            payload={},
        )
        # Should not raise
        handler.handle(db_session, event)

    def test_payment_event_queues_receipt_aware_email(self, db_session, subscriber):
        payment = Payment(
            account_id=subscriber.id,
            amount=18712.50,
            currency="NGN",
            status=PaymentStatus.succeeded,
            paid_at=datetime.now(UTC),
            is_active=True,
        )
        db_session.add_all(
            [
                payment,
                NotificationTemplate(
                    code="payment_received",
                    name="Payment Received",
                    channel=NotificationChannel.email,
                    subject="Payment receipt {receipt_number}",
                    body=("Receipt {receipt_number}. View or download: {receipt_url}"),
                    is_active=True,
                ),
            ]
        )
        db_session.commit()

        NotificationHandler().handle(
            db_session,
            Event(
                event_type=EventType.payment_received,
                payload={"payment_id": str(payment.id), "amount": "18712.50"},
                account_id=subscriber.id,
            ),
        )
        db_session.commit()

        notification = db_session.query(Notification).one()
        receipt_reference = f"#RCP-{payment.id.hex[:8].upper()}"
        assert notification.channel == NotificationChannel.email
        assert notification.subject == f"Payment receipt {receipt_reference}"
        assert receipt_reference in (notification.body or "")
        assert (
            f"https://selfcare.dotmac.io/portal/billing/payments/{payment.id}/receipt"
        ) in (notification.body or "")

    def test_prepaid_renewal_event_uses_owner_renewed_through_date(
        self, db_session, subscriber, subscription
    ):
        template = (
            db_session.query(NotificationTemplate)
            .filter_by(
                code="prepaid_service_renewed",
                channel=NotificationChannel.email,
            )
            .one()
        )
        assert template.is_active is True

        NotificationHandler().handle(
            db_session,
            Event(
                event_type=EventType.prepaid_service_renewed,
                payload={
                    "amount": "18812.50",
                    "currency": "NGN",
                    "renewed_through": "2026-08-20T00:00:00+00:00",
                },
                account_id=subscriber.id,
                subscription_id=subscription.id,
            ),
        )
        db_session.commit()

        notification = (
            db_session.query(Notification)
            .filter_by(event_type="prepaid_service_renewed")
            .one()
        )
        assert notification.subject == (
            f"Your {subscription.offer.name} service is renewed"
        )
        assert "₦18,812.50" in (notification.body or "")
        assert "August 20, 2026" in (notification.body or "")

    def test_notification_handler_uses_fallback_copy_when_no_template(
        self, db_session, subscriber
    ):
        subscriber.phone = "+2348000000099"
        db_session.commit()
        handler = NotificationHandler()
        event = Event(
            event_type=EventType.subscription_upgraded,
            payload={"old_offer_name": "Basic", "new_offer_name": "Pro"},
            subscriber_id=subscriber.id,
        )
        handler.handle(db_session, event)
        db_session.commit()

        notifications = db_session.query(Notification).all()
        assert len(notifications) == 2
        assert {row.channel for row in notifications} == {
            NotificationChannel.email,
            NotificationChannel.sms,
        }
        assert all(row.subscriber_id == subscriber.id for row in notifications)
        assert all(row.event_type == "subscription_upgraded" for row in notifications)
        assert all(row.category == "service" for row in notifications)
        assert any("upgraded" in (row.subject or "").lower() for row in notifications)

    def test_render_subject_with_template(self):
        handler = NotificationHandler()
        template = MagicMock()
        template.subject = "Your {plan_name} subscription is ready"
        spec = EVENT_NOTIFICATION_SPECS[EventType.subscription_activated]
        context = {"plan_name": "Gold", "subscriber_name": "Test"}
        result = handler._render_subject(template, spec, context)
        assert result == "Your Gold subscription is ready"

    def test_render_subject_without_template(self):
        handler = NotificationHandler()
        spec = EVENT_NOTIFICATION_SPECS[EventType.subscription_activated]
        context = {"subscriber_name": "Test"}
        result = handler._render_subject(None, spec, context)
        assert "service is now active" in result.lower()

    def test_render_body_with_template(self):
        handler = NotificationHandler()
        template = MagicMock()
        template.body = "Dear customer, your invoice #{invoice_number} is ready."
        spec = EVENT_NOTIFICATION_SPECS[EventType.invoice_created]
        context = {"invoice_number": "INV-001", "subscriber_name": "Test"}
        result = handler._render_body(template, spec, context)
        assert "INV-001" in result

    def test_render_body_without_template(self):
        handler = NotificationHandler()
        spec = EVENT_NOTIFICATION_SPECS[EventType.invoice_created]
        context = {"amount": "100", "subscriber_name": "Test"}
        result = handler._render_body(None, spec, context)
        assert "invoice" in result.lower()

    def test_resolve_recipient_uses_phone_for_sms_channel(self, db_session, subscriber):
        subscriber.phone = "+2348000000001"
        db_session.commit()

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.subscription_created,
            payload={},
            account_id=subscriber.id,
        )

        recipient = handler._resolve_recipient(
            db_session,
            event,
            NotificationChannel.sms,
        )

        assert recipient == "+2348000000001"

    def test_handle_queues_base_and_sms_templates(self, db_session, subscriber):
        subscriber.phone = "+2348000000002"
        db_session.add_all(
            [
                NotificationTemplate(
                    code="invoice_overdue",
                    name="Invoice Overdue",
                    channel=NotificationChannel.email,
                    subject="Email overdue",
                    body="Email body for {invoice_number}",
                    is_active=True,
                ),
                NotificationTemplate(
                    code="invoice_overdue",
                    name="Invoice Overdue SMS",
                    channel=NotificationChannel.sms,
                    subject=None,
                    body="SMS body for {invoice_number}",
                    is_active=True,
                ),
            ]
        )
        db_session.commit()

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.invoice_overdue,
            payload={
                "invoice_number": "INV-100",
                "amount": "5000",
                "due_date": "2026-07-21",
            },
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        db_session.commit()

        notifications = db_session.query(Notification).all()
        assert len(notifications) == 2
        channels = {row.channel for row in notifications}
        recipients = {row.channel: row.recipient for row in notifications}
        assert channels == {NotificationChannel.email, NotificationChannel.sms}
        assert recipients[NotificationChannel.email] == subscriber.email
        assert recipients[NotificationChannel.sms] == subscriber.phone
        assert all(row.subscriber_id == subscriber.id for row in notifications)
        assert all(row.category == "billing" for row in notifications)

    def test_retired_balance_notification_row_does_not_suppress_debt_events(
        self, db_session, subscriber
    ):
        subscriber.phone = "+2348000000100"
        db_session.commit()
        self._set_customer_balance_notifications(db_session, False)

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.invoice_overdue,
            payload={
                "invoice_number": "INV-100",
                "amount": "5000",
                "due_date": "2026-07-21",
            },
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        db_session.commit()

        assert db_session.query(Notification).count() == 2

    def test_retired_balance_notification_row_keeps_receipts(
        self, db_session, subscriber
    ):
        self._set_customer_balance_notifications(db_session, False)

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.invoice_paid,
            payload={"invoice_number": "INV-200", "amount": "5000"},
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        db_session.commit()

        notifications = db_session.query(Notification).all()
        assert len(notifications) == 1
        assert notifications[0].event_type == "invoice_paid"

    def test_retired_balance_notification_row_keeps_billing_suspension(
        self, db_session, subscriber
    ):
        subscriber.phone = "+2348000000100"
        db_session.commit()
        self._set_customer_balance_notifications(db_session, False)

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.subscription_suspended,
            payload={
                "reason": "overdue",
                "source": "invoice:INV-100",
                "offer_name": "Gold",
            },
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        db_session.commit()

        assert db_session.query(Notification).count() == 2

    def test_retired_balance_notification_row_keeps_admin_suspension(
        self, db_session, subscriber
    ):
        subscriber.phone = "+2348000000101"
        db_session.commit()
        self._set_customer_balance_notifications(db_session, False)

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.subscription_suspended,
            payload={
                "reason": "admin",
                "source": "admin",
                "offer_name": "Gold",
            },
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        db_session.commit()

        notifications = db_session.query(Notification).all()
        assert len(notifications) == 2
        assert {row.channel for row in notifications} == {
            NotificationChannel.email,
            NotificationChannel.sms,
        }

    def test_handle_service_order_notifications(self, db_session, subscriber):
        subscriber.phone = "+2348000000003"
        handler = NotificationHandler()
        event = Event(
            event_type=EventType.service_order_completed,
            payload={"service_order_id": "SO-100"},
            account_id=subscriber.id,
            service_order_id=uuid.uuid4(),
        )

        handler.handle(db_session, event)
        db_session.commit()

        notifications = db_session.query(Notification).all()
        assert len(notifications) == 2
        assert {row.event_type for row in notifications} == {"service_order_completed"}
        assert {row.category for row in notifications} == {"service"}

    def test_handle_respects_billing_notification_preference(
        self, db_session, subscriber
    ):
        subscriber.metadata_ = {"billing_notifications": False}
        db_session.commit()

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.invoice_paid,
            payload={"invoice_number": "INV-200"},
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        db_session.commit()

        assert db_session.query(Notification).count() == 0

    def test_handle_respects_sms_updates_preference(self, db_session, subscriber):
        subscriber.phone = "+2348000000013"
        subscriber.metadata_ = {"sms_updates": False}
        db_session.commit()

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.service_order_assigned,
            payload={"service_order_id": "SO-2"},
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        db_session.commit()

        notifications = db_session.query(Notification).all()
        assert len(notifications) == 1
        assert notifications[0].channel == NotificationChannel.email

    def test_handle_respects_contact_receives_notifications(
        self, db_session, subscriber
    ):
        from app.models.subscriber import SubscriberContact

        contact = SubscriberContact(
            subscriber_id=subscriber.id,
            email="contact@example.com",
            receives_notifications=False,
        )
        db_session.add(contact)
        db_session.commit()

        handler = NotificationHandler()
        event = Event(
            event_type=EventType.service_order_created,
            payload={"email": "contact@example.com", "service_order_id": "SO-1"},
        )

        handler.handle(db_session, event)
        db_session.commit()

        assert db_session.query(Notification).count() == 0

    def test_handle_invoice_overdue_is_observation_only(
        self,
        db_session,
        subscriber,
        monkeypatch,
    ):
        subscriber.status = AccountStatus.active
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number="INV-GRACE-1",
            status=InvoiceStatus.overdue,
            total=100,
            balance_due=100,
            due_at=datetime.now(UTC) - timedelta(hours=6),
            metadata_={},
        )
        db_session.add(invoice)
        db_session.commit()

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.invoice_overdue,
            payload={"invoice_id": str(invoice.id)},
            invoice_id=invoice.id,
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        handler.handle(db_session, event)
        db_session.refresh(invoice)

        assert not (invoice.metadata_ or {}).get("suspension_warning_sent_at")

    def test_handle_invoice_overdue_shield_suppresses_warning(
        self,
        db_session,
        subscriber,
        monkeypatch,
    ):
        subscriber.status = AccountStatus.active
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number="INV-GRACE-2",
            status=InvoiceStatus.overdue,
            total=100,
            balance_due=100,
            due_at=datetime.now(UTC) - timedelta(hours=6),
            metadata_={},
        )
        db_session.add(invoice)
        db_session.commit()

        # The event adapter must not even resolve shields. Dunning owns both
        # notification eligibility and service-access consequences.
        monkeypatch.setattr(
            "app.services.service_extensions.extension_shield_reason",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("event adapter resolved a dunning shield")
            ),
        )

        handler = EnforcementHandler()
        event = Event(
            event_type=EventType.invoice_overdue,
            payload={"invoice_id": str(invoice.id)},
            invoice_id=invoice.id,
            account_id=subscriber.id,
        )

        handler.handle(db_session, event)
        db_session.refresh(invoice)

        assert not (invoice.metadata_ or {}).get("suspension_warning_sent_at")


# ---------------------------------------------------------------------------
# Payment-received restore guard tests
# ---------------------------------------------------------------------------


class TestPaymentReceivedRestoreGuard:
    """Payment events delegate eligibility to the access owner."""

    def _payment_event(self, account_id, invoice_id=None):
        payload = {"amount": "100.00", "status": "succeeded"}
        if invoice_id:
            payload["invoice_id"] = str(invoice_id)
        return Event(
            event_type=EventType.payment_received,
            payload=payload,
            account_id=account_id,
        )

    def _make_invoice(self, db_session, subscriber, **kwargs):
        defaults = {
            "account_id": subscriber.id,
            "invoice_number": f"INV-{uuid.uuid4().hex[:8]}",
            "status": InvoiceStatus.overdue,
            "total": 50000,
            "balance_due": 49900,
            "due_at": datetime.now(UTC) - timedelta(days=3),
            "metadata_": {},
        }
        defaults.update(kwargs)
        invoice = Invoice(**defaults)
        db_session.add(invoice)
        db_session.commit()
        return invoice

    @patch("app.services.collections.restore_account_services")
    def test_partial_payment_is_submitted_to_owner(
        self, mock_restore, db_session, subscriber
    ):
        """The event adapter never decides from its invoice snapshot."""
        mock_restore.return_value = 0
        invoice = self._make_invoice(
            db_session, subscriber, total=50000, balance_due=49900
        )

        handler = EnforcementHandler()
        handler.handle(db_session, self._payment_event(subscriber.id, invoice.id))

        mock_restore.assert_called_once_with(
            db_session, str(subscriber.id), invoice_id=str(invoice.id)
        )

    @patch("app.services.collections.restore_account_services")
    def test_past_due_payment_is_submitted_to_owner(
        self, mock_restore, db_session, subscriber
    ):
        mock_restore.return_value = 0
        invoice = self._make_invoice(
            db_session,
            subscriber,
            status=InvoiceStatus.partially_paid,
            total=50000,
            balance_due=10000,
        )

        handler = EnforcementHandler()
        handler.handle(db_session, self._payment_event(subscriber.id, invoice.id))

        mock_restore.assert_called_once_with(
            db_session, str(subscriber.id), invoice_id=str(invoice.id)
        )

    @patch("app.services.collections.restore_account_services")
    def test_full_clearance_restores(self, mock_restore, db_session, subscriber):
        """No overdue balance left -> restore proceeds."""
        mock_restore.return_value = 1
        invoice = self._make_invoice(
            db_session,
            subscriber,
            status=InvoiceStatus.paid,
            total=50000,
            balance_due=0,
        )

        handler = EnforcementHandler()
        handler.handle(db_session, self._payment_event(subscriber.id, invoice.id))

        mock_restore.assert_called_once_with(
            db_session, str(subscriber.id), invoice_id=str(invoice.id)
        )

    @patch("app.services.collections.restore_account_services")
    def test_prepaid_topup_restore_still_works(
        self, mock_restore, db_session, subscriber
    ):
        """Prepaid suspension scenario (no overdue invoice debt) still restores.

        A prepaid account suspended for balance depletion has no overdue
        invoice with an unpaid balance — only (at most) invoices that are
        not yet due — so a top-up payment.received must keep restoring.
        """
        mock_restore.return_value = 1
        # An open invoice that is NOT yet due must not block the restore.
        self._make_invoice(
            db_session,
            subscriber,
            status=InvoiceStatus.issued,
            total=5000,
            balance_due=5000,
            due_at=datetime.now(UTC) + timedelta(days=14),
        )

        handler = EnforcementHandler()
        handler.handle(db_session, self._payment_event(subscriber.id))

        mock_restore.assert_called_once_with(
            db_session, str(subscriber.id), invoice_id=None
        )

    @patch("app.services.collections.restore_account_services")
    def test_payment_event_recomputes_stale_blocked_account_without_lock(
        self, mock_restore, db_session, subscriber, subscription
    ):
        from app.models.catalog import BillingMode

        mock_restore.return_value = 0
        subscriber.status = AccountStatus.blocked
        subscription.status = SubscriptionStatus.active
        subscription.billing_mode = BillingMode.prepaid
        db_session.commit()

        handler = EnforcementHandler()
        handler.handle(db_session, self._payment_event(subscriber.id))

        mock_restore.assert_called_once_with(
            db_session, str(subscriber.id), invoice_id=None
        )
        db_session.refresh(subscriber)
        assert subscriber.status == AccountStatus.active

    @patch("app.services.collections.restore_account_services")
    def test_restore_failure_propagates_so_payment_event_remains_retryable(
        self, mock_restore, db_session, subscriber
    ):
        mock_restore.side_effect = RuntimeError("financial access projection failed")

        with pytest.raises(RuntimeError, match="financial access projection failed"):
            EnforcementHandler().handle(
                db_session,
                self._payment_event(subscriber.id),
            )

    @patch("app.services.collections.restore_account_services")
    def test_payment_event_without_account_is_ignored(self, mock_restore, db_session):
        handler = EnforcementHandler()
        handler.handle(
            db_session,
            Event(event_type=EventType.payment_received, payload={}),
        )
        mock_restore.assert_not_called()


# ---------------------------------------------------------------------------
# Invoice-overdue suspension shield tests
# ---------------------------------------------------------------------------


class TestInvoiceOverdueObservationBoundary:
    """Invoice-overdue delivery never decides a service-access consequence."""

    def _setup_overdue_account(self, db_session, subscriber, subscription):
        subscriber.status = AccountStatus.active
        subscriber.billing_mode = BillingMode.prepaid
        subscription.status = SubscriptionStatus.active
        invoice = Invoice(
            account_id=subscriber.id,
            invoice_number=f"INV-{uuid.uuid4().hex[:8]}",
            status=InvoiceStatus.overdue,
            total=10000,
            balance_due=10000,
            due_at=datetime.now(UTC) - timedelta(hours=72),
            metadata_={"suspension_warning_sent_at": "2026-01-01T00:00:00+00:00"},
        )
        db_session.add(invoice)
        db_session.commit()
        return invoice

    def _overdue_event(self, subscriber, invoice):
        return Event(
            event_type=EventType.invoice_overdue,
            payload={"invoice_id": str(invoice.id)},
            invoice_id=invoice.id,
            account_id=subscriber.id,
        )

    def _make_arrangement(self, db_session, subscriber, status):
        from datetime import date

        from app.models.payment_arrangement import PaymentArrangement

        arrangement = PaymentArrangement(
            subscriber_id=subscriber.id,
            total_amount=10000,
            installment_amount=2500,
            installments_total=4,
            start_date=date.today(),
            status=status,
        )
        db_session.add(arrangement)
        db_session.commit()
        return arrangement

    def _make_proof(self, db_session, subscriber, status):
        from app.models.payment_proof import PaymentProof

        proof = PaymentProof(
            account_id=subscriber.id,
            amount=10000,
            file_path="/uploads/proofs/test.pdf",
            status=status,
        )
        db_session.add(proof)
        db_session.commit()
        return proof

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_active_arrangement_shields_from_suspension(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscriber,
        subscription,
    ):
        from app.models.payment_arrangement import ArrangementStatus

        mock_reject_ip.return_value = {"ok": False}
        invoice = self._setup_overdue_account(db_session, subscriber, subscription)
        self._make_arrangement(db_session, subscriber, ArrangementStatus.active)

        EnforcementHandler().handle(
            db_session, self._overdue_event(subscriber, invoice)
        )
        db_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.active
        mock_cleanup.assert_not_called()

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_defaulted_arrangement_does_not_directly_suspend(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscriber,
        subscription,
    ):
        from app.models.payment_arrangement import ArrangementStatus

        mock_reject_ip.return_value = {"ok": False}
        invoice = self._setup_overdue_account(db_session, subscriber, subscription)
        self._make_arrangement(db_session, subscriber, ArrangementStatus.defaulted)

        EnforcementHandler().handle(
            db_session, self._overdue_event(subscriber, invoice)
        )
        db_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.active
        mock_reject_ip.assert_not_called()
        mock_cleanup.assert_not_called()

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_covering_prepaid_credit_shields_from_suspension(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscriber,
        subscription,
    ):
        """A prepaid account whose wallet/ledger credit covers the overdue debt
        must NOT be suspended by the overdue event path — the same balance gate
        the dunning reconciler applies. Regression for the ungated 2nd writer
        that cut off credited customers (e.g. 100008817, ₦702k credit)."""
        from decimal import Decimal

        from app.models.billing import (
            LedgerEntry,
            LedgerEntryType,
            LedgerSource,
        )

        mock_reject_ip.return_value = {"ok": False}
        invoice = self._setup_overdue_account(db_session, subscriber, subscription)
        # Unallocated credit of 15,000 covers the 10,000 overdue invoice.
        db_session.add(
            LedgerEntry(
                account_id=subscriber.id,
                invoice_id=None,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("15000"),
                currency="NGN",
                is_active=True,
            )
        )
        db_session.commit()

        EnforcementHandler().handle(
            db_session, self._overdue_event(subscriber, invoice)
        )
        db_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.active
        mock_cleanup.assert_not_called()

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_insufficient_prepaid_credit_does_not_directly_suspend(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscriber,
        subscription,
    ):
        """The overdue event writer never cuts service; dunning owns that."""
        from decimal import Decimal

        from app.models.billing import (
            LedgerEntry,
            LedgerEntryType,
            LedgerSource,
        )

        mock_reject_ip.return_value = {"ok": False}
        invoice = self._setup_overdue_account(db_session, subscriber, subscription)
        db_session.add(
            LedgerEntry(
                account_id=subscriber.id,
                invoice_id=None,
                entry_type=LedgerEntryType.credit,
                source=LedgerSource.payment,
                amount=Decimal("3000"),
                currency="NGN",
                is_active=True,
            )
        )
        db_session.commit()

        EnforcementHandler().handle(
            db_session, self._overdue_event(subscriber, invoice)
        )
        db_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.active
        mock_reject_ip.assert_not_called()
        mock_cleanup.assert_not_called()

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_pending_payment_proof_shields_from_suspension(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscriber,
        subscription,
    ):
        from app.models.payment_proof import PaymentProofStatus

        mock_reject_ip.return_value = {"ok": False}
        invoice = self._setup_overdue_account(db_session, subscriber, subscription)
        self._make_proof(db_session, subscriber, PaymentProofStatus.submitted)

        EnforcementHandler().handle(
            db_session, self._overdue_event(subscriber, invoice)
        )
        db_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.active
        mock_cleanup.assert_not_called()

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_rejected_proof_does_not_directly_suspend(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscriber,
        subscription,
    ):
        from app.models.payment_proof import PaymentProofStatus

        mock_reject_ip.return_value = {"ok": False}
        invoice = self._setup_overdue_account(db_session, subscriber, subscription)
        self._make_proof(db_session, subscriber, PaymentProofStatus.rejected)

        EnforcementHandler().handle(
            db_session, self._overdue_event(subscriber, invoice)
        )
        db_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.active
        mock_reject_ip.assert_not_called()
        mock_cleanup.assert_not_called()

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_no_shield_does_not_directly_suspend_past_grace(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscriber,
        subscription,
    ):
        mock_reject_ip.return_value = {"ok": False}
        invoice = self._setup_overdue_account(db_session, subscriber, subscription)

        EnforcementHandler().handle(
            db_session, self._overdue_event(subscriber, invoice)
        )
        db_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.active
        mock_reject_ip.assert_not_called()
        mock_cleanup.assert_not_called()

    @patch("app.tasks.enforcement.cleanup_subscription_block_sessions.delay")
    @patch(
        "app.services.events.handlers.enforcement.radius_reject_service.enforce_subscription_reject_ip"
    )
    def test_postpaid_overdue_does_not_directly_suspend_before_dunning_policy(
        self,
        mock_reject_ip,
        mock_cleanup,
        db_session,
        subscriber,
        subscription,
    ):
        invoice = self._setup_overdue_account(db_session, subscriber, subscription)
        subscriber.billing_mode = BillingMode.postpaid
        subscription.status = SubscriptionStatus.active
        db_session.commit()

        EnforcementHandler().handle(
            db_session, self._overdue_event(subscriber, invoice)
        )
        db_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.active
        assert db_session.query(EnforcementLock).count() == 0
        mock_reject_ip.assert_not_called()
        mock_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# WebhookHandler tests
# ---------------------------------------------------------------------------


class TestWebhookHandler:
    def test_webhook_delivery_scope_is_subscription_driven(self):
        from app.services.control_relationships import handler_event_types

        assert handler_event_types("WebhookHandler") is None

    def test_webhook_handler_ignores_unmapped_events(self, db_session):
        handler = WebhookHandler()
        # dunning events are not in the mapping
        event = Event(
            event_type=EventType.dunning_started,
            payload={},
        )
        handler.handle(db_session, event)

    def test_webhook_handler_no_subscriptions(self, db_session):
        handler = WebhookHandler()
        event = Event(
            event_type=EventType.subscriber_created,
            payload={"name": "Test"},
        )
        # No webhook subscriptions in DB - should silently return
        handler.handle(db_session, event)


# ---------------------------------------------------------------------------
# ProvisioningHandler tests
# ---------------------------------------------------------------------------


class TestProvisioningHandler:
    def _make_event(self, event_type, payload=None, **kwargs):
        return Event(
            event_type=event_type,
            payload=payload or {},
            **kwargs,
        )

    @patch.object(ProvisioningHandler, "_push_nas_provisioning")
    @patch.object(ProvisioningHandler, "_sync_radius_on_activation")
    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_subscription_activated_triggers_ip_allocation(
        self, mock_prov_svc, _mock_radius, _mock_nas, db_session
    ):
        handler = ProvisioningHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_activated,
            subscription_id=sub_id,
        )
        handler.handle(db_session, event)
        mock_prov_svc.ensure_ip_assignments_for_subscription.assert_called_once_with(
            db_session, str(sub_id)
        )

    @patch.object(ProvisioningHandler, "_push_nas_provisioning")
    @patch.object(ProvisioningHandler, "_sync_radius_on_activation")
    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_subscription_activated_uses_payload_fallback(
        self, mock_prov_svc, _mock_radius, _mock_nas, db_session
    ):
        handler = ProvisioningHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_activated,
            payload={"subscription_id": str(sub_id)},
        )
        handler.handle(db_session, event)
        mock_prov_svc.ensure_ip_assignments_for_subscription.assert_called_once_with(
            db_session, str(sub_id)
        )

    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_subscription_activated_skips_without_id(self, mock_prov_svc, db_session):
        handler = ProvisioningHandler()
        event = self._make_event(EventType.subscription_activated)
        handler.handle(db_session, event)
        mock_prov_svc.ensure_ip_assignments_for_subscription.assert_not_called()

    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_subscription_activated_handles_failure_gracefully(
        self, mock_prov_svc, db_session
    ):
        mock_prov_svc.ensure_ip_assignments_for_subscription.side_effect = RuntimeError(
            "IP pool exhausted"
        )
        handler = ProvisioningHandler()
        sub_id = uuid.uuid4()
        event = self._make_event(
            EventType.subscription_activated,
            subscription_id=sub_id,
        )
        # Projection failures propagate so the event stays retryable and the
        # service order is never confirmed on a failed projection.
        with pytest.raises(RuntimeError, match="IP pool exhausted"):
            handler.handle(db_session, event)

    def test_provisioning_handler_ignores_unrelated_events(self, db_session):
        handler = ProvisioningHandler()
        event = self._make_event(EventType.invoice_created)
        handler.handle(db_session, event)

    @patch("app.services.events.handlers.provisioning.provisioning_service")
    def test_service_order_assigned_skips_without_id(self, mock_prov_svc, db_session):
        handler = ProvisioningHandler()
        event = self._make_event(EventType.service_order_assigned)
        handler.handle(db_session, event)
        mock_prov_svc.resolve_workflow_for_service_order.assert_not_called()


# ---------------------------------------------------------------------------
# Provisioning Adapters tests
# ---------------------------------------------------------------------------


class TestProvisioningAdapters:
    def test_unsupported_provisioner_assign_ont(self):
        provisioner = UnsupportedProvisioner(ProvisioningVendor.other)
        result = provisioner.assign_ont({}, {"key": "value"})
        assert isinstance(result, ProvisioningResult)
        assert result.status == "failed"
        assert "not supported" in str(result.detail)
        assert result.payload == {"vendor": "other", "supported": False}

    def test_unsupported_provisioner_push_config(self):
        provisioner = UnsupportedProvisioner(ProvisioningVendor.other)
        result = provisioner.push_config({}, None)
        assert result.status == "failed"
        assert "not supported" in str(result.detail)

    def test_unsupported_provisioner_confirm_up(self):
        provisioner = UnsupportedProvisioner(ProvisioningVendor.other)
        result = provisioner.confirm_up({"ctx": True}, None)
        assert result.status == "failed"
        assert "not supported" in str(result.detail)

    def test_get_provisioner_returns_registered(self):
        provisioner = UnsupportedProvisioner(ProvisioningVendor.nokia)
        register_provisioner(provisioner)
        result = get_provisioner(ProvisioningVendor.nokia)
        assert result is provisioner

    def test_get_provisioner_returns_unsupported_for_unknown(self):
        # Use a vendor that we intentionally de-register
        from app.services.provisioning_adapters import _PROVISIONERS

        # Nokia is registered above, but "other" may not be
        original = _PROVISIONERS.pop(ProvisioningVendor.other, None)
        try:
            provisioner = get_provisioner(ProvisioningVendor.other)
            assert isinstance(provisioner, UnsupportedProvisioner)
            assert provisioner.vendor == ProvisioningVendor.other
        finally:
            if original is not None:
                _PROVISIONERS[ProvisioningVendor.other] = original

    def test_get_provisioner_mikrotik(self):
        from app.services.provisioning_adapters import MikrotikProvisioner

        provisioner = get_provisioner(ProvisioningVendor.mikrotik)
        assert isinstance(provisioner, MikrotikProvisioner)

    def test_get_provisioner_huawei(self):
        from app.services.provisioning_adapters import HuaweiProvisioner

        provisioner = get_provisioner(ProvisioningVendor.huawei)
        assert isinstance(provisioner, HuaweiProvisioner)

    def test_huawei_mutating_step_rejects_read_only_filter(self):
        from app.services.provisioning_adapters import HuaweiProvisioner

        result = HuaweiProvisioner().push_config(
            {"connector": {"auth_config": {"host": "olt.example"}}},
            {"get_filter": "<filter/>"},
        )

        assert result.success is False
        assert result.status == "failed"
        assert "read-only get_filter" in (result.detail or "")

    def test_get_provisioner_zte_is_explicitly_unsupported(self):
        from app.services.provisioning_adapters import ZteProvisioner

        provisioner = get_provisioner(ProvisioningVendor.zte)
        assert isinstance(provisioner, ZteProvisioner)
        result = provisioner.push_config({}, {})
        assert result.success is False
        assert "not supported" in (result.detail or "")

    def test_get_provisioner_genieacs(self):
        from app.services.provisioning_adapters import GenieACSProvisioner

        provisioner = get_provisioner(ProvisioningVendor.genieacs)
        assert isinstance(provisioner, GenieACSProvisioner)

    def test_resolve_connection_from_connector(self):
        context = {
            "connector": {
                "base_url": "https://router.local:8728",
                "auth_config": {
                    "username": "admin",
                    "password": "secret",
                },
            },
        }
        conn = _resolve_connection(context)
        assert conn["host"] == "router.local"
        assert conn["port"] == 8728
        assert conn["username"] == "admin"
        assert conn["password"] == "secret"

    def test_resolve_connection_empty_context(self):
        conn = _resolve_connection({})
        assert conn["host"] is None
        assert conn["username"] is None

    def test_resolve_connection_explicit_host(self):
        context = {
            "connector": {
                "auth_config": {
                    "host": "10.0.0.1",
                    "port": 22,
                    "username": "root",
                },
            },
        }
        conn = _resolve_connection(context)
        assert conn["host"] == "10.0.0.1"
        assert conn["port"] == 22

    def test_provisioning_result_fields(self):
        result = ProvisioningResult(
            status="ok",
            detail="test",
            payload={"key": "value"},
        )
        assert result.status == "ok"
        assert result.success is True
        assert result.message == "test"
        assert result.data == {"key": "value"}
        assert result.detail == "test"
        assert result.payload == {"key": "value"}

    def test_provisioning_result_defaults(self):
        result = ProvisioningResult(status="error")
        assert result.success is False
        assert result.message == "error"
        assert result.detail is None
        assert result.payload is None


# ---------------------------------------------------------------------------
# Enforcement service helper tests
# ---------------------------------------------------------------------------


class TestEnforcementServiceHelpers:
    def test_apply_mikrotik_address_list_add(self, monkeypatch):
        commands: list[str] = []

        def _fake_ssh(_device, command):
            commands.append(command)

        monkeypatch.setattr(
            "app.services.enforcement.DeviceProvisioner._execute_ssh", _fake_ssh
        )
        from app.services.enforcement import _apply_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.mikrotik)
        result = _apply_mikrotik_address_list(device, "blocked", "10.0.0.1")
        assert result is True
        assert len(commands) == 1
        # Conditional add so re-blocking is a no-op instead of an error.
        assert commands[0].startswith(":if ([:len [/ip firewall address-list find")
        assert 'list="blocked"' in commands[0]
        assert 'address="10.0.0.1"' in commands[0]
        assert "do={/ip firewall address-list add" in commands[0]

    def test_apply_mikrotik_address_list_idempotent_on_repeat(self, monkeypatch):
        """RouterOS executes the conditional locally; from our side calling
        the helper twice in a row must succeed both times without raising."""
        calls = {"n": 0}

        def _fake_ssh(_device, _command):
            calls["n"] += 1

        monkeypatch.setattr(
            "app.services.enforcement.DeviceProvisioner._execute_ssh", _fake_ssh
        )
        from app.services.enforcement import _apply_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.mikrotik)
        assert _apply_mikrotik_address_list(device, "blocked", "10.0.0.1") is True
        assert _apply_mikrotik_address_list(device, "blocked", "10.0.0.1") is True
        assert calls["n"] == 2

    def test_apply_mikrotik_address_list_wrong_vendor(self):
        from app.services.enforcement import _apply_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.huawei)
        result = _apply_mikrotik_address_list(device, "blocked", "10.0.0.1")
        assert result is False

    def test_remove_mikrotik_address_list(self, monkeypatch):
        commands: list[str] = []

        def _fake_ssh(_device, command):
            commands.append(command)

        monkeypatch.setattr(
            "app.services.enforcement.DeviceProvisioner._execute_ssh", _fake_ssh
        )
        from app.services.enforcement import _remove_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.mikrotik)
        result = _remove_mikrotik_address_list(device, "blocked", "10.0.0.1")
        assert result is True
        assert "remove" in commands[0]

    def test_remove_mikrotik_address_list_wrong_vendor(self):
        from app.services.enforcement import _remove_mikrotik_address_list

        device = NasDevice(name="r1", vendor=NasVendor.cisco)
        result = _remove_mikrotik_address_list(device, "blocked", "10.0.0.1")
        assert result is False

    def test_setting_bool_true_values(self, monkeypatch):
        from app.models.domain_settings import SettingDomain
        from app.services.enforcement import _setting_bool

        monkeypatch.setattr(
            "app.services.enforcement.settings_spec.resolve_value",
            lambda db, domain, key: "true",
        )
        assert _setting_bool(None, SettingDomain.radius, "test", False) is True

    def test_setting_bool_false_values(self, monkeypatch):
        from app.models.domain_settings import SettingDomain
        from app.services.enforcement import _setting_bool

        monkeypatch.setattr(
            "app.services.enforcement.settings_spec.resolve_value",
            lambda db, domain, key: "no",
        )
        assert _setting_bool(None, SettingDomain.radius, "test", True) is False

    def test_setting_bool_none_uses_default(self, monkeypatch):
        from app.models.domain_settings import SettingDomain
        from app.services.enforcement import _setting_bool

        monkeypatch.setattr(
            "app.services.enforcement.settings_spec.resolve_value",
            lambda db, domain, key: None,
        )
        assert _setting_bool(None, SettingDomain.radius, "test", True) is True
        assert _setting_bool(None, SettingDomain.radius, "test", False) is False

    def test_setting_bool_with_actual_bool(self, monkeypatch):
        from app.models.domain_settings import SettingDomain
        from app.services.enforcement import _setting_bool

        monkeypatch.setattr(
            "app.services.enforcement.settings_spec.resolve_value",
            lambda db, domain, key: True,
        )
        assert _setting_bool(None, SettingDomain.radius, "test", False) is True


# ---------------------------------------------------------------------------
# emit_event integration test
# ---------------------------------------------------------------------------


class TestEmitEvent:
    @patch("app.services.events.dispatcher._dispatcher", None)
    @patch("app.services.events.dispatcher._initialize_handlers")
    def test_emit_event_creates_and_dispatches(self, mock_init_handlers, db_session):
        from app.services.events.dispatcher import emit_event

        mock_db = MagicMock()
        sub_id = uuid.uuid4()
        event = emit_event(
            mock_db,
            EventType.subscriber_created,
            {"first_name": "Jane"},
            actor="system",
            subscriber_id=sub_id,
        )

        assert event.event_type == EventType.subscriber_created
        assert event.payload == {"first_name": "Jane"}
        assert event.actor == "system"
        assert event.subscriber_id == sub_id

    @patch("app.services.events.dispatcher._dispatcher", None)
    @patch("app.services.events.dispatcher._initialize_handlers")
    def test_emit_event_accepts_string_uuids(self, mock_init_handlers, db_session):
        from app.services.events.dispatcher import emit_event

        mock_db = MagicMock()
        sub_id = uuid.uuid4()
        event = emit_event(
            mock_db,
            EventType.subscriber_created,
            {},
            subscriber_id=str(sub_id),
        )
        assert event.subscriber_id == sub_id
